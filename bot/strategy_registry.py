"""Explicit mapping from configured strategy ids to live implementations."""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class StrategyImplementation:
    strategy_id: str
    strategy_version: str
    status: str
    signal_source: str
    exit_contract_id: str | None
    implementation_ready: bool
    reason: str


IMPLEMENTATIONS = {
    ("btc_breakout_retest_continuation", "1.0.0"): StrategyImplementation(
        "btc_breakout_retest_continuation",
        "1.0.0",
        "ARCHIVED",
        "bot.state_machine.StateMachine",
        None,
        False,
        "archived breakout strategy failed research and cannot be authorized",
    ),
    ("btc_derivatives_stress_exhaustion", "1.0.0"): StrategyImplementation(
        "btc_derivatives_stress_exhaustion",
        "1.0.0",
        "READY_FOR_EVIDENCE",
        "verified GCS advisory -> candidate_advisory_bridge.py",
        "derivatives_stress_exit_v1",
        True,
        "live adapter, expiry, explicit exits, and parity gates are implemented",
    ),
}


def get_implementation(
    strategy_id: str,
    strategy_version: str,
) -> StrategyImplementation | None:
    return IMPLEMENTATIONS.get((str(strategy_id), str(strategy_version)))


def implementation_evidence(
    strategy_id: str,
    strategy_version: str,
) -> dict:
    implementation = get_implementation(strategy_id, strategy_version)
    return asdict(implementation) if implementation else {
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "status": "MISSING",
        "implementation_ready": False,
        "reason": "no registered live implementation",
    }
