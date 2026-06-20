"""
Microstructure feature family (contract §11). The spine ships ONE fully implemented
feature (`quoted_spread_bps`); the rest are pre-registered specs (not yet computed),
so the variant budget/denominator is honest from day one.

Every feature declares: required inputs, event-time window, freshness rule, missing-data
behavior (emit null + flag, never impute), output unit, and version.
"""
from __future__ import annotations

import hashlib
import math
import statistics
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    version: str
    inputs: Tuple[str, ...]
    window_us: int
    freshness_us: int
    missing_behavior: str
    unit: str
    implemented: bool = False


# First family — exactly one implemented, nine specified (contract §11).
REGISTRY: Dict[str, FeatureSpec] = {
    "quoted_spread_bps": FeatureSpec(
        "quoted_spread_bps", "v1", ("best_bid", "best_ask"), 0, 2_000_000,
        "null+stale flag", "bps", implemented=True),
    "top_of_book_imbalance": FeatureSpec(
        "top_of_book_imbalance", "v1", ("best_bid_qty", "best_ask_qty"), 0, 2_000_000,
        "null+flag", "ratio", implemented=True),
    "depth_imbalance_10bps": FeatureSpec(
        "depth_imbalance_10bps", "v1", ("l2_book",), 0, 5_000_000,
        "null+flag", "ratio", implemented=True),
    "depth_imbalance_25bps": FeatureSpec(
        "depth_imbalance_25bps", "v1", ("l2_book",), 0, 5_000_000,
        "null+flag", "ratio", implemented=True),
    "multilevel_imbalance": FeatureSpec(
        "multilevel_imbalance", "v1", ("l2_book",), 0, 5_000_000,
        "null+flag", "ratio", implemented=True),
    "signed_trade_flow": FeatureSpec(
        "signed_trade_flow", "v1", ("trades",), 60_000_000, 60_000_000,
        "null+quiet flag", "base_units", implemented=True),
    "trade_intensity": FeatureSpec(
        "trade_intensity", "v1", ("trades",), 60_000_000, 60_000_000,
        "null+quiet flag", "count/s", implemented=True),
    "orderbook_pressure_change": FeatureSpec(
        "orderbook_pressure_change", "v1", ("l2_book",), 5_000_000, 5_000_000, "null+flag", "ratio"),
    "liquidity_shock": FeatureSpec(
        "liquidity_shock", "v1", ("l2_book",), 5_000_000, 5_000_000, "null+flag", "zscore"),
    "realized_volatility": FeatureSpec(
        "realized_volatility", "v1", ("mid",), 300_000_000, 5_000_000,
        "null+flag", "fraction", implemented=True),
    "short_horizon_adverse_selection": FeatureSpec(
        "short_horizon_adverse_selection", "v1", ("quotes", "markout"), 60_000_000, 5_000_000,
        "null+flag", "bps"),
    "order_math_state": FeatureSpec(
        "order_math_state", "v1", ("l2_book",), 60_000_000, 5_000_000,
        "wide order_math row + health flag", "state", implemented=True),
}


def _inputs_hash(*vals: Any) -> str:
    return hashlib.sha256("|".join(repr(v) for v in vals).encode()).hexdigest()[:16]


def quoted_spread_bps(quote_row: Optional[Dict[str, Any]], decision_time_us: int,
                      max_stale_us: int) -> Tuple[Optional[float], str, Optional[int]]:
    """Quoted spread in bps from a top-of-book quote.

    Returns (value, flags, freshness_us). Missing/stale/crossed -> (None, flag, freshness);
    inputs are never imputed.
    """
    if quote_row is None:
        return None, "missing", None
    freshness = decision_time_us - quote_row["recv_time_us"]
    if freshness > max_stale_us:
        return None, "stale", freshness
    bb, ba = quote_row.get("best_bid"), quote_row.get("best_ask")
    if bb is None or ba is None:
        return None, "missing", freshness
    if bb >= ba:
        return None, "crossed", freshness
    mid = (bb + ba) / 2.0
    return (ba - bb) / mid * 10_000.0, "ok", freshness


def top_of_book_imbalance(
    quote_row: Optional[Dict[str, Any]],
    decision_time_us: int,
    max_stale_us: int,
) -> Tuple[Optional[float], str, Optional[int]]:
    """(bid_qty - ask_qty) / (bid_qty + ask_qty), with null+flag semantics."""
    if quote_row is None:
        return None, "missing", None
    freshness = decision_time_us - quote_row["recv_time_us"]
    if freshness > max_stale_us:
        return None, "stale", freshness
    bid_qty, ask_qty = quote_row.get("best_bid_qty"), quote_row.get("best_ask_qty")
    if bid_qty is None or ask_qty is None:
        return None, "missing", freshness
    total = float(bid_qty) + float(ask_qty)
    if total <= 0:
        return None, "empty", freshness
    return (float(bid_qty) - float(ask_qty)) / total, "ok", freshness


def _imbalance(bid_value: Optional[float], ask_value: Optional[float]) -> Optional[float]:
    if bid_value is None or ask_value is None:
        return None
    total = bid_value + ask_value
    return (bid_value - ask_value) / total if total > 0 else None


def book_feature_values(book, decision_time_us: int, max_stale_us: int) -> Dict[str, Tuple[Optional[float], str]]:
    """Compute the four currently implemented Level2 snapshot features."""
    health = book.health(decision_time_us, max_stale_us)
    if not health.valid:
        flag = health.reason.lower()
        return {
            name: (None, flag)
            for name in (
                "depth_imbalance_10bps",
                "depth_imbalance_25bps",
                "multilevel_imbalance",
            )
        }
    d10 = _imbalance(
        book.depth_within_bps("bid", 10),
        book.depth_within_bps("ask", 10),
    )
    d25 = _imbalance(
        book.depth_within_bps("bid", 25),
        book.depth_within_bps("ask", 25),
    )
    bid_levels = sorted(book.bids.items(), reverse=True)[:10]
    ask_levels = sorted(book.asks.items())[:10]
    multi = _imbalance(
        sum(q for _p, q in bid_levels),
        sum(q for _p, q in ask_levels),
    )
    return {
        "depth_imbalance_10bps": (d10, "ok" if d10 is not None else "empty"),
        "depth_imbalance_25bps": (d25, "ok" if d25 is not None else "empty"),
        "multilevel_imbalance": (multi, "ok" if multi is not None else "empty"),
    }


def trade_window_features(
    trade_rows: Sequence[Dict[str, Any]],
    start_us: int,
    end_us: int,
    max_trade_gap_us: int,
) -> Dict[str, Tuple[Optional[float], str]]:
    """Signed flow and intensity over a closed event-time window.

    Coinbase's `side` is the maker side. Therefore maker SELL implies an
    aggressive buy (+size), while maker BUY implies an aggressive sell (-size).
    """
    rows = [
        r for r in trade_rows
        if start_us <= int(r["event_time_us"]) <= end_us
    ]
    if not rows:
        return {
            "signed_trade_flow": (None, "quiet"),
            "trade_intensity": (None, "quiet"),
        }
    latest = max(int(r["event_time_us"]) for r in rows)
    if end_us - latest > max_trade_gap_us:
        return {
            "signed_trade_flow": (None, "quiet"),
            "trade_intensity": (None, "quiet"),
        }
    signed = 0.0
    for row in rows:
        maker_side = str(row.get("side", "")).upper()
        aggressor_sign = 1.0 if maker_side == "SELL" else -1.0 if maker_side == "BUY" else 0.0
        signed += aggressor_sign * float(row["size"])
    seconds = max((end_us - start_us) / 1_000_000.0, 1e-9)
    return {
        "signed_trade_flow": (signed, "ok"),
        "trade_intensity": (len(rows) / seconds, "ok"),
    }


def realized_volatility(
    quote_rows: Sequence[Dict[str, Any]],
    start_us: int,
    end_us: int,
) -> Tuple[Optional[float], str]:
    mids = []
    for row in quote_rows:
        if not (start_us <= int(row["recv_time_us"]) <= end_us):
            continue
        bb, ba = row.get("best_bid"), row.get("best_ask")
        if bb is None or ba is None or bb <= 0 or ba <= bb:
            continue
        mids.append((float(bb) + float(ba)) / 2.0)
    if len(mids) < 3:
        return None, "insufficient"
    log_returns = [math.log(b / a) for a, b in zip(mids, mids[1:]) if a > 0 and b > 0]
    if len(log_returns) < 2:
        return None, "insufficient"
    return statistics.stdev(log_returns), "ok"


def compute_quoted_spread_series(store, product_id: str, decision_times: Sequence[int],
                                 max_stale_us: int, persist: bool = True) -> List[Dict[str, Any]]:
    """Compute quoted_spread_bps at each decision time from stored quotes."""
    from ..labeling.labeler import QuoteSeries

    if not decision_times:
        return []
    lo, hi = min(decision_times) - max_stale_us, max(decision_times)
    rows = [dict(r) for r in store.get_quotes(product_id, lo, hi)]
    quotes = QuoteSeries(rows)
    spec = REGISTRY["quoted_spread_bps"]

    out: List[Dict[str, Any]] = []
    for t in decision_times:
        q = quotes.latest_at(t)
        value, flags, freshness = quoted_spread_bps(q, t, max_stale_us)
        ih = _inputs_hash(q.get("best_bid") if q else None, q.get("best_ask") if q else None)
        rec = {"name": spec.name, "version": spec.version, "product_id": product_id,
               "event_time_us": t, "value": value, "freshness_us": freshness,
               "flags": flags, "inputs_hash": ih}
        if persist:
            store.insert_feature(spec.name, spec.version, product_id, t, value,
                                 freshness, flags, ih)
        out.append(rec)
    return out


def compute_quote_feature_series(
    store,
    product_id: str,
    decision_times: Sequence[int],
    max_stale_us: int,
    trade_window_us: int = 60_000_000,
    volatility_window_us: int = 300_000_000,
    max_trade_gap_us: int = 60_000_000,
    persist: bool = True,
) -> List[Dict[str, Any]]:
    """Compute all implemented quote/trade features at each decision time."""
    from ..labeling.labeler import QuoteSeries

    if not decision_times:
        return []
    lo = min(decision_times) - max(volatility_window_us, trade_window_us, max_stale_us)
    hi = max(decision_times)
    quote_rows = [dict(r) for r in store.get_quotes(product_id, lo, hi)]
    trade_rows = [dict(r) for r in store.conn.execute(
        "SELECT * FROM trades WHERE product_id=? AND event_time_us BETWEEN ? AND ? "
        "ORDER BY event_time_us",
        (product_id, lo, hi),
    )]
    quotes = QuoteSeries(quote_rows)
    out: List[Dict[str, Any]] = []

    def add(name: str, t: int, value: Optional[float], flags: str,
            freshness: Optional[int], inputs: Any) -> None:
        spec = REGISTRY[name]
        rec = {
            "name": name, "version": spec.version, "product_id": product_id,
            "event_time_us": t, "value": value, "freshness_us": freshness,
            "flags": flags, "inputs_hash": _inputs_hash(inputs),
        }
        if persist:
            store.insert_feature(
                name, spec.version, product_id, t, value, freshness, flags,
                rec["inputs_hash"],
            )
        out.append(rec)

    for t in decision_times:
        q = quotes.latest_at(t)
        spread, spread_flag, freshness = quoted_spread_bps(q, t, max_stale_us)
        add("quoted_spread_bps", t, spread, spread_flag, freshness, q)

        tob, tob_flag, tob_fresh = top_of_book_imbalance(q, t, max_stale_us)
        add("top_of_book_imbalance", t, tob, tob_flag, tob_fresh, q)

        trade_values = trade_window_features(
            trade_rows, t - trade_window_us, t, max_trade_gap_us
        )
        for name, (value, flag) in trade_values.items():
            add(name, t, value, flag, None, (t - trade_window_us, t, len(trade_rows)))

        vol, vol_flag = realized_volatility(
            quote_rows, t - volatility_window_us, t
        )
        add("realized_volatility", t, vol, vol_flag, freshness, (t, len(quote_rows)))
    return out


def register_specs_as_variants(store) -> int:
    """Register every first-family feature spec in the variant registry (the
    multiple-testing denominator). Returns the number newly registered."""
    n = 0
    for spec in REGISTRY.values():
        variant_id = f"microstructure::{spec.name}::{spec.version}"
        if store.register_variant(variant_id, "microstructure", spec.name,
                                  {"inputs": list(spec.inputs), "unit": spec.unit,
                                   "implemented": spec.implemented}):
            n += 1
    return n
