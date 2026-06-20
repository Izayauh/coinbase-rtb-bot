"""
Deterministic Level 2 order-book reconstruction with explicit health.

Coinbase `level2` semantics (confirmed against official docs):
  * events are `snapshot` or `update`;
  * each update carries `side` ("bid"/"offer"), `price_level`, `new_quantity`;
  * `new_quantity` is the ABSOLUTE resting size; "0" removes the level;
  * sequence numbers are per-connection — a new connection_epoch resets them.

Health rules (IMPLEMENTATION_CONTRACT.md §5):
  * a book is valid only after a snapshot followed by contiguous updates;
  * a sequence gap or reconnect invalidates the book until the next snapshot;
  * a crossed book (best_bid >= best_ask) is invalid;
  * no update within max_book_staleness_us is `stale`.
We never synthesise a healthy book after an unrepaired gap.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple


def _norm_side(side: str) -> str:
    s = side.lower()
    if s in ("bid", "buy"):
        return "bid"
    if s in ("offer", "ask", "sell"):
        return "ask"
    raise ValueError(f"unknown L2 side: {side!r}")


@dataclass
class BookHealth:
    valid: bool
    reason: str            # OK | NO_SNAPSHOT | GAP | RECONNECT | CROSSED | STALE
    best_bid: Optional[float]
    best_ask: Optional[float]
    last_update_us: Optional[int]


class OrderBook:
    def __init__(self, product_id: str):
        self.product_id = product_id
        self.bids: Dict[float, float] = {}
        self.asks: Dict[float, float] = {}
        self.connection_epoch: int = -1
        self.valid: bool = False
        self.invalid_reason: str = "NO_SNAPSHOT"
        self.last_update_us: Optional[int] = None

    # --- mutation ----------------------------------------------------------
    def apply_snapshot(self, bids: List[Tuple[float, float]], asks: List[Tuple[float, float]],
                       connection_epoch: int, event_time_us: Optional[int]) -> None:
        self.bids = {float(p): float(q) for p, q in bids if float(q) > 0}
        self.asks = {float(p): float(q) for p, q in asks if float(q) > 0}
        self.connection_epoch = connection_epoch
        self.last_update_us = event_time_us
        self._revalidate()

    def apply_update(self, side: str, price_level: float, new_quantity: float,
                     connection_epoch: int, event_time_us: Optional[int]) -> None:
        if connection_epoch != self.connection_epoch:
            # Update from a different epoch with no fresh snapshot — cannot trust the book.
            self.invalidate("RECONNECT")
            return
        if not self.valid and self.invalid_reason in ("GAP", "RECONNECT", "NO_SNAPSHOT"):
            # Still waiting for a snapshot to rebuild — ignore updates, stay invalid.
            return
        book = self.bids if _norm_side(side) == "bid" else self.asks
        price = float(price_level)
        qty = float(new_quantity)
        if qty <= 0:
            book.pop(price, None)
        else:
            book[price] = qty
        self.last_update_us = event_time_us
        self._revalidate()

    def invalidate(self, reason: str) -> None:
        self.valid = False
        self.invalid_reason = reason

    def _revalidate(self) -> None:
        bb, ba = self.best_bid(), self.best_ask()
        if bb is not None and ba is not None and bb >= ba:
            self.valid = False
            self.invalid_reason = "CROSSED"
        elif bb is None or ba is None:
            self.valid = False
            self.invalid_reason = "NO_SNAPSHOT"
        else:
            self.valid = True
            self.invalid_reason = "OK"

    # --- reads -------------------------------------------------------------
    def best_bid(self) -> Optional[float]:
        return max(self.bids) if self.bids else None

    def best_ask(self) -> Optional[float]:
        return min(self.asks) if self.asks else None

    def best_bid_qty(self) -> Optional[float]:
        bb = self.best_bid()
        return self.bids.get(bb) if bb is not None else None

    def best_ask_qty(self) -> Optional[float]:
        ba = self.best_ask()
        return self.asks.get(ba) if ba is not None else None

    def mid(self) -> Optional[float]:
        bb, ba = self.best_bid(), self.best_ask()
        return (bb + ba) / 2.0 if bb is not None and ba is not None else None

    def spread_bps(self) -> Optional[float]:
        bb, ba, m = self.best_bid(), self.best_ask(), self.mid()
        if bb is None or ba is None or not m:
            return None
        return (ba - bb) / m * 10_000.0

    def depth_within_bps(self, side: str, bps: float) -> Optional[float]:
        """Sum resting size on `side` within `bps` of the best price on that side."""
        if _norm_side(side) == "bid":
            bb = self.best_bid()
            if bb is None:
                return None
            floor = bb * (1 - bps / 10_000.0)
            return sum(q for p, q in self.bids.items() if p >= floor)
        ba = self.best_ask()
        if ba is None:
            return None
        cap = ba * (1 + bps / 10_000.0)
        return sum(q for p, q in self.asks.items() if p <= cap)

    def health(self, now_us: Optional[int] = None, max_stale_us: Optional[int] = None) -> BookHealth:
        valid, reason = self.valid, self.invalid_reason
        if valid and now_us is not None and max_stale_us is not None \
                and self.last_update_us is not None \
                and now_us - self.last_update_us > max_stale_us:
            valid, reason = False, "STALE"
        return BookHealth(valid, reason, self.best_bid(), self.best_ask(), self.last_update_us)


def replay_l2_rows(product_id: str, rows: Iterable[Any]) -> Tuple[OrderBook, Dict[str, int]]:
    """Replay normalized L2 rows, preserving frame-level snapshot boundaries."""
    book = OrderBook(product_id)
    current_raw_id = None
    frame_rows: List[Any] = []
    snapshots = updates = 0

    def apply_frame(group: List[Any]) -> None:
        nonlocal snapshots, updates
        if not group:
            return
        first = group[0]
        if first["update_kind"] == "snapshot":
            bids = [
                (r["price_level"], r["new_quantity"])
                for r in group if _norm_side(r["side"]) == "bid"
            ]
            asks = [
                (r["price_level"], r["new_quantity"])
                for r in group if _norm_side(r["side"]) == "ask"
            ]
            event_times = [r["event_time_us"] for r in group if r["event_time_us"] is not None]
            book.apply_snapshot(
                bids,
                asks,
                int(first["connection_epoch"]),
                max(event_times) if event_times else None,
            )
            snapshots += 1
            return
        for r in group:
            book.apply_update(
                r["side"],
                r["price_level"],
                r["new_quantity"],
                int(r["connection_epoch"]),
                r["event_time_us"],
            )
            updates += 1

    for row in rows:
        raw_id = row["raw_id"]
        if current_raw_id is None:
            current_raw_id = raw_id
        if raw_id != current_raw_id:
            apply_frame(frame_rows)
            frame_rows = []
            current_raw_id = raw_id
        frame_rows.append(row)
    apply_frame(frame_rows)
    return book, {"snapshots_applied": snapshots, "updates_applied": updates}
