import unittest
import os
import sqlite3
import time
from bot.models import Signal, Order
from bot.db import Database, db
from bot.journal import Journal
from bot.execution import ExecutionService

class TestExecution(unittest.TestCase):
    def setUp(self):
        import uuid
        self.test_db_path = f"test_execution_{uuid.uuid4().hex}.db"
        try:
            if os.path.exists(self.test_db_path):
                os.remove(self.test_db_path)
        except OSError:
            pass

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
            status="NEW",
            execution_price=50200.0
        )
        # Setup signals table mock
        query = """
            INSERT INTO signals (signal_id, symbol, signal_type, regime_snapshot, breakout_level, retest_level, atr, rsi, status, execution_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        db.execute(query, (
            self.signal.signal_id, self.signal.symbol, self.signal.signal_type,
            self.signal.regime_snapshot, self.signal.breakout_level,
            self.signal.retest_level, self.signal.atr, self.signal.rsi, self.signal.status, self.signal.execution_price
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

        orders = db.fetch_all("SELECT * FROM orders")
        self.assertEqual(len(orders), 1)

    def test_repeated_processing_does_not_duplicate_order_intent(self):
        for _ in range(5):
            self.service.process_signal(self.signal)

        orders = db.fetch_all("SELECT * FROM orders")
        self.assertEqual(len(orders), 1)

    def test_one_position_rule_enforced(self):
        # Process first signal successfully
        order1 = self.service.process_signal(self.signal)

        # Simulate an execution to open a position
        self.service.handle_fill(order1, self.signal, fill_price=50200.0, fill_size=order1.size)

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
            status="NEW",
            execution_price=51200.0
        )

        order2 = self.service.process_signal(signal2)
        self.assertIsNotNone(order2)
        # Should be rejected because an active exposure is already open
        self.assertEqual(order2.status, "REJECTED_POSITION_OPEN")

    def test_failed_order_attempt_persists_safely(self):
        order = self.service.process_signal(self.signal)
        self.service.mark_order_failed(order)

        saved_order = Journal.get_order_for_signal(self.signal.signal_id)
        self.assertEqual(saved_order["status"], "FAILED")

        order2 = self.service.process_signal(self.signal)
        self.assertEqual(order2.status, "FAILED")

    def test_restart_resumes_without_duplicate_execution(self):
        order1 = self.service.process_signal(self.signal)

        service_restarted = ExecutionService(portfolio_value=10000.0)
        order2 = service_restarted.process_signal(self.signal)

        self.assertEqual(order1.order_id, order2.order_id)
        orders = db.fetch_all("SELECT * FROM orders")
        self.assertEqual(len(orders), 1)

    def test_second_signal_rejected_while_pending(self):
        order1 = self.service.process_signal(self.signal)
        self.assertEqual(order1.status, "PENDING")

        signal2 = Signal(
            signal_id="sig_test_456",
            symbol="BTC-USD",
            signal_type="LONG",
            regime_snapshot="",
            breakout_level=51000.0,
            retest_level=50000.0,
            atr=500.0,
            rsi=65.0,
            status="NEW",
            execution_price=51200.0
        )
        order2 = self.service.process_signal(signal2)
        # Active order exists, so should reject new signal even without an open position
        self.assertEqual(order2.status, "REJECTED_POSITION_OPEN")

    def test_partial_fills_summing_to_filled(self):
        order = self.service.process_signal(self.signal)
        total_size = order.size
        half_size = total_size / 2.0

        # First partial fill
        self.service.handle_fill(order, self.signal, fill_price=50200.0, fill_size=half_size, execution_id="exec_1")
        self.assertEqual(order.status, "PARTIAL")
        self.assertEqual(order.executed_size, half_size)

        # Second partial fill
        self.service.handle_fill(order, self.signal, fill_price=50250.0, fill_size=half_size, execution_id="exec_2")
        self.assertEqual(order.status, "FILLED")
        self.assertEqual(order.executed_size, total_size)

    def test_ioc_anchor_price_uses_execution_price(self):
        order = self.service.process_signal(self.signal)
        # execution_price is 50200.0, MAX_SLIPPAGE is 0.005. Limit is 50200 * 1.005
        from bot.risk import RiskManager
        expected_limit = RiskManager.get_ioc_limit(50200.0)
        self.assertEqual(order.price, expected_limit)

    def test_duplicate_handle_fill_does_not_over_credit(self):
        order = self.service.process_signal(self.signal)
        # Handle the identical fill ID twice - should raise error inside insert_execution and return early
        self.service.handle_fill(order, self.signal, fill_price=50200.0, fill_size=order.size, execution_id="exec_duplicate")
        self.assertEqual(order.executed_size, order.size)
        self.assertEqual(order.status, "FILLED")

        # This one fails silently from exception, keeping executed_size same
        self.service.handle_fill(order, self.signal, fill_price=50200.0, fill_size=order.size, execution_id="exec_duplicate")
        self.assertEqual(order.executed_size, order.size)

    def test_overfill_rejected(self):
        order = self.service.process_signal(self.signal)
        # Try to fill more than requested
        self.service.handle_fill(order, self.signal, fill_price=50200.0, fill_size=order.size * 1.5, execution_id="exec_overfill")
        self.assertEqual(order.executed_size, 0.0)
        self.assertEqual(order.status, "PENDING")

if __name__ == '__main__':
    unittest.main()
