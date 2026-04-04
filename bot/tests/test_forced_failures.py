"""
Forced-failure tests — prove every safety rail fires.

Each test exercises one guard end-to-end so there is a single authoritative
place to verify all rails are wired and working before paper validation.

Rails covered:
  1. Oversize order   — ExecutionService rejects REJECTED_SIZE_CAP
  2. Wrong product    — config.validate() exits on allowlist violation
  3. Daily loss cap   — Safeguards trips on equity drawdown > threshold
  4. Kill switch      — Safeguards.can_trade() returns False immediately
  5. Live mode gate   — config.validate() exits without both flags set
"""
import os
import time
import pytest

from bot.db import db
from bot.journal import Journal
from bot.safeguards import Safeguards
from bot.execution import ExecutionService
from bot.models import Signal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_signal(
    signal_id="sig_ff_001",
    symbol="BTC-USD",
    execution_price=50000.0,
    retest_level=49000.0,
    atr=500.0,
):
    return Signal(
        signal_id=signal_id,
        symbol=symbol,
        signal_type="LONG",
        regime_snapshot="ATR:0.01_LEVEL:49000",
        breakout_level=49500.0,
        retest_level=retest_level,
        atr=atr,
        rsi=62.0,
        status="NEW",
        execution_price=execution_price,
    )


def _insert_signal(sig):
    db.execute(
        "INSERT INTO signals (signal_id, symbol, signal_type, regime_snapshot, "
        "breakout_level, retest_level, atr, rsi, status, execution_price) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            sig.signal_id, sig.symbol, sig.signal_type, sig.regime_snapshot,
            sig.breakout_level, sig.retest_level, sig.atr, sig.rsi,
            sig.status, sig.execution_price,
        ),
    )


def _make_safeguards(**kw) -> Safeguards:
    defaults = dict(
        trading_enabled=True,
        ws_stale_timeout_sec=15,
        max_daily_loss_fraction=0.015,
        portfolio_value=10000.0,
        max_order_size_usd=10000.0,
        max_position_size_usd=100000.0,
    )
    defaults.update(kw)
    return Safeguards(**defaults)


def _run_validate(overrides: dict):
    import bot.config as cfg

    base = {
        "runtime": {"mode": "paper", "trading_enabled": True, "portfolio_value": 10000.0},
        "symbols": ["BTC-USD"],
        "execution": {"reconcile_interval_sec": 5, "max_pending_order_age_sec": 60},
        "risk": {"max_daily_loss": 0.015},
        "safety": {
            "product_allowlist": ["BTC-USD"],
            "max_order_size_usd": 500.0,
            "max_position_size_usd": 1000.0,
        },
    }

    def _merge(base, patch):
        result = dict(base)
        for k, v in patch.items():
            if isinstance(v, dict) and isinstance(result.get(k), dict):
                result[k] = _merge(result[k], v)
            else:
                result[k] = v
        return result

    patched = _merge(base, overrides)
    original = cfg._raw
    cfg._raw = patched
    try:
        cfg.validate()
    finally:
        cfg._raw = original


# ---------------------------------------------------------------------------
# Rail 1: Oversize order is blocked before submission
# ---------------------------------------------------------------------------

def test_rail_oversize_order_blocked(test_db):
    """
    An order whose notional (size × limit_price) exceeds max_order_size_usd
    must be rejected with status REJECTED_SIZE_CAP. Trading is NOT disabled —
    it's a per-signal rejection, not a global halt.
    """
    sig = _make_signal()
    _insert_signal(sig)

    sg = _make_safeguards(max_order_size_usd=0.01)  # absurdly small cap
    svc = ExecutionService(portfolio_value=10000.0, safeguards=sg)

    order = svc.process_signal(sig)

    assert order.status == "REJECTED_SIZE_CAP", (
        f"Expected REJECTED_SIZE_CAP, got {order.status}"
    )
    # Cap rejection must NOT disable trading globally — just reject this order
    assert sg.trading_enabled is True, "Oversize rejection must not disable trading"

    # Confirm the rejection is persisted to the DB
    saved = Journal.get_order_for_signal(sig.signal_id)
    assert saved["status"] == "REJECTED_SIZE_CAP"


# ---------------------------------------------------------------------------
# Rail 2: Wrong product is blocked at startup
# ---------------------------------------------------------------------------

def test_rail_wrong_product_blocked_at_startup(test_db):
    """
    config.validate() must exit (sys.exit) when the configured symbol is not
    in the product allowlist. This prevents the bot from ever trading an
    unauthorised instrument.
    """
    with pytest.raises(SystemExit):
        _run_validate({
            "symbols": ["ETH-USD"],
            "safety": {"product_allowlist": ["BTC-USD"]},
        })


def test_rail_allowed_product_passes_startup(test_db):
    """Counterpart: the allowed product must pass validation cleanly."""
    _run_validate({
        "symbols": ["BTC-USD"],
        "safety": {"product_allowlist": ["BTC-USD"]},
    })


# ---------------------------------------------------------------------------
# Rail 3: Daily loss cap blocks new orders
# ---------------------------------------------------------------------------

def test_rail_daily_loss_cap_blocks_trading(test_db):
    """
    When today's equity drawdown (first snapshot → latest snapshot) exceeds
    max_daily_loss_fraction × portfolio_value, can_trade() must return False
    and 'daily_loss' must be in _tripped.
    """
    portfolio = 10000.0
    max_loss_frac = 0.015  # $150 loss cap
    sg = _make_safeguards(
        portfolio_value=portfolio,
        max_daily_loss_fraction=max_loss_frac,
    )

    # Insert two equity snapshots for today: start=$10000, now=$9800 (−$200 > $150 cap)
    today_ts = int(time.time()) - 3600  # 1 hour ago
    Journal.insert_equity_snapshot(portfolio, 0.0, 0.0, 10000.0, 0)
    # Manually set the first snapshot to 1 hour ago so it's "earlier today"
    db.execute(
        "UPDATE equity_snapshots SET ts=? WHERE id=(SELECT MIN(id) FROM equity_snapshots)",
        (today_ts,),
    )
    Journal.insert_equity_snapshot(portfolio, -200.0, 0.0, 9800.0, 1)

    result = sg.can_trade()

    assert result is False, "can_trade() must return False when daily loss cap is exceeded"
    assert "daily_loss" in sg._tripped


def test_rail_daily_loss_cap_does_not_fire_when_loss_is_small(test_db):
    """
    A drawdown below the threshold must NOT trip the daily_loss guard.
    """
    portfolio = 10000.0
    sg = _make_safeguards(
        portfolio_value=portfolio,
        max_daily_loss_fraction=0.015,  # $150 threshold
    )

    # Insert two snapshots: $10000 → $9950 (−$50, under $150 threshold)
    today_ts = int(time.time()) - 3600
    Journal.insert_equity_snapshot(portfolio, 0.0, 0.0, 10000.0, 0)
    db.execute(
        "UPDATE equity_snapshots SET ts=? WHERE id=(SELECT MIN(id) FROM equity_snapshots)",
        (today_ts,),
    )
    Journal.insert_equity_snapshot(portfolio, -50.0, 0.0, 9950.0, 1)

    result = sg.can_trade()

    assert result is True
    assert "daily_loss" not in sg._tripped


# ---------------------------------------------------------------------------
# Rail 4: Kill switch stops trading immediately
# ---------------------------------------------------------------------------

def test_rail_kill_switch_stops_trading(test_db, tmp_path):
    """
    Creating the kill switch file must cause can_trade() to return False on
    the very next call — no restart required.
    """
    ks_file = str(tmp_path / "KILL_SWITCH")
    sg = _make_safeguards(kill_switch_file=ks_file)

    # Trading is enabled before the file exists
    assert sg.can_trade() is True

    # Operator creates the kill switch file
    open(ks_file, "w").close()

    # Must block immediately on the next call
    assert sg.can_trade() is False
    assert sg.trading_enabled is False
    assert "kill_switch" in sg._tripped


def test_rail_kill_switch_absent_allows_trading(test_db, tmp_path):
    """Counterpart: no kill switch file → trading is allowed."""
    ks_file = str(tmp_path / "NO_KILL_SWITCH")
    sg = _make_safeguards(kill_switch_file=ks_file)
    assert sg.can_trade() is True


# ---------------------------------------------------------------------------
# Rail 5: Live mode cannot start without explicit enable
# ---------------------------------------------------------------------------

def test_rail_live_mode_blocked_without_config_flag(monkeypatch):
    """
    Live mode must not start unless safety.live_trading_confirmed=true in
    config AND LIVE_TRADING_CONFIRMED=true in the environment.
    """
    monkeypatch.delenv("LIVE_TRADING_CONFIRMED", raising=False)
    with pytest.raises(SystemExit):
        _run_validate({"runtime": {"mode": "live"}})


def test_rail_live_mode_blocked_without_env_var(monkeypatch):
    """
    Even with live_trading_confirmed=true in config, the env var is also
    required. Missing env var alone must cause sys.exit.
    """
    monkeypatch.delenv("LIVE_TRADING_CONFIRMED", raising=False)
    with pytest.raises(SystemExit):
        _run_validate({
            "runtime": {"mode": "live"},
            "safety": {"live_trading_confirmed": True},
        })
