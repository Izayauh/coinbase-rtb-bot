import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from state_machine import StateMachine
import state_machine
state_machine.is_bullish_regime = lambda b: True

from strategy import Indicators
Indicators.calc_rsi = lambda c, p=14: [65.0]*len(c)
Indicators.calc_atr = lambda b, p=14: [100.0]*len(b)

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

breakout_bar = Bar("BTC", "1h", bars_1h[-1].ts_open + 3600, 100.0, 150.0, 90.0, 140.0, 500.0)
new_1h = bars_1h + [breakout_bar]

print("Last 1h TS before:", sm.last_1h_ts)
print("New 1h TS:", new_1h[-1].ts_open)
print("Last 4h TS before:", sm.last_4h_ts)
print("New 4h TS:", bars_4h[-1].ts_open)

def mocked_process_bars(self, bars_1h, bars_4h):
    latest_1h = bars_1h[-1]
    latest_4h = bars_4h[-1]
    
    if self.last_1h_ts:
            gap_1h = latest_1h.ts_open - self.last_1h_ts
            if gap_1h <= 0:
                print("RET 1", gap_1h)
                return 
            elif gap_1h > 3600:
                print("RET 2")
                self.state = self.DISABLED
                return
    
    if self.last_4h_ts:
            gap_4h = latest_4h.ts_open - self.last_4h_ts
            if gap_4h < 0:
                print("RET 3")
                return
            elif gap_4h > 14400:
                print("RET 4")
                self.state = self.DISABLED
                return
    print("MADE IT")
    self.last_1h_ts = latest_1h.ts_open
    self.last_4h_ts = latest_4h.ts_open
    self._eval_breakout(bars_1h, bars_4h)

mocked_process_bars(sm, new_1h, bars_4h)
print("State is:", sm.state)
