"""
Evaluation & governance (contract §11-§14).

Implemented:
  * purged walk-forward splits with embargo  (real, tested)
  * effective sample size                     (real, tested)
  * no-trade baseline                         (real: expectancy 0)
  * variant counting                          (via the store registry)
  * Deflated Sharpe Ratio                      (Bailey & Lopez de Prado)
  * CSCV / Probability of Backtest Overfitting

The evidence gate remains BLOCKED unless every contract input is supplied and
passes. Even EVIDENCE_PASSED never grants live order authority; promotion still
requires a separate human-authorized implementation step (contract §14/§16).
"""
from __future__ import annotations

import itertools
import math
import statistics
from dataclasses import dataclass, field
from statistics import NormalDist
from typing import Any, Dict, List, Optional, Sequence, Tuple


@dataclass
class GovernanceStatus:
    promotion: str                       # BLOCKED | EVIDENCE_PASSED
    reasons: List[str] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)


def purged_walk_forward_splits(n: int, n_folds: int, horizon_bars: int,
                               embargo_mult: float = 1.0) -> List[Tuple[List[int], List[int]]]:
    """Time-ordered purged walk-forward splits with embargo.

    For each contiguous test fold, the training set excludes any index whose label window
    [i, i+horizon_bars] overlaps the test block, plus an embargo of
    ceil(horizon_bars * embargo_mult) indices after the test block. No shuffling.
    Returns a list of (train_idx, test_idx).
    """
    if n <= 0 or n_folds <= 1:
        return []
    embargo = math.ceil(horizon_bars * embargo_mult)
    fold = max(1, n // n_folds)
    splits: List[Tuple[List[int], List[int]]] = []
    start = 0
    for f in range(n_folds):
        test_lo = start
        test_hi = n if f == n_folds - 1 else min(n, start + fold)
        if test_lo >= test_hi:
            break
        test_idx = list(range(test_lo, test_hi))
        # purge: drop training rows whose [i, i+horizon] overlaps the test block,
        # and the embargo band after the test block.
        purge_lo = test_lo - horizon_bars
        purge_hi = test_hi + embargo
        train_idx = [i for i in range(n)
                     if (i < purge_lo or i >= purge_hi) and not (test_lo <= i < test_hi)]
        splits.append((train_idx, test_idx))
        start = test_hi
    return splits


def effective_sample_size(decision_times_us: Sequence[int], horizon_us: int) -> Dict[str, float]:
    """Report raw N and a non-overlapping ESS = time_span / horizon (contract §13)."""
    n = len(decision_times_us)
    if n == 0:
        return {"n": 0, "ess_nonoverlap": 0.0}
    span = max(decision_times_us) - min(decision_times_us)
    ess = (span / horizon_us) if horizon_us > 0 else float(n)
    return {"n": n, "ess_nonoverlap": round(min(float(n), ess), 4)}


def baseline_no_trade(net_returns: Sequence[float]) -> Dict[str, float]:
    """No-trade baseline: zero expectancy by construction."""
    return {"baseline": "no_trade", "expectancy": 0.0, "n": len(net_returns)}


def baseline_buy_and_hold(*args, **kwargs) -> Dict[str, Any]:
    """SCAFFOLD: needs a continuous price series over the eval window (not from labels)."""
    return {"baseline": "buy_and_hold", "status": "scaffold",
            "note": "requires continuous price series; not computed in the spine"}


def _sample_sharpe(values: Sequence[float]) -> float:
    clean = [float(x) for x in values if math.isfinite(float(x))]
    if len(clean) < 2:
        return 0.0
    mean = statistics.fmean(clean)
    stdev = statistics.stdev(clean)
    if stdev == 0:
        if mean > 0:
            return float("inf")
        if mean < 0:
            return float("-inf")
        return 0.0
    return mean / stdev


def _return_moments(values: Sequence[float]) -> Dict[str, float]:
    clean = [float(x) for x in values if math.isfinite(float(x))]
    n = len(clean)
    if n < 2:
        raise ValueError("at least two finite returns are required")
    mean = statistics.fmean(clean)
    centered = [x - mean for x in clean]
    m2 = statistics.fmean(x * x for x in centered)
    if m2 <= 0:
        return {
            "n": n, "mean": mean, "stdev": 0.0,
            "skew": 0.0, "kurtosis": 3.0,
        }
    m3 = statistics.fmean(x ** 3 for x in centered)
    m4 = statistics.fmean(x ** 4 for x in centered)
    return {
        "n": n,
        "mean": mean,
        "stdev": math.sqrt(m2),
        "skew": m3 / (m2 ** 1.5),
        "kurtosis": m4 / (m2 * m2),
    }


def expected_max_sharpe(
    num_trials: int,
    sharpe_variance: float,
    null_mean: float = 0.0,
) -> float:
    """Expected maximum Sharpe under the null (paper Eq. 1 / Appendix snippet).

    Sharpe ratios must be non-annualized and measured at the same observation
    frequency as the selected strategy.
    """
    if num_trials <= 1 or sharpe_variance <= 0:
        return float(null_mean)
    nd = NormalDist()
    gamma = 0.5772156649015329

    def q(p: float) -> float:
        return nd.inv_cdf(min(1.0 - 1e-12, max(1e-12, p)))

    max_z = (
        (1.0 - gamma) * q(1.0 - 1.0 / num_trials)
        + gamma * q(1.0 - 1.0 / (num_trials * math.e))
    )
    return float(null_mean) + math.sqrt(sharpe_variance) * max_z


def deflated_sharpe_ratio(
    returns: Sequence[float],
    *,
    trial_sharpes: Optional[Sequence[float]] = None,
    num_trials: Optional[int] = None,
    sharpe_variance: Optional[float] = None,
    null_mean: float = 0.0,
) -> Dict[str, float]:
    """Compute the Deflated Sharpe Ratio probability and z-score.

    Implements Bailey & Lopez de Prado (2014): the observed non-annualized
    Sharpe is tested against the expected maximum Sharpe produced by repeated
    trials, while correcting for sample length, skewness, and kurtosis.
    """
    moments = _return_moments(returns)
    observed = _sample_sharpe(returns)
    if not math.isfinite(observed):
        observed = math.copysign(1e12, observed)

    finite_trial_sharpes = [
        float(x) for x in (trial_sharpes or [])
        if math.isfinite(float(x))
    ]
    if num_trials is None:
        num_trials = len(finite_trial_sharpes) or 1
    if num_trials < 1:
        raise ValueError("num_trials must be >= 1")
    if sharpe_variance is None:
        if len(finite_trial_sharpes) >= 2:
            sharpe_variance = statistics.variance(finite_trial_sharpes)
        elif num_trials == 1:
            sharpe_variance = 0.0
        else:
            raise ValueError(
                "trial_sharpes or sharpe_variance is required when num_trials > 1"
            )
    if sharpe_variance < 0:
        raise ValueError("sharpe_variance must be non-negative")

    sr0 = expected_max_sharpe(num_trials, sharpe_variance, null_mean)
    denom_term = (
        1.0
        - moments["skew"] * observed
        + ((moments["kurtosis"] - 1.0) / 4.0) * (observed ** 2)
    )
    if denom_term <= 0:
        raise ValueError("DSR denominator is non-positive for the supplied moments")
    z = (
        (observed - sr0)
        * math.sqrt(moments["n"] - 1)
        / math.sqrt(denom_term)
    )
    probability = NormalDist().cdf(z)
    return {
        "probability": probability,
        "z_score": z,
        "observed_sharpe": observed,
        "expected_max_sharpe": sr0,
        "num_trials": int(num_trials),
        "sharpe_variance": float(sharpe_variance),
        "n": int(moments["n"]),
        "skew": moments["skew"],
        "kurtosis": moments["kurtosis"],
    }


def _average_rank_ascending(values: Sequence[float], selected_index: int) -> float:
    selected = values[selected_index]
    less = sum(v < selected for v in values)
    equal = sum(v == selected for v in values)
    return less + (equal + 1.0) / 2.0


def cscv_pbo(
    trial_returns: Sequence[Sequence[float]],
    *,
    n_slices: int = 8,
    threshold: float = 0.40,
    max_combinations: int = 50_000,
) -> Dict[str, Any]:
    """Estimate Probability of Backtest Overfitting with CSCV.

    `trial_returns` is strategy-major: one equal-length return series per
    registered trial. Observations are split into an even number of contiguous
    slices. For every symmetric half/half combination, the best in-sample
    strategy is ranked out-of-sample. PBO is the fraction whose OOS logit is
    <= 0, meaning the IS winner landed at or below the OOS median.
    """
    matrix = [
        [float(x) for x in row]
        for row in trial_returns
    ]
    n_trials = len(matrix)
    if n_trials < 2:
        raise ValueError("CSCV requires at least two strategy trials")
    lengths = {len(row) for row in matrix}
    if len(lengths) != 1:
        raise ValueError("all trial return series must have equal length")
    n_obs = lengths.pop()
    if n_slices < 2 or n_slices % 2:
        raise ValueError("n_slices must be an even integer >= 2")
    if n_obs < n_slices:
        raise ValueError("number of observations must be >= n_slices")

    base, extra = divmod(n_obs, n_slices)
    slices: List[List[int]] = []
    cursor = 0
    for i in range(n_slices):
        width = base + (1 if i < extra else 0)
        slices.append(list(range(cursor, cursor + width)))
        cursor += width

    choose = n_slices // 2
    n_combinations = math.comb(n_slices, choose)
    if n_combinations > max_combinations:
        raise ValueError(
            f"CSCV combinations {n_combinations} exceed limit {max_combinations}"
        )

    logits: List[float] = []
    oos_ranks: List[float] = []
    selected_counts = [0] * n_trials
    oos_selected_scores: List[float] = []
    degradation: List[float] = []
    all_slice_ids = set(range(n_slices))

    for is_slice_ids in itertools.combinations(range(n_slices), choose):
        is_set = set(is_slice_ids)
        oos_slice_ids = sorted(all_slice_ids - is_set)
        is_idx = sorted(i for s in is_slice_ids for i in slices[s])
        oos_idx = sorted(i for s in oos_slice_ids for i in slices[s])

        is_perf = [_sample_sharpe([row[i] for i in is_idx]) for row in matrix]
        winner = max(range(n_trials), key=lambda i: (is_perf[i], -i))
        selected_counts[winner] += 1

        oos_perf = [_sample_sharpe([row[i] for i in oos_idx]) for row in matrix]
        rank = _average_rank_ascending(oos_perf, winner)
        omega = rank / (n_trials + 1.0)
        logit = math.log(omega / (1.0 - omega))
        logits.append(logit)
        oos_ranks.append(rank)
        oos_selected_scores.append(oos_perf[winner])
        degradation.append(oos_perf[winner] - is_perf[winner])

    pbo = sum(x <= 0.0 for x in logits) / len(logits)
    return {
        "pbo": pbo,
        "threshold": threshold,
        "is_overfit": pbo >= threshold,
        "n_trials": n_trials,
        "n_observations": n_obs,
        "n_slices": n_slices,
        "n_combinations": len(logits),
        "logits": logits,
        "oos_ranks": oos_ranks,
        "selected_counts": selected_counts,
        "mean_performance_degradation": statistics.fmean(degradation),
        "probability_of_oos_loss": (
            sum(x < 0.0 for x in oos_selected_scores) / len(oos_selected_scores)
        ),
    }


def promotion_gate(store, track: str, net_returns: Sequence[float],
                   decision_times_us: Sequence[int], horizon_us: int,
                   ess_floor: int = 50,
                   *,
                   stress_net_returns: Optional[Sequence[float]] = None,
                   baseline_expectancies: Optional[Dict[str, float]] = None,
                   oos_fold_expectancies: Optional[Sequence[float]] = None,
                   trial_returns: Optional[Sequence[Sequence[float]]] = None,
                   trial_sharpes: Optional[Sequence[float]] = None,
                   operational_gates: Optional[Dict[str, bool]] = None,
                   pbo_threshold: float = 0.40,
                   cscv_slices: int = 8) -> GovernanceStatus:
    """Evaluate every evidence gate in contract §14.

    Passing returns EVIDENCE_PASSED, not live authorization. Missing evidence is
    a blocker rather than an implicit pass.
    """
    reasons: List[str] = []
    metrics: Dict[str, Any] = {}

    ess = effective_sample_size(decision_times_us, horizon_us)
    metrics["ess"] = ess
    metrics["variants_registered"] = store.count_variants(track)
    mean_net = statistics.fmean(net_returns) if net_returns else None
    metrics["mean_net_return"] = mean_net
    if mean_net is None or mean_net <= 0:
        reasons.append("1.0x post-friction expectancy is not positive")

    if stress_net_returns:
        stress_mean = statistics.fmean(stress_net_returns)
        metrics["mean_stress_net_return"] = stress_mean
        if stress_mean <= 0:
            reasons.append("binding-stress post-friction expectancy is not positive")
    else:
        metrics["mean_stress_net_return"] = None
        reasons.append("binding-stress returns missing")

    required_baselines = {
        "no_trade", "buy_and_hold", "price_volatility_only", "archived_breakout"
    }
    baseline_expectancies = baseline_expectancies or {}
    metrics["baseline_expectancies"] = dict(baseline_expectancies)
    missing_baselines = sorted(required_baselines - set(baseline_expectancies))
    if missing_baselines:
        reasons.append(f"baseline evidence missing: {', '.join(missing_baselines)}")
    elif mean_net is not None:
        failed = sorted(
            name for name, value in baseline_expectancies.items()
            if name in required_baselines and mean_net <= float(value)
        )
        if failed:
            reasons.append(f"no incremental value over baselines: {', '.join(failed)}")

    if not oos_fold_expectancies:
        metrics["oos_fold_expectancies"] = []
        reasons.append("purged walk-forward OOS fold results missing")
    else:
        folds = [float(x) for x in oos_fold_expectancies]
        metrics["oos_fold_expectancies"] = folds
        if any(x <= 0 for x in folds):
            reasons.append("one or more purged-WF OOS folds are non-positive")

    if ess["ess_nonoverlap"] < ess_floor:
        reasons.append(
            f"ESS {ess['ess_nonoverlap']} < floor {ess_floor}")

    if trial_sharpes is None and trial_returns is not None:
        trial_sharpes = [_sample_sharpe(row) for row in trial_returns]
    if trial_sharpes is None:
        metrics["dsr"] = None
        reasons.append("DSR trial Sharpe evidence missing")
    else:
        try:
            dsr = deflated_sharpe_ratio(
                net_returns,
                trial_sharpes=trial_sharpes,
                num_trials=max(store.count_variants(track), len(trial_sharpes)),
            )
            metrics["dsr"] = dsr
            # Contract says DSR > 0; use the signed z-score, while reporting the
            # probability as the conventional DSR confidence.
            if dsr["z_score"] <= 0:
                reasons.append("DSR z-score is not positive")
        except ValueError as exc:
            metrics["dsr"] = None
            reasons.append(f"DSR unavailable: {exc}")

    if trial_returns is None:
        metrics["pbo"] = None
        reasons.append("CSCV/PBO trial return matrix missing")
    else:
        try:
            pbo = cscv_pbo(
                trial_returns,
                n_slices=cscv_slices,
                threshold=pbo_threshold,
            )
            metrics["pbo"] = pbo
            if pbo["pbo"] >= pbo_threshold:
                reasons.append(
                    f"PBO {pbo['pbo']:.4f} >= threshold {pbo_threshold:.4f}"
                )
        except ValueError as exc:
            metrics["pbo"] = None
            reasons.append(f"CSCV/PBO unavailable: {exc}")

    required_ops = {"replay_parity", "freshness", "outage", "storage"}
    operational_gates = operational_gates or {}
    metrics["operational_gates"] = dict(operational_gates)
    missing_ops = sorted(required_ops - set(operational_gates))
    if missing_ops:
        reasons.append(f"operational gates missing: {', '.join(missing_ops)}")
    failed_ops = sorted(k for k in required_ops if operational_gates.get(k) is False)
    if failed_ops:
        reasons.append(f"operational gates failed: {', '.join(failed_ops)}")

    promotion = "EVIDENCE_PASSED" if not reasons else "BLOCKED"
    return GovernanceStatus(promotion=promotion, reasons=reasons, metrics=metrics)
