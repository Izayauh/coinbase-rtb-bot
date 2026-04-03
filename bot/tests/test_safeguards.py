"""
Safeguards unit tests.

Tests:
  1. trading_enabled=False blocks signal processing immediately.
  2. stale_stream guard fires when last_trade_ts is old, recovers when fresh.
  3. check_stop_invariant disables trading if position missing stop.
  4. Persisted disabled state survives restart (new Safeguards instance).
"""
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
