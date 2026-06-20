"""Pre-registered strategy candidates. Research-only; no order authority."""
from .derivatives_stress import (
    PRODUCT_ID,
    STRATEGY_ID,
    STRATEGY_VERSION,
    VARIANT_NAMES,
    build_candidate_decisions,
    evaluate_candidate,
)

__all__ = [
    "PRODUCT_ID",
    "STRATEGY_ID",
    "STRATEGY_VERSION",
    "VARIANT_NAMES",
    "build_candidate_decisions",
    "evaluate_candidate",
]
