from research_pipeline.candidates.derivatives_stress import (
    MIN_FUNDING_HISTORY,
    MIN_MINUTE_HISTORY,
    _purged_walk_forward_expectancies,
    build_candidate_decisions,
    evaluate_candidate,
)


MINUTE = 60_000_000


def _row(i, *, shock=False, recovery=False):
    mid = 100.0 + i * 0.001
    oi = 1000.0 + i * 0.01
    funding = -0.000001 + (i % 24) * 0.00000001
    mark = mid * 1.0001
    if shock:
        mid *= 0.97
        oi *= 0.97
        funding = -0.001
        mark = mid * 0.98
    return {
        "event_time_us": i * MINUTE,
        "mid": mid,
        "open_interest": oi,
        "funding_event_time_us": (i // 60) * 60 * MINUTE,
        "funding_rate": funding,
        "mark_price": mark,
        "depth_imbalance_10bps": 0.3 if recovery else -0.2,
        "ofi_60s": 10.0 if recovery else -1.0,
        "microprice_delta_bps": 0.2 if recovery else -0.1,
        "bid_replenishment_ratio": 1.4 if recovery else 0.9,
        "ask_replenishment_ratio": 0.8 if recovery else 1.1,
    }


def test_candidate_requires_prior_only_history():
    rows = [_row(i) for i in range(120)]
    result = build_candidate_decisions(rows)
    assert result["eligible_rows"] == 0
    assert all(not values for values in result["decisions"].values())


def test_candidate_emits_episode_once_after_history():
    total = max(MIN_MINUTE_HISTORY + 20, MIN_FUNDING_HISTORY * 60 + 20)
    rows = [_row(i) for i in range(total)]
    # Build varied prior distributions, then produce a single severe episode.
    for i, row in enumerate(rows):
        row["mid"] += ((i % 17) - 8) * 0.01
        row["open_interest"] += ((i % 19) - 9) * 0.2
        row["mark_price"] = row["mid"] * (1 + ((i % 13) - 6) * 0.0001)
        row["funding_rate"] = ((i // 60) % 11 - 5) * 0.00001
    start = total - 5
    for i in range(start, total):
        rows[i].update(_row(i, shock=True, recovery=True))
        rows[i]["funding_event_time_us"] = i * MINUTE
    result = build_candidate_decisions(rows)
    assert len(result["decisions"]["combined_balanced_v1"]) == 1
    assert len(result["decisions"]["combined_strict_v1"]) == 1
    assert len(result["baselines"]["price_volatility_only"]) == 1
    strict = result["decisions"]["combined_strict_v1"][0]
    assert strict["derivative_count"] == 3
    assert strict["book_count"] == 4


def test_purged_walk_forward_uses_future_contiguous_test_folds():
    spacing = 4 * 60 * MINUTE
    times = [i * spacing for i in range(48)]
    matrix = [
        [-0.01] * 24 + [0.01] * 24,
        [0.01] * 24 + [-0.01] * 24,
    ]
    folds = _purged_walk_forward_expectancies(
        matrix,
        times,
        horizon_us=60 * MINUTE,
    )
    assert len(folds[0]) == 6
    assert all(value > 0 for value in folds[0])
    assert all(value < 0 for value in folds[1])


def test_evaluation_stays_insufficient_without_events():
    candidate = build_candidate_decisions([_row(i) for i in range(100)])
    result = evaluate_candidate(
        candidate,
        {},
        operational_gates={
            "replay_parity": True,
            "freshness": True,
            "outage": True,
            "storage": True,
        },
    )
    assert result["evidence_status"] == "INSUFFICIENT_EVIDENCE"
    assert result["live_authority_granted"] is False


def _passing_candidate_evidence():
    spacing = 8 * 60 * MINUTE
    candidate_times = [index * spacing for index in range(60)]
    baseline_times = [
        index * spacing + 4 * 60 * MINUTE for index in range(60)
    ]
    decisions = lambda values: [
        {"event_time_us": value} for value in values
    ]
    candidate = {
        "decisions": {
            "stress_only_v1": decisions(baseline_times),
            "book_only_exhaustion_v1": decisions(baseline_times[::2]),
            "combined_balanced_v1": decisions(candidate_times),
            "combined_strict_v1": decisions(candidate_times[::2]),
        },
        "baselines": {
            "price_volatility_only": decisions(baseline_times),
        },
        "eligible_rows": 1000,
        "rows_seen": 3000,
        "frozen_thresholds": {},
    }
    outcomes = {}
    for index, event_time in enumerate(candidate_times):
        value = 0.006 + (index % 4) * 0.0002
        outcomes[event_time] = {
            "1h": {1.0: value, 2.0: value * 0.7},
            "4h": {1.0: value * 0.8, 2.0: value * 0.55},
        }
    for index, event_time in enumerate(baseline_times):
        value = -0.002 - (index % 3) * 0.0001
        outcomes[event_time] = {
            "1h": {1.0: value, 2.0: value - 0.002},
            "4h": {1.0: value, 2.0: value - 0.002},
        }
    path = {
        event_time: {
            1.0: 0.009 + (index % 4) * 0.0002,
            2.0: 0.006 + (index % 4) * 0.0001,
            "reason": "TAKE_PROFIT",
        }
        for index, event_time in enumerate(candidate_times)
    }
    operations = {
        "replay_parity": True,
        "freshness": True,
        "outage": True,
        "storage": True,
    }
    return candidate, outcomes, path, operations


def test_candidate_cannot_pass_without_path_dependent_exit_evidence():
    candidate, outcomes, _path, operations = _passing_candidate_evidence()
    result = evaluate_candidate(
        candidate,
        outcomes,
        operational_gates=operations,
    )
    assert result["evidence_status"] == "INSUFFICIENT_EVIDENCE"
    assert result["exit_contract"]["path_dependent_evidence_complete"] is False


def test_candidate_can_pass_only_with_matching_path_exit_and_horizon_evidence():
    candidate, outcomes, path, operations = _passing_candidate_evidence()
    result = evaluate_candidate(
        candidate,
        outcomes,
        path_outcomes=path,
        operational_gates=operations,
    )
    assert result["evidence_status"] == "EVIDENCE_PASSED"
    assert result["path_dependent_exit"]["winner"] == (
        result["horizons"]["1h"]["winner"]
    )
    assert result["horizons"]["1h"]["winner"] == (
        result["horizons"]["4h"]["winner"]
    )
    assert result["exit_contract"]["path_dependent_evidence_complete"] is True
    assert result["live_authority_granted"] is False
