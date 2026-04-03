import os
import time
import pytest
from db import db
from journal import Journal
from execution import ExecutionService
from models import Signal, Order

def setup_module(module):
    db.db_path = "test_reconcile.db"
    if os.path.exists(db.db_path):
        os.remove(db.db_path)
    db._init_db()

def teardown_module(module):
    if os.path.exists("test_reconcile.db"):
        pass

@pytest.fixture(autouse=True)
def clean_tables():
    db.execute("DELETE FROM signals")
    db.execute("DELETE FROM orders")
    db.execute("DELETE FROM executions")
    db.execute("DELETE FROM positions")

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
                "REJECTED_INVALID_SIZE": "REJECTED_INVALID_SIZE"
            }
            new_status = status_mappings.get(order.status, "PROCESSED")
            Journal.update_signal_status(signal.signal_id, new_status)
    
    exec_service.reconcile_pending_orders(timeout=30, adapter=adapter)

def test_new_signal_transitions_to_order_pending():
    exec_service = ExecutionService(portfolio_value=10000.0)
    
    # 1. Insert NEW signal
    db.execute(
        "INSERT INTO signals (signal_id, symbol, signal_type, regime_snapshot, breakout_level, retest_level, atr, rsi, status, execution_price) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("sig1", "BTC-USD", "BREAKOUT", "{}", 50000.0, 49000.0, 1000.0, 60.0, "NEW", 49500.0)
    )
    
    # 2. Process
    process_consumer_tick(exec_service)
    
    # Signal should be ORDER_PENDING
    signals = db.fetch_all("SELECT * FROM signals")
    assert len(signals) == 1
    assert signals[0]['status'] == "ORDER_PENDING"
    
    # Order should be PENDING
    orders = db.fetch_all("SELECT * FROM orders")
    assert len(orders) == 1
    assert orders[0]['status'] == "PENDING"
    assert orders[0]['signal_id'] == "sig1"

def test_repeated_consumer_loop_no_duplicate():
    exec_service = ExecutionService()
    
    db.execute(
        "INSERT INTO signals (signal_id, symbol, signal_type, regime_snapshot, breakout_level, retest_level, atr, rsi, status, execution_price) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("sig2", "BTC-USD", "BREAKOUT", "{}", 50000.0, 49000.0, 1000.0, 60.0, "NEW", 49500.0)
    )
    process_consumer_tick(exec_service)
    process_consumer_tick(exec_service)
    
    signals = db.fetch_all("SELECT * FROM signals")
    assert signals[0]['status'] == "ORDER_PENDING"
    orders = db.fetch_all("SELECT * FROM orders")
    assert len(orders) == 1  # No duplicates

def test_fill_application_updates_order_and_position():
    exec_service = ExecutionService()
    
    db.execute(
        "INSERT INTO signals (signal_id, symbol, signal_type, regime_snapshot, breakout_level, retest_level, atr, rsi, status, execution_price) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("sig3", "BTC-USD", "BREAKOUT", "{}", 50000.0, 49000.0, 1000.0, 60.0, "NEW", 49500.0)
    )
    process_consumer_tick(exec_service)
    
    orders = Journal.get_pending_orders()
    assert len(orders) == 1
    order = Order(**orders[0])
    
    signals = Journal.get_new_signals() # Returns NEW signals
    # fetch the hydrated signal 
    sig_raw = db.fetch_all("SELECT * FROM signals WHERE signal_id='sig3'")[0]
    signal = Signal(**sig_raw)
    
    # Apply a full fill
    exec_service.handle_fill(order, signal, fill_price=49500.0, fill_size=order.size)
    
    # Assert Order FILLED
    filled_order = db.fetch_all("SELECT * FROM orders WHERE order_id=?", (order.order_id,))[0]
    assert filled_order['status'] == "FILLED"
    assert filled_order['executed_size'] >= order.size
    
    # Assert position created and stop_active is true/1
    pos = Journal.get_open_position("BTC-USD")
    assert pos['state'] == "OPEN"
    assert pos['stop_active'] == 1
    expected_stop = signal.retest_level - signal.atr
    assert pos['stop_price'] == expected_stop
    
    # Assert signal status updated to ORDER_FILLED
    signal_updated = db.fetch_all("SELECT * FROM signals WHERE signal_id='sig3'")[0]
    assert signal_updated['status'] == "ORDER_FILLED"

def test_stale_pending_order_timeout():
    exec_service = ExecutionService()
    
    # Needs a signal to map FAILED_TIMEOUT back
    db.execute(
        "INSERT INTO signals (signal_id, symbol, signal_type, regime_snapshot, breakout_level, retest_level, atr, rsi, status, execution_price) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("sig_stale", "BTC-USD", "BREAKOUT", "{}", 50000.0, 49000.0, 1000.0, 60.0, "ORDER_PENDING", 49500.0)
    )
    
    # Fake old pending order
    db.execute(
        "INSERT INTO orders (order_id, signal_id, symbol, side, price, size, executed_size, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("ord_stale", "sig_stale", "BTC-USD", "BUY", 50000.0, 1.0, 0.0, "PENDING", int(time.time()) - 100)
    )
    
    # Process tick - should expire it (timeout is 30 in our helper tick)
    process_consumer_tick(exec_service)
    
    orders = db.fetch_all("SELECT * FROM orders WHERE order_id='ord_stale'")
    assert orders[0]['status'] == "EXPIRED"
    assert orders[0]['fail_reason'] == "TIMEOUT"
    
    # Assert signal updated to FAILED_TIMEOUT
    signals = db.fetch_all("SELECT * FROM signals WHERE signal_id='sig_stale'")
    assert signals[0]['status'] == "FAILED_TIMEOUT"

class MockAdapter:
    def submit_order_intent(self, order):
        return {
            "exchange_order_id": f"cb_{order.order_id}",
            "submitted_at": 1000,
            "status": "OPEN"
        }

def test_exchange_metadata_persists_on_submit():
    exec_service = ExecutionService()
    adapter = MockAdapter()
    
    db.execute(
        "INSERT INTO signals (signal_id, symbol, signal_type, regime_snapshot, breakout_level, retest_level, atr, rsi, status, execution_price) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("sig_mock", "BTC-USD", "BREAKOUT", "{}", 50000.0, 49000.0, 1000.0, 60.0, "NEW", 49500.0)
    )
    process_consumer_tick(exec_service, adapter=adapter)
    
    orders = db.fetch_all("SELECT * FROM orders WHERE signal_id='sig_mock'")
    assert orders[0]['exchange_order_id'] == f"cb_{orders[0]['order_id']}"
    assert orders[0]['submitted_at'] == 1000

def test_restart_resumes_state_cleanly():
    exec_service = ExecutionService()
    db.execute(
        "INSERT INTO signals (signal_id, symbol, signal_type, regime_snapshot, breakout_level, retest_level, atr, rsi, status, execution_price) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("sig4", "BTC-USD", "BREAKOUT", "{}", 50000.0, 49000.0, 1000.0, 60.0, "ORDER_PENDING", 49500.0)
    )
    db.execute(
        "INSERT INTO orders (order_id, signal_id, symbol, side, price, size, executed_size, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("ord4", "sig4", "BTC-USD", "BUY", 49500.0, 1.0, 0.0, "PENDING", int(time.time()))
    )
    
    process_consumer_tick(exec_service)
    orders = db.fetch_all("SELECT * FROM orders WHERE order_id='ord4'")
    # Timeout hasn't happened. Status is still PENDING. Re-running loop doesn't break it
    assert orders[0]['status'] == "PENDING"
