from bot.db import db
from bot.learning import record_trade_outcome


def test_trade_outcome_is_idempotent_and_updates_review(test_db):
    kwargs = {
        "symbol": "BTC-USD",
        "strategy_id": "strategy_x",
        "strategy_version": "1",
        "entry_order_id": "entry_1",
        "exit_order_id": "exit_1",
        "entry_ts": 100,
        "exit_ts": 200,
        "quantity": 0.1,
        "avg_entry": 100.0,
        "avg_exit": 110.0,
        "entry_fee": 0.05,
        "exit_fee": 0.05,
        "exit_reason": "TAKE_PROFIT",
        "position_closed": True,
    }
    first = record_trade_outcome(**kwargs)
    second = record_trade_outcome(**kwargs)

    assert first == second
    outcomes = db.fetch_all("SELECT * FROM trade_outcomes")
    assert len(outcomes) == 1
    assert outcomes[0]["gross_pnl"] == 1.0
    assert round(outcomes[0]["net_pnl"], 8) == 0.9

    reviews = db.fetch_all("SELECT * FROM learning_reviews")
    assert len(reviews) == 1
    assert reviews[0]["sample_count"] == 1
    assert reviews[0]["status"] == "COLLECTING"


def test_learning_review_never_grants_live_authority(test_db):
    result = record_trade_outcome(
        symbol="BTC-USD",
        strategy_id="strategy_x",
        strategy_version="1",
        entry_order_id="entry_2",
        exit_order_id="exit_2",
        entry_ts=100,
        exit_ts=200,
        quantity=0.1,
        avg_entry=100.0,
        avg_exit=101.0,
        entry_fee=0.0,
        exit_fee=0.0,
        exit_reason="TIME_STOP",
        position_closed=True,
    )
    assert "live_authority_granted" not in result
