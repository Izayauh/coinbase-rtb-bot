"""
Historical bar backfill via Coinbase REST API.

The strategy requires 205 four-hour bars (for EMA-200 on 4h) and 25 one-hour
bars. Accumulating 205 4h bars from live streaming alone takes 34+ days of
uninterrupted uptime. This module fills the gap by fetching 1h candles from
the REST API and aggregating them into 4h bars.

Usage:
    Called automatically at startup when the DB has insufficient bars.
    Can also be run standalone via ``python -m bot.backfill``.
"""
import logging
import time
from typing import List, Optional

from .coinbase_adapter import CoinbaseAdapter
from .models import Bar
from .journal import Journal

logger = logging.getLogger(__name__)

# Coinbase REST returns max 300 candles per request.
_MAX_CANDLES_PER_REQUEST = 300
_1H_SECONDS = 3600
_4H_SECONDS = 14400

# Need 205 4h bars → 820 1h bars.  Fetch extra for safety.
REQUIRED_1H = 830


def _parse_candles(raw, symbol: str) -> List[Bar]:
    """Parse a Coinbase REST get_candles response into 1h Bar objects."""
    candles: list = []
    if hasattr(raw, "candles") and raw.candles is not None:
        try:
            candles = list(raw.candles)
        except Exception:
            pass
    elif isinstance(raw, dict):
        candles = raw.get("candles", [])
    elif hasattr(raw, "__iter__"):
        try:
            candles = list(raw)
        except Exception:
            pass

    bars: List[Bar] = []
    for c in candles:
        try:
            if hasattr(c, "start"):
                ts = int(c.start)
                o, h, lo, cl, v = (
                    float(c.open), float(c.high), float(c.low),
                    float(c.close), float(c.volume),
                )
            elif isinstance(c, dict):
                ts = int(c["start"])
                o, h, lo, cl, v = (
                    float(c["open"]), float(c["high"]), float(c["low"]),
                    float(c["close"]), float(c["volume"]),
                )
            else:
                continue
            bars.append(Bar(
                symbol=symbol, timeframe="1h", ts_open=ts,
                open=o, high=h, low=lo, close=cl, volume=v,
            ))
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning("Skipping unparseable candle: %s", exc)
    return bars


def aggregate_4h(
    bars_1h: List[Bar],
    symbol: str,
    now: Optional[int] = None,
) -> List[Bar]:
    """
    Aggregate sorted 1h bars into 4h bars using 14400-second boundaries.

    Excludes the current (potentially incomplete) 4h bar to stay consistent
    with the live BarBuilder, which only emits completed bars.

    ``now`` can be injected for testing; defaults to time.time().
    """
    if now is None:
        now = int(time.time())

    buckets: dict = {}
    for b in bars_1h:
        boundary = (b.ts_open // _4H_SECONDS) * _4H_SECONDS
        if boundary not in buckets:
            buckets[boundary] = Bar(
                symbol=symbol, timeframe="4h", ts_open=boundary,
                open=b.open, high=b.high, low=b.low,
                close=b.close, volume=b.volume,
            )
        else:
            agg = buckets[boundary]
            agg.high = max(agg.high, b.high)
            agg.low = min(agg.low, b.low)
            agg.close = b.close
            agg.volume += b.volume

    # Drop current in-progress 4h bar
    current_boundary = (now // _4H_SECONDS) * _4H_SECONDS
    buckets.pop(current_boundary, None)

    return [buckets[k] for k in sorted(buckets)]


def backfill_bars(
    adapter: CoinbaseAdapter,
    symbol: str,
    target_1h: int = REQUIRED_1H,
) -> dict:
    """
    Fetch historical 1h candles and derive 4h bars.  Persist all to DB.

    Returns ``{"bars_1h": int, "bars_4h": int}``.
    Raises on auth failure or REST error so the caller can decide severity.
    """
    if not adapter._enabled:
        raise RuntimeError("Cannot backfill: adapter has no credentials")

    now = int(time.time())
    start = now - (target_1h + 1) * _1H_SECONDS
    all_1h: List[Bar] = []

    logger.info(
        "Backfill: fetching ~%d 1h candles for %s from REST API ...",
        target_1h, symbol,
    )

    cursor = start
    while cursor < now:
        chunk_end = min(cursor + _MAX_CANDLES_PER_REQUEST * _1H_SECONDS, now)
        try:
            raw = adapter.rest.get_candles(
                product_id=symbol,
                start=str(cursor),
                end=str(chunk_end),
                granularity="ONE_HOUR",
            )
            chunk = _parse_candles(raw, symbol)
            all_1h.extend(chunk)
            logger.info(
                "Backfill chunk: %d candles (%d total so far)",
                len(chunk), len(all_1h),
            )
        except Exception as exc:
            logger.error(
                "Backfill REST call failed at offset %d: %s", cursor, exc,
            )
            raise
        cursor = chunk_end

    # Deduplicate by ts_open and sort
    seen: dict = {}
    for b in all_1h:
        seen[b.ts_open] = b
    all_1h = [seen[k] for k in sorted(seen)]

    # Exclude current incomplete 1h bar
    current_1h = (now // _1H_SECONDS) * _1H_SECONDS
    all_1h = [b for b in all_1h if b.ts_open < current_1h]

    # Derive 4h bars
    bars_4h = aggregate_4h(all_1h, symbol, now=now)

    # Persist via upsert — safe against duplicates
    for bar in all_1h:
        Journal.upsert_bar(bar)
    for bar in bars_4h:
        Journal.upsert_bar(bar)

    logger.info(
        "Backfill complete: %d 1h bars, %d 4h bars persisted for %s.",
        len(all_1h), len(bars_4h), symbol,
    )
    return {"bars_1h": len(all_1h), "bars_4h": len(bars_4h)}
