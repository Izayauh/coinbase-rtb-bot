from bot.strategy_registry import (
    get_implementation,
    implementation_evidence,
)


def test_archived_breakout_cannot_be_live_ready():
    implementation = get_implementation(
        "btc_breakout_retest_continuation",
        "1.0.0",
    )
    assert implementation is not None
    assert implementation.status == "ARCHIVED"
    assert implementation.implementation_ready is False


def test_derivatives_candidate_implementation_is_ready_but_still_needs_evidence():
    evidence = implementation_evidence(
        "btc_derivatives_stress_exhaustion",
        "1.0.0",
    )
    assert evidence["status"] == "READY_FOR_EVIDENCE"
    assert evidence["signal_source"].endswith("candidate_advisory_bridge.py")
    assert evidence["exit_contract_id"] == "derivatives_stress_exit_v1"
    assert evidence["implementation_ready"] is True
