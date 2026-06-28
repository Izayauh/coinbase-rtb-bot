"""
research/multiple_testing.py — Multiple-testing correction for Lane A.

Stage 1 (MVP): Benjamini-Hochberg FDR on OOS t-test p-values.
Stage 2 (reserved): CSCV/PBO or White's Reality Check — built only if Stage 1 produces survivors.

References:
  - Benjamini & Hochberg (1995): Controlling the False Discovery Rate
  - Bailey et al. (2014): Probability of Backtest Overfitting (SSRN 2326253)
  - White (2000): A Reality Check for Data Snooping (Econometrica)
"""
import math
from typing import List, Tuple, Optional

from .types import BacktestResult


def ttest_one_sample(values: List[float]) -> Tuple[float, float]:
    """
    One-sample t-test: H0: mean(values) = 0.

    Returns (t_statistic, p_value).
    Uses two-tailed test.
    """
    n = len(values)
    if n < 2:
        return 0.0, 1.0

    mean = sum(values) / n
    var = sum((x - mean) ** 2 for x in values) / (n - 1)
    se = math.sqrt(var / n) if var > 0 else 1e-10

    t_stat = mean / se

    # Approximate p-value using normal distribution for large n
    # For small n this is imprecise, but sufficient for screening
    p_value = 2 * _normal_cdf(-abs(t_stat))

    return t_stat, p_value


def _normal_cdf(x: float) -> float:
    """Standard normal CDF approximation (Abramowitz & Stegun)."""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def benjamini_hochberg(
    p_values: List[Tuple[str, float]],
    alpha: float = 0.10,
) -> List[Tuple[str, float, bool]]:
    """
    Benjamini-Hochberg FDR correction.

    Args:
        p_values: list of (config_id, p_value)
        alpha: FDR threshold (default 0.10)

    Returns:
        list of (config_id, adjusted_p, is_significant)
        Sorted by original p-value ascending.
    """
    if not p_values:
        return []

    n = len(p_values)
    # Sort by p-value
    sorted_pv = sorted(p_values, key=lambda x: x[1])

    # BH procedure
    results = []
    max_significant_rank = 0

    for rank, (config_id, p) in enumerate(sorted_pv, 1):
        bh_threshold = alpha * rank / n
        adjusted_p = min(p * n / rank, 1.0)
        is_sig = p <= bh_threshold
        if is_sig:
            max_significant_rank = rank
        results.append((config_id, adjusted_p, False))  # will set significance below

    # All hypotheses with rank <= max_significant_rank are significant
    final = []
    for i, (config_id, adj_p, _) in enumerate(results):
        is_sig = (i + 1) <= max_significant_rank
        final.append((config_id, adj_p, is_sig))

    return final


def screen_oos_results(
    results: List[BacktestResult],
    min_trades: int = 10,
    alpha: float = 0.10,
) -> List[Tuple[str, float, bool, BacktestResult]]:
    """
    Stage 1 screen: t-test each config's OOS pnl series, then BH-FDR correct.

    Args:
        results: list of OOS BacktestResult objects
        min_trades: minimum trades to be eligible
        alpha: FDR threshold

    Returns:
        list of (config_id, adjusted_p, is_significant, result)
        Sorted by adjusted p-value.
    """
    eligible = []
    for r in results:
        if r.trade_count < min_trades:
            continue

        pnls = [t.pnl_pct for t in r.trades]
        config_id = f"{r.rule_name}|{_params_key(r.params)}"
        _, p_val = ttest_one_sample(pnls)
        eligible.append((config_id, p_val, r))

    if not eligible:
        return []

    # BH correction
    p_values = [(cid, p) for cid, p, _ in eligible]
    bh_results = benjamini_hochberg(p_values, alpha)

    # Merge with BacktestResult
    result_map = {cid: r for cid, _, r in eligible}
    output = []
    for config_id, adj_p, is_sig in bh_results:
        output.append((config_id, adj_p, is_sig, result_map[config_id]))

    return output


def _params_key(params: dict) -> str:
    """Deterministic string key for a params dict."""
    return "|".join(f"{k}={v}" for k, v in sorted(params.items()))


# -----------------------------------------------------------------------
# Stage 2 stub (built only if Stage 1 produces survivors)
# -----------------------------------------------------------------------
def cscv_pbo_check(results: List[BacktestResult], n_paths: int = 16):
    """
    PLACEHOLDER: Combinatorially Symmetric Cross-Validation.

    Not implemented yet. Will be built only if Stage 1 produces survivors
    that need post-selection overfitting validation.

    Expected interface:
      - Split equity curve into n_paths segments
      - For each combinatorial subset, compute IS-selected vs OOS performance
      - PBO = fraction of paths where IS-best underperforms in OOS
      - Threshold: PBO < 0.40

    Returns:
        dict with pbo_estimate, is_overfit, details
    """
    raise NotImplementedError(
        "Stage 2 (CSCV/PBO) is reserved for post-Stage-1-survivor validation. "
        "Build this only when Stage 1 produces candidates worth validating."
    )
