import unittest
import os
import sqlite3
import time
from models import Signal, Order
from db import Database, db
from journal import Journal
from execution import ExecutionService

class TestExecution(unittest.TestCase):
    def setUp(self):
        import uuid
        self.test_db_path = f"test_execution_{uuid.uuid4().hex}.db"
        if os.path.exists(self.test_db_path):
            os.remove(self.test_db_path)
        db.db_path = self.test_db_path
        db._init_db()

        self.service = ExecutionService(portfolio_value=10000.0)
        self.signal = Signal(
            signal_id="sig_test_123",
            symbol="BTC-USD",
            signal_type="LONG",
            regime_snapshot="ATR:0.01_LEVEL:50000",
            breakout_level=50000.0,
            retest_level=49000.0,
            atr=500.0,
            rsi=60.0,
            status="NEW"
        )
        # Setup signals table mock
        query = """
            INSERT INTO signals (signal_id, symbol, signal_type, regime_snapshot, breakout_level, retest_level, atr, rsi, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        db.execute(query, (
            self.signal.signal_id, self.signal.symbol, self.signal.signal_type, 
            self.signal.regime_snapshot, self.signal.breakout_level, 
            self.signal.retest_level, self.signal.atr, self.signal.rsi, self.signal.status
        ))

    def tearDown(self):
        try:
            if os.path.exists(self.test_db_path):
                os.remove(self.test_db_path)
        except OSError:
            pass

    def test_exactly_one_execution_attempt_per_signal_id(self):
        order1 = self.service.process_signal(self.signal)
        self.assertIsNotNone(order1)
        self.assertEqual(order1.status, "PENDING")

        # Second attempt should return the same existing order and not create a new one
        order2 = self.service.process_signal(self.signal)
        self.assertEqual(order1.order_id, order2.order_id)
        
        # Check DB directly to ensure only one order exists
        orders = db.fetch_all("SELECT * FROM orders")
        self.assertEqual(len(orders), 1)

    def test_repeated_processing_does_not_duplicate_order_intent(self):
        # This explicitly tests idempotency across identical calls
        for _ in range(5):
            self.service.process_signal(self.signal)
            
        orders = db.fetch_all("SELECT * FROM orders")
        self.assertEqual(len(orders), 1)

    def test_one_position_rule_enforced(self):
        # Process first signal successfully
        order1 = self.service.process_signal(self.signal)
        
        # Simulate an execution to open a position
        self.service.handle_fill(order1, self.signal, fill_price=50000.0, fill_size=order1.size)
        
        # Now create a new signal on the same symbol
        signal2 = Signal(
            signal_id="sig_test_456",
            symbol="BTC-USD",
            signal_type="LONG",
            regime_snapshot="",
            breakout_level=51000.0,
            retest_level=50000.0,
            atr=500.0,
            rsi=65.0,
            status="NEW"
        )
        
        order2 = self.service.process_signal(signal2)
        self.assertIsNotNone(order2)
        # Should be rejected because a position is already open
        self.assertEqual(order2.status, "REJECTED_POSITION_OPEN")

    def test_failed_order_attempt_persists_safely(self):
        # Process signal resulting in a PENDING order
        order = self.service.process_signal(self.signal)
        
        # Mark it failed
        self.service.mark_order_failed(order)
        
        saved_order = Journal.get_order_for_signal(self.signal.signal_id)
        self.assertEqual(saved_order["status"], "FAILED")
        
        # Try processing again, it should return the FAILED order, not re-attempt
        order2 = self.service.process_signal(self.signal)
        self.assertEqual(order2.status, "FAILED")

    def test_restart_resumes_without_duplicate_execution(self):
        # Process once
        order1 = self.service.process_signal(self.signal)
        
        # Simulate restart by creating a new ExecutionService instance
        service_restarted = ExecutionService(portfolio_value=10000.0)
        
        # Process same signal on the new instance
        order2 = service_restarted.process_signal(self.signal)
        
        # Should pull the exact same order_id instead of generating a new pending order
        self.assertEqual(order1.order_id, order2.order_id)
        
        orders = db.fetch_all("SELECT * FROM orders")
        self.assertEqual(len(orders), 1)

if __name__ == '__main__':
    unittest.main()
