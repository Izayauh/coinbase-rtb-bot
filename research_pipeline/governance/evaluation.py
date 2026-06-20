"""Diagnostic policy tournament over derived features and executable labels.

This is research evidence only. It cannot promote or execute a strategy.
"""
from __future__ import annotations

import math
import statistics
from typing import Any, Dict, List

from .gates import cscv_pbo, deflated_sharpe_ratio, effective_sample_size


POLICIES = {
    "all_setups_v1": lambda f: True,
    "tob_positive_v1": lambda f: (f.get("top_of_book_imbalance") or 0.0) > 0.0,
    "tob_strong_v1": lambda f: (f.get("top_of_book_imbalance") or 0.0) >= 0.25,
    "flow_positive_v1": lambda f: (f.get("signed_trade_flow") or 0.0) > 0.0,
    "flow_plus_tob_v1": lambda f: (
        (f.get("signed_trade_flow") or 0.0) > 0.0
        and (f.get("top_of_book_imbalance") or 0.0) > 0.0
    ),
    "active_tape_v1": lambda f: (f.get("trade_intensity") or 0.0) >= 1.0,
    "tight_spread_v1": lambda f: (
        f.get("quoted_spread_bps") is not None
        and f["quoted_spread_bps"] <= 0.10
    ),
}


def _sharpe(values: List[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = statistics.fmean(values)
    stdev = statistics.stdev(values)
    if stdev == 0:
        return math.copysign(1e12, mean) if mean else 0.0
    return mean / stdev


def evaluate_policy_variants(
    store,
    *,
    product_id: str = "BTC-USD",
    horizon: str = "5m",
    sensitivity: float = 1.0,
    stress_sensitivity: float = 2.0,
    horizon_us: int = 300_000_000,
    cscv_slices: int = 8,
) -> Dict[str, Any]:
    labels = list(store.conn.execute(
        "SELECT * FROM labels WHERE product_id=? AND horizon=? "
        "AND sensitivity=? AND valid=1 ORDER BY decision_time_us",
        (product_id, horizon, sensitivity),
    ))
    stress_rows = {
        r["decision_time_us"]: r for r in store.conn.execute(
            "SELECT * FROM labels WHERE product_id=? AND horizon=? "
            "AND sensitivity=? AND valid=1",
            (product_id, horizon, stress_sensitivity),
        )
    }
    feature_map: Dict[int, Dict[str, float]] = {}
    for row in store.conn.execute(
        "SELECT name, event_time_us, value FROM features "
        "WHERE product_id=? AND value IS NOT NULL",
        (product_id,),
    ):
        feature_map.setdefault(row["event_time_us"], {})[row["name"]] = row["value"]

    times = [r["decision_time_us"] for r in labels]
    if len(times) < max(8, cscv_slices):
        return {
            "status": "BLOCKED",
            "reasons": [
                f"need at least {max(8, cscv_slices)} valid aligned labels; have {len(times)}"
            ],
            "n_labels": len(times),
        }

    policy_returns: Dict[str, List[float]] = {}
    policy_stress: Dict[str, List[float]] = {}
    for name, policy in POLICIES.items():
        normal = []
        stress = []
        for label in labels:
            t = label["decision_time_us"]
            take = bool(policy(feature_map.get(t, {})))
            normal.append(float(label["net_return"]) if take else 0.0)
            stress_label = stress_rows.get(t)
            stress.append(
                float(stress_label["net_return"])
                if take and stress_label is not None else 0.0
            )
        policy_returns[name] = normal
        policy_stress[name] = stress

    names = list(POLICIES)
    matrix = [policy_returns[name] for name in names]
    sharpes = [_sharpe(row) for row in matrix]
    winner_idx = max(
        range(len(names)),
        key=lambda i: (statistics.fmean(matrix[i]), sharpes[i], -i),
    )
    winner = names[winner_idx]
    dsr = deflated_sharpe_ratio(
        matrix[winner_idx],
        trial_sharpes=sharpes,
        num_trials=len(names),
    )
    pbo = cscv_pbo(matrix, n_slices=cscv_slices)
    ess = effective_sample_size(times, horizon_us)

    policies = {}
    for name in names:
        values = policy_returns[name]
        stress_values = policy_stress[name]
        policies[name] = {
            "mean_net_return": statistics.fmean(values),
            "mean_stress_net_return": statistics.fmean(stress_values),
            "sharpe": _sharpe(values),
            "trade_rate": sum(v != 0.0 for v in values) / len(values),
        }
    return {
        "status": "DIAGNOSTIC_ONLY",
        "product_id": product_id,
        "horizon": horizon,
        "n_labels": len(labels),
        "winner": winner,
        "policies": policies,
        "dsr": dsr,
        "pbo": pbo,
        "ess": ess,
        "promotion": "BLOCKED",
        "promotion_reasons": [
            "diagnostic policies are not a pre-authorized live strategy family",
            "full purged walk-forward selection and all four baselines are not yet complete",
            "separate human-authorized one-writer promotion is always required",
        ],
    }
