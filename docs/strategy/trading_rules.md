# Strategy Rules

This maps the breakout-retest-continuation strategy strictly for BTC-USD.

## 1. Regime Filter
Executed securely via mathematical arrays properly matching Wilder's smoothing logic:
* 4h Close > 4h 200 EMA
* 4h 50 EMA > 4h 200 EMA
* 4h 200 EMA slope positive (current > prev)
* 4h ATR(14) / Close > Volatility threshold (0.005)

## 2. Breakout Setup
Evaluated exclusively inside the 1h timescale:
* The finalizing 1h candle closes strictly above the highest high of the prior 20 1h candles.
* Volume strictly exceeds 1.25 * simple moving average of the last 20 1h volume bounds.
* The closing body rests firmly inside the top 30% of its total range constraints.
* 1h RSI(14) operates between 56 and 74.

## 3. Bounded Retest
Must cleanly manifest within the ensuing 5 1h candles:
* The retracing low pierces accurately inside an explicitly mapped bounded parameter: (Breakout Level - 0.5 * ATR(14)) through (Breakout Level + 0.3 * Breakout Height).
* Closes back safely above the defined Breakout Level.
* Closes back safely above the midway-point of the defining Breakout candle.

## 4. Continuation Confirmation
Requires distinct candle autonomy from the localized Retest:
* A finalizing 1h candle closes explicitly above the highest point of the formalized Retest candle.
* Capping explicit velocity chases limits the confirmation close to within `0.8 * ATR(14)` above the initial Breakout Level. 

## No-Trade Checks
- A failure inside Retest bounds explicitly reverts the system to `IDLE`.
- A violation in Bullish regimes forcibly resets setups independently to `IDLE`.
- The `COOLDOWN` constraint is designated safely exclusively for explicit physical limits dictated externally (daily loss caps hit, Stop logic disconnected structurally).
