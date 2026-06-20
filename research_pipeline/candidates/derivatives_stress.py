"""BTC derivatives-stress exhaustion candidate (pre-registered v1).

This module is deterministic and research-only. It consumes rows whose context
has already been joined with strict availability-time semantics. It imports no
live bot module and has no brokerage path.

Frozen mechanism:
  1. Downside spot shock.
  2. At least two of: open-interest flush, negative-funding tail, negative
     mark/spot-basis tail.
  3. Spot-book exhaustion/recovery: bid replenishment, positive OFI, positive
     microprice displacement, and improving 10-bps depth imbalance.

The broad stress-only and book-only policies are diagnostic baselines. Only
combined_balanced_v1 and combined_strict_v1 are promotable.
"""
from __future__ import annotations

from collections import deque
import math
import statistics
from typing import Any, Dict, Iterable, List, Mapping, Sequence

from ..governance import promotion_gate


STRATEGY_ID = "btc_derivatives_stress_exhaustion"
STRATEGY_VERSION = "1.0.0"
PRODUCT_ID = "BTC-USD"

VARIANT_NAMES = (
    "stress_only_v1",
    "book_only_exhaustion_v1",
    "combined_balanced_v1",
    "combined_strict_v1",
)
PROMOTABLE_VARIANTS = {
    "combined_balanced_v1",
    "combined_strict_v1",
}
BASELINE_NAMES = (
    "no_trade",
    "buy_and_hold",
    "price_volatility_only",
    "archived_breakout",
)

LOOKBACK_US = 24 * 60 * 60 * 1_000_000
CHANGE_WINDOW_US = 15 * 60 * 1_000_000
BOOK_COMPARE_US = 5 * 60 * 1_000_000
EPISODE_COOLDOWN_US = 4 * 60 * 60 * 1_000_000
MIN_MINUTE_HISTORY = 360
MIN_FUNDING_HISTORY = 24
MIN_TRADE_COUNT = 30
MIN_DISTINCT_EPISODES = 15
ESS_FLOOR = 50

SPOT_Z_THRESHOLD = -1.5
OI_Z_THRESHOLD = -1.5
FUNDING_Z_THRESHOLD = -1.0
BASIS_Z_THRESHOLD = -1.0

ARCHIVED_BREAKOUT_EXPECTANCY = -0.004396598351996498


def _float(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _zscore(value: float | None, history: Sequence[float], minimum: int) -> float | None:
    clean = [float(x) for x in history if math.isfinite(float(x))]
    if value is None or len(clean) < minimum:
        return None
    stdev = statistics.pstdev(clean)
    if stdev <= 0:
        return None
    return (float(value) - statistics.fmean(clean)) / stdev


def _row_time(row: Any) -> int:
    if isinstance(row, Mapping):
        return int(row["event_time_us"])
    return int(row[0])


def _trim(rows: deque, cutoff_us: int) -> None:
    while rows and _row_time(rows[0]) < cutoff_us:
        rows.popleft()


def _previous(rows: Iterable[Mapping[str, Any]], target_us: int) -> Mapping[str, Any] | None:
    for row in reversed(list(rows)):
        if int(row["event_time_us"]) <= target_us:
            return row
    return None


def _episode_signal(
    condition: bool,
    *,
    variant: str,
    event_time_us: int,
    active: Dict[str, bool],
    last_signal: Dict[str, int],
) -> bool:
    if not condition:
        active[variant] = False
        return False
    if active.get(variant):
        return False
    if event_time_us - last_signal.get(variant, -10**30) < EPISODE_COOLDOWN_US:
        active[variant] = True
        return False
    active[variant] = True
    last_signal[variant] = event_time_us
    return True


def build_candidate_decisions(
    rows: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Return frozen candidate decisions using prior-only rolling statistics."""
    ordered = sorted(rows, key=lambda row: int(row["event_time_us"]))
    prior_rows: deque = deque()
    spot_changes: deque = deque()
    oi_changes: deque = deque()
    basis_values: deque = deque()
    funding_values: deque = deque()
    last_funding_event: int | None = None
    current_funding_z: float | None = None
    active: Dict[str, bool] = {}
    last_signal: Dict[str, int] = {}
    decisions = {name: [] for name in VARIANT_NAMES}
    baselines = {"price_volatility_only": []}
    eligible_rows = 0

    for raw in ordered:
        row = dict(raw)
        t = int(row["event_time_us"])
        cutoff = t - LOOKBACK_US
        for history in (prior_rows, spot_changes, oi_changes, basis_values, funding_values):
            _trim(history, cutoff)

        mid = _float(row.get("mid"))
        oi = _float(row.get("open_interest"))
        funding = _float(row.get("funding_rate"))
        mark = _float(row.get("mark_price"))
        previous_15m = _previous(prior_rows, t - CHANGE_WINDOW_US)
        previous_5m = _previous(prior_rows, t - BOOK_COMPARE_US)

        previous_mid = _float(previous_15m.get("mid")) if previous_15m else None
        previous_oi = _float(previous_15m.get("open_interest")) if previous_15m else None
        spot_return = (
            mid / previous_mid - 1.0
            if mid is not None and previous_mid not in (None, 0.0)
            else None
        )
        oi_return = (
            oi / previous_oi - 1.0
            if oi is not None and previous_oi not in (None, 0.0)
            else None
        )
        basis = (
            mark / mid - 1.0
            if mark is not None and mid not in (None, 0.0)
            else None
        )

        spot_z = _zscore(
            spot_return,
            [value for _, value in spot_changes],
            MIN_MINUTE_HISTORY,
        )
        oi_z = _zscore(
            oi_return,
            [value for _, value in oi_changes],
            MIN_MINUTE_HISTORY,
        )
        basis_z = _zscore(
            basis,
            [value for _, value in basis_values],
            MIN_MINUTE_HISTORY,
        )

        funding_event = row.get("funding_event_time_us")
        if funding is not None and funding_event is not None:
            funding_event = int(funding_event)
            if funding_event != last_funding_event:
                current_funding_z = _zscore(
                    funding,
                    [value for _, value in funding_values],
                    MIN_FUNDING_HISTORY,
                )
                funding_values.append((t, funding))
                last_funding_event = funding_event

        previous_depth = (
            _float(previous_5m.get("depth_imbalance_10bps"))
            if previous_5m else None
        )
        current_depth = _float(row.get("depth_imbalance_10bps"))
        bid_replenishment = _float(row.get("bid_replenishment_ratio"))
        ask_replenishment = _float(row.get("ask_replenishment_ratio"))
        ofi = _float(row.get("ofi_60s"))
        microprice_delta = _float(row.get("microprice_delta_bps"))

        spot_down = (
            spot_return is not None
            and spot_return < 0
            and spot_z is not None
            and spot_z <= SPOT_Z_THRESHOLD
        )
        derivative_flags = {
            "oi_flush": (
                oi_return is not None
                and oi_return < 0
                and oi_z is not None
                and oi_z <= OI_Z_THRESHOLD
            ),
            "funding_tail": (
                funding is not None
                and funding < 0
                and current_funding_z is not None
                and current_funding_z <= FUNDING_Z_THRESHOLD
            ),
            "negative_basis_tail": (
                basis is not None
                and basis < 0
                and basis_z is not None
                and basis_z <= BASIS_Z_THRESHOLD
            ),
        }
        book_flags = {
            "bid_replenishment": (
                bid_replenishment is not None
                and ask_replenishment is not None
                and bid_replenishment > 1.0
                and bid_replenishment > ask_replenishment
            ),
            "positive_ofi": ofi is not None and ofi > 0,
            "positive_microprice": (
                microprice_delta is not None and microprice_delta > 0
            ),
            "improving_depth": (
                current_depth is not None
                and previous_depth is not None
                and current_depth > previous_depth
            ),
        }
        derivative_count = sum(derivative_flags.values())
        book_count = sum(book_flags.values())

        fully_eligible = all(
            value is not None
            for value in (spot_z, oi_z, current_funding_z, basis_z)
        )
        if fully_eligible:
            eligible_rows += 1

        conditions = {
            "stress_only_v1": fully_eligible and spot_down and derivative_count >= 2,
            "book_only_exhaustion_v1": (
                fully_eligible and spot_down and book_count >= 3
            ),
            "combined_balanced_v1": (
                fully_eligible
                and spot_down
                and derivative_count >= 2
                and book_count >= 3
            ),
            "combined_strict_v1": (
                fully_eligible
                and spot_down
                and derivative_count == 3
                and book_count == 4
            ),
        }
        baseline_conditions = {
            "price_volatility_only": spot_down,
        }
        snapshot = {
            "event_time_us": t,
            "mid": mid,
            "spot_return_15m": spot_return,
            "spot_return_z": spot_z,
            "oi_return_15m": oi_return,
            "oi_return_z": oi_z,
            "funding_rate": funding,
            "funding_z": current_funding_z,
            "basis": basis,
            "basis_z": basis_z,
            "derivative_flags": derivative_flags,
            "book_flags": book_flags,
            "derivative_count": derivative_count,
            "book_count": book_count,
        }
        for variant, condition in conditions.items():
            if _episode_signal(
                bool(condition),
                variant=variant,
                event_time_us=t,
                active=active,
                last_signal=last_signal,
            ):
                decisions[variant].append(dict(snapshot))
        for baseline, condition in baseline_conditions.items():
            if _episode_signal(
                bool(condition),
                variant=f"baseline:{baseline}",
                event_time_us=t,
                active=active,
                last_signal=last_signal,
            ):
                baselines[baseline].append(dict(snapshot))

        if spot_return is not None:
            spot_changes.append((t, spot_return))
        if oi_return is not None:
            oi_changes.append((t, oi_return))
        if basis is not None:
            basis_values.append((t, basis))
        prior_rows.append(row)

    return {
        "strategy_id": STRATEGY_ID,
        "strategy_version": STRATEGY_VERSION,
        "product_id": PRODUCT_ID,
        "variant_names": list(VARIANT_NAMES),
        "promotable_variants": sorted(PROMOTABLE_VARIANTS),
        "baseline_names": list(BASELINE_NAMES),
        "rows_seen": len(ordered),
        "eligible_rows": eligible_rows,
        "decisions": decisions,
        "baselines": baselines,
        "frozen_thresholds": {
            "spot_z": SPOT_Z_THRESHOLD,
            "oi_z": OI_Z_THRESHOLD,
            "funding_z": FUNDING_Z_THRESHOLD,
            "basis_z": BASIS_Z_THRESHOLD,
            "minimum_minute_history": MIN_MINUTE_HISTORY,
            "minimum_funding_history": MIN_FUNDING_HISTORY,
            "episode_cooldown_hours": EPISODE_COOLDOWN_US / 3_600_000_000,
        },
    }


class _VariantStore:
    def count_variants(self, _track: str | None = None) -> int:
        return len(VARIANT_NAMES)


def _mean(values: Sequence[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def _purged_walk_forward_expectancies(
    trial_matrix: Sequence[Sequence[float]],
    event_times_us: Sequence[int],
    *,
    horizon_us: int,
    n_folds: int = 6,
) -> Dict[int, List[float]]:
    """Return fixed-variant OOS folds with past-only purging.

    The candidate thresholds are pre-registered and are not fitted. Each fold
    therefore evaluates every fixed variant on a future contiguous test block.
    Training observations whose label window reaches the test start are
    explicitly purged; future observations are never used.
    """
    if not trial_matrix:
        return {}
    n = len(event_times_us)
    if (
        n < max(24, n_folds * 4)
        or any(len(row) != n for row in trial_matrix)
        or any(
            int(event_times_us[i]) >= int(event_times_us[i + 1])
            for i in range(n - 1)
        )
    ):
        return {}

    first_test = n // 2
    remaining = n - first_test
    fold_width = max(1, remaining // n_folds)
    output = {index: [] for index in range(len(trial_matrix))}
    for fold in range(n_folds):
        test_lo = first_test + fold * fold_width
        test_hi = n if fold == n_folds - 1 else min(n, test_lo + fold_width)
        if test_lo >= test_hi:
            continue
        test_start = int(event_times_us[test_lo])
        train = [
            index
            for index in range(test_lo)
            if int(event_times_us[index]) + horizon_us < test_start
        ]
        if len(train) < 12:
            continue
        for variant_index, values in enumerate(trial_matrix):
            output[variant_index].append(_mean(values[test_lo:test_hi]))
    return output


def _select_promotable_winner(
    variant_metrics: Mapping[str, Mapping[str, Any]],
    folds: Mapping[int, Sequence[float]],
) -> str:
    def score(name: str) -> tuple:
        index = VARIANT_NAMES.index(name)
        oos = list(folds.get(index, []))
        return (
            min(oos) if oos else -math.inf,
            _mean(oos) if oos else -math.inf,
            float(variant_metrics[name]["mean_signal_return"]),
            int(variant_metrics[name]["signals"]),
            name,
        )

    return max(PROMOTABLE_VARIANTS, key=score)


def _concentration_ok(values: Sequence[float]) -> tuple[bool, float | None]:
    positives = [value for value in values if value > 0]
    if not positives:
        return False, None
    total = sum(positives)
    share = max(positives) / total if total else 1.0
    return share <= 0.50, share


def evaluate_candidate(
    candidate: Mapping[str, Any],
    outcomes: Mapping[int, Mapping[str, Mapping[float, float]]],
    *,
    path_outcomes: Mapping[int, Mapping[Any, Any]] | None = None,
    operational_gates: Mapping[str, bool] | None = None,
) -> Dict[str, Any]:
    """Evaluate the frozen family at 1h and 4h with complete governance."""
    decisions = candidate["decisions"]
    candidate_times = sorted({
        int(item["event_time_us"])
        for variant in VARIANT_NAMES
        for item in decisions.get(variant, [])
    })
    price_baseline_times = {
        int(item["event_time_us"])
        for item in candidate.get("baselines", {}).get(
            "price_volatility_only", []
        )
    }
    opportunity_times = sorted(set(candidate_times) | price_baseline_times)
    decision_sets = {
        variant: {
            int(item["event_time_us"])
            for item in decisions.get(variant, [])
        }
        for variant in VARIANT_NAMES
    }
    operational = dict(operational_gates or {})
    required_ops = {"replay_parity", "freshness", "outage", "storage"}
    missing_ops = sorted(required_ops - set(operational))
    global_reasons: List[str] = []
    if candidate.get("eligible_rows", 0) <= 0:
        global_reasons.append("prior-only derivative/spot history is insufficient")
    if not candidate_times:
        global_reasons.append("no distinct stress/exhaustion episodes observed")
    if missing_ops:
        global_reasons.append(
            f"operational evidence missing: {', '.join(missing_ops)}"
        )

    horizon_results: Dict[str, Any] = {}
    for horizon in ("1h", "4h"):
        horizon_us = (3600 if horizon == "1h" else 14400) * 1_000_000
        normal_matrix: List[List[float]] = []
        stress_matrix: List[List[float]] = []
        available_times = [
            t for t in opportunity_times
            if horizon in outcomes.get(t, {})
            and 1.0 in outcomes[t][horizon]
            and 2.0 in outcomes[t][horizon]
        ]
        for variant in VARIANT_NAMES:
            selected = decision_sets[variant]
            normal_matrix.append([
                float(outcomes[t][horizon][1.0]) if t in selected else 0.0
                for t in available_times
            ])
            stress_matrix.append([
                float(outcomes[t][horizon][2.0]) if t in selected else 0.0
                for t in available_times
            ])
        variant_metrics = {}
        for idx, variant in enumerate(VARIANT_NAMES):
            signal_times = [t for t in available_times if t in decision_sets[variant]]
            signal_returns = [
                float(outcomes[t][horizon][1.0]) for t in signal_times
            ]
            stress_returns = [
                float(outcomes[t][horizon][2.0]) for t in signal_times
            ]
            concentration_pass, max_win_share = _concentration_ok(signal_returns)
            variant_metrics[variant] = {
                "signals": len(signal_times),
                "mean_signal_return": _mean(signal_returns),
                "mean_signal_stress_return": _mean(stress_returns),
                "positive_signal_rate": (
                    sum(value > 0 for value in signal_returns) / len(signal_returns)
                    if signal_returns else 0.0
                ),
                "max_positive_event_share": max_win_share,
                "concentration_pass": concentration_pass,
            }

        fold_expectancies = _purged_walk_forward_expectancies(
            normal_matrix,
            available_times,
            horizon_us=horizon_us,
        )
        winner = _select_promotable_winner(
            variant_metrics,
            fold_expectancies,
        )
        winner_idx = VARIANT_NAMES.index(winner)
        winner_signal_times = [
            t for t in available_times if t in decision_sets[winner]
        ]
        buy_hold = _mean([
            float(outcomes[t][horizon][1.0]) for t in available_times
        ])
        volatility_baseline = _mean([
            float(outcomes[t][horizon][1.0])
            if t in price_baseline_times else 0.0
            for t in available_times
        ])
        gate = promotion_gate(
            _VariantStore(),
            "derivatives_stress",
            net_returns=normal_matrix[winner_idx],
            stress_net_returns=stress_matrix[winner_idx],
            decision_times_us=winner_signal_times,
            horizon_us=horizon_us,
            ess_floor=ESS_FLOOR,
            baseline_expectancies={
                "no_trade": 0.0,
                "buy_and_hold": buy_hold,
                "price_volatility_only": volatility_baseline,
                "archived_breakout": ARCHIVED_BREAKOUT_EXPECTANCY,
            },
            oos_fold_expectancies=fold_expectancies.get(winner_idx, []),
            trial_returns=normal_matrix if available_times else None,
            operational_gates=operational,
            cscv_slices=4,
        )
        extra_reasons = []
        if variant_metrics[winner]["signals"] < MIN_TRADE_COUNT:
            extra_reasons.append(
                f"winner has {variant_metrics[winner]['signals']} completed signals; "
                f"minimum is {MIN_TRADE_COUNT}"
            )
        if len(winner_signal_times) < MIN_DISTINCT_EPISODES:
            extra_reasons.append(
                f"winner has {len(winner_signal_times)} completed distinct episodes; "
                f"minimum is {MIN_DISTINCT_EPISODES}"
            )
        if not variant_metrics[winner]["concentration_pass"]:
            extra_reasons.append("winner depends too heavily on one positive event")
        horizon_results[horizon] = {
            "winner": winner,
            "available_opportunities": len(available_times),
            "purged_walk_forward": {
                VARIANT_NAMES[index]: list(values)
                for index, values in fold_expectancies.items()
            },
            "variants": variant_metrics,
            "governance": {
                "promotion": (
                    "EVIDENCE_PASSED"
                    if gate.promotion == "EVIDENCE_PASSED" and not extra_reasons
                    else "BLOCKED"
                ),
                "reasons": list(gate.reasons) + extra_reasons,
                "metrics": gate.metrics,
            },
        }

    same_winner = (
        horizon_results["1h"]["winner"] == horizon_results["4h"]["winner"]
    )
    if not same_winner:
        global_reasons.append("1h and 4h selected different combined variants")

    path_outcomes = path_outcomes or {}
    path_available_times = [
        t for t in candidate_times
        if 1.0 in path_outcomes.get(t, {})
        and 2.0 in path_outcomes.get(t, {})
    ]
    path_normal_matrix = []
    path_stress_matrix = []
    for variant in VARIANT_NAMES:
        selected = decision_sets[variant]
        path_normal_matrix.append([
            float(path_outcomes[t][1.0]) if t in selected else 0.0
            for t in path_available_times
        ])
        path_stress_matrix.append([
            float(path_outcomes[t][2.0]) if t in selected else 0.0
            for t in path_available_times
        ])
    path_variant_metrics = {}
    for variant in VARIANT_NAMES:
        signal_times = [
            t for t in path_available_times if t in decision_sets[variant]
        ]
        values = [float(path_outcomes[t][1.0]) for t in signal_times]
        stress_values = [
            float(path_outcomes[t][2.0]) for t in signal_times
        ]
        concentration_pass, max_win_share = _concentration_ok(values)
        exit_reasons: Dict[str, int] = {}
        for t in signal_times:
            reason = str(path_outcomes[t].get("reason") or "UNKNOWN")
            exit_reasons[reason] = exit_reasons.get(reason, 0) + 1
        path_variant_metrics[variant] = {
            "signals": len(signal_times),
            "mean_signal_return": _mean(values),
            "mean_signal_stress_return": _mean(stress_values),
            "positive_signal_rate": (
                sum(value > 0 for value in values) / len(values)
                if values else 0.0
            ),
            "max_positive_event_share": max_win_share,
            "concentration_pass": concentration_pass,
            "exit_reasons": exit_reasons,
        }
    path_folds = _purged_walk_forward_expectancies(
        path_normal_matrix,
        path_available_times,
        horizon_us=14_400_000_000,
    )
    path_winner = _select_promotable_winner(
        path_variant_metrics,
        path_folds,
    )
    path_winner_index = VARIANT_NAMES.index(path_winner)
    path_signal_times = [
        t for t in path_available_times if t in decision_sets[path_winner]
    ]
    buy_hold_path_values = [
        float(outcomes[t]["4h"][1.0])
        for t in path_available_times
        if "4h" in outcomes.get(t, {})
        and 1.0 in outcomes[t]["4h"]
    ]
    volatility_path_values = [
        (
            float(outcomes[t]["4h"][1.0])
            if (
                t in price_baseline_times
                and "4h" in outcomes.get(t, {})
                and 1.0 in outcomes[t]["4h"]
            )
            else 0.0
        )
        for t in path_available_times
    ]
    path_gate = promotion_gate(
        _VariantStore(),
        "derivatives_stress_exit",
        net_returns=(
            path_normal_matrix[path_winner_index]
            if path_normal_matrix else []
        ),
        stress_net_returns=(
            path_stress_matrix[path_winner_index]
            if path_stress_matrix else []
        ),
        decision_times_us=path_signal_times,
        horizon_us=14_400_000_000,
        ess_floor=ESS_FLOOR,
        baseline_expectancies={
            "no_trade": 0.0,
            "buy_and_hold": _mean(buy_hold_path_values),
            "price_volatility_only": _mean(volatility_path_values),
            "archived_breakout": ARCHIVED_BREAKOUT_EXPECTANCY,
        },
        oos_fold_expectancies=path_folds.get(path_winner_index, []),
        trial_returns=path_normal_matrix if path_available_times else None,
        operational_gates=operational,
        cscv_slices=4,
    )
    path_extra_reasons = []
    if path_variant_metrics[path_winner]["signals"] < MIN_TRADE_COUNT:
        path_extra_reasons.append(
            f"winner has {path_variant_metrics[path_winner]['signals']} "
            f"completed signals; minimum is {MIN_TRADE_COUNT}"
        )
    if len(path_signal_times) < MIN_DISTINCT_EPISODES:
        path_extra_reasons.append(
            f"winner has {len(path_signal_times)} completed distinct episodes; "
            f"minimum is {MIN_DISTINCT_EPISODES}"
        )
    if not path_variant_metrics[path_winner]["concentration_pass"]:
        path_extra_reasons.append(
            "winner depends too heavily on one positive event"
        )
    path_result = {
        "winner": path_winner,
        "available_candidate_episodes": len(path_available_times),
        "purged_walk_forward": {
            VARIANT_NAMES[index]: list(values)
            for index, values in path_folds.items()
        },
        "variants": path_variant_metrics,
        "governance": {
            "promotion": (
                "EVIDENCE_PASSED"
                if path_gate.promotion == "EVIDENCE_PASSED"
                and not path_extra_reasons
                else "BLOCKED"
            ),
            "reasons": list(path_gate.reasons) + path_extra_reasons,
            "metrics": path_gate.metrics,
        },
    }
    exit_path_complete = (
        path_result["governance"]["promotion"] == "EVIDENCE_PASSED"
        and not path_result["governance"]["reasons"]
        and path_winner == horizon_results["1h"]["winner"]
    )
    if not exit_path_complete:
        global_reasons.append(
            "path-dependent stop/target/time-stop evidence is not complete"
        )
    enough_to_grade = (
        candidate.get("eligible_rows", 0) > 0
        and bool(candidate_times)
        and all(
            horizon_results[h]["variants"][
                horizon_results[h]["winner"]
            ]["signals"] >= MIN_TRADE_COUNT
            for h in ("1h", "4h")
        )
        and path_variant_metrics[path_winner]["signals"] >= MIN_TRADE_COUNT
        and not missing_ops
    )
    passed = (
        enough_to_grade
        and all(
            horizon_results[h]["governance"]["promotion"] == "EVIDENCE_PASSED"
            and not horizon_results[h]["governance"]["reasons"]
            for h in ("1h", "4h")
        )
        and same_winner
        and exit_path_complete
    )
    if passed:
        evidence_status = "EVIDENCE_PASSED"
    elif enough_to_grade:
        evidence_status = "DEMOTED"
    else:
        evidence_status = "INSUFFICIENT_EVIDENCE"

    return {
        "schema_version": 1,
        "strategy_id": STRATEGY_ID,
        "strategy_version": STRATEGY_VERSION,
        "product_id": PRODUCT_ID,
        "evidence_status": evidence_status,
        "live_authority_granted": False,
        "automatic_parameter_changes": False,
        "global_reasons": global_reasons,
        "rows_seen": candidate.get("rows_seen", 0),
        "eligible_rows": candidate.get("eligible_rows", 0),
        "distinct_candidate_episodes": len(candidate_times),
        "distinct_evaluation_opportunities": len(opportunity_times),
        "operational_gates": operational,
        "exit_contract": {
            "exit_contract_id": "derivatives_stress_exit_v1",
            "stop_distance": (
                "clamp(0.5 * abs(15m spot return), 0.5%, 2.0%)"
            ),
            "take_profit_r": 1.5,
            "time_stop_seconds": 14400,
            "path_dependent_evidence_complete": exit_path_complete,
        },
        "baseline_provenance": {
            "archived_breakout": {
                "source": "research/results/pilot_results.json",
                "field": "BTC-USD sensitivity=1.0 oos_expect_median",
                "expectancy": ARCHIVED_BREAKOUT_EXPECTANCY,
            },
            "buy_and_hold": "long every available registered opportunity",
            "price_volatility_only": (
                "episode-start 15-minute downside spot shock with z <= -1.5"
            ),
            "no_trade": "zero return",
        },
        "frozen_thresholds": candidate.get("frozen_thresholds", {}),
        "horizons": horizon_results,
        "path_dependent_exit": path_result,
    }
