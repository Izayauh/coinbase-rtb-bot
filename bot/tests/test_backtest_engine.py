"""
Backtest engine unit tests.

Tests the strategy replay logic in isolation — no API calls, no DB.

Tests:
  1. Engine requires minimum bar counts
  2. No trades produced when regime is bearish
  3. Full breakout-retest-continuation cycle produces a trade
  4. Stop loss exit triggers correctly
  5. Time stop exits after 12 bars
  6. Setup cancelled if retest window (5 bars) expires
  7. Setup cancelled if regime turns bearish during retest wait
"""
import time
import pytest

from bot.models import Bar
from backtest import BacktestEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bars_1h(count: int, base_price: float = 50000.0,
                  volume: float = 100.0, start_ts: int = 0) -> list:
    """Generate flat 1h bars. Price slightly varies to avoid zero-range bars."""
    bars = []
    for i in range(count):
        ts = start_ts + i * 3600
        p = base_price + (i % 5) * 10  # slight variation
        bars.append(Bar("BTC-USD", "1h", ts, p, p + 50, p - 50, p + 5, volume))
    return bars


def _make_bars_4h(count: int, base_price: float = 50000.0, start_ts: int = 0) -> list:
    """Generate flat 4h bars with slight uptrend for bullish regime."""
    bars = []
    for i in range(count):
        ts = start_ts + i * 14400
        p = base_price + i * 5  # slight uptrend so EMA-50 > EMA-200
        bars.append(Bar("BTC-USD", "4h", ts, p, p + 200, p - 100, p + 50, 500.0))
    return bars


def _breakout_bar(ts: int, breakout_level: float, atr: float = 500.0) -> Bar:
    """Create a bar that satisfies all breakout conditions."""
    # close > breakout_level, volume > 1.25x average, close_pct > 0.70
    entry = breakout_level + atr * 0.3
    return Bar(
        "BTC-USD", "1h", ts,
        open=breakout_level - 50,
        high=entry + 100,
        low=breakout_level - 200,
        close=entry,
        volume=5000.0,  # very high volume to pass volume filter
    )


def _retest_bar(ts: int, breakout_level: float, breakout_bar: Bar) -> Bar:
    """Create a bar that satisfies retest conditions."""
    midpoint = (breakout_bar.high + breakout_bar.low) / 2
    return Bar(
        "BTC-USD", "1h", ts,
        open=breakout_level + 50,
        high=breakout_level + 200,
        low=breakout_level - 100,  # touches zone
        close=breakout_level + 100,  # closes above breakout + above midpoint
        volume=100.0,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_engine_requires_minimum_bars(capsys):
    """Engine prints error and produces no trades with insufficient bars."""
    engine = BacktestEngine()
    engine.run([], [])
    assert len(engine.trades) == 0


def test_no_trades_in_flat_market():
    """No breakouts in completely flat, low-volume bars → zero trades."""
    bars_1h = _make_bars_1h(500, volume=10.0)  # very low volume
    bars_4h = _make_bars_4h(210)

    engine = BacktestEngine()
    engine.run(bars_1h, bars_4h)

    assert len(engine.trades) == 0


def test_retest_window_expiry():
    """
    Setup cancelled if retest doesn't occur within 5 bars after breakout.
    """
    engine = BacktestEngine()
    # Manually force a state after breakout
    engine.state = engine.WAITING_RETEST
    engine.bars_since_breakout = 0
    engine.breakout_bar = Bar("BTC-USD", "1h", 0, 100, 150, 90, 140, 500)
    engine.breakout_level = 110.0

    # Simulate 6 retest eval calls that don't match
    bars_4h = _make_bars_4h(210)  # bullish
    bars_1h = _make_bars_1h(30, base_price=200)  # price way above breakout zone
    for _ in range(6):
        engine._eval_retest(bars_1h, bars_4h)

    assert engine.state == engine.IDLE, (
        "Should have reset to IDLE after retest window expired"
    )


def test_stop_loss_exit():
    """Position closed at stop price when bar low touches it."""
    engine = BacktestEngine()
    from backtest import Trade

    entry = 50000.0
    stop = 49000.0
    engine.position = Trade(
        entry_ts=100, entry_price=entry, stop_price=stop,
        size=0.01, atr_at_entry=500.0,
    )
    engine.state = engine.IN_POSITION
    engine.bars_in_position = 0

    # Bar that hits the stop
    bar = Bar("BTC-USD", "1h", 200, entry, entry + 100, stop - 100, stop - 50, 50)
    engine._check_exit(bar, [])

    assert engine.position is None, "Position should be closed"
    assert len(engine.trades) == 1
    assert engine.trades[0].exit_reason == "STOP_LOSS"
    assert engine.trades[0].pnl_usd < 0


def test_time_stop_exit():
    """Position closed at bar close after 12 bars."""
    engine = BacktestEngine()
    from backtest import Trade

    entry = 50000.0
    stop = 49000.0
    engine.position = Trade(
        entry_ts=100, entry_price=entry, stop_price=stop,
        size=0.01, atr_at_entry=500.0,
    )
    engine.state = engine.IN_POSITION
    engine.bars_in_position = 11  # next check will be bar 12

    # Bar above stop (no stop hit)
    bar = Bar("BTC-USD", "1h", 200, entry + 50, entry + 100, entry - 50, entry + 30, 50)
    engine._check_exit(bar, [])

    assert engine.position is None
    assert engine.trades[0].exit_reason == "TIME_STOP"


def test_bearish_regime_cancels_retest():
    """Waiting retest → IDLE when regime turns bearish."""
    engine = BacktestEngine()
    engine.state = engine.WAITING_RETEST
    engine.bars_since_breakout = 0
    engine.breakout_bar = Bar("BTC-USD", "1h", 0, 100, 150, 90, 140, 500)
    engine.breakout_level = 110.0

    # Bearish 4h bars: price trending down
    bars_4h = []
    for i in range(210):
        p = 50000 - i * 50  # downtrend
        bars_4h.append(Bar("BTC-USD", "4h", i * 14400, p, p + 100, p - 100, p - 30, 500))

    bars_1h = _make_bars_1h(30)
    engine._eval_retest(bars_1h, bars_4h)

    assert engine.state == engine.IDLE
