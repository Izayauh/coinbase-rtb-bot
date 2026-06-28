"""
test_backtest.py — Determinism, no-lookahead, and fee correctness tests.

Uses a synthetic 50-bar dataset with known outcomes.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from research.types import Bar, Signal, BacktestResult
from research.costs import FrictionModel
from research.backtest import run_backtest, calc_atr
from research.rules import (
    _ema, _rsi, _sma, momentum_ema_cross,
    failed_breakdown_rebound, volatility_shock_reversion, range_expansion_continuation,
)


def _make_bars(prices: list, symbol: str = "TEST-USD") -> list:
    """Create synthetic bars from a list of close prices."""
    bars = []
    for i, p in enumerate(prices):
        bars.append(Bar(
            symbol=symbol, timeframe="1h", ts=1000000 + i * 3600,
            open=p * 0.999, high=p * 1.005, low=p * 0.995, close=p, volume=100.0,
        ))
    return bars


def test_no_lookahead():
    """Signal on bar[i] must enter at bar[i+1] open, not bar[i]."""
    # Create 50 bars with an uptrend (need enough for ATR warmup)
    prices = [100 + i * 0.5 for i in range(50)]
    bars = _make_bars(prices)

    # Manual signal on bar 20 (after ATR warmup at bar 14)
    signals = [Signal(bar_index=20, direction="long", rule_name="test")]

    friction = FrictionModel(taker_fee_bps=0, half_spread_bps=0, slippage_bps=0)
    result = run_backtest(bars, signals, friction, time_stop_bars=5)

    assert result.trade_count == 1, f"Expected 1 trade, got {result.trade_count}"
    trade = result.trades[0]

    # Entry should be at bar[21].open, not bar[20]
    expected_entry = bars[21].open
    assert abs(trade.entry_price - expected_entry) < 0.01, \
        f"Entry {trade.entry_price} != bar[21].open {expected_entry} (lookahead violation)"

    print("  PASS: no_lookahead")


def test_fee_deduction():
    """Fees must reduce equity; zero-fee run must have higher equity."""
    prices = [100 + i * 0.2 for i in range(50)]
    bars = _make_bars(prices)
    signals = [Signal(bar_index=20, direction="long", rule_name="test")]

    no_fee = FrictionModel(taker_fee_bps=0, half_spread_bps=0, slippage_bps=0)
    with_fee = FrictionModel(taker_fee_bps=10, half_spread_bps=2, slippage_bps=3)

    r_no = run_backtest(bars, signals, no_fee, time_stop_bars=5)
    r_fee = run_backtest(bars, signals, with_fee, time_stop_bars=5)

    if r_no.trade_count > 0 and r_fee.trade_count > 0:
        assert r_no.equity_curve[-1] >= r_fee.equity_curve[-1], \
            "Zero-fee equity should be >= with-fee equity"
        # Fee trades should have explicit cost > 0
        assert r_fee.trades[0].entry_cost > 0, "Entry cost should be > 0 with fees"
        assert r_fee.trades[0].exit_cost > 0, "Exit cost should be > 0 with fees"

    print("  PASS: fee_deduction")


def test_determinism():
    """Same input must produce same output."""
    prices = [100 + i * 0.3 for i in range(50)]
    bars = _make_bars(prices)
    signals = [Signal(bar_index=20, direction="long", rule_name="test")]
    friction = FrictionModel()

    r1 = run_backtest(bars, signals, friction, time_stop_bars=5)
    r2 = run_backtest(bars, signals, friction, time_stop_bars=5)

    assert r1.trade_count == r2.trade_count
    assert r1.equity_curve == r2.equity_curve
    if r1.trades:
        assert r1.trades[0].pnl_pct == r2.trades[0].pnl_pct

    print("  PASS: determinism")


def test_stop_loss_hit():
    """Stop loss must trigger when price drops below stop."""
    # Create bars: flat for warmup, then up, then sharp crash
    prices = [100] * 20 + [101, 102, 103, 104, 105] + [80] * 10
    bars = _make_bars(prices)

    signals = [Signal(bar_index=22, direction="long", rule_name="test")]
    friction = FrictionModel(taker_fee_bps=0, half_spread_bps=0, slippage_bps=0)
    result = run_backtest(bars, signals, friction, stop_atr_mult=1.0, time_stop_bars=100)

    if result.trade_count > 0:
        trade = result.trades[0]
        assert trade.exit_reason == "STOP_LOSS", \
            f"Expected STOP_LOSS, got {trade.exit_reason}"
        assert trade.pnl_pct < 0, "Stop loss trade should have negative PnL"

    print("  PASS: stop_loss_hit")


def test_time_stop():
    """Time stop must exit after time_stop_bars."""
    prices = [100] * 50  # flat price, no stop or TP hit
    bars = _make_bars(prices)

    signals = [Signal(bar_index=20, direction="long", rule_name="test")]
    friction = FrictionModel(taker_fee_bps=0, half_spread_bps=0, slippage_bps=0)
    result = run_backtest(
        bars, signals, friction,
        stop_atr_mult=10.0,  # very wide stop
        tp_atr_mult=20.0,    # very wide TP
        time_stop_bars=5,
    )

    if result.trade_count > 0:
        trade = result.trades[0]
        assert trade.exit_reason == "TIME_STOP", \
            f"Expected TIME_STOP, got {trade.exit_reason}"
        assert trade.bars_held == 5, f"Expected 5 bars held, got {trade.bars_held}"

    print("  PASS: time_stop")


def test_ema_indicator():
    """EMA calculation sanity check."""
    values = [10.0] * 10 + [20.0] * 10
    ema = _ema(values, 5)

    # After warmup, EMA should be 10.0 for the flat section
    assert ema[4] is not None
    assert abs(ema[4] - 10.0) < 0.01

    # After jump, EMA should be moving toward 20.0
    assert ema[-1] is not None
    assert ema[-1] > 15.0  # should have moved substantially toward 20

    print("  PASS: ema_indicator")


def test_rsi_bounds():
    """RSI should always be in [0, 100]."""
    values = [100 + i * 2 for i in range(30)]  # uptrend
    rsi = _rsi(values)
    for v in rsi:
        if v is not None:
            assert 0 <= v <= 100, f"RSI out of bounds: {v}"

    # Downtrend
    values_down = [200 - i * 3 for i in range(30)]
    rsi_down = _rsi(values_down)
    for v in rsi_down:
        if v is not None:
            assert 0 <= v <= 100, f"RSI out of bounds: {v}"

    print("  PASS: rsi_bounds")


def test_friction_sensitivity():
    """1.5x friction should produce worse results than 0.5x."""
    prices = [100 + i * 0.2 for i in range(60)]
    bars = _make_bars(prices)
    signals = [
        Signal(bar_index=20, direction="long", rule_name="test"),
        Signal(bar_index=35, direction="long", rule_name="test"),
    ]

    f_low = FrictionModel(sensitivity=0.5)
    f_high = FrictionModel(sensitivity=1.5)

    r_low = run_backtest(bars, signals, f_low, time_stop_bars=8)
    r_high = run_backtest(bars, signals, f_high, time_stop_bars=8)

    if r_low.trade_count > 0 and r_high.trade_count > 0:
        assert r_low.equity_curve[-1] >= r_high.equity_curve[-1], \
            "Lower friction should produce >= equity"

    print("  PASS: friction_sensitivity")


def test_mae_mfe_populated():
    """MAE and MFE should be populated for trades."""
    prices = [100] * 20 + [101, 102, 103, 104, 105, 103, 101, 99, 97, 95] + [93] * 10
    bars = _make_bars(prices)
    signals = [Signal(bar_index=20, direction="long", rule_name="test")]
    friction = FrictionModel(taker_fee_bps=0, half_spread_bps=0, slippage_bps=0)

    result = run_backtest(bars, signals, friction, stop_atr_mult=1.0, time_stop_bars=20)

    if result.trade_count > 0:
        trade = result.trades[0]
        # MFE should be positive (price went up initially)
        # MAE should be negative (price dropped after peak)
        assert trade.mfe_pct != 0 or trade.mae_pct != 0, "MAE/MFE should be populated"

    print("  PASS: mae_mfe_populated")


def test_new_event_rules_emit_signals():
    """New event-driven spot rules should fire on obvious synthetic patterns."""
    bars = []
    base_prices = [100.0] * 60
    for i, p in enumerate(base_prices):
        low = p * 0.995
        high = p * 1.005
        close = p
        volume = 100.0

        if i == 30:
            low = 96.0
            high = 101.0
            close = 99.8  # failed breakdown reclaim + still below prior close
            volume = 220.0
        elif i == 40:
            low = 97.0
            high = 104.0
            close = 103.2  # breakout continuation / shock reclaim
            volume = 240.0

        bars.append(Bar(
            symbol="TEST-USD", timeframe="1h", ts=1000000 + i * 3600,
            open=p, high=high, low=low, close=close, volume=volume,
        ))

    assert failed_breakdown_rebound(bars, lookback=20), "failed_breakdown_rebound should emit"
    assert volatility_shock_reversion(bars, shock_atr_mult=1.5, volume_mult=1.0), "volatility_shock_reversion should emit"
    assert range_expansion_continuation(bars, lookback=20, range_atr_mult=1.0, volume_mult=1.0), "range_expansion_continuation should emit"

    print("  PASS: new_event_rules_emit_signals")


if __name__ == "__main__":
    print("Running research harness tests...\n")
    test_no_lookahead()
    test_fee_deduction()
    test_determinism()
    test_stop_loss_hit()
    test_time_stop()
    test_ema_indicator()
    test_rsi_bounds()
    test_friction_sensitivity()
    test_mae_mfe_populated()
    test_new_event_rules_emit_signals()
    print("\nAll tests passed.")
