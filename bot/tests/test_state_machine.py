import pytest

from bot.models import Bar
from bot.state_machine import StateMachine
import bot.state_machine as state_machine_module


@pytest.fixture
def mock_indicators(monkeypatch):
    monkeypatch.setattr(state_machine_module, "is_bullish_regime", lambda b: True)
    monkeypatch.setattr(state_machine_module.Indicators, "calc_rsi", lambda c, p=14: [65.0] * len(c))
    monkeypatch.setattr(state_machine_module.Indicators, "calc_atr", lambda b, p=14: [100.0] * len(b))


def generate_bars(count, timeframe="1h", start_ts=0, close_val=1000.0, high_val=1050.0, low_val=950.0, vol=50.0):
    bars = []
    interval = 3600 if timeframe == "1h" else 14400
    for i in range(count):
        bars.append(Bar(
            symbol="BTC-USD", timeframe=timeframe,
            ts_open=start_ts + i * interval,
            open=1000.0, high=high_val, low=low_val, close=close_val, volume=vol,
        ))
    return bars


def test_unchanged_4h_context_advances_1h(test_db, mock_indicators):
    sm = StateMachine()
    bars_4h = generate_bars(205, "4h")
    bars_1h = generate_bars(25, "1h", close_val=100.0, high_val=110.0)

    sm.process_bars(bars_1h, bars_4h)
    assert sm.state == StateMachine.IDLE

    breakout_bar = Bar("BTC-USD", "1h", bars_1h[-1].ts_open + 3600, 100.0, 150.0, 90.0, 140.0, 500.0)
    sm.process_bars(bars_1h + [breakout_bar], bars_4h)
    assert sm.state == StateMachine.WAITING_RETEST


def test_repeated_processing_is_idempotent(test_db, mock_indicators):
    sm = StateMachine()
    bars_4h = generate_bars(205, "4h")
    bars_1h = generate_bars(25, "1h", close_val=100.0, high_val=110.0)

    sm.process_bars(bars_1h, bars_4h)
    assert sm.state == StateMachine.IDLE

    breakout_bar = Bar("BTC-USD", "1h", bars_1h[-1].ts_open + 3600, 100.0, 150.0, 90.0, 140.0, 500.0)
    new_1h = bars_1h + [breakout_bar]

    sm.process_bars(new_1h, bars_4h)
    assert sm.state == StateMachine.WAITING_RETEST

    sm.process_bars(new_1h, bars_4h)
    assert sm.state == StateMachine.WAITING_RETEST
    sm.process_bars(new_1h, bars_4h)
    assert sm.bars_since_breakout == 0


def test_out_of_order_bars_ignored(test_db, mock_indicators):
    sm = StateMachine()
    bars_4h = generate_bars(205, "4h")
    bars_1h = generate_bars(25, "1h")
    sm.process_bars(bars_1h, bars_4h)

    old_bar = Bar("BTC-USD", "1h", bars_1h[-1].ts_open - 3600, 100, 100, 100, 100, 5)
    sm.process_bars(bars_1h[:-1] + [old_bar], bars_4h)
    assert sm.state == StateMachine.IDLE
    assert sm.last_1h_ts == bars_1h[-1].ts_open


def test_four_hour_gap_disables(test_db, mock_indicators):
    sm = StateMachine()
    bars_4h = generate_bars(205, "4h")
    bars_1h = generate_bars(25, "1h")
    sm.process_bars(bars_1h, bars_4h)

    gapped_4h = Bar("BTC-USD", "4h", bars_4h[-1].ts_open + 14400 * 2, 1, 1, 1, 1, 1)
    sm.process_bars(
        bars_1h + [Bar("BTC-USD", "1h", bars_1h[-1].ts_open + 3600, 1, 1, 1, 1, 1)],
        bars_4h + [gapped_4h],
    )
    assert sm.state == StateMachine.DISABLED


def test_full_lifecycle_single_signal(test_db, mock_indicators):
    sm = StateMachine()
    bars_4h = generate_bars(205, "4h")
    bars_1h = generate_bars(25, "1h", close_val=100.0, high_val=110.0)

    sm.process_bars(bars_1h, bars_4h)

    ts = bars_1h[-1].ts_open
    b1 = Bar("BTC-USD", "1h", ts + 3600, 100.0, 150.0, 90.0, 140.0, 500.0)
    bars_1h.append(b1)
    sm.process_bars(bars_1h, bars_4h)

    setup_id = sm.setup_id
    assert setup_id is not None

    b2 = Bar("BTC-USD", "1h", ts + 7200, 140.0, 140.0, 80.0, 125.0, 50.0)
    bars_1h.append(b2)
    sm.process_bars(bars_1h, bars_4h)
    assert sm.state == StateMachine.RETEST_CONFIRMED

    b3 = Bar("BTC-USD", "1h", ts + 10800, 115.0, 160.0, 115.0, 150.0, 50.0)
    bars_1h.append(b3)
    sm.process_bars(bars_1h, bars_4h)
    assert sm.state == StateMachine.SIGNAL_EMITTED

    rows = test_db.fetch_all("SELECT * FROM signals")
    assert len(rows) == 1
    assert rows[0]["signal_id"] == setup_id
    assert rows[0]["symbol"] == "BTC-USD"

    sm.process_bars(bars_1h, bars_4h)
    assert sm.state == StateMachine.SIGNAL_EMITTED
    rows_after = test_db.fetch_all("SELECT * FROM signals")
    assert len(rows_after) == 1

    b4 = Bar("BTC-USD", "1h", ts + 14400, 150.0, 155.0, 140.0, 145.0, 50.0)
    bars_1h.append(b4)
    sm.process_bars(bars_1h, bars_4h)
    assert sm.state == StateMachine.IDLE


def test_bearish_regime_invalidates_retest(test_db, mock_indicators, monkeypatch):
    sm = StateMachine()
    bars_4h = generate_bars(205, "4h")
    bars_1h = generate_bars(25, "1h", close_val=100.0, high_val=110.0)

    sm.process_bars(bars_1h, bars_4h)

    ts = bars_1h[-1].ts_open
    b1 = Bar("BTC-USD", "1h", ts + 3600, 100.0, 150.0, 90.0, 140.0, 500.0)
    bars_1h.append(b1)
    sm.process_bars(bars_1h, bars_4h)
    assert sm.state == StateMachine.WAITING_RETEST

    monkeypatch.setattr(state_machine_module, "is_bullish_regime", lambda b: False)

    b2 = Bar("BTC-USD", "1h", ts + 7200, 140.0, 140.0, 80.0, 125.0, 50.0)
    bars_1h.append(b2)
    sm.process_bars(bars_1h, bars_4h)
    assert sm.state == StateMachine.IDLE


def test_restart_persistence(test_db, mock_indicators):
    sm = StateMachine()
    bars_4h = generate_bars(205, "4h")
    bars_1h = generate_bars(25, "1h", close_val=100.0, high_val=110.0)

    b1 = Bar("BTC-USD", "1h", bars_1h[-1].ts_open + 3600, 100.0, 150.0, 90.0, 140.0, 500.0)
    bars_1h.append(b1)
    sm.process_bars(bars_1h, bars_4h)

    assert sm.state == StateMachine.WAITING_RETEST
    setup_id = sm.setup_id

    sm2 = StateMachine()
    assert sm2.state == StateMachine.WAITING_RETEST
    assert sm2.setup_id == setup_id

    b2 = Bar("BTC-USD", "1h", bars_1h[-1].ts_open + 3600, 140.0, 140.0, 80.0, 125.0, 50.0)
    bars_1h.append(b2)
    sm2.process_bars(bars_1h, bars_4h)

    assert sm2.state == StateMachine.RETEST_CONFIRMED
