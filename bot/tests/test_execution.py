import pytest
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from execution import ExecutionEngine
from journal import Journal
from db import db

@pytest.fixture
def temp_db(monkeypatch):
    test_path = "test_ex_journal.db"
    from db import Database
    import db as global_db
    import journal
    import execution
    test_db = Database(test_path)
    
    # Truncate tables cleanly since we don't drop the file immediately due to windows locks
    test_db.execute("DELETE FROM orders;")
    test_db.execute("DELETE FROM positions;")
    test_db.execute("DELETE FROM signals;")
    
    monkeypatch.setattr(global_db, 'db', test_db)
    monkeypatch.setattr(journal, 'db', test_db)
    monkeypatch.setattr(execution, 'db', test_db)
    yield test_db

def insert_mock_signal(temp_db, signal_id="sig-1", symbol="BTC-USD", status="NEW"):
    query = """
        INSERT INTO signals (signal_id, symbol, signal_type, regime_snapshot, breakout_level, retest_level, atr, rsi, status)
        VALUES (?, ?, 'LONG', 'TEST', 10000, 9500, 100, 60, ?)
    """
    temp_db.execute(query, (signal_id, symbol, status))

def test_execution_single_signal_creates_one_order_and_position(temp_db):
    insert_mock_signal(temp_db)
    engine = ExecutionEngine(portfolio_value=10000.0)
    engine.process_pending_signals(latest_price=10100.0)
    
    # Assert Order exists correctly
    orders = temp_db.fetch_all("SELECT * FROM orders")
    assert len(orders) == 1
    assert orders[0]["signal_id"] == "sig-1"
    
    # Assert Position created natively
    pos = temp_db.fetch_all("SELECT * FROM positions")
    assert len(pos) == 1
    assert pos[0]["symbol"] == "BTC-USD"
    
    # Stop is exactly computed natively from the DB insert constraints (9500 - 0.5 * 100 = 9450)
    assert pos[0]["stop_price"] == 9450.0
    
    # Assert signal safely neutralized functionally
    sig = temp_db.fetch_all("SELECT * FROM signals WHERE signal_id='sig-1'")
    assert sig[0]["status"] == "EXECUTED"

def test_idempotency_prevents_duplicate_orders(temp_db):
    insert_mock_signal(temp_db)
    engine = ExecutionEngine()
    engine.process_pending_signals(10100.0)
    
    # Re-call loops safely identical
    engine.process_pending_signals(10100.0)
    
    orders = temp_db.fetch_all("SELECT * FROM orders")
    pos = temp_db.fetch_all("SELECT * FROM positions")
    
    # Stays precisely exactly 1 intent and 1 position safely
    assert len(orders) == 1
    assert len(pos) == 1

def test_one_position_rule(temp_db):
    # Already actively open position locally injected
    temp_db.execute(
        "INSERT INTO positions (symbol, entry_ts, avg_entry, current_size, realized_pnl, unrealized_pnl, stop_price, state) VALUES (?,?,?,?,?,?,?,?)",
        ("BTC-USD", 100, 10000, 1, 0, 0, 9000, "OPEN")
    )
    insert_mock_signal(temp_db)
    engine = ExecutionEngine()
    engine.process_pending_signals(10100.0)
    
    orders = temp_db.fetch_all("SELECT * FROM orders")
    assert len(orders) == 0
    
    # Rejection status properly documented safely
    sig = temp_db.fetch_all("SELECT status FROM signals WHERE signal_id='sig-1'")
    assert sig[0]["status"] == "REJECTED_ALREADY_OPEN"
    
def test_failed_order_attempt_persists_safely(temp_db):
    insert_mock_signal(temp_db)
    
    # Trigger risk rejection natively
    engine = ExecutionEngine(portfolio_value=0.0) 
    engine.process_pending_signals(10100.0)
    
    orders = temp_db.fetch_all("SELECT * FROM orders")
    assert len(orders) == 0
    
    sig = temp_db.fetch_all("SELECT status FROM signals WHERE signal_id='sig-1'")
    assert sig[0]["status"] == "REJECTED_RISK"
