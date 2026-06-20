"""Time-series order-book and execution mathematics from public Coinbase L2.

Coinbase Level2 publishes aggregate absolute quantity by price level. It does
not identify individual orders or why displayed size disappeared. Therefore:

* additions and depletion are directly observable;
* cancellation versus execution is not exactly observable from L2 alone;
* queue position and fill probability must remain estimates.
"""
from __future__ import annotations

from collections import deque
import hashlib
import math
from typing import Any, Dict, Iterable, Mapping, Sequence

from ..book import OrderBook

VERSION = "order_math_v1"
BANDS_BPS = (5, 10, 25, 50)
FLOW_WINDOW_US = 60_000_000


def _side(side: str) -> str:
    return "bid" if str(side).lower() in ("bid", "buy") else "ask"


def _imbalance(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None:
        return None
    total = float(bid) + float(ask)
    return (float(bid) - float(ask)) / total if total > 0 else None


def microprice(
    best_bid: float | None,
    best_ask: float | None,
    bid_qty: float | None,
    ask_qty: float | None,
) -> float | None:
    """Quantity-weighted fair-price estimate.

    More bid quantity moves the estimate toward the ask; more ask quantity
    moves it toward the bid.
    """
    if None in (best_bid, best_ask, bid_qty, ask_qty):
        return None
    total = float(bid_qty) + float(ask_qty)
    if total <= 0:
        return None
    return (
        float(best_ask) * float(bid_qty)
        + float(best_bid) * float(ask_qty)
    ) / total


def _top_levels(book: OrderBook, side: str, n: int = 10) -> list[tuple[float, float]]:
    if side == "bid":
        return sorted(book.bids.items(), reverse=True)[:n]
    return sorted(book.asks.items())[:n]


def _rank_ofi(
    previous: list[tuple[float, float]],
    current: list[tuple[float, float]],
    side: str,
    rank: int,
) -> float:
    prev = previous[rank] if rank < len(previous) else None
    cur = current[rank] if rank < len(current) else None
    if prev is None and cur is None:
        return 0.0
    if prev is None:
        price, qty = cur
        return float(qty) if side == "bid" else -float(qty)
    if cur is None:
        _price, qty = prev
        return -float(qty) if side == "bid" else float(qty)
    prev_price, prev_qty = map(float, prev)
    cur_price, cur_qty = map(float, cur)
    if side == "bid":
        if cur_price > prev_price:
            return cur_qty
        if cur_price == prev_price:
            return cur_qty - prev_qty
        return -prev_qty
    if cur_price < prev_price:
        return -cur_qty
    if cur_price == prev_price:
        return -(cur_qty - prev_qty)
    return prev_qty


def multi_level_ofi(
    previous_bids: list[tuple[float, float]],
    previous_asks: list[tuple[float, float]],
    current_bids: list[tuple[float, float]],
    current_asks: list[tuple[float, float]],
    levels: int,
) -> float:
    return sum(
        _rank_ofi(previous_bids, current_bids, "bid", rank)
        + _rank_ofi(previous_asks, current_asks, "ask", rank)
        for rank in range(levels)
    )


def _shape(depths: dict[int, float | None]) -> tuple[float | None, float | None]:
    values = [depths.get(band) for band in BANDS_BPS]
    if any(value is None for value in values):
        return None, None
    d5, d10, d25, d50 = map(float, values)
    slope = (d50 - d5) / 45.0
    near_slope = (d25 - d10) / 15.0
    far_slope = (d50 - d25) / 25.0
    return slope, far_slope - near_slope


class _FlowWindow:
    def __init__(self, window_us: int = FLOW_WINDOW_US):
        self.window_us = window_us
        self.rows: deque[tuple[int, Dict[str, float]]] = deque()
        self.totals = {
            "ofi": 0.0,
            "bid_add": 0.0,
            "bid_deplete": 0.0,
            "ask_add": 0.0,
            "ask_deplete": 0.0,
        }

    def clear(self) -> None:
        self.rows.clear()
        for key in self.totals:
            self.totals[key] = 0.0

    def add(self, time_us: int, row: Dict[str, float]) -> None:
        self.rows.append((time_us, row))
        for key in self.totals:
            self.totals[key] += float(row.get(key, 0.0))
        self.trim(time_us)

    def trim(self, time_us: int) -> None:
        cutoff = time_us - self.window_us
        while self.rows and self.rows[0][0] < cutoff:
            _old_time, old = self.rows.popleft()
            for key in self.totals:
                self.totals[key] -= float(old.get(key, 0.0))


class _SampleState:
    """Previous sampled top levels for minute-scale MLOFI."""

    def __init__(self) -> None:
        self.time_us: int | None = None
        self.bids: list[tuple[float, float]] = []
        self.asks: list[tuple[float, float]] = []

    def clear(self) -> None:
        self.time_us = None
        self.bids = []
        self.asks = []

    def values(
        self,
        time_us: int,
        bids: list[tuple[float, float]],
        asks: list[tuple[float, float]],
    ) -> tuple[float | None, float | None]:
        within_window = (
            self.time_us is not None
            and 0 <= time_us - self.time_us <= FLOW_WINDOW_US
        )
        mlofi_5 = (
            multi_level_ofi(self.bids, self.asks, bids, asks, 5)
            if within_window else None
        )
        mlofi_10 = (
            multi_level_ofi(self.bids, self.asks, bids, asks, 10)
            if within_window else None
        )
        self.time_us = time_us
        self.bids = bids
        self.asks = asks
        return mlofi_5, mlofi_10


class OnlineOrderMathSampler:
    """Persist the same one-minute order math while L2 frames are live.

    The offline replay remains authoritative and idempotently recomputes the
    rows after shard close. Tests require both paths to produce the same
    record, so an online advisory cannot silently drift from research replay.
    """

    def __init__(
        self,
        store,
        product_id: str,
        max_stale_us: int,
        *,
        step_us: int = FLOW_WINDOW_US,
        on_record=None,
    ):
        self.store = store
        self.product_id = product_id
        self.max_stale_us = int(max_stale_us)
        self.step_us = int(step_us)
        self.on_record = on_record
        self.book = OrderBook(product_id)
        self.flow = _FlowWindow()
        self.sample = _SampleState()
        self.next_time_us: int | None = None

    def _emit_until(self, limit_us: int, *, inclusive: bool) -> list[Dict[str, Any]]:
        if self.next_time_us is None:
            self.next_time_us = (
                math.ceil(limit_us / self.step_us) * self.step_us
            )
        output = []
        compare = (
            (lambda value: value <= limit_us)
            if inclusive else
            (lambda value: value < limit_us)
        )
        while compare(self.next_time_us):
            record = _snapshot_record(
                self.book,
                self.flow,
                self.sample,
                self.product_id,
                self.next_time_us,
                self.max_stale_us,
            )
            self.store.insert_order_math(record)
            output.append(record)
            if self.on_record is not None:
                self.on_record(dict(record))
            self.next_time_us += self.step_us
        return output

    def observe_frame(
        self,
        frame: Mapping[str, Any],
        recv_time_us: int,
        connection_epoch: int,
    ) -> list[Dict[str, Any]]:
        rows = []
        for event in frame.get("events", []):
            kind = event.get("type", "update")
            product_id = event.get(
                "product_id", frame.get("product_id", "")
            )
            if product_id != self.product_id:
                continue
            for update in event.get("updates", []):
                try:
                    rows.append({
                        "update_kind": kind,
                        "side": update["side"],
                        "price_level": float(update["price_level"]),
                        "new_quantity": float(update["new_quantity"]),
                        "recv_time_us": int(recv_time_us),
                        "connection_epoch": int(connection_epoch),
                    })
                except (KeyError, TypeError, ValueError):
                    continue
        if not rows:
            return []
        emitted = self._emit_until(_frame_time(rows), inclusive=False)
        _apply_frame(self.book, self.flow, rows)
        return emitted

    def flush(self, until_us: int) -> list[Dict[str, Any]]:
        return self._emit_until(int(until_us), inclusive=True)


def _frame_time(rows: Sequence[Any]) -> int:
    return max(int(row["recv_time_us"]) for row in rows)


def _snapshot_record(
    book: OrderBook,
    flow: _FlowWindow,
    sample: _SampleState,
    product_id: str,
    decision_time_us: int,
    max_stale_us: int,
) -> Dict[str, Any]:
    flow.trim(decision_time_us)
    health = book.health(decision_time_us, max_stale_us)
    freshness = (
        decision_time_us - book.last_update_us
        if book.last_update_us is not None else None
    )
    record: Dict[str, Any] = {
        "feature_version": VERSION,
        "product_id": product_id,
        "event_time_us": decision_time_us,
        "freshness_us": freshness,
        "connection_epoch": book.connection_epoch if book.connection_epoch >= 0 else None,
        "best_bid": health.best_bid,
        "best_ask": health.best_ask,
        "flags": health.reason.lower(),
    }
    if not health.valid:
        sample.clear()
        record["inputs_hash"] = hashlib.sha256(
            f"{product_id}|{decision_time_us}|{health.reason}".encode()
        ).hexdigest()[:24]
        return record

    bid_qty = book.best_bid_qty()
    ask_qty = book.best_ask_qty()
    mid = book.mid()
    micro = microprice(health.best_bid, health.best_ask, bid_qty, ask_qty)
    top_bids = _top_levels(book, "bid", 10)
    top_asks = _top_levels(book, "ask", 10)
    mlofi_5, mlofi_10 = sample.values(
        decision_time_us, top_bids, top_asks
    )
    record.update({
        "mid": mid,
        "spread_bps": book.spread_bps(),
        "best_bid_qty": bid_qty,
        "best_ask_qty": ask_qty,
        "queue_imbalance": _imbalance(bid_qty, ask_qty),
        "multilevel_depth_imbalance": _imbalance(
            sum(quantity for _, quantity in top_bids),
            sum(quantity for _, quantity in top_asks),
        ),
        "microprice": micro,
        "microprice_delta_bps": (
            (micro - mid) / mid * 10_000.0
            if micro is not None and mid else None
        ),
    })
    bid_depths: dict[int, float | None] = {}
    ask_depths: dict[int, float | None] = {}
    for band in BANDS_BPS:
        bid = book.depth_within_bps("bid", band)
        ask = book.depth_within_bps("ask", band)
        bid_depths[band] = bid
        ask_depths[band] = ask
        record[f"bid_depth_{band}bps"] = bid
        record[f"ask_depth_{band}bps"] = ask
        record[f"depth_imbalance_{band}bps"] = _imbalance(bid, ask)

    seconds = FLOW_WINDOW_US / 1_000_000.0
    totals = flow.totals
    record.update({
        "ofi_60s": totals["ofi"],
        "mlofi_5_60s": mlofi_5,
        "mlofi_10_60s": mlofi_10,
        "bid_add_rate_60s": totals["bid_add"] / seconds,
        "bid_deplete_rate_60s": totals["bid_deplete"] / seconds,
        "ask_add_rate_60s": totals["ask_add"] / seconds,
        "ask_deplete_rate_60s": totals["ask_deplete"] / seconds,
        "bid_replenishment_ratio": (
            totals["bid_add"] / totals["bid_deplete"]
            if totals["bid_deplete"] > 0 else None
        ),
        "ask_replenishment_ratio": (
            totals["ask_add"] / totals["ask_deplete"]
            if totals["ask_deplete"] > 0 else None
        ),
    })
    bid_slope, bid_convexity = _shape(bid_depths)
    ask_slope, ask_convexity = _shape(ask_depths)
    record.update({
        "bid_book_slope": bid_slope,
        "ask_book_slope": ask_slope,
        "bid_book_convexity": bid_convexity,
        "ask_book_convexity": ask_convexity,
    })
    hash_values = [
        record.get(key)
        for key in (
            "best_bid", "best_ask", "queue_imbalance",
            "multilevel_depth_imbalance",
            "microprice_delta_bps", "ofi_60s", "mlofi_5_60s",
            "mlofi_10_60s", "bid_depth_25bps", "ask_depth_25bps",
        )
    ]
    record["inputs_hash"] = hashlib.sha256(
        repr(hash_values).encode()
    ).hexdigest()[:24]
    return record


def _apply_frame(book: OrderBook, flow: _FlowWindow, rows: Sequence[Any]) -> None:
    first = rows[0]
    time_us = _frame_time(rows)
    if first["update_kind"] == "snapshot":
        bids = [
            (float(row["price_level"]), float(row["new_quantity"]))
            for row in rows if _side(row["side"]) == "bid"
        ]
        asks = [
            (float(row["price_level"]), float(row["new_quantity"]))
            for row in rows if _side(row["side"]) == "ask"
        ]
        book.apply_snapshot(
            bids,
            asks,
            int(first["connection_epoch"]),
            time_us,
        )
        flow.clear()
        return

    if int(first["connection_epoch"]) != book.connection_epoch:
        book.invalidate("RECONNECT")
        return
    if not book.valid and book.invalid_reason in ("GAP", "RECONNECT", "NO_SNAPSHOT"):
        return
    contribution = {
        "bid_add": 0.0,
        "bid_deplete": 0.0,
        "ask_add": 0.0,
        "ask_deplete": 0.0,
    }
    for row in rows:
        side = _side(row["side"])
        price = float(row["price_level"])
        new_qty = float(row["new_quantity"])
        levels = book.bids if side == "bid" else book.asks
        old_qty = float(levels.get(price, 0.0))
        delta = new_qty - old_qty
        if delta >= 0:
            contribution[f"{side}_add"] += delta
        else:
            contribution[f"{side}_deplete"] += -delta
        if new_qty <= 0:
            levels.pop(price, None)
        else:
            levels[price] = new_qty
    book.last_update_us = time_us
    book._revalidate()
    contribution["ofi"] = (
        contribution["bid_add"]
        - contribution["bid_deplete"]
        + contribution["ask_deplete"]
        - contribution["ask_add"]
    )
    flow.add(time_us, contribution)


def compute_order_math_series(
    store,
    product_id: str,
    decision_times: Sequence[int],
    max_stale_us: int,
    *,
    persist: bool = True,
) -> list[Dict[str, Any]]:
    """Replay L2 once and sample full order mathematics without lookahead."""
    if not decision_times:
        return []
    times = sorted(set(int(value) for value in decision_times))
    rows: Iterable[Any] = store.conn.execute(
        "SELECT raw_id, update_kind, side, price_level, new_quantity, "
        "recv_time_us, connection_epoch "
        "FROM l2_updates WHERE product_id=? ORDER BY id",
        (product_id,),
    )
    book = OrderBook(product_id)
    flow = _FlowWindow()
    sample = _SampleState()
    output: list[Dict[str, Any]] = []
    index = 0
    current_raw_id = None
    frame: list[Any] = []

    def emit_before(frame_time_us: int) -> None:
        nonlocal index
        while index < len(times) and times[index] < frame_time_us:
            record = _snapshot_record(
                book, flow, sample, product_id, times[index], max_stale_us
            )
            if persist:
                store.insert_order_math(record)
            output.append(record)
            index += 1

    def finish_frame(group: list[Any]) -> None:
        if not group:
            return
        emit_before(_frame_time(group))
        _apply_frame(book, flow, group)

    for row in rows:
        raw_id = row["raw_id"]
        if current_raw_id is None:
            current_raw_id = raw_id
        if raw_id != current_raw_id:
            finish_frame(frame)
            frame = []
            current_raw_id = raw_id
        frame.append(row)
    finish_frame(frame)
    while index < len(times):
        record = _snapshot_record(
            book, flow, sample, product_id, times[index], max_stale_us
        )
        if persist:
            store.insert_order_math(record)
        output.append(record)
        index += 1
    return output
