"""
Safeguards unit tests.

Tests:
  1. trading_enabled=False blocks signal processing immediately.
  2. stale_stream guard fires when last_trade_ts is old, recovers when fresh.
  3. check_stop_invariant disables trading if position missing stop.
  4. Persisted disabled state survives restart (new Safeguards instance).
  5. Kill switch file blocks trading while present.
  6. check_order_size allows/rejects based on notional cap.
  7. check_position_size disables trading when position exceeds cap.
  8. Stale kill_switch guard auto-cleared on init when file no longer exists.
"""
import os
import time
import pytest

from bot.db import db
from bot.journal import Journal
from bot.safeguards import Safeguards


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeMDProcessor:
    def __init__(self, last_trade_ts: float = 0.0):
        self.last_trade_ts = last_trade_ts


def _make_safeguards(trading_enabled=True, ws_timeout=15, **kw):
    return Safeguards(
        trading_enabled=trading_enabled,
        ws_stale_timeout_sec=ws_timeout,
        max_daily_loss_fraction=0.015,
        portfolio_value=10000.0,
        **kw,
    )


# ---------------------------------------------------------------------------
# Test 1: trading_enabled=False blocks can_trade()
# ---------------------------------------------------------------------------

def test_trading_disabled_blocks_can_trade(test_db):
    """Safeguards constructed with trading_enabled=False always returns False."""
    sg = _make_safeguards(trading_enabled=False)
    assert sg.trading_enabled is False
    assert sg.can_trade() is False


# ---------------------------------------------------------------------------
# Test 2: stale stream fires and recovers
# ---------------------------------------------------------------------------

def test_stale_stream_disables_trading(test_db):
    """can_trade() returns False when last_trade_ts is older than ws_stale_timeout_sec."""
    sg = _make_safeguards(ws_timeout=10)
    md = _FakeMDProcessor(last_trade_ts=time.time() - 20)  # 20s old, threshold 10s
    sg.set_md_processor(md)

    assert sg.can_trade() is False
    assert sg.trading_enabled is False
    assert "stale_stream" in sg._tripped


def test_stale_stream_no_fire_when_ts_is_zero(test_db):
    """Stream that hasn't started (last_trade_ts=0) does not trip the guard."""
    sg = _make_safeguards(ws_timeout=10)
    md = _FakeMDProcessor(last_trade_ts=0.0)
    sg.set_md_processor(md)

    assert sg.can_trade() is True


def test_stale_stream_recovers_when_fresh(test_db):
    """Guard re-enables trading when the sole tripped guard (stale_stream) recovers."""
    sg = _make_safeguards(ws_timeout=10)
    # First, trip the guard with a stale timestamp
    md = _FakeMDProcessor(last_trade_ts=time.time() - 20)
    sg.set_md_processor(md)
    sg.can_trade()
    assert sg.trading_enabled is False

    # Update to a fresh timestamp — recovery should happen on next can_trade()
    md.last_trade_ts = time.time()
    result = sg.can_trade()
    assert result is True
    assert sg.trading_enabled is True


# ---------------------------------------------------------------------------
# Test 3: check_stop_invariant
# ---------------------------------------------------------------------------

def test_stop_invariant_passes_when_no_position(test_db):
    """check_stop_invariant returns True when there is no open position."""
    sg = _make_safeguards()
    assert sg.check_stop_invariant("BTC-USD") is True
    assert sg.trading_enabled is True


def test_stop_invariant_fails_missing_stop(test_db):
    """check_stop_invariant disables trading when stop_active=0 or stop_price=0."""
    # Manually insert a position with stop_active=0
    db.execute(
        "INSERT INTO positions (symbol, state, avg_entry, current_size, "
        "stop_active, stop_price) VALUES (?, ?, ?, ?, ?, ?)",
        ("BTC-USD", "OPEN", 49500.0, 0.2, 0, 0.0),
    )

    sg = _make_safeguards()
    ok = sg.check_stop_invariant("BTC-USD")
    assert ok is False
    assert sg.trading_enabled is False
    assert "stop_required" in sg._tripped


def test_stop_invariant_passes_with_valid_stop(test_db):
    """check_stop_invariant returns True when position has stop_active=1 and stop_price>0."""
    db.execute(
        "INSERT INTO positions (symbol, state, avg_entry, current_size, "
        "stop_active, stop_price) VALUES (?, ?, ?, ?, ?, ?)",
        ("BTC-USD", "OPEN", 49500.0, 0.2, 1, 48000.0),
    )

    sg = _make_safeguards()
    assert sg.check_stop_invariant("BTC-USD") is True
    assert sg.trading_enabled is True


# ---------------------------------------------------------------------------
# Test 4: Persisted disabled state survives restart
# ---------------------------------------------------------------------------

def test_disabled_state_persists_across_restart(test_db):
    """
    A disabled Safeguards instance persists its state.
    A new instance created from the same DB loads that persisted state.
    """
    sg1 = _make_safeguards(trading_enabled=True)
    sg1.disable("test_guard")
    assert sg1.trading_enabled is False

    # New instance — simulates restart
    sg2 = _make_safeguards(trading_enabled=True)
    assert sg2.trading_enabled is False


def test_tripped_set_persists_across_restart(test_db):
    """_tripped must be restored from DB so guard metadata survives restart."""
    sg1 = _make_safeguards()
    sg1.disable("stop_required")
    assert "stop_required" in sg1._tripped

    sg2 = _make_safeguards()
    assert "stop_required" in sg2._tripped


def test_stale_stream_recovery_blocked_when_stop_required_tripped(test_db):
    """
    Safety-critical: stale_stream recovery must NOT re-enable trading when
    stop_required was also tripped in a previous session.

    Sequence:
      Session 1 — stop_required trips.
      Restart    — new instance loads _tripped from DB.
      Session 2  — stale_stream trips, then recovers.
      Expected   — trading remains disabled (stop_required is non-recoverable).
    """
    # Session 1: trip stop_required
    sg1 = _make_safeguards()
    sg1.disable("stop_required")
    assert sg1.trading_enabled is False

    # Restart: new instance should restore _tripped
    sg2 = _make_safeguards()
    assert "stop_required" in sg2._tripped
    assert sg2.trading_enabled is False

    # Session 2: stale_stream trips
    md = _FakeMDProcessor(last_trade_ts=time.time() - 20)
    sg2.set_md_processor(md)
    sg2.can_trade()
    assert "stale_stream" in sg2._tripped

    # stale_stream recovers — trading must stay disabled because stop_required is present
    md.last_trade_ts = time.time()
    result = sg2.can_trade()
    assert result is False
    assert sg2.trading_enabled is False


# ---------------------------------------------------------------------------
# Test 5: Kill switch
# ---------------------------------------------------------------------------

def test_kill_switch_blocks_trading(test_db, tmp_path):
    """can_trade() returns False when the kill switch file exists."""
    ks_file = str(tmp_path / "KILL_SWITCH")
    sg = _make_safeguards(kill_switch_file=ks_file)
    assert sg.can_trade() is True  # no file yet

    # Create the kill switch file
    open(ks_file, "w").close()
    assert sg.can_trade() is False
    assert sg.trading_enabled is False


def test_kill_switch_absent_allows_trading(test_db, tmp_path):
    """can_trade() returns True when the kill switch file does not exist."""
    ks_file = str(tmp_path / "KILL_SWITCH_ABSENT")
    sg = _make_safeguards(kill_switch_file=ks_file)
    assert sg.can_trade() is True


# ---------------------------------------------------------------------------
# Test 6: Order size cap
# ---------------------------------------------------------------------------

def test_check_order_size_allows_within_cap(test_db):
    """check_order_size returns True when notional is within cap."""
    sg = _make_safeguards(max_order_size_usd=10000.0)
    # 0.1 BTC @ 50000 = 5000 USD — within cap
    assert sg.check_order_size(0.1, 50000.0) is True
    assert sg.trading_enabled is True  # does not disable


def test_check_order_size_rejects_oversized(test_db):
    """check_order_size returns False when notional exceeds cap."""
    sg = _make_safeguards(max_order_size_usd=100.0)
    # 0.1 BTC @ 50000 = 5000 USD — exceeds 100 USD cap
    assert sg.check_order_size(0.1, 50000.0) is False
    # check_order_size does NOT disable trading — caller decides
    assert sg.trading_enabled is True


# ---------------------------------------------------------------------------
# Test 7: Position size cap
# ---------------------------------------------------------------------------

def test_check_position_size_disables_when_exceeded(test_db):
    """check_position_size disables trading when position notional exceeds cap."""
    sg = _make_safeguards(max_position_size_usd=100.0)
    # 0.5 BTC @ 50000 = 25000 USD — exceeds 100 USD cap
    assert sg.check_position_size(0.5, 50000.0) is False
    assert sg.trading_enabled is False
    assert "position_size_exceeded" in sg._tripped


def test_check_position_size_allows_within_cap(test_db):
    """check_position_size returns True when position is within cap."""
    sg = _make_safeguards(max_position_size_usd=10000.0)
    # 0.1 BTC @ 50000 = 5000 USD — within 10000 USD cap
    assert sg.check_position_size(0.1, 50000.0) is True
    assert sg.trading_enabled is True


# ---------------------------------------------------------------------------
# Test 8: Stale kill_switch auto-cleared on restart when file is gone
# ---------------------------------------------------------------------------

def test_stale_kill_switch_auto_cleared_on_init(test_db, tmp_path):
    """
    If kill_switch was persisted as tripped but the file no longer exists,
    a new Safeguards instance must auto-clear it and re-enable trading.

    This fixes the mismatch where the banner says 'kill switch not present'
    but the bot silently cannot trade due to stale persisted state.
    """
    ks_file = str(tmp_path / "KILL_SWITCH")

    # Session 1: trip kill_switch via file presence
    sg1 = _make_safeguards(kill_switch_file=ks_file)
    open(ks_file, "w").close()
    sg1.can_trade()  # trips kill_switch, persists state
    assert sg1.trading_enabled is False
    assert "kill_switch" in sg1._tripped

    # Operator removes the kill switch file
    os.unlink(ks_file)

    # Session 2 (restart): new instance must auto-clear stale kill_switch
    sg2 = _make_safeguards(kill_switch_file=ks_file)
    assert sg2.trading_enabled is True, (
        "Expected trading re-enabled after stale kill_switch cleared on init"
    )
    assert "kill_switch" not in sg2._tripped


def test_stale_kill_switch_cleared_but_other_guard_keeps_trading_disabled(test_db, tmp_path):
    """
    When kill_switch + stop_required are both tripped and the kill_switch file
    is removed, kill_switch is cleared on restart but trading stays disabled
    because stop_required is a non-recoverable guard.
    """
    ks_file = str(tmp_path / "KILL_SWITCH")
    open(ks_file, "w").close()

    sg1 = _make_safeguards(kill_switch_file=ks_file)
    sg1.can_trade()         # trips kill_switch
    sg1.disable("stop_required")  # also trip stop_required
    assert sg1.trading_enabled is False
    assert "kill_switch" in sg1._tripped
    assert "stop_required" in sg1._tripped

    # Operator removes the kill switch file
    os.unlink(ks_file)

    # Restart: kill_switch cleared, stop_required must keep trading disabled
    sg2 = _make_safeguards(kill_switch_file=ks_file)
    assert sg2.trading_enabled is False, (
        "stop_required should keep trading disabled even after kill_switch clears"
    )
    assert "kill_switch" not in sg2._tripped, "kill_switch should be auto-cleared"
    assert "stop_required" in sg2._tripped, "stop_required must persist"
