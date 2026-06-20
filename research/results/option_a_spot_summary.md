# Option A spot-only exploration (event-driven families)

Date: 2026-04-08

## What was reused

Kept the existing research harness intact and reused:
- `research/data.py` cached 1h Coinbase spot datasets
- `research/costs.py` friction model (0.5x / 1.0x / 1.5x)
- `research/backtest.py` no-lookahead ATR-based execution engine
- `research/walkforward.py` 180d train / 60d test / 60d step walk-forward
- `research/universe.py` frozen 5-asset pilot universe

## New hypotheses tested

All tests were **spot-only / long-only** and materially different from the dead EMA/RSI/Bollinger families.

1. **Failed Breakdown Rebound**
   - Idea: downside break of prior support that quickly reclaims should mean-revert upward.
2. **Volatility Shock Reversion**
   - Idea: outsized selloff + elevated volume + close off the lows may mark capitulation.
3. **Range Expansion Continuation**
   - Idea: breakout closes with expanding range/volume may continue in the same direction.

## 1.0x friction OOS results

| Family | Symbol | Trades | PF median | Expectancy median |
|---|---:|---:|---:|---:|
| volatility_shock_reversion | ETH-USD | 47 | 1.539 | 0.0070 |
| range_expansion_continuation | XRP-USD | 105 | 1.187 | 0.0026 |
| failed_breakdown_rebound | SOL-USD | 119 | 1.187 | 0.0023 |
| failed_breakdown_rebound | ETH-USD | 111 | 1.262 | 0.0022 |
| volatility_shock_reversion | SOL-USD | 90 | 0.914 | -0.0012 |
| failed_breakdown_rebound | BTC-USD | 103 | 0.653 | -0.0028 |
| volatility_shock_reversion | XRP-USD | 102 | 0.759 | -0.0031 |
| range_expansion_continuation | DOGE-USD | 135 | 0.813 | -0.0034 |
| failed_breakdown_rebound | XRP-USD | 115 | 0.843 | -0.0034 |
| failed_breakdown_rebound | DOGE-USD | 158 | 0.764 | -0.0045 |
| range_expansion_continuation | BTC-USD | 117 | 0.411 | -0.0050 |
| volatility_shock_reversion | BTC-USD | 47 | 0.283 | -0.0067 |
| range_expansion_continuation | SOL-USD | 119 | 0.497 | -0.0078 |
| range_expansion_continuation | ETH-USD | 99 | 0.439 | -0.0080 |
| volatility_shock_reversion | DOGE-USD | 49 | 0.263 | -0.0123 |

## Robustness notes

- **Failed Breakdown Rebound** was the only family with **two positive symbols at 1.0x** (`ETH-USD`, `SOL-USD`).
- But at **1.5x friction**, only `SOL-USD` stayed barely positive (`PF 1.016`, expectancy `0.0002`), which is too thin to call real.
- **Volatility Shock Reversion** looked strongest on **ETH-USD** and stayed positive at **1.5x** (`PF 1.806`, expectancy `0.0054`) — but this is still **single-asset confinement** with only **41 OOS trades**.
- **Range Expansion Continuation** does not look robust. It only worked on `XRP-USD` at 1.0x and died at 1.5x.

## Bottom line

No family is clean enough yet to claim a viable spot-only Option A survivor.

- **Kill next:**
  - `range_expansion_continuation` as currently defined
  - broad, all-asset versions of `volatility_shock_reversion`
- **Iterate next:**
  - `failed_breakdown_rebound` with stricter regime/context filters
  - `volatility_shock_reversion` on ETH-like assets only, but only if we can add a principled regime filter instead of curve-fitting thresholds

## Files changed

- `research/rules.py`
- `research/option_a_spot.py`
- `research/tests/test_backtest.py`
- `research/results/option_a_spot_results.json`
- `research/results/option_a_spot_summary.md`
