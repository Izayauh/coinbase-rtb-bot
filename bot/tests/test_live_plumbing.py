"""
Live plumbing unit tests.

None of these tests touch the real Coinbase API.
All REST calls are mocked at the adapter level.

Tests:
  1. Fixed notional overrides risk sizing in ExecutionService
  2. Fixed notional = 0 falls back to normal risk sizing
  3. submit_order_intent calls rest.create_order in live mode
  4. submit_order_intent returns synthetic ID in paper/disconnected mode
  5. Kill switch blocks live order submission (defense-in-depth)
  6. _extract_order_id handles all three SDK response formats
  7. Live DB path is separate from paper DB path
  8. live_test_order_notional_usd config accessor works
"""
import os
import time
import types
import pytest

from bot.db import db
from bot.journal import Journal
from bot.execution import ExecutionService
from bot.safeguards import Safeguards
from bot.models import Signal, Order
from bot.coinbase_adapter import CoinbaseAdapter, _extract_order_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_signal(signal_id="sig_lp_001", execution_price=66820.0):
    return Signal(
        signal_id=signal_id,
        symbol="BTC-USD",
        signal_type="LONG",
        regime_snapshot="ATR:400_LEVEL:66000",
        breakout_level=66500.0,
        retest_level=66000.0,
        atr=400.0,
        rsi=62.0,
        status="NEW",
        execution_price=execution_price,
    )


def _insert_signal(sig):
    db.execute(
        "INSERT INTO signals "
        "(signal_id, symbol, signal_type, regime_snapshot, "
        "breakout_level, retest_level, atr, rsi, status, execution_price) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            sig.signal_id, sig.symbol, sig.signal_type, sig.regime_snapshot,
            sig.breakout_level, sig.retest_level, sig.atr, sig.rsi,
            sig.status, sig.execution_price,
        ),
    )


def _make_permissive_safeguards():
    return Safeguards(
        trading_enabled=True,
        ws_stale_timeout_sec=15,
        max_daily_loss_fraction=0.015,
        portfolio_value=10000.0,
        max_order_size_usd=100000.0,
        max_position_size_usd=1000000.0,
    )


# ---------------------------------------------------------------------------
# Test 1 & 2: Fixed notional override
# ---------------------------------------------------------------------------

def test_fixed_notional_overrides_risk_sizing(test_db):
    """
    When live_test_notional_usd > 0, process_signal must use
    notional / entry_price as the order size, not risk-calculated sizing.
    """
    sig = _make_signal(execution_price=66820.0)
    _insert_signal(sig)

    live_notional = 10.0
    svc = ExecutionService(
        portfolio_value=10000.0,
        safeguards=_make_permissive_safeguards(),
        live_test_notional_usd=live_notional,
    )
    order = svc.process_signal(sig)

    assert order.status == "PENDING"
    expected_size = round(live_notional / 66820.0, 8)
    assert order.size == expected_size, (
        f"Expected size={expected_size}, got {order.size}"
    )


def test_zero_notional_uses_risk_sizing(test_db):
    """
    When live_test_notional_usd == 0, normal risk-based sizing is used.
    """
    sig = _make_signal(execution_price=66820.0)
    _insert_signal(sig)

    svc = ExecutionService(
        portfolio_value=10000.0,
        safeguards=_make_permissive_safeguards(),
        live_test_notional_usd=0.0,
    )
    order = svc.process_signal(sig)

    assert order.status == "PENDING"
    # Risk: stop = 66000 - 400 = 65600, distance = 1220
    # size = (10000 * 0.002) / 1220 = 0.01639
    from bot.risk import RiskManager
    stop = 66000.0 - 400.0
    expected_size = RiskManager.calculate_size(10000.0, 66820.0, stop)
    assert order.size == expected_size


# ---------------------------------------------------------------------------
# Test 3: submit_order_intent calls REST in live mode
# ---------------------------------------------------------------------------

def test_submit_order_intent_calls_rest_in_live_mode(tmp_path):
    """
    When _enabled=True, submit_order_intent must call rest.create_order
    and return the exchange_order_id from the response.
    """
    # Build a mock REST client that returns a successful response
    mock_response = types.SimpleNamespace(
        success=True,
        success_response=types.SimpleNamespace(order_id="exch_abc123"),
    )

    adapter = CoinbaseAdapter.__new__(CoinbaseAdapter)
    adapter.api_key = "fake_key"
    adapter.api_secret = "fake_secret"
    adapter._enabled = True
    adapter.rest = types.SimpleNamespace(
        create_order=lambda **kw: mock_response
    )

    order = Order(
        order_id="ord_test_live",
        signal_id="sig_test",
        symbol="BTC-USD",
        side="BUY",
        price=67000.0,
        size=0.00015,
        executed_size=0.0,
        status="PENDING",
        created_at=int(time.time()),
    )

    # Ensure no kill switch file exists at the default path
    import bot.config as _cfg
    _cfg._raw.setdefault("safety", {})["kill_switch_file"] = str(tmp_path / "NO_KS")

    result = adapter.submit_order_intent(order)

    assert result["exchange_order_id"] == "exch_abc123"
    assert "submitted_at" in result


def test_submit_order_intent_returns_synthetic_when_disconnected():
    """
    When _enabled=False (paper/disconnected), submit_order_intent must
    return a synthetic cb_-prefixed ID without calling the REST client.
    """
    adapter = CoinbaseAdapter.__new__(CoinbaseAdapter)
    adapter._enabled = False
    adapter.rest = None

    order = Order(
        order_id="ord_paper_001",
        signal_id="sig_paper",
        symbol="BTC-USD",
        side="BUY",
        price=66820.0,
        size=0.0002,
        executed_size=0.0,
        status="PENDING",
        created_at=int(time.time()),
    )

    result = adapter.submit_order_intent(order)

    assert result["exchange_order_id"] == "cb_ord_paper_001"
    assert result["status"] == "OPEN"


# ---------------------------------------------------------------------------
# Test 5: Kill switch blocks live order (defense-in-depth)
# ---------------------------------------------------------------------------

def test_kill_switch_blocks_live_submit(tmp_path):
    """
    A live submit_order_intent must raise RuntimeError if the kill switch
    file exists, even if can_trade() was True earlier in the tick.
    """
    ks_file = str(tmp_path / "KILL_SWITCH")
    open(ks_file, "w").close()  # create kill switch

    import bot.config as _cfg
    original = _cfg._raw.get("safety", {}).get("kill_switch_file")
    _cfg._raw.setdefault("safety", {})["kill_switch_file"] = ks_file

    try:
        adapter = CoinbaseAdapter.__new__(CoinbaseAdapter)
        adapter._enabled = True
        adapter.rest = types.SimpleNamespace(create_order=lambda **kw: None)

        order = Order(
            order_id="ord_ks_test",
            signal_id="sig_ks",
            symbol="BTC-USD",
            side="BUY",
            price=66820.0,
            size=0.00015,
            executed_size=0.0,
            status="PENDING",
            created_at=int(time.time()),
        )

        with pytest.raises(RuntimeError, match="Kill switch"):
            adapter.submit_order_intent(order)
    finally:
        if original is not None:
            _cfg._raw["safety"]["kill_switch_file"] = original


# ---------------------------------------------------------------------------
# Test 6: _extract_order_id handles all response formats
# ---------------------------------------------------------------------------

def test_extract_order_id_sdk_object_format():
    """Format 1: SDK response object with .success/.success_response.order_id"""
    response = types.SimpleNamespace(
        success=True,
        success_response=types.SimpleNamespace(order_id="cb_order_111"),
    )
    assert _extract_order_id(response) == "cb_order_111"


def test_extract_order_id_sdk_object_rejected():
    """Format 1 rejected: success=False → returns empty string."""
    response = types.SimpleNamespace(
        success=False,
        error_response="INSUFFICIENT_FUND",
    )
    assert _extract_order_id(response) == ""


def test_extract_order_id_dict_format():
    """Format 2: plain dict response."""
    response = {"success": True, "success_response": {"order_id": "cb_order_222"}}
    assert _extract_order_id(response) == "cb_order_222"


def test_extract_order_id_direct_attribute():
    """Format 3: object with direct .order_id attribute."""
    response = types.SimpleNamespace(order_id="cb_order_333")
    assert _extract_order_id(response) == "cb_order_333"


# ---------------------------------------------------------------------------
# Test 7: Separate DB paths
# ---------------------------------------------------------------------------

def test_paper_and_live_db_paths_are_different():
    """Paper and live DB paths must be distinct to prevent data mixing."""
    import bot.config as cfg
    assert cfg.paper_db_path() != cfg.live_db_path(), (
        "paper_db_path and live_db_path must be different files"
    )


# ---------------------------------------------------------------------------
# Test 8: Config accessor
# ---------------------------------------------------------------------------

def test_live_test_order_notional_usd_accessor():
    """live_test_order_notional_usd() returns the configured value."""
    import bot.config as cfg
    original = cfg._raw
    cfg._raw = {"live": {"test_order_notional_usd": 25.0}}
    try:
        assert cfg.live_test_order_notional_usd() == 25.0
    finally:
        cfg._raw = original


def test_live_test_order_notional_usd_defaults_to_zero():
    """live_test_order_notional_usd() defaults to 0.0 when key absent."""
    import bot.config as cfg
    original = cfg._raw
    cfg._raw = {}
    try:
        assert cfg.live_test_order_notional_usd() == 0.0
    finally:
        cfg._raw = original
