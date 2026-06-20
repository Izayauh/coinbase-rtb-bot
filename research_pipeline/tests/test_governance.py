"""Governance tests: purged WF, ESS, DSR, CSCV/PBO, and evidence gating."""

import math

from research_pipeline.governance import (
    purged_walk_forward_splits, effective_sample_size, baseline_no_trade,
    deflated_sharpe_ratio, cscv_pbo, promotion_gate,
)
from research_pipeline.features import register_specs_as_variants

SEC = 1_000_000


def test_purged_walk_forward_purges_and_embargoes():
    n, folds, horizon, emb = 100, 5, 5, 1.0
    splits = purged_walk_forward_splits(n, folds, horizon, emb)
    assert len(splits) == folds
    for train, test in splits:
        # test is contiguous and time-ordered
        assert test == list(range(test[0], test[-1] + 1))
        # no train index falls in the purge window [test_lo - horizon, test_hi + embargo)
        test_lo, test_hi = test[0], test[-1] + 1
        for i in train:
            assert not (test_lo - horizon <= i < test_hi + horizon)  # purge+embargo (emb=horizon here)
        # train and test are disjoint
        assert not (set(train) & set(test))


def test_effective_sample_size_nonoverlap():
    times = [i * 60 * SEC for i in range(10)]  # 10 obs, 60s apart, span 540s
    ess = effective_sample_size(times, horizon_us=300 * SEC)  # 5m horizon
    assert ess["n"] == 10
    # span 540s / 300s ~= 1.8 non-overlapping observations
    assert abs(ess["ess_nonoverlap"] - 1.8) < 1e-6


def test_no_trade_baseline_zero():
    assert baseline_no_trade([0.01, -0.02, 0.03])["expectancy"] == 0.0


def test_dsr_rewards_real_edge_and_penalizes_noise_trials():
    good = [0.012, 0.008, 0.011, 0.009, 0.013, 0.007] * 20
    bad = [-0.012, -0.008, -0.011, -0.009, -0.013, -0.007] * 20
    trials = [-0.2, -0.1, 0.0, 0.1, 0.2]
    good_dsr = deflated_sharpe_ratio(good, trial_sharpes=trials)
    bad_dsr = deflated_sharpe_ratio(bad, trial_sharpes=trials)
    assert good_dsr["z_score"] > 0
    assert good_dsr["probability"] > 0.95
    assert bad_dsr["z_score"] < 0
    assert bad_dsr["probability"] < 0.05


def test_dsr_equation_uses_selected_strategy_sharpe_in_non_normality_term():
    returns = [0.03, -0.01, 0.02, 0.004, -0.006, 0.05, 0.001, 0.009] * 8
    result = deflated_sharpe_ratio(
        returns,
        trial_sharpes=[-0.25, -0.05, 0.10, 0.30, 0.55],
    )
    observed = result["observed_sharpe"]
    expected = (
        (observed - result["expected_max_sharpe"])
        * math.sqrt(result["n"] - 1)
        / math.sqrt(
            1.0
            - result["skew"] * observed
            + ((result["kurtosis"] - 1.0) / 4.0) * observed ** 2
        )
    )
    assert math.isclose(result["z_score"], expected, rel_tol=1e-12, abs_tol=1e-12)


def test_cscv_pbo_low_for_stable_winner():
    stable = [
        [0.012, 0.008, 0.011, 0.009] * 8,
        [0.004, -0.001, 0.003, 0.0] * 8,
        [-0.003, 0.002, -0.002, 0.001] * 8,
    ]
    result = cscv_pbo(stable, n_slices=4)
    assert result["pbo"] == 0.0
    assert result["is_overfit"] is False
    assert result["n_combinations"] == 6


def test_cscv_pbo_high_for_slice_specific_winners():
    overfit = [
        [0.03] * 8 + [0.03] * 8 + [-0.03] * 8 + [-0.03] * 8,
        [-0.03] * 8 + [-0.03] * 8 + [0.03] * 8 + [0.03] * 8,
    ]
    result = cscv_pbo(overfit, n_slices=4)
    assert result["pbo"] >= 0.5
    assert result["is_overfit"] is True


def test_promotion_gate_blocks_when_evidence_is_missing(store):
    register_specs_as_variants(store)
    times = [i * 60 * SEC for i in range(200)]
    status = promotion_gate(store, "microstructure", net_returns=[0.001] * 200,
                            decision_times_us=times, horizon_us=300 * SEC, ess_floor=50)
    assert status.promotion == "BLOCKED"
    assert any("stress" in r for r in status.reasons)
    assert any("DSR" in r for r in status.reasons)
    assert any("PBO" in r for r in status.reasons)
    assert status.metrics["variants_registered"] == store.count_variants("microstructure")


def test_promotion_gate_can_pass_evidence_without_granting_order_authority(store):
    register_specs_as_variants(store)
    n = 120
    times = [i * 300 * SEC for i in range(n)]
    selected = [0.012, 0.008, 0.011, 0.009] * (n // 4)
    trials = [
        selected,
        [0.003, -0.001, 0.002, 0.0] * (n // 4),
        [-0.002, 0.001, -0.001, 0.0] * (n // 4),
    ]
    status = promotion_gate(
        store,
        "microstructure",
        net_returns=selected,
        stress_net_returns=[x * 0.5 for x in selected],
        decision_times_us=times,
        horizon_us=300 * SEC,
        ess_floor=50,
        baseline_expectancies={
            "no_trade": 0.0,
            "buy_and_hold": 0.001,
            "price_volatility_only": 0.0015,
            "archived_breakout": 0.002,
        },
        oos_fold_expectancies=[0.004, 0.005, 0.003, 0.006],
        trial_returns=trials,
        operational_gates={
            "replay_parity": True,
            "freshness": True,
            "outage": True,
            "storage": True,
        },
        cscv_slices=4,
    )
    assert status.promotion == "EVIDENCE_PASSED"
    assert status.reasons == []
    assert status.metrics["dsr"]["z_score"] > 0
    assert status.metrics["pbo"]["pbo"] < 0.40
