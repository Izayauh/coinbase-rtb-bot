"""
Tests for main.py event-emission helpers.

Covers:
  - _check_fills_and_positions: STOP_REQUIRED false positive (Issue 3)
  - _collect_reconcile_events: ORDER_SUBMITTED emission (Issue 2)
"""
import json
import time
import pytest

from bot.db import db
from bot.journal import Journal
from bot.execution import ExecutionService
from bot.safeguards import Safeguards
from bot.adapters import PaperAdapter


# ---------------------------------------------------------------------------
# Helpers shared with main.py helpers under test
# ---------------------------------------------------------------------------

def _import_helpers():
    """Import the helper functions directly from main module."""
    import importlib, sys
    # main.py is at repo root, not in a package — import by file path
    import importlib.util, os
    spec = importlib.util.spec_from_file_location(
        "main",
        os.path.join(os.path.dirname(__file__), "..", "..", "main.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _insert_signal(signal_id="sig_evt", symbol="BTC-USD", execution_price=49500.0):
    db.execute(
        "INSERT INTO signals (signal_id, symbol, signal_type, regime_snapshot, "
        "breakout_level, retest_level, atr, rsi, status, execution_price) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (signal_id, symbol, "LONG", "{}", 50000.0, 49000.0, 1000.0, 62.0, "NEW", execution_price),
    )


def _insert_position(symbol="BTC-USD", stop_active=1, stop_price=48000.0):
    db.execute(
        "INSERT INTO positions (symbol, state, avg_entry, current_size, "
        "stop_active, stop_price) VALUES (?, ?, ?, ?, ?, ?)",
        (symbol, "OPEN", 49500.0, 0.2, stop_active, stop_price),
    )


def _make_safeguards(trading_enabled=True):
    return Safeguards(
        trading_enabled=trading_enabled,
        ws_stale_timeout_sec=15,
        max_daily_loss_fraction=0.015,
        portfolio_value=10000.0,
    )


# ---------------------------------------------------------------------------
# Issue 3: STOP_REQUIRED false positive
# ---------------------------------------------------------------------------

class TestStopRequiredEvent:

    def test_stop_required_logged_when_invariant_fails(self, test_db):
        """STOP_REQUIRED must be logged when stop_active=0 on open position."""
        main = _import_helpers()
        exec_service = ExecutionService(portfolio_value=10000.0)
        sg = _make_safeguards()
        _insert_position(stop_active=0, stop_price=0.0)

        main._check_fills_and_positions(exec_service, sg, "BTC-USD")

        events = db.fetch_all("SELECT * FROM event_log WHERE event_type='STOP_REQUIRED'")
        assert len(events) == 1

    def test_stop_required_not_logged_when_stop_is_valid(self, test_db):
        """STOP_REQUIRED must not be logged when position has a valid stop."""
        main = _import_helpers()
        exec_service = ExecutionService(portfolio_value=10000.0)
        sg = _make_safeguards()
        _insert_position(stop_active=1, stop_price=48000.0)

        main._check_fills_and_positions(exec_service, sg, "BTC-USD")

        events = db.fetch_all("SELECT * FROM event_log WHERE event_type='STOP_REQUIRED'")
        assert len(events) == 0

    def test_stop_required_not_logged_when_other_guard_disabled_trading(self, test_db):
        """
        False-positive guard: if trading is disabled for an unrelated reason
        (e.g. stale_stream) but the stop invariant is valid, STOP_REQUIRED
        must NOT be logged.
        """
        main = _import_helpers()
        exec_service = ExecutionService(portfolio_value=10000.0)
        sg = _make_safeguards()
        # Disable via an unrelated guard
        sg.disable("stale_stream")
        assert sg.trading_enabled is False

        # Position has a valid stop
        _insert_position(stop_active=1, stop_price=48000.0)

        main._check_fills_and_positions(exec_service, sg, "BTC-USD")

        events = db.fetch_all("SELECT * FROM event_log WHERE event_type='STOP_REQUIRED'")
        assert len(events) == 0

    def test_stop_required_not_logged_when_no_position(self, test_db):
        """STOP_REQUIRED must not fire when there is no open position."""
        main = _import_helpers()
        exec_service = ExecutionService(portfolio_value=10000.0)
        sg = _make_safeguards()

        main._check_fills_and_positions(exec_service, sg, "BTC-USD")

        events = db.fetch_all("SELECT * FROM event_log WHERE event_type='STOP_REQUIRED'")
        assert len(events) == 0


# ---------------------------------------------------------------------------
# Issue 2: ORDER_SUBMITTED emission
# ---------------------------------------------------------------------------

class TestOrderSubmittedEvent:

    def _tick(self, exec_service, adapter, safeguards):
        """Single consumer iteration matching main.py signal_consumer_task.

        Snapshot is taken AFTER _process_new_signals so newly created orders
        are visible with exchange_order_id=None before reconcile submits them.
        """
        main = _import_helpers()
        main._process_new_signals(exec_service, safeguards)

        pending_rows = db.fetch_all(
            "SELECT order_id, status, exchange_order_id FROM orders "
            "WHERE status IN ('PENDING','PARTIAL')"
        )
        before_orders = {
            r["order_id"]: {"status": r["status"], "exchange_order_id": r["exchange_order_id"]}
            for r in pending_rows
        }

        exec_service.reconcile_pending_orders(timeout=300, adapter=adapter)
        main._collect_reconcile_events(before_orders, "BTC-USD")

    def test_order_submitted_logged_exactly_once(self, test_db):
        """
        ORDER_SUBMITTED must be emitted exactly once per order — on the tick
        the exchange_order_id first appears — not on subsequent pending ticks.
        """
        exec_service = ExecutionService(portfolio_value=10000.0)
        adapter = PaperAdapter()
        sg = _make_safeguards()
        _insert_signal("sig_sub_once")

        # Tick 1: signal → PENDING + submitted
        self._tick(exec_service, adapter, sg)
        # Tick 2: fill → FILLED
        self._tick(exec_service, adapter, sg)
        # Tick 3: extra tick (order already FILLED, no more pending)
        self._tick(exec_service, adapter, sg)

        events = db.fetch_all("SELECT * FROM event_log WHERE event_type='ORDER_SUBMITTED'")
        assert len(events) == 1

    def test_order_submitted_not_logged_before_submission(self, test_db):
        """No ORDER_SUBMITTED should appear before the order is submitted."""
        exec_service = ExecutionService(portfolio_value=10000.0)
        adapter = PaperAdapter()
        sg = _make_safeguards()

        # No signals inserted — no orders, no submissions
        self._tick(exec_service, adapter, sg)

        events = db.fetch_all("SELECT * FROM event_log WHERE event_type='ORDER_SUBMITTED'")
        assert len(events) == 0
