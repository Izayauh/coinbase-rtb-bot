"""
Coinbase Advanced Trade public WebSocket collector (read-only / shadow-only).

Channels (all public per Coinbase's channel table):
  market_trades, level2, ticker, heartbeats at wss://advanced-trade-ws.coinbase.com

Design:
  * `ingest_frame(...)` is a pure-ish normalizer: append raw evidence, then (only if
    the raw frame was newly inserted) write normalized rows. Raw dedup therefore gates
    normalization, giving end-to-end idempotency. It needs no network and is unit-tested
    with fixtures.
  * `CoinbaseCollector.run(...)` is the async transport. `websockets` is imported lazily
    so importing this module (and the boundary test) never needs the network library.

This module imports NOTHING from bot/. It has no order path.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..features import OnlineOrderMathSampler
from ..storage import ResearchStore

WS_URL = "wss://advanced-trade-ws.coinbase.com"
# The initial BTC-USD Level2 snapshot is roughly 4-5 MB, larger than the
# websockets library's 1 MB default. The earlier "auth gap" diagnosis was a
# message-size failure, not an authentication failure.
DEFAULT_CHANNELS = ["market_trades", "level2", "ticker", "heartbeats"]
DEFAULT_MAX_MESSAGE_BYTES = 16 * 1024 * 1024

_ISO_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})(?:\.(\d+))?(.*)$")


def iso_to_us(s: Optional[str]) -> Optional[int]:
    """Parse a Coinbase ISO-8601 timestamp to integer microseconds UTC.

    Robust to nanosecond precision (truncated to micros) and a trailing 'Z'.
    """
    if not s:
        return None
    s = s.strip()
    m = _ISO_RE.match(s)
    if not m:
        return None
    base, frac, tz = m.group(1), m.group(2) or "", m.group(3) or ""
    micros = (frac[:6]).ljust(6, "0") if frac else "000000"
    tz = tz.replace("Z", "+00:00")
    iso = f"{base}.{micros}{tz}"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1_000_000)


def subscribe_payload(channel: str, product_ids: List[str]) -> str:
    return json.dumps({"type": "subscribe", "product_ids": product_ids, "channel": channel})


def ingest_frame(store: ResearchStore, run_id: str, source_id: str, frame: Dict[str, Any],
                 recv_time_us: int, connection_epoch: int) -> Dict[str, int]:
    """Append raw + normalize one WS frame. Returns counts. Normalization runs only
    when the raw frame is newly inserted (dedup-gated idempotency)."""
    channel = frame.get("channel", "unknown")
    seq = frame.get("sequence_num")
    msg_event_us = iso_to_us(frame.get("timestamp"))
    raw_id, inserted = store.append_raw(
        run_id, source_id, channel, frame, msg_event_us, recv_time_us, seq, connection_epoch)
    summary = {"raw_inserted": int(inserted), "trades": 0, "l2": 0, "quotes": 0}
    if not inserted or raw_id is None:
        return summary

    if channel == "market_trades":
        summary["trades"] = _ingest_trades(store, frame, recv_time_us, raw_id)
    elif channel in ("level2", "l2_data"):  # subscribe "level2"; messages arrive as "l2_data"
        summary["l2"] = _ingest_level2(store, frame, recv_time_us, connection_epoch, raw_id)
    elif channel == "ticker":
        summary["quotes"] = _ingest_ticker(store, frame, recv_time_us, raw_id)
    # heartbeats: raw-only (used for liveness/sequence health, no normalized table)
    return summary


def _ingest_trades(store, frame, recv_time_us, raw_id) -> int:
    n = 0
    for event in frame.get("events", []):
        for t in event.get("trades", []):
            try:
                ok = store.insert_trade(
                    product_id=t["product_id"], trade_id=str(t["trade_id"]),
                    price=float(t["price"]), size=float(t["size"]), side=t.get("side", ""),
                    event_time_us=iso_to_us(t.get("time")) or recv_time_us,
                    recv_time_us=recv_time_us, raw_id=raw_id)
                n += int(ok)
            except (KeyError, ValueError, TypeError):
                continue
    return n


def _ingest_level2(store, frame, recv_time_us, connection_epoch, raw_id) -> int:
    rows = []
    for event in frame.get("events", []):
        kind = event.get("type", "update")
        for u in event.get("updates", []):
            try:
                rows.append((
                    event.get("product_id", frame.get("product_id", "")),
                    connection_epoch,
                    frame.get("sequence_num"),
                    kind,
                    u["side"],
                    float(u["price_level"]),
                    float(u["new_quantity"]),
                    iso_to_us(u.get("event_time")) or recv_time_us,
                    recv_time_us,
                    raw_id,
                ))
            except (KeyError, ValueError, TypeError):
                continue
    return store.insert_l2_updates(rows)


def _ingest_ticker(store, frame, recv_time_us, raw_id) -> int:
    n = 0
    for event in frame.get("events", []):
        for tk in event.get("tickers", []):
            def _f(key):
                v = tk.get(key)
                try:
                    return float(v) if v not in (None, "") else None
                except (ValueError, TypeError):
                    return None
            store.insert_quote(
                product_id=tk.get("product_id", ""),
                best_bid=_f("best_bid"), best_bid_qty=_f("best_bid_quantity"),
                best_ask=_f("best_ask"), best_ask_qty=_f("best_ask_quantity"),
                event_time_us=iso_to_us(frame.get("timestamp")) or recv_time_us,
                recv_time_us=recv_time_us, raw_id=raw_id)
            n += 1
    return n


class CoinbaseCollector:
    """Bounded async collector. Public channels only; no credentials; no order path."""

    def __init__(self, store: ResearchStore, product_ids: List[str],
                 channels: Optional[List[str]] = None, ws_url: str = WS_URL,
                 source_id: str = "coinbase_ws",
                 max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
                 storage_warn_bytes: int = 2 * 1024 ** 3,
                 storage_block_bytes: int = 8 * 1024 ** 3,
                 max_book_staleness_us: int = 5_000_000,
                 on_order_math=None):
        self.store = store
        self.product_ids = product_ids
        self.channels = channels or list(DEFAULT_CHANNELS)
        self.ws_url = ws_url
        self.source_id = source_id
        self.max_message_bytes = int(max_message_bytes)
        self.storage_warn_bytes = int(storage_warn_bytes)
        self.storage_block_bytes = int(storage_block_bytes)
        self.connection_epoch = 0
        self.order_math_sampler = (
            OnlineOrderMathSampler(
                store,
                product_ids[0],
                max_book_staleness_us,
                on_record=on_order_math,
            )
            if len(product_ids) == 1 and "level2" in self.channels
            else None
        )
        # Coinbase sends a SINGLE per-connection sequence_num shared across all channels,
        # so gap detection is connection-level (not per-channel).
        self._last_seq_global: Optional[int] = None

    def _check_sequence(self, seq: Optional[int]) -> None:
        if seq is None:
            return
        last = self._last_seq_global
        if last is not None and seq > last + 1:
            self.store.record_gap("*", self.connection_epoch, "GAP", last, seq,
                                  note="connection sequence_num skip")
        if last is None or seq > last:
            self._last_seq_global = seq

    async def run(self, max_seconds: float = 30.0, max_frames: int = 10_000) -> Dict[str, Any]:
        """Collect for up to max_seconds or max_frames, whichever comes first."""
        import asyncio
        import websockets  # lazy: import-time of this module stays network-free

        self.store.register_source(self.source_id, "coinbase_ws", self.ws_url, 1,
                                   notes="public Advanced Trade WS")
        run_id = self.store.start_run("coinbase_collector", {
            "product_ids": self.product_ids, "channels": self.channels,
            "max_seconds": max_seconds, "max_frames": max_frames})
        totals = {
            "frames": 0, "raw_inserted": 0, "trades": 0, "l2": 0,
            "quotes": 0, "order_math": 0, "gaps": 0, "storage_warning": False,
            "storage_blocked": False,
        }
        deadline = asyncio.get_event_loop().time() + max_seconds
        status = "OK"
        try:
            while asyncio.get_event_loop().time() < deadline and totals["frames"] < max_frames:
                try:
                    async with websockets.connect(
                        self.ws_url,
                        ping_interval=20,
                        ping_timeout=20,
                        close_timeout=3,
                        max_size=self.max_message_bytes,
                    ) as ws:
                        self.connection_epoch += 1
                        if self.connection_epoch > 1:
                            self.store.record_gap("*", self.connection_epoch, "RECONNECT",
                                                  note="ws reconnect")
                        self._last_seq_global = None
                        for ch in self.channels:
                            await ws.send(subscribe_payload(ch, self.product_ids))
                        while asyncio.get_event_loop().time() < deadline \
                                and totals["frames"] < max_frames:
                            timeout = max(0.1, deadline - asyncio.get_event_loop().time())
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                            except asyncio.TimeoutError:
                                break
                            projected = self.store.storage_bytes() + len(raw.encode("utf-8"))
                            if projected >= self.storage_warn_bytes:
                                totals["storage_warning"] = True
                            if projected >= self.storage_block_bytes:
                                totals["storage_blocked"] = True
                                status = "STORAGE_BLOCKED"
                                break
                            frame = json.loads(raw)
                            # Advance the connection sequence on EVERY frame (incl.
                            # subscription confirmations) before skipping non-data frames,
                            # so the shared counter stays continuous and gaps are real.
                            self._check_sequence(frame.get("sequence_num"))
                            if frame.get("channel") == "subscriptions":
                                continue
                            recv_time_us = _now_us()
                            s = ingest_frame(
                                self.store,
                                run_id,
                                self.source_id,
                                frame,
                                recv_time_us,
                                self.connection_epoch,
                            )
                            totals["frames"] += 1
                            totals["raw_inserted"] += s["raw_inserted"]
                            totals["trades"] += s["trades"]
                            totals["l2"] += s["l2"]
                            totals["quotes"] += s["quotes"]
                            if (
                                self.order_math_sampler is not None
                                and frame.get("channel") in ("level2", "l2_data")
                            ):
                                totals["order_math"] += len(
                                    self.order_math_sampler.observe_frame(
                                        frame,
                                        recv_time_us,
                                        self.connection_epoch,
                                    )
                                )
                        if totals["storage_blocked"]:
                            break
                except (OSError, websockets.WebSocketException) as exc:  # transient — reconnect
                    self.store.record_gap("*", self.connection_epoch, "PARSE_ERROR",
                                          note=f"ws error: {type(exc).__name__}")
                    await asyncio.sleep(1.0)
        except Exception as exc:  # pragma: no cover - defensive
            status = "ERROR"
            self.store.end_run(run_id, status, error=str(exc)[:400])
            raise
        totals["gaps"] = self.store.count("gaps")
        if self.order_math_sampler is not None:
            totals["order_math"] += len(
                self.order_math_sampler.flush(_now_us())
            )
        self.store.end_run(run_id, status)
        totals["run_id"] = run_id
        return totals


def _now_us() -> int:
    import time
    return int(time.time() * 1_000_000)
