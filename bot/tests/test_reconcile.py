import time
import pytest
from bot.db import db
from bot.journal import Journal
from bot.execution import ExecutionService
from bot.models import Signal, Order


def process_consumer_tick(exec_service, adapter=None):
    """Simulates a single iteration of the signal consumer and reconciliation loop."""
    new_signals = Journal.get_new_signals()
    for s_data in new_signals:
        signal = Signal(**s_data)
        order = exec_service.process_signal(signal)

        if order:
            status_mappings = {
                "PENDING": "ORDER_PENDING",
                "REJECTED_POSITION_OPEN": "REJECTED_POSITION_OPEN",
                "REJECTED_INVALID_DATA": "REJECTED_INVALID_DATA",
                "REJECTED_INVALID_SIZE": "REJECTED_INVALID_SIZE",
            }
            new_status = status_mappings.get(order.status, "PROCESSED")
            Journal.update_signal_status(signal.signal_id, new_status)

    exec_service.reconcile_pending_orders(timeout=30, adapter=adapter)


def insert_signal(signal_id, status="NEW"):
    db.execute(
        "INSERT INTO signals (signal_id, symbol, signal_type, regime_snapshot, "
        "breakout_level, retest_level, atr, rsi, status, execution_price) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (signal_id, "BTC-USD", "BREAKOUT", "{}", 50000.0, 49000.0, 1000.0, 60.0, status, 49500.0),
    )


def test_new_signal_transitions_to_order_pending(test_db):
    exec_service = ExecutionService(portfolio_value=10000.0)
    insert_signal("sig1")

    process_consumer_tick(exec_service)

    signals = db.fetch_all("SELECT * FROM signals")
    assert len(signals) == 1
    assert signals[0]["status"] == "ORDER_PENDING"

    orders = db.fetch_all("SELECT * FROM orders")
    assert len(orders) == 1
    assert orders[0]["status"] == "PENDING"
    assert orders[0]["signal_id"] == "sig1"


def test_repeated_consumer_loop_no_duplicate(test_db):
    exec_service = ExecutionService()
    insert_signal("sig2")

    process_consumer_tick(exec_service)
    process_consumer_tick(exec_service)

    signals = db.fetch_all("SELECT * FROM signals")
    assert signals[0]["status"] == "ORDER_PENDING"
    orders = db.fetch_all("SELECT * FROM orders")
    assert len(orders) == 1


def test_fill_application_updates_order_and_position(test_db):
    exec_service = ExecutionService()
    insert_signal("sig3")

    process_consumer_tick(exec_service)

    pending = Journal.get_pending_orders()
    assert len(pending) == 1
    order = Order(**pending[0])

    sig_raw = db.fetch_all("SELECT * FROM signals WHERE signal_id='sig3'")[0]
    signal = Signal(**sig_raw)

    exec_service.handle_fill(order, signal, fill_price=49500.0, fill_size=order.size)

    filled_order = db.fetch_all("SELECT * FROM orders WHERE order_id=?", (order.order_id,))[0]
    assert filled_order["status"] == "FILLED"
    assert filled_order["executed_size"] >= order.size

    pos = Journal.get_open_position("BTC-USD")
    assert pos["state"] == "OPEN"
    assert pos["stop_active"] == 1
    assert pos["stop_price"] == signal.retest_level - signal.atr

    updated_sig = db.fetch_all("SELECT * FROM signals WHERE signal_id='sig3'")[0]
    assert updated_sig["status"] == "ORDER_FILLED"


def test_stale_pending_order_timeout(test_db):
    exec_service = ExecutionService()
    insert_signal("sig_stale", status="ORDER_PENDING")

    db.execute(
        "INSERT INTO orders (order_id, signal_id, symbol, side, price, size, "
        "executed_size, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("ord_stale", "sig_stale", "BTC-USD", "BUY", 50000.0, 1.0, 0.0, "PENDING",
         int(time.time()) - 100),
    )

    process_consumer_tick(exec_service)

    orders = db.fetch_all("SELECT * FROM orders WHERE order_id='ord_stale'")
    assert orders[0]["status"] == "EXPIRED"
    assert orders[0]["fail_reason"] == "TIMEOUT"

    signals = db.fetch_all("SELECT * FROM signals WHERE signal_id='sig_stale'")
    assert signals[0]["status"] == "FAILED_TIMEOUT"


class MockSubmitAdapter:
    def submit_order_intent(self, order):
        return {
            "exchange_order_id": f"cb_{order.order_id}",
            "submitted_at": 1000,
            "status": "OPEN",
        }


def test_exchange_metadata_persists_on_submit(test_db):
    exec_service = ExecutionService()
    adapter = MockSubmitAdapter()
    insert_signal("sig_mock")

    process_consumer_tick(exec_service, adapter=adapter)

    orders = db.fetch_all("SELECT * FROM orders WHERE signal_id='sig_mock'")
    assert orders[0]["exchange_order_id"] == f"cb_{orders[0]['order_id']}"
    assert orders[0]["submitted_at"] == 1000


def test_restart_resumes_state_cleanly(test_db):
    exec_service = ExecutionService()
    insert_signal("sig4", status="ORDER_PENDING")

    db.execute(
        "INSERT INTO orders (order_id, signal_id, symbol, side, price, size, "
        "executed_size, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("ord4", "sig4", "BTC-USD", "BUY", 49500.0, 1.0, 0.0, "PENDING", int(time.time())),
    )

    process_consumer_tick(exec_service)
    orders = db.fetch_all("SELECT * FROM orders WHERE order_id='ord4'")
    assert orders[0]["status"] == "PENDING"
