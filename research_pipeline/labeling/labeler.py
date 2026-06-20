"""
Executable, bid/ask-aware forward labels with deterministic replay.

For a long/flat spot account (no shorting), a label at decision time `t` for horizon
`h` buys at the executable ASK at `t` and sells at the executable BID at `t+h`.

Net return uses a transparent FIRST-ORDER additive decomposition (contract §8/§10):

    net = gross + spread + slippage + fee + adverse           (each cost term <= 0)

    gross    = mid(t+h)/mid(t) - 1                              (diagnostic, mid-to-mid)
    spread   = -((ask_t - mid_t)/mid_t + (mid_th - bid_th)/mid_th)   (cross half-spread each side)
    slippage = -(2 * slippage_per_side)
    fee      = -(2 * taker_fee_per_side)
    adverse  = -(adverse_selection)

The second-order compounding error between this and the exact ratio is O(bps^2) and is
documented as negligible at these magnitudes. `gross` is mid-to-mid and never used for
promotion math; only `net` (executable, friction-loaded) is.

The same LabelEngine is used for the live-shadow path and the replay path, so
live/replay parity holds by construction; tests assert determinism within tolerance.
"""
from __future__ import annotations

from bisect import bisect_right
from typing import Any, Dict, List, Optional, Sequence

from ..config import CostModel


class QuoteSeries:
    """Time-indexed best bid/ask quotes for one product, sorted by recv_time_us."""

    def __init__(self, rows: Sequence[Dict[str, Any]]):
        rows = sorted(rows, key=lambda r: r["recv_time_us"])
        self.rows = rows
        self.times = [r["recv_time_us"] for r in rows]

    def latest_at(self, t_us: int) -> Optional[Dict[str, Any]]:
        i = bisect_right(self.times, t_us) - 1
        return self.rows[i] if i >= 0 else None

    def max_time(self) -> Optional[int]:
        return self.times[-1] if self.times else None

    def window(self, a_us: int, b_us: int) -> List[Dict[str, Any]]:
        return [r for r in self.rows if a_us <= r["recv_time_us"] <= b_us]


def _mid(row: Dict[str, Any]) -> Optional[float]:
    bb, ba = row.get("best_bid"), row.get("best_ask")
    if bb is None or ba is None:
        return None
    return (bb + ba) / 2.0


class LabelEngine:
    def __init__(self, cost_model: CostModel, max_quote_staleness_us: int,
                 replay_version: str = "replay_v1"):
        self.cm = cost_model
        self.max_stale = max_quote_staleness_us
        self.replay_version = replay_version

    def _invalid(self, product_id, t, horizon, reason, sensitivity) -> Dict[str, Any]:
        return {
            "product_id": product_id, "decision_time_us": t, "horizon": horizon,
            "entry_side": "BUY", "entry_price": None, "exit_side": "SELL", "exit_price": None,
            "gross_return": None, "fee_component": None, "slippage_component": None,
            "adverse_selection_component": None, "spread_component": None, "net_return": None,
            "mfe": None, "mae": None, "valid": 0, "invalid_reason": reason,
            "quote_source": "ticker", "sensitivity": sensitivity,
            "cost_model_version": self.cm.version, "replay_version": self.replay_version,
        }

    def label_one(self, quotes: QuoteSeries, product_id: str, t_us: int,
                  horizon_name: str, horizon_us: int, sensitivity: float = 1.0,
                  data_max_us: Optional[int] = None) -> Optional[Dict[str, Any]]:
        target = t_us + horizon_us
        # Horizon availability is decided by the TRUE data extent (data_max_us), not by
        # the windowed QuoteSeries — otherwise a stale exit quote is misread as "no horizon".
        extent = data_max_us if data_max_us is not None else quotes.max_time()
        if extent is None or extent < target:
            return None  # horizon not yet available -> emit no row (contract §8)

        entry = quotes.latest_at(t_us)
        if entry is None or t_us - entry["recv_time_us"] > self.max_stale:
            return self._invalid(product_id, t_us, horizon_name, "STALE_QUOTE", sensitivity)
        exit_q = quotes.latest_at(target)
        if exit_q is None or target - exit_q["recv_time_us"] > self.max_stale:
            return self._invalid(product_id, t_us, horizon_name, "STALE_QUOTE", sensitivity)

        ask_t, bid_t = entry.get("best_ask"), entry.get("best_bid")
        ask_th, bid_th = exit_q.get("best_ask"), exit_q.get("best_bid")
        if None in (ask_t, bid_t, ask_th, bid_th):
            return self._invalid(product_id, t_us, horizon_name, "NO_QUOTE", sensitivity)
        if bid_t >= ask_t or bid_th >= ask_th:
            return self._invalid(product_id, t_us, horizon_name, "CROSSED", sensitivity)

        mid_t, mid_th = (bid_t + ask_t) / 2.0, (bid_th + ask_th) / 2.0
        slip = self.cm.slippage_bps * sensitivity / 10_000.0
        fee = self.cm.taker_fee_bps * sensitivity / 10_000.0
        adverse = self.cm.adverse_frac(sensitivity)

        gross = mid_th / mid_t - 1.0
        spread = -(((ask_t - mid_t) / mid_t) + ((mid_th - bid_th) / mid_th))
        slippage = -(2.0 * slip)
        fee_c = -(2.0 * fee)
        adverse_c = -adverse
        net = gross + spread + slippage + fee_c + adverse_c

        # MFE/MAE from intermediate mids over (t, t+h].
        mfe = mae = None
        inter = [m for m in (_mid(r) for r in quotes.window(t_us + 1, target)) if m is not None]
        if inter:
            entry_exec = ask_t * (1 + slip)
            rets = [m / entry_exec - 1.0 for m in inter]
            mfe, mae = max(rets), min(rets)

        return {
            "product_id": product_id, "decision_time_us": t_us, "horizon": horizon_name,
            "entry_side": "BUY", "entry_price": ask_t * (1 + slip),
            "exit_side": "SELL", "exit_price": bid_th * (1 - slip),
            "gross_return": gross, "fee_component": fee_c, "slippage_component": slippage,
            "adverse_selection_component": adverse_c, "spread_component": spread,
            "net_return": net, "mfe": mfe, "mae": mae, "valid": 1, "invalid_reason": None,
            "quote_source": "ticker", "sensitivity": sensitivity,
            "cost_model_version": self.cm.version, "replay_version": self.replay_version,
        }


def build_labels(store, product_id: str, decision_times: Sequence[int],
                 horizons: Dict[str, int], cost_model: CostModel,
                 max_quote_staleness_us: int, sensitivities: Sequence[float] = (1.0,),
                 replay_version: str = "replay_v1", persist: bool = True) -> List[Dict[str, Any]]:
    """Build executable labels for the given decision times and horizons.

    Reads quotes deterministically from the store; identical inputs yield identical
    labels (live-shadow and replay share this code path)."""
    if not decision_times:
        return []
    max_h_us = max(horizons.values()) * 1_000_000
    lo = min(decision_times) - max_quote_staleness_us
    hi = max(decision_times) + max_h_us + max_quote_staleness_us
    rows = [dict(r) for r in store.get_quotes(product_id, lo, hi)]
    quotes = QuoteSeries(rows)
    # True data extent for the horizon-availability check (independent of the window).
    row = store.conn.execute(
        "SELECT MAX(recv_time_us) AS hi FROM quotes WHERE product_id=?", (product_id,)
    ).fetchone()
    data_max = row["hi"] if row else None
    engine = LabelEngine(cost_model, max_quote_staleness_us, replay_version)

    out: List[Dict[str, Any]] = []
    for t in decision_times:
        for hname, hsec in horizons.items():
            for sens in sensitivities:
                lbl = engine.label_one(quotes, product_id, t, hname, hsec * 1_000_000, sens,
                                       data_max_us=data_max)
                if lbl is None:
                    continue
                if persist:
                    store.insert_label(lbl)
                out.append(lbl)
    return out
