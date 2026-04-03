import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from state_machine import StateMachine
import state_machine
state_machine.is_bullish_regime = lambda b: True
state_machine.Indicators.calc_rsi = lambda c, p=14: [65.0]*len(c)
state_machine.Indicators.calc_atr = lambda b, p=14: [100.0]*len(b)

from models import Bar
def generate_bars(count, timeframe="1h", start_ts=0, close_val=100.0, high_val=110.0, low_val=90.0, vol=50.0):
    bars = []
    interval = 3600 if timeframe == "1h" else 14400
    for i in range(count):
        bars.append(Bar(
            symbol="BTC-USD", timeframe=timeframe,
            ts_open=start_ts + i*interval,
            open=100.0, high=high_val, low=low_val, close=close_val, volume=vol
        ))
    return bars

sm = StateMachine()
bars_4h = generate_bars(205, "4h")
bars_1h = generate_bars(25, "1h", close_val=100.0, high_val=110.0)
sm.process_bars(bars_1h, bars_4h)
print("Initial:", sm.state)

breakout_bar = Bar("BTC", "1h", bars_1h[-1].ts_open + 3600, 100.0, 150.0, 90.0, 140.0, 500.0)
new_1h = bars_1h + [breakout_bar]
print('Highest 20:', max([b.high for b in new_1h[-21:-1]]))
print('Avg vol:', sum([b.volume for b in new_1h[-21:-1]]) / 20)
print('Latest close:', new_1h[-1].close)
print('Latest Vol:', new_1h[-1].volume)
candle_range = new_1h[-1].high - new_1h[-1].low
print('Candle range:', candle_range)
close_percentile = (new_1h[-1].close - new_1h[-1].low) / candle_range
print('Percentile:', close_percentile)
sm.process_bars(new_1h, bars_4h)
print("After breakout:", sm.state)

# Retest
retest_bar = Bar("BTC", "1h", new_1h[-1].ts_open + 3600, 140.0, 140.0, 80.0, 115.0, 50.0)
new_1h_2 = new_1h + [retest_bar]
sm.process_bars(new_1h_2, bars_4h)
print("After retest:", sm.state)
