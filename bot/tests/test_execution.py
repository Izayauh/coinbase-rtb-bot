import pytest
from bot.models import Signal, Order
from bot.db import db
from bot.journal import Journal
from bot.execution import ExecutionService


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
