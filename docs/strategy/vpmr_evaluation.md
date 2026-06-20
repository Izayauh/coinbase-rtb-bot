# VPMR Strategy Evaluation

> **Date**: 2026-04-04  
> **Asset**: BTC-USD  
> **Period**: 365 days (2025-04-04 to 2026-04-04)  
> **Data**: 8,731 1h bars → 2,190 4h bars  
> **Status**: Under evaluation — one diagnostic cycle permitted

---

## 1. Strategy Definition

### What Is VPMR?

**Volume Profile Mean Reversion** fades price at the boundaries of a rolling daily Value Area and takes profit at the Point of Control (POC). It only operates in range-bound markets (ADX < 25).

### Why VPMR Is Expected to Have Higher Frequency Than Breakout-Retest

| Property | Breakout-Retest | VPMR |
|----------|----------------|------|
| Signal type | Sequential multi-stage gate (breakout → retest → continuation) | Single-condition entry at VA boundary |
| Direction | Long only | Long AND short |
| Regime filter | 4-way AND (EMA cross + slope + vol) | Single ADX threshold |
| Pattern frequency | ~22 setups/year, 1-11 entries | Hundreds of setups/year, 149+ entries |
| Market condition | Trending/breakout only | Range-bound only (which is the majority of time) |

**Core insight**: Markets spend more time ranging than trending. A mean reversion strategy at established volume nodes should fire orders of magnitude more frequently than a breakout strategy requiring a specific sequential price pattern.

### Entry Rules

| Rule | Long | Short |
|------|------|-------|
| **Volume Profile** | Rolling 24-bar (1-day) fixed-range profile. Calculate POC (highest-volume price), VAH, VAL (70% value area). | Same |
| **Proximity** | `close <= VAL * (1 + 0.1%)` | `close >= VAH * (1 - 0.1%)` |
| **Regime** | 4h ADX(14) < 25 (range-bound market) | Same |
| **RSI** | 1h RSI(14) < 30 (oversold) | 1h RSI(14) > 70 (overbought) |

### Exit Rules

| Exit Type | Rule |
|-----------|------|
| **Take Profit** | Limit order at POC (mean reversion target) |
| **Stop Loss** | 1.5 x ATR(14) from entry price |
| **Time Stop** | 12 bars (12 hours) if neither TP nor SL hit |

### Risk and Execution

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Risk per trade | 1.5% of equity | Research Pack recommendation for maker-order strategies |
| Slippage | 3 bps | Maker orders = lower slippage than taker |
| Fee | 4 bps per side | Coinbase maker fee tier |
| Position sizing | Fixed fractional: `equity * 0.015 / (entry - stop)` | ATR-normalized |
| Max daily loss | 3.0% (not yet enforced in backtest) | Research Pack requirement |

---

## 2. Funnel Audit (Baseline)

```
Stage                             Count    % of Total
-----------------------------------------------------
1. Total eval bars                8,731     100.0%
2. Valid volume profile           5,528      63.3%
3. ADX < 25 (ranging)             3,629      41.6%
4. Near VA boundary               1,935      22.2%
5. RSI confirmed                    149       1.7%
6. Entries                          149       1.7%
```

### Funnel Analysis

| Stage | Kill Rate | Comment |
|-------|-----------|---------|
| Volume Profile | 36.7% killed | Expected — need 24 bars of warmup + some bars fall in gaps |
| ADX regime | 34.3% killed | **Correct behavior** — filtering out trending periods |
| VA proximity | 46.7% killed | Expected — price inside VA most of the time |
| RSI | **92.3% killed** | **Primary bottleneck** — RSI <= 30 or >= 70 is rare |
| Entry execution | 0.7% killed | Minimal — bad stop geometry blocks very few |

> **The RSI filter is the single largest bottleneck**, requiring RSI <= 30 or >= 70 when price is already at VA boundary. This eliminates 92.3% of otherwise-qualifying setups.

---

## 3. Backtest Results — BASELINE

```
Trades executed:               149
Final equity:                  $6,816 (from $10,000)
Total return:                  -31.84%
Max drawdown:                  38.70%
Win rate:                      59/149 (39.6%)
Avg PnL%:                      -0.039%
Profit factor:                 0.92
Expectancy:                    -0.039%
Avg bars held:                 6.8
Long / Short:                  73 / 76
```

### Exit Breakdown

| Reason | Interpretation |
|--------|----------------|
| STOP_LOSS | Strategy enters near VA boundaries but price continues through = stop hit |
| TAKE_PROFIT | Some trades successfully revert to POC |
| TIME_STOP | 12-bar timeout expires before TP or SL |

---

## 4. Diagnostic Experiments

```
Experiment              Trades  WR         Avg PnL   PF     Max DD   Edge
-----------------------------------------------------------------------
BASELINE                 149    59/149     -0.039%   0.92   38.70%   NEG
A: RSI [35,65]           268   121/268     -0.002%   0.99   41.61%   NEG
B: RSI [40,60]           421   204/421     +0.014%   1.03   54.58%   POS
C: ADX < 30              202    76/202     -0.085%   0.84   51.31%   NEG
D: PROXIMITY 0.3%        150    60/150     -0.029%   0.94   38.70%   NEG
E: LONGS ONLY             73    26/73      -0.120%   0.78   31.31%   NEG
F: RELAXED               354   156/354     -0.026%   0.94   53.64%   NEG
G: MAX ADDRESSABLE       656   328/656     +0.018%   1.04   67.72%   POS
```

### Key Findings

1. **RSI is the primary lever.** Widening from [30, 70] to [40, 60]:
   - Trade count: 149 → 421 (2.8x)
   - Edge flips from -0.039% to **+0.014%** (marginally positive)
   - But drawdown explodes: 38.7% → **54.6%**

2. **ADX is not the bottleneck.** Raising ADX threshold from 25 to 30 adds trades (202) but *worsens* edge (-0.085%). Trending markets are correctly excluded.

3. **Proximity relaxation is irrelevant.** 0.1% to 0.3% adds only 1 more trade.

4. **Longs-only is worse than baseline.** 73 trades, -0.120% avg. The short side is not hurting — it is neutral.

5. **MAX_ADDR (RSI [40,60], ADX < 35, PROX 0.5%)**: 656 trades, +0.018% avg, PF 1.04. Massive trade count but the edge is fragile (0.018% is ~$1.80 per $10K trade before any additional friction).

---

## 5. Head-to-Head: VPMR vs. Archived Breakout-Retest

| Metric | Breakout-Retest (best) | VPMR Baseline | VPMR RSI[40,60] |
|--------|----------------------|---------------|------------------|
| **Trade count (365d)** | 14 (ALL_RELAXED) | **149** | **421** |
| **Meets 30-trade floor** | NO | **YES** | **YES** |
| **Win rate** | 50% | 39.6% | 48.5% |
| **Avg PnL%** | +0.09% | -0.039% | **+0.014%** |
| **Profit factor** | ~1.0 | 0.92 | **1.03** |
| **Max drawdown** | N/A (too few trades) | 38.70% | 54.58% |
| **Expectancy** | Unmeasurable | -0.039% | +0.014% |
| **Statistical testability** | NO (n=14) | **YES (n=149)** | **YES (n=421)** |
| **Complexity** | High (5 sequential gates) | **Low (single-gate)** | Same |

### VPMR Advantages
- **10x more trades** — actually testable and falsifiable
- **Simpler logic** — fewer moving parts = fewer failure modes
- **Bi-directional** — captures both long and short setups
- **Range-regime aligned** — designed for the market condition that dominates

### VPMR Problems
- **Negative or marginal edge** in every configuration
- **High drawdown** — 38-55% is unacceptable for a live strategy
- **RSI filter tradeoff** — strict RSI gives negative edge; relaxed RSI gives marginal edge with massive drawdown
- **The edge, even when positive, is ~0.01-0.02%** — this is within the noise floor

---

## 6. Blunt Verdict

### Trade Frequency: PASS

VPMR **crushes** the 30-trade floor. Even the strict baseline produces 149 trades. The relaxed RSI variant produces 421. This is the correct order of magnitude for a statistically testable strategy.

### Edge: MARGINAL / FAIL

- Baseline (RSI [30,70]): **negative edge** (-0.039%). FAIL.
- RSI [40,60]: **marginally positive** (+0.014%, PF 1.03). This is barely above zero — within noise.
- RSI [35,65]: **zero edge** (-0.002%, PF 0.99). Coin flip.

The strategy has no robust, fee-resistant edge in any tested configuration.

### Drawdown: FAIL

- Baseline: 38.7% max DD → exceeds the 20% disqualifying threshold
- RSI [40,60]: 54.6% max DD → catastrophic
- Even LONGS_ONLY: 31.3% → still over threshold

### Risk-Adjusted Return: FAIL

No configuration produces a positive Sharpe ratio. The strategy systematically destroys capital over 365 days in most configurations.

---

## 7. Recommended Diagnostic Cycle (ONE permitted)

The VPMR concept has the **frequency** the breakout strategy lacked, but it lacks **edge**. Before archiving, one diagnostic cycle should test whether the problem is structural or a matter of signal quality:

### Hypothesis to Test

The current VPMR enters too frequently at boundaries where price continues through the VA into a trend. The fix would be to add a **confirmation bar requirement** — instead of entering immediately when price touches VAL/VAH, wait for a reversal candle (close back inside VA on the next bar).

### Experiment to Run

| Variant | Change | Purpose |
|---------|--------|---------|
| **CONFIRM_1** | Require close[i] near boundary + close[i+1] back inside VA | Filter false boundary touches |
| **CONFIRM_RSI** | Same + RSI [35, 65] | Combine confirmation with moderate RSI |
| **TIGHTER_STOP** | Stop at 1.0x ATR instead of 1.5x | Limit loss per trade (accepts lower win rate) |
| **WIDER_TP** | TP at midpoint between POC and entry instead of POC | Closer target, higher fill probability |

### Decision Threshold

- If any variant produces **PF >= 1.2, avg PnL >= 0.05%, max DD < 25%, and >= 50 trades**: proceed to walk-forward
- If the best variant still has **PF < 1.1 or avg PnL < 0.03%**: archive VPMR, evaluate Dynamic EMA Crossover (Rank 2 from Research Pack)
- **Do not run more than one diagnostic cycle.**

---

## 8. Files

| File | Purpose |
|------|---------|
| `vpmr_backtest.py` | Complete VPMR backtest + funnel audit + diagnostic experiments |
| `docs/strategy/vpmr_evaluation.md` | This document |
| `docs/strategy/research_plan.md` | Updated with VPMR results when complete |
| `cache_BTC-USD_365d_1h.csv` | Shared cached data (reused from breakout audit) |

---

## 9. Comparison Summary

| Strategy | Frequency | Edge | Drawdown | Verdict |
|----------|-----------|------|----------|---------|
| Breakout-Retest-Continuation | 1-14 trades/year | Unmeasurable | N/A | **ARCHIVED** — insufficient frequency |
| VPMR (baseline, RSI 30/70) | 149 trades/year | -0.039% | 38.7% | **FAILING** — negative edge, high DD |
| VPMR (RSI 40/60) | 421 trades/year | +0.014% | 54.6% | **MARGINAL** — edge within noise, unacceptable DD |

**The VPMR strategy solves the frequency problem but introduces an edge problem.** One diagnostic cycle with confirmation bars is warranted before the concept is archived.
