import main
from bot import config
from bot.state_machine import StateMachine


def test_archived_breakout_id_maps_only_to_breakout_engine(monkeypatch, test_db):
    monkeypatch.setattr(
        config,
        "strategy_id",
        lambda: "btc_breakout_retest_continuation",
    )
    engine = main._build_strategy_engine()
    assert isinstance(engine, StateMachine)


def test_derivatives_candidate_disables_breakout_engine(monkeypatch):
    monkeypatch.setattr(
        config,
        "strategy_id",
        lambda: "btc_derivatives_stress_exhaustion",
    )
    assert main._build_strategy_engine() is None
