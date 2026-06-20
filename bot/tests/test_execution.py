import time

import pytest
from bot.models import Signal, Order
from bot.db import db
from bot.journal import Journal
from bot.execution import ExecutionService
from bot.safeguards import Safeguards


def make_signal(signal_id="sig_test_123", symbol="BTC-USD", execution_price=50200.0):
    return Signal(
        signal_id=signal_id,
        symbol=symbol,
        signal_type="LONG",
        regime_snapshot="ATR:0.01_LEVEL:50000",
        breakout_level=50000.0,
        retest_level=49000.0,
        atr=500.0,
        rsi=60.0,
        status="NEW",
        execution_price=execution_price,
    )


def insert_signal(sig):
    db.execute(
        "INSERT INTO signals (signal_id, symbol, signal_type, regime_snapshot, "
        "breakout_level, retest_level, atr, rsi, status, execution_price) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            sig.signal_id, sig.symbol, sig.signal_type,
            sig.regime_snapshot, sig.breakout_level,
            sig.retest_level, sig.atr, sig.rsi, sig.status, sig.execution_price,
        ),
    )


def test_exactly_one_execution_attempt_per_signal_id(test_db):
    signal = make_signal()
    insert_signal(signal)
    service = ExecutionService(portfolio_value=10000.0)

    order1 = service.process_signal(signal)
    assert order1 is not None
    assert order1.status == "PENDING"

    order2 = service.process_signal(signal)
    assert order1.order_id == order2.order_id

    orders = db.fetch_all("SELECT * FROM orders")
    assert len(orders) == 1


def test_repeated_processing_does_not_duplicate_order_intent(test_db):
    signal = make_signal()
    insert_signal(signal)
    service = ExecutionService(portfolio_value=10000.0)

    for _ in range(5):
        service.process_signal(signal)

    orders = db.fetch_all("SELECT * FROM orders")
    assert len(orders) == 1


def test_one_position_rule_enforced(test_db):
    signal = make_signal()
    insert_signal(signal)
    service = ExecutionService(portfolio_value=10000.0)

    order1 = service.process_signal(signal)
    service.handle_fill(order1, signal, fill_price=50200.0, fill_size=order1.size)

    signal2 = make_signal(signal_id="sig_test_456", execution_price=51200.0)
    order2 = service.process_signal(signal2)
    assert order2 is not None
    assert order2.status == "REJECTED_POSITION_OPEN"


def test_failed_order_attempt_persists_safely(test_db):
    signal = make_signal()
    insert_signal(signal)
    service = ExecutionService(portfolio_value=10000.0)

    order = service.process_signal(signal)
    service.mark_order_failed(order)

    saved_order = Journal.get_order_for_signal(signal.signal_id)
    assert saved_order["status"] == "FAILED"

    order2 = service.process_signal(signal)
    assert order2.status == "FAILED"


def test_restart_resumes_without_duplicate_execution(test_db):
    signal = make_signal()
    insert_signal(signal)
    service = ExecutionService(portfolio_value=10000.0)

    order1 = service.process_signal(signal)

    service_restarted = ExecutionService(portfolio_value=10000.0)
    order2 = service_restarted.process_signal(signal)

    assert order1.order_id == order2.order_id
    orders = db.fetch_all("SELECT * FROM orders")
    assert len(orders) == 1


def test_second_signal_rejected_while_pending(test_db):
    signal = make_signal()
    insert_signal(signal)
    service = ExecutionService(portfolio_value=10000.0)

    order1 = service.process_signal(signal)
    assert order1.status == "PENDING"

    signal2 = make_signal(signal_id="sig_test_456", execution_price=51200.0)
    order2 = service.process_signal(signal2)
    assert order2.status == "REJECTED_POSITION_OPEN"


def test_partial_fills_summing_to_filled(test_db):
    signal = make_signal()
    insert_signal(signal)
    service = ExecutionService(portfolio_value=10000.0)

    order = service.process_signal(signal)
    total_size = order.size
    half_size = total_size / 2.0

    service.handle_fill(order, signal, fill_price=50200.0, fill_size=half_size, execution_id="exec_1")
    assert order.status == "PARTIAL"
    assert order.executed_size == half_size

    service.handle_fill(order, signal, fill_price=50250.0, fill_size=half_size, execution_id="exec_2")
    assert order.status == "FILLED"
    assert order.executed_size == total_size


def test_ioc_anchor_price_uses_execution_price(test_db):
    signal = make_signal()
    insert_signal(signal)
    service = ExecutionService(portfolio_value=10000.0)

    order = service.process_signal(signal)
    from bot.risk import RiskManager
    expected_limit = RiskManager.get_ioc_limit(50200.0)
    assert order.price == expected_limit


def test_duplicate_handle_fill_does_not_over_credit(test_db):
    signal = make_signal()
    insert_signal(signal)
    service = ExecutionService(portfolio_value=10000.0)

    order = service.process_signal(signal)
    service.handle_fill(order, signal, fill_price=50200.0, fill_size=order.size, execution_id="exec_duplicate")
    assert order.executed_size == order.size
    assert order.status == "FILLED"

    # Second call with same exec id fails silently, does not over-credit
    service.handle_fill(order, signal, fill_price=50200.0, fill_size=order.size, execution_id="exec_duplicate")
    assert order.executed_size == order.size


def test_overfill_rejected(test_db):
    signal = make_signal()
    insert_signal(signal)
    service = ExecutionService(portfolio_value=10000.0)

    order = service.process_signal(signal)
    service.handle_fill(order, signal, fill_price=50200.0, fill_size=order.size * 1.5, execution_id="exec_overfill")
    assert order.executed_size == 0.0
    assert order.status == "PENDING"


# ---------------------------------------------------------------------------
# Safety rails: order size cap
# ---------------------------------------------------------------------------

def test_order_size_cap_rejects_signal(test_db):
    """process_signal returns REJECTED_SIZE_CAP when order notional exceeds cap."""
    signal = make_signal()
    insert_signal(signal)

    # Cap of $1 — any real order will exceed this
    sg = Safeguards(
        trading_enabled=True,
        ws_stale_timeout_sec=15,
        max_daily_loss_fraction=0.015,
        portfolio_value=10000.0,
        max_order_size_usd=1.0,
        max_position_size_usd=1000.0,
    )
    service = ExecutionService(portfolio_value=10000.0, safeguards=sg)
    order = service.process_signal(signal)

    assert order is not None
    assert order.status == "REJECTED_SIZE_CAP"
    assert sg.trading_enabled is True  # rejection does not disable trading


def test_order_size_cap_passes_when_within_limit(test_db):
    """process_signal succeeds when notional is within cap."""
    signal = make_signal()
    insert_signal(signal)

    sg = Safeguards(
        trading_enabled=True,
        ws_stale_timeout_sec=15,
        max_daily_loss_fraction=0.015,
        portfolio_value=10000.0,
        max_order_size_usd=10000.0,
        max_position_size_usd=100000.0,
    )
    service = ExecutionService(portfolio_value=10000.0, safeguards=sg)
    order = service.process_signal(signal)

    assert order is not None
    assert order.status == "PENDING"


# ---------------------------------------------------------------------------
# Safety rails: position size cap
# ---------------------------------------------------------------------------

def test_position_size_cap_disables_trading_after_fill(test_db):
    """handle_fill disables trading via safeguards when position exceeds cap."""
    signal = make_signal()
    insert_signal(signal)

    # Cap of $1 — any real position will exceed this
    sg = Safeguards(
        trading_enabled=True,
        ws_stale_timeout_sec=15,
        max_daily_loss_fraction=0.015,
        portfolio_value=10000.0,
        max_order_size_usd=10000.0,
        max_position_size_usd=1.0,
    )
    service = ExecutionService(portfolio_value=10000.0, safeguards=sg)
    order = service.process_signal(signal)
    assert order.status == "PENDING"

    service.handle_fill(order, signal, fill_price=50200.0, fill_size=order.size)

    assert sg.trading_enabled is False
    assert "position_size_exceeded" in sg._tripped


def test_expired_candidate_signal_is_rejected(test_db, monkeypatch):
    from bot import config

    monkeypatch.setattr(
        config,
        "strategy_id",
        lambda: "btc_derivatives_stress_exhaustion",
    )
    monkeypatch.setattr(config, "strategy_version", lambda: "1.0.0")
    signal = Signal(
        signal_id="expired_candidate",
        symbol="BTC-USD",
        signal_type="LONG",
        regime_snapshot="{}",
        breakout_level=100.0,
        retest_level=100.0,
        atr=1.0,
        rsi=0.0,
        status="NEW",
        execution_price=100.0,
        strategy_id="btc_derivatives_stress_exhaustion",
        strategy_version="1.0.0",
        expires_at_us=int(time.time() * 1_000_000) - 1,
        stop_price=99.0,
        target_price=101.5,
        time_stop_seconds=14400,
    )
    Journal.insert_signal(signal)

    order = ExecutionService(live_test_notional_usd=10.0).process_signal(signal)

    assert order.status == "REJECTED_EXPIRED"


def test_candidate_fill_persists_explicit_exit_contract(
    test_db,
    monkeypatch,
):
    from bot import config

    monkeypatch.setattr(
        config,
        "strategy_id",
        lambda: "btc_derivatives_stress_exhaustion",
    )
    monkeypatch.setattr(config, "strategy_version", lambda: "1.0.0")
    signal = Signal(
        signal_id="candidate_exit_contract",
        symbol="BTC-USD",
        signal_type="LONG",
        regime_snapshot="{}",
        breakout_level=100.0,
        retest_level=100.0,
        atr=1.0,
        rsi=0.0,
        status="NEW",
        execution_price=100.0,
        strategy_id="btc_derivatives_stress_exhaustion",
        strategy_version="1.0.0",
        expires_at_us=int(time.time() * 1_000_000) + 60_000_000,
        stop_price=99.0,
        target_price=101.5,
        time_stop_seconds=14400,
        source_hash="advisory-hash",
    )
    Journal.insert_signal(signal)
    service = ExecutionService(live_test_notional_usd=10.0)
    order = service.process_signal(signal)

    service.handle_fill(
        order,
        signal,
        fill_price=100.0,
        fill_size=order.size,
    )

    position = Journal.get_open_position("BTC-USD")
    assert position["stop_price"] == 99.0
    assert position["target_price"] == 101.5
    assert position["time_stop_at"] >= int(time.time()) + 14395
    assert position["source_signal_hash"] == "advisory-hash"


def test_candidate_mode_rejects_untagged_legacy_signal(
    test_db,
    monkeypatch,
):
    from bot import config

    monkeypatch.setattr(
        config,
        "strategy_id",
        lambda: "btc_derivatives_stress_exhaustion",
    )
    monkeypatch.setattr(config, "strategy_version", lambda: "1.0.0")
    signal = make_signal(signal_id="legacy_under_candidate")
    insert_signal(signal)

    order = ExecutionService().process_signal(signal)

    assert order.status == "REJECTED_STRATEGY_MISMATCH"
