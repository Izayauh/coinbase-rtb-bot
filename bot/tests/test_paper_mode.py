"""
Paper-mode end-to-end tests.

Tests:
  1. Full lifecycle: aggregator -> state machine signal -> PENDING order ->
     PaperAdapter reconcile -> FILLED -> position OPEN with correct stop.
  2. Duplicate reconcile ticks are idempotent (no double fills/positions).
  3. Restart resumes: new StateMachine + ExecutionService pick up pending
     state and reconcile correctly without creating duplicates.
"""
import time
import pytest

from bot.db import db
from bot.journal import Journal
from bot.models import Bar, Signal, Order
from bot.execution import ExecutionService
from bot.adapters import PaperAdapter
from bot.aggregator import BarAggregator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _insert_signal(signal_id="sig_paper_1", symbol="BTC-USD", execution_price=49500.0):
    db.execute(
        "INSERT INTO signals (signal_id, symbol, signal_type, regime_snapshot, "
        "breakout_level, retest_level, atr, rsi, status, execution_price) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (signal_id, symbol, "LONG", "{}", 50000.0, 49000.0, 1000.0, 62.0, "NEW", execution_price),
    )


def _tick(exec_service, adapter, timeout=300):
    """One consumer tick: process NEW signals then reconcile."""
    new_sigs = Journal.get_new_signals()
    for s_data in new_sigs:
        signal = Signal(**s_data)
        order = exec_service.process_signal(signal)
        if order and order.status == "PENDING":
            Journal.update_signal_status(signal.signal_id, "ORDER_PENDING")
    exec_service.reconcile_pending_orders(timeout=timeout, adapter=adapter)


# ---------------------------------------------------------------------------
# Test 1: Full paper lifecycle — signal to position
# ---------------------------------------------------------------------------

def test_paper_e2e_signal_to_position(test_db):
    """
    PENDING order -> adapter submits (tick 1) -> adapter fills (tick 2) ->
    position OPEN with correct stop_price.
    """
    exec_service = ExecutionService(portfolio_value=10000.0)
    adapter = PaperAdapter()
    _insert_signal()

    # Tick 1: process NEW signal -> PENDING, then reconcile submits to paper adapter
    _tick(exec_service, adapter)

    orders = db.fetch_all("SELECT * FROM orders")
    assert len(orders) == 1
    assert orders[0]["status"] == "PENDING"
    assert orders[0]["exchange_order_id"] == f"cb_{orders[0]['order_id']}"

    signals = db.fetch_all("SELECT * FROM signals")
    assert signals[0]["status"] == "ORDER_PENDING"

    # Tick 2: reconcile fetches synthetic fill from PaperAdapter
    _tick(exec_service, adapter)

    orders = db.fetch_all("SELECT * FROM orders")
    assert orders[0]["status"] == "FILLED"
    assert orders[0]["executed_size"] > 0

    signals = db.fetch_all("SELECT * FROM signals")
    assert signals[0]["status"] == "ORDER_FILLED"

    pos = Journal.get_open_position("BTC-USD")
    assert pos["state"] == "OPEN"
    assert pos["stop_active"] == 1
    assert pos["stop_price"] > 0
    # stop_price = retest_level - atr = 49000 - 1000 = 48000
    assert pos["stop_price"] == pytest.approx(48000.0)


# ---------------------------------------------------------------------------
# Test 2: Duplicate reconcile ticks — idempotency
# ---------------------------------------------------------------------------

def test_duplicate_reconcile_ticks_are_idempotent(test_db):
    """Running the consumer tick multiple times after a fill must not
    create duplicate executions or change position size."""
    exec_service = ExecutionService(portfolio_value=10000.0)
    adapter = PaperAdapter()
    _insert_signal("sig_dup")

    _tick(exec_service, adapter)  # submit
    _tick(exec_service, adapter)  # fill

    # Position is now OPEN
    pos_after_fill = Journal.get_open_position("BTC-USD")
    assert pos_after_fill is not None
    size_after_fill = pos_after_fill["current_size"]

    executions_after_fill = db.fetch_all("SELECT * FROM executions")
    assert len(executions_after_fill) == 1

    # Run several more ticks — nothing should change
    for _ in range(3):
        _tick(exec_service, adapter)

    pos_final = Journal.get_open_position("BTC-USD")
    assert pos_final["current_size"] == pytest.approx(size_after_fill)

    executions_final = db.fetch_all("SELECT * FROM executions")
    assert len(executions_final) == 1


# ---------------------------------------------------------------------------
# Test 3: Restart resumes cleanly
# ---------------------------------------------------------------------------

def test_restart_resumes_paper_mode(test_db):
    """
    After a signal has been processed to PENDING + submitted, a new
    StateMachine + ExecutionService should resume reconciliation without
    creating duplicate orders.
    """
    exec_service = ExecutionService(portfolio_value=10000.0)
    adapter = PaperAdapter()
    _insert_signal("sig_restart")

    # First tick: signal -> PENDING order, adapter submission happens in reconcile
    _tick(exec_service, adapter)

    orders_before = db.fetch_all("SELECT * FROM orders")
    assert len(orders_before) == 1
    assert orders_before[0]["status"] == "PENDING"

    # --- Simulate restart: new instances load from DB ---
    exec_service2 = ExecutionService(portfolio_value=10000.0)

    # Tick after restart: should fill (not duplicate)
    _tick(exec_service2, adapter)

    orders_after = db.fetch_all("SELECT * FROM orders")
    assert len(orders_after) == 1
    assert orders_after[0]["status"] == "FILLED"

    pos = Journal.get_open_position("BTC-USD")
    assert pos is not None
    assert pos["state"] == "OPEN"


# ---------------------------------------------------------------------------
# Test 4: BarAggregator warms from DB correctly
# ---------------------------------------------------------------------------

def test_aggregator_warms_from_db(test_db):
    """Bars persisted in DB are loaded into deques on aggregator init."""
    symbol = "BTC-USD"
    # Insert some 1h bars and 4h bars
    for i in range(5):
        Journal.upsert_bar(Bar(symbol, "1h", i * 3600, 100.0, 105.0, 95.0, 102.0, 10.0))
    for i in range(5):
        Journal.upsert_bar(Bar(symbol, "4h", i * 14400, 100.0, 105.0, 95.0, 102.0, 40.0))

    agg = BarAggregator(symbol)
    assert len(agg.get_bars_1h()) == 5
    assert len(agg.get_bars_4h()) == 5
    assert not agg.ready()  # not enough bars yet


# ---------------------------------------------------------------------------
# Test 5: PaperAdapter deterministic fill
# ---------------------------------------------------------------------------

def test_paper_adapter_deterministic_fill(test_db):
    """
    PaperAdapter.sync_get_fills returns a fill with deterministic trade_id.
    Same call twice returns same trade_id so handle_fill rejects the second.
    """
    exec_service = ExecutionService(portfolio_value=10000.0)
    adapter = PaperAdapter()
    _insert_signal("sig_det")

    # Process to PENDING
    _tick(exec_service, adapter)
    orders = db.fetch_all("SELECT * FROM orders WHERE signal_id='sig_det'")
    order_id = orders[0]["order_id"]
    exch_id = f"cb_{order_id}"

    fills1 = adapter.sync_get_fills(exch_id)
    fills2 = adapter.sync_get_fills(exch_id)

    assert len(fills1) == 1
    assert fills1[0]["trade_id"] == f"paper_fill_{order_id}"
    assert fills1[0]["trade_id"] == fills2[0]["trade_id"]
    assert fills1[0]["commission"] == 0.0
