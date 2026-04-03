import pytest
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from models import Bar
from state_machine import StateMachine
import strategy

@pytest.fixture
def temp_db(monkeypatch):
    test_path = "test_sm_journal.db"
    if os.path.exists(test_path):
        os.remove(test_path)
    from db import Database
    import db as global_db
    import journal
    import state_machine
    test_db = Database(test_path)
    monkeypatch.setattr(global_db, 'db', test_db)
    monkeypatch.setattr(journal, 'db', test_db)
    monkeypatch.setattr(state_machine, 'db', test_db)
    yield test_db
    if os.path.exists(test_path):
        os.remove(test_path)

@pytest.fixture
def mock_indicators(monkeypatch):
    import state_machine
    monkeypatch.setattr(state_machine, 'is_bullish_regime', lambda b: True)
    monkeypatch.setattr(state_machine.Indicators, 'calc_rsi', lambda c, p=14: [65.0]*len(c))
    monkeypatch.setattr(state_machine.Indicators, 'calc_atr', lambda b, p=14: [100.0]*len(b))

def generate_bars(count, timeframe="1h", start_ts=0, close_val=1000.0, high_val=1050.0, low_val=950.0, vol=50.0):
    bars = []
    interval = 3600 if timeframe == "1h" else 14400
    for i in range(count):
        bars.append(Bar(
            symbol="BTC-USD", timeframe=timeframe,
            ts_open=start_ts + i*interval,
            open=1000.0, high=high_val, low=low_val, close=close_val, volume=vol
        ))
    return bars

def test_unchanged_4h_context_advances_1h(temp_db, mock_indicators):
    sm = StateMachine()
    # Provide 205 4h bars and 25 1h bars
    bars_4h = generate_bars(205, "4h")
    # Base 1h history (low values so we can force a breakout easily)
    bars_1h = generate_bars(25, "1h", close_val=100.0, high_val=110.0) 
    
    # Process initial safely
    sm.process_bars(bars_1h, bars_4h)
    assert sm.state == StateMachine.IDLE
    
    # Next 1h tick (breakout!). The 4h bar stays exactly the same, but 1h ticks forward.
    breakout_bar = Bar("BTC", "1h", bars_1h[-1].ts_open + 3600, 100.0, 150.0, 90.0, 140.0, 500.0)
    new_1h = bars_1h + [breakout_bar]
    
    sm.process_bars(new_1h, bars_4h)
    assert sm.state == StateMachine.WAITING_RETEST

def test_repeated_processing_is_idempotent(temp_db, mock_indicators):
    sm = StateMachine()
    bars_4h = generate_bars(205, "4h")
    bars_1h = generate_bars(25, "1h", close_val=100.0, high_val=110.0)
    
    sm.process_bars(bars_1h, bars_4h)
    assert sm.state == StateMachine.IDLE
    
    breakout_bar = Bar("BTC", "1h", bars_1h[-1].ts_open + 3600, 100.0, 150.0, 90.0, 140.0, 500.0)
    new_1h = bars_1h + [breakout_bar]
    
    sm.process_bars(new_1h, bars_4h)
    assert sm.state == StateMachine.WAITING_RETEST
    
    # Re-pass identical data
    sm.process_bars(new_1h, bars_4h)
    assert sm.state == StateMachine.WAITING_RETEST
    # Re-pass identical data 5 times, ensure it doesn't decay the retest timeout counter wrongly
    sm.process_bars(new_1h, bars_4h)
    assert sm.bars_since_breakout == 0

def test_out_of_order_bars_ignored(temp_db, mock_indicators):
    sm = StateMachine()
    bars_4h = generate_bars(205, "4h")
    bars_1h = generate_bars(25, "1h")
    sm.process_bars(bars_1h, bars_4h)
    
    # older 1h bar 
    old_bar = Bar("BTC", "1h", bars_1h[-1].ts_open - 3600, 100, 100, 100, 100, 5)
    sm.process_bars(bars_1h[:-1] + [old_bar], bars_4h)
    assert sm.state == StateMachine.IDLE
    assert sm.last_1h_ts == bars_1h[-1].ts_open # Remains unchanged

def test_four_hour_gap_disables(temp_db, mock_indicators):
    sm = StateMachine()
    bars_4h = generate_bars(205, "4h")
    bars_1h = generate_bars(25, "1h")
    sm.process_bars(bars_1h, bars_4h)
    
    gapped_4h = Bar("B", "4h", bars_4h[-1].ts_open + 14400*2, 1, 1, 1, 1, 1)
    sm.process_bars(bars_1h + [Bar("B", "1h", bars_1h[-1].ts_open+3600,1,1,1,1,1)], bars_4h + [gapped_4h])
    assert sm.state == StateMachine.DISABLED

def test_full_lifecycle_single_signal(temp_db, mock_indicators):
    sm = StateMachine()
    bars_4h = generate_bars(205, "4h")
    bars_1h = generate_bars(25, "1h", close_val=100.0, high_val=110.0) 
    
    sm.process_bars(bars_1h, bars_4h)
    
    # 1. Breakout (Level = 110)
    ts = bars_1h[-1].ts_open
    b1 = Bar("B", "1h", ts + 3600, 100.0, 150.0, 90.0, 140.0, 500.0)
    bars_1h.append(b1)
    sm.process_bars(bars_1h, bars_4h)
    assert sm.state == StateMachine.WAITING_RETEST
    
    # 2. Retest (Requires close > 110, low between ~60 and 130). We pass low 80, close 115.
    b2 = Bar("B", "1h", ts + 7200, 140.0, 140.0, 80.0, 115.0, 50.0)
    bars_1h.append(b2)
    sm.process_bars(bars_1h, bars_4h)
    assert sm.state == StateMachine.RETEST_CONFIRMED
    
    # 3. Continuation (close > 140, without chasing > 110+80=190). Close 150.
    b3 = Bar("B", "1h", ts + 10800, 115.0, 160.0, 115.0, 150.0, 50.0)
    bars_1h.append(b3)
    sm.process_bars(bars_1h, bars_4h)
    assert sm.state == StateMachine.SIGNAL_EMITTED
    
    # 4. Same bar re-passed -> Stays SIGNAL_EMITTED. NOT re-evaluating. No duplicate signal!
    sm.process_bars(bars_1h, bars_4h)
    assert sm.state == StateMachine.SIGNAL_EMITTED
    
    # 5. NEXT bar cleanly transitions and resets to IDLE, natively looking for new setups!
    b4 = Bar("B", "1h", ts + 14400, 150.0, 155.0, 140.0, 145.0, 50.0)
    bars_1h.append(b4)
    sm.process_bars(bars_1h, bars_4h)
    assert sm.state == StateMachine.IDLE
