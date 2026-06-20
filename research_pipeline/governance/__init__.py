"""Evaluation & governance: baselines, purged walk-forward, variant counting, gates."""
from .gates import (
    purged_walk_forward_splits, effective_sample_size, baseline_no_trade,
    baseline_buy_and_hold, expected_max_sharpe, deflated_sharpe_ratio,
    cscv_pbo, promotion_gate,
    GovernanceStatus,
)
from .evaluation import POLICIES, evaluate_policy_variants

__all__ = [
    "purged_walk_forward_splits", "effective_sample_size", "baseline_no_trade",
    "baseline_buy_and_hold", "expected_max_sharpe", "deflated_sharpe_ratio",
    "cscv_pbo", "promotion_gate",
    "GovernanceStatus",
    "POLICIES", "evaluate_policy_variants",
]
