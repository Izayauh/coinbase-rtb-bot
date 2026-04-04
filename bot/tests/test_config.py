"""
Config validation tests (bot/config.py:validate()).

Tests:
  1. Paper mode passes validation.
  2. Live mode exits.
  3. Invalid portfolio_value exits.
  4. Zero reconcile_interval_sec exits.
  5. Zero max_pending_order_age_sec exits.
  6. Missing symbols exits.
"""
import pytest


def _run_validate(overrides: dict):
    """
    Run config.validate() with a patched _raw dict.
    overrides is merged on top of a minimal valid config.
    """
    import bot.config as cfg

    base = {
        "runtime": {
            "mode": "paper",
            "trading_enabled": True,
            "portfolio_value": 10000.0,
            "paper_db_path": "paper_journal.db",
        },
        "symbols": ["BTC-USD"],
        "execution": {
            "reconcile_interval_sec": 5,
            "max_pending_order_age_sec": 60,
        },
        "risk": {"max_daily_loss": 0.015},
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


def test_valid_paper_config_passes(monkeypatch):
    """A fully valid paper config must not raise or exit."""
    _run_validate({})  # no overrides — base is valid


def test_live_mode_exits_without_gate(monkeypatch):
    """Live mode without the safety gate flags must call sys.exit(1)."""
    monkeypatch.delenv("LIVE_TRADING_CONFIRMED", raising=False)
    with pytest.raises(SystemExit):
        _run_validate({"runtime": {"mode": "live"}})


def test_invalid_mode_exits(monkeypatch):
    """An unrecognised mode string must call sys.exit(1)."""
    with pytest.raises(SystemExit):
        _run_validate({"runtime": {"mode": "simulation"}})


def test_zero_portfolio_value_exits(monkeypatch):
    """portfolio_value <= 0 must call sys.exit(1)."""
    with pytest.raises(SystemExit):
        _run_validate({"runtime": {"portfolio_value": 0.0}})


def test_negative_portfolio_value_exits(monkeypatch):
    """Negative portfolio_value must call sys.exit(1)."""
    with pytest.raises(SystemExit):
        _run_validate({"runtime": {"portfolio_value": -500.0}})


def test_zero_reconcile_interval_exits(monkeypatch):
    """reconcile_interval_sec <= 0 must call sys.exit(1)."""
    with pytest.raises(SystemExit):
        _run_validate({"execution": {"reconcile_interval_sec": 0}})


def test_zero_max_pending_age_exits(monkeypatch):
    """max_pending_order_age_sec <= 0 must call sys.exit(1)."""
    with pytest.raises(SystemExit):
        _run_validate({"execution": {"max_pending_order_age_sec": 0}})


def test_empty_symbols_exits(monkeypatch):
    """No symbols configured must call sys.exit(1)."""
    with pytest.raises(SystemExit):
        _run_validate({"symbols": []})


def test_multiple_symbols_exits(monkeypatch):
    """More than one symbol must call sys.exit(1)."""
    with pytest.raises(SystemExit):
        _run_validate({"symbols": ["BTC-USD", "ETH-USD"]})


def test_symbol_not_in_allowlist_exits(monkeypatch):
    """Symbol not present in product_allowlist must call sys.exit(1)."""
    with pytest.raises(SystemExit):
        _run_validate({
            "symbols": ["ETH-USD"],
            "safety": {"product_allowlist": ["BTC-USD"]},
        })


def test_symbol_in_allowlist_passes(monkeypatch):
    """Symbol present in product_allowlist must pass validation."""
    _run_validate({
        "symbols": ["BTC-USD"],
        "safety": {
            "product_allowlist": ["BTC-USD"],
            "max_order_size_usd": 500.0,
            "max_position_size_usd": 1000.0,
        },
    })


def test_zero_max_order_size_exits(monkeypatch):
    """max_order_size_usd <= 0 must call sys.exit(1)."""
    with pytest.raises(SystemExit):
        _run_validate({"safety": {"max_order_size_usd": 0.0}})


def test_zero_max_position_size_exits(monkeypatch):
    """max_position_size_usd <= 0 must call sys.exit(1)."""
    with pytest.raises(SystemExit):
        _run_validate({"safety": {"max_position_size_usd": 0.0}})
