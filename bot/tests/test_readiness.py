"""
Readiness check and live-startup tests.

Tests:
  1.  check_readiness returns NO when COINBASE_API_KEY missing
  2.  check_readiness returns NO when COINBASE_API_SECRET missing
  3.  check_readiness returns NO when kill switch file exists
  4.  check_readiness returns NO when LIVE_TRADING_CONFIRMED not set
  5.  check_readiness returns NO on stale non-recoverable persisted guard
  6.  check_readiness treats stale kill_switch (file gone) as NOT a blocker
  7.  check_readiness returns NO when Coinbase REST auth fails
  8.  check_readiness returns NO when REST returns no accounts
  9.  _abort_if_live_creds_missing raises SystemExit when adapter._enabled=False
  10. _abort_if_live_creds_missing is a no-op when adapter._enabled=True
"""
import json
import os
import sqlite3
import sys
import types
import pytest

import bot.config as config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _live_config(ks_file: str, live_db: str = "live_journal_NONEXISTENT.db") -> dict:
    """Minimal config that satisfies all non-credential checks."""
    return {
        "runtime": {"mode": "live", "live_db_path": live_db},
        "safety": {
            "live_trading_confirmed": True,
            "kill_switch_file": ks_file,
            "product_allowlist": ["BTC-USD"],
            "max_order_size_usd": 500.0,
            "max_position_size_usd": 1000.0,
        },
        "symbols": ["BTC-USD"],
        "risk": {"max_daily_loss": 0.015},
        "execution": {"reconcile_interval_sec": 5, "max_pending_order_age_sec": 60},
    }


def _patch_rest(success: bool = True, accounts=None, fail_msg: str = "mock auth error"):
    """Return a class suitable for monkeypatching coinbase.rest.RESTClient."""
    if not success:
        class FailClient:
            def __init__(self, *a, **kw): pass
            def get_accounts(self): raise RuntimeError(fail_msg)
        return FailClient

    _accounts = accounts or []

    class OkClient:
        def __init__(self, *a, **kw): pass
        def get_accounts(self):
            r = types.SimpleNamespace()
            r.accounts = _accounts
            return r
    return OkClient


def _make_account(currency: str, value: float):
    ab = types.SimpleNamespace(currency=currency, value=str(value))
    return types.SimpleNamespace(available_balance=ab)


# ---------------------------------------------------------------------------
# check_readiness tests
# ---------------------------------------------------------------------------

def test_readiness_no_when_api_key_missing(monkeypatch, tmp_path):
    """BLOCKER reported when COINBASE_API_KEY is absent."""
    ks_file = str(tmp_path / "KS_ABSENT")

    monkeypatch.delenv("COINBASE_API_KEY", raising=False)
    monkeypatch.setenv("COINBASE_API_SECRET", "some_secret")
    monkeypatch.setenv("LIVE_TRADING_CONFIRMED", "true")
    sys.modules["coinbase.rest"].RESTClient = _patch_rest(
        accounts=[_make_account("USD", 1000.0)]
    )

    original = config._raw
    config._raw = _live_config(ks_file)
    try:
        from bot.readiness import check_readiness
        ready, blockers, _ = check_readiness()
        assert not ready
        assert any("COINBASE_API_KEY" in b for b in blockers), blockers
    finally:
        config._raw = original


def test_readiness_no_when_api_secret_missing(monkeypatch, tmp_path):
    """BLOCKER reported when COINBASE_API_SECRET is absent."""
    ks_file = str(tmp_path / "KS_ABSENT")

    monkeypatch.setenv("COINBASE_API_KEY", "test_key")
    monkeypatch.delenv("COINBASE_API_SECRET", raising=False)
    monkeypatch.setenv("LIVE_TRADING_CONFIRMED", "true")

    original = config._raw
    config._raw = _live_config(ks_file)
    try:
        from bot.readiness import check_readiness
        ready, blockers, _ = check_readiness()
        assert not ready
        assert any("COINBASE_API_SECRET" in b for b in blockers), blockers
    finally:
        config._raw = original


def test_readiness_no_when_kill_switch_file_exists(monkeypatch, tmp_path):
    """BLOCKER reported when kill switch file is present."""
    ks_file = str(tmp_path / "KILL_SWITCH")
    open(ks_file, "w").close()

    monkeypatch.setenv("COINBASE_API_KEY", "test_key")
    monkeypatch.setenv("COINBASE_API_SECRET", "test_secret")
    monkeypatch.setenv("LIVE_TRADING_CONFIRMED", "true")
    sys.modules["coinbase.rest"].RESTClient = _patch_rest(
        accounts=[_make_account("USD", 1000.0)]
    )

    original = config._raw
    config._raw = _live_config(ks_file)
    try:
        from bot.readiness import check_readiness
        ready, blockers, _ = check_readiness()
        assert not ready
        assert any("kill switch" in b.lower() or "Kill switch" in b for b in blockers), blockers
    finally:
        config._raw = original


def test_readiness_no_when_live_confirmed_env_not_set(monkeypatch, tmp_path):
    """BLOCKER reported when LIVE_TRADING_CONFIRMED env var is absent."""
    ks_file = str(tmp_path / "KS_ABSENT")

    monkeypatch.setenv("COINBASE_API_KEY", "test_key")
    monkeypatch.setenv("COINBASE_API_SECRET", "test_secret")
    monkeypatch.delenv("LIVE_TRADING_CONFIRMED", raising=False)
    sys.modules["coinbase.rest"].RESTClient = _patch_rest(
        accounts=[_make_account("USD", 1000.0)]
    )

    original = config._raw
    config._raw = _live_config(ks_file)
    try:
        from bot.readiness import check_readiness
        ready, blockers, _ = check_readiness()
        assert not ready
        assert any("LIVE_TRADING_CONFIRMED" in b for b in blockers), blockers
    finally:
        config._raw = original


def test_readiness_no_when_stale_non_recoverable_guard(monkeypatch, tmp_path):
    """
    BLOCKER reported when live DB has stop_required tripped.
    stop_required is non-recoverable and must block readiness.
    """
    ks_file = str(tmp_path / "KS_ABSENT")
    live_db = str(tmp_path / "live.db")

    conn = sqlite3.connect(live_db)
    conn.execute("CREATE TABLE runtime_state (key TEXT PRIMARY KEY, value TEXT)")
    state = json.dumps({"trading_enabled": False, "tripped": ["stop_required"]})
    conn.execute("INSERT INTO runtime_state VALUES ('safeguards', ?)", (state,))
    conn.commit()
    conn.close()

    monkeypatch.setenv("COINBASE_API_KEY", "test_key")
    monkeypatch.setenv("COINBASE_API_SECRET", "test_secret")
    monkeypatch.setenv("LIVE_TRADING_CONFIRMED", "true")
    sys.modules["coinbase.rest"].RESTClient = _patch_rest(
        accounts=[_make_account("USD", 1000.0)]
    )

    original = config._raw
    config._raw = _live_config(ks_file, live_db)
    try:
        from bot.readiness import check_readiness
        ready, blockers, _ = check_readiness()
        assert not ready
        assert any("stop_required" in b for b in blockers), blockers
    finally:
        config._raw = original


def test_readiness_stale_kill_switch_not_blocker_when_file_gone(monkeypatch, tmp_path):
    """
    Stale kill_switch-only entry in live DB is NOT reported as a blocker when
    the kill switch file is gone — it would be auto-cleared by Safeguards.__init__.
    """
    ks_file = str(tmp_path / "KS_ABSENT")   # file does NOT exist
    live_db = str(tmp_path / "live.db")

    conn = sqlite3.connect(live_db)
    conn.execute("CREATE TABLE runtime_state (key TEXT PRIMARY KEY, value TEXT)")
    state = json.dumps({"trading_enabled": False, "tripped": ["kill_switch"]})
    conn.execute("INSERT INTO runtime_state VALUES ('safeguards', ?)", (state,))
    conn.commit()
    conn.close()

    monkeypatch.setenv("COINBASE_API_KEY", "test_key")
    monkeypatch.setenv("COINBASE_API_SECRET", "test_secret")
    monkeypatch.setenv("LIVE_TRADING_CONFIRMED", "true")
    sys.modules["coinbase.rest"].RESTClient = _patch_rest(
        accounts=[_make_account("USD", 1000.0)]
    )

    original = config._raw
    config._raw = _live_config(ks_file, live_db)
    try:
        from bot.readiness import check_readiness
        ready, blockers, _ = check_readiness()
        # Only the stale kill_switch (file gone) should be in DB — not a blocker
        stale_blockers = [b for b in blockers if "Non-recoverable" in b or "persisted safeguard" in b.lower()]
        assert not stale_blockers, (
            f"Stale kill_switch (file gone) must not be a blocker. Got: {stale_blockers}"
        )
    finally:
        config._raw = original


def test_readiness_no_when_rest_auth_fails(monkeypatch, tmp_path):
    """BLOCKER reported when Coinbase REST call raises an exception."""
    ks_file = str(tmp_path / "KS_ABSENT")

    monkeypatch.setenv("COINBASE_API_KEY", "bad_key")
    monkeypatch.setenv("COINBASE_API_SECRET", "bad_secret")
    monkeypatch.setenv("LIVE_TRADING_CONFIRMED", "true")
    sys.modules["coinbase.rest"].RESTClient = _patch_rest(
        success=False, fail_msg="401 Unauthorized"
    )

    original = config._raw
    config._raw = _live_config(ks_file)
    try:
        from bot.readiness import check_readiness
        ready, blockers, _ = check_readiness()
        assert not ready
        assert any("REST" in b or "auth" in b.lower() for b in blockers), blockers
    finally:
        config._raw = original


def test_readiness_no_when_rest_returns_no_accounts(monkeypatch, tmp_path):
    """BLOCKER reported when Coinbase REST succeeds but returns no accounts."""
    ks_file = str(tmp_path / "KS_ABSENT")

    monkeypatch.setenv("COINBASE_API_KEY", "test_key")
    monkeypatch.setenv("COINBASE_API_SECRET", "test_secret")
    monkeypatch.setenv("LIVE_TRADING_CONFIRMED", "true")
    sys.modules["coinbase.rest"].RESTClient = _patch_rest(accounts=[])  # empty list

    original = config._raw
    config._raw = _live_config(ks_file)
    try:
        from bot.readiness import check_readiness
        ready, blockers, _ = check_readiness()
        assert not ready
        assert any("no accounts" in b.lower() or "portfolio scope" in b.lower() for b in blockers), blockers
    finally:
        config._raw = original


# ---------------------------------------------------------------------------
# Live startup abort tests
# ---------------------------------------------------------------------------

def test_abort_if_live_creds_missing_raises_exit():
    """_abort_if_live_creds_missing raises SystemExit when adapter._enabled=False."""
    from main import _abort_if_live_creds_missing

    class FakeAdapter:
        _enabled = False

    with pytest.raises(SystemExit):
        _abort_if_live_creds_missing(FakeAdapter())


def test_abort_if_live_creds_missing_no_raise_when_enabled():
    """_abort_if_live_creds_missing is a no-op when adapter._enabled=True."""
    from main import _abort_if_live_creds_missing

    class FakeAdapter:
        _enabled = True

    _abort_if_live_creds_missing(FakeAdapter())  # must not raise
