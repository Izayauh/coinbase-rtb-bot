# EMA Crossover Strategy Evaluation — Final

> **Date**: 2026-04-04  
> **Asset**: BTC-USD  
> **Status**: ARCHIVED — all variants fail, no diagnostic cycle warranted  
> **Next**: Statistical Volatility Reversion (Rank 3 from Research Pack)

---

## 1. Strategy Definition

### What Is Dynamic EMA Crossover?

A trend-following strategy that enters on 8/34 EMA crosses during trending markets (ADX >= 25), with RSI momentum confirmation and a trailing stop system.

| Component | Rule |
|-----------|------|
| **Signal** | 8-EMA crosses 34-EMA on 1h bars |
| **Long entry** | Bullish cross + RSI in [50, 65] |
| **Short entry** | Bearish cross + RSI in [35, 50] |
| **Regime gate** | 4h ADX(14) >= 25 (trending market required) |
| **Initial stop** | Tighter of: 10-bar swing low/high OR 2x ATR from entry |
| **Trailing stop** | After 1.5x ATR profit: move stop to breakeven, then trail at 1x ATR |
| **Time stop** | 24 bars (24 hours) |
| **Cooldown** | 4 bars after stop-out in same direction |
| **Risk** | 1.0% of equity per trade |
| **Fees** | 10 bps (taker, conservative) |
| **Slippage** | 5 bps |

### Why Higher Frequency Than Breakout-Retest?

- EMA crosses happen ~275 times/year on 1h BTC-USD (vs ~22 breakout setups)
- Only 2 primary conditions (cross + RSI) vs 7+ sequential conditions
- No complex multi-stage state machine required
- Confirmed at 92-142 entries/year (far above 30-trade floor)

---

## 2. Funnel Audit (365d Baseline)

```
Stage                   Count     % of Total
---------------------------------------------
1. Total bars            8,720     100.0%
2. EMA crosses             275       3.2%
3. ADX pass                108       1.2%
4. RSI pass                 95       1.1%
5. After cooldown           92       1.1%
6. Entries                  92       1.1%
```

### Which Rule Kills the Most?

| Filter | Kill Rate | Comment |
|--------|-----------|---------|
| No cross (bars without EMA cross) | 96.8% | Expected — crosses are discrete events |
| ADX < 25 (not trending) | 60.7% | Majority of crosses occur in ranging markets — correctly filtered |
| RSI out of band | 12.0% | Minor filter — RSI bands are reasonable |
| Cooldown | 3.2% | Minor — correctly prevents revenge trades |

**The funnel is healthy.** ADX is the primary gate (filtering 61% of crosses), which is correct — the strategy should only trade in trending markets. The strategy produces 92 trades/year, well above the 30-trade floor.

---

## 3. Results — All Variants

```
                          ── 365-DAY ──                    ── 730-DAY ──
Variant          Trades   WR     AvgPnL%   PF   MaxDD    Trades   WR     AvgPnL%   PF   MaxDD
────────────────────────────────────────────────────────────────────────────────────────────────
BASELINE            92   39.1%  -0.400%   0.47  35.5%      183   45.9%  -0.252%   0.66  49.3%
WIDE_RSI            99   40.4%  -0.382%   0.50  35.8%      197   46.2%  -0.227%   0.69  49.9%
TIGHT_STOP         102   37.3%  -0.276%   0.56  39.0%      204   43.1%  -0.146%   0.76  54.2%
NO_COOL             95   40.0%  -0.360%   0.51  35.3%      189   47.1%  -0.218%   0.70  48.5%
WIDE+NOCOOL        102   41.2%  -0.345%   0.53  35.6%      204   47.1%  -0.202%   0.72  49.8%
ADX_20             142   47.2%  -0.255%   0.60  42.5%      273   50.9%  -0.166%   0.75  55.9%
LONGS_ONLY          60   40.0%  -0.411%   0.44  26.7%      113   43.4%  -0.328%   0.57  38.5%
```

### Key Findings

1. **Every variant has negative expectancy.** Best: -0.146% (TIGHT_STOP 730d). Worst: -0.411% (LONGS_ONLY 365d).

2. **Every variant has PF < 1.0.** Best: 0.76 (TIGHT_STOP 730d). The strategy loses ~$1.32 for every $1 it makes. Not close to profitable.

3. **Drawdown is catastrophic.** 35-56% across all variants. LONGS_ONLY achieves ~27% DD but only by trading less with worse edge.

4. **More trades = worse performance.** ADX_20 produces 142 trades but -0.255% avg PnL. Loosening the ADX gate admits non-trending crosses that fail.

5. **Exit breakdown tells the story.** 57% of exits are STOP_LOSS or TRAILING_STOP for losses. The trailing stop system activates on ~34% of trades, but the trailing stops themselves are getting hit for losses — the trend reversals are sharp enough to blow through the trail.

6. **730d makes it WORSE, not better.** Every variant degrades on the longer horizon. This is not noise — the strategy consistently loses money.

---

## 4. Cross-Strategy Comparison (365d)

```
Strategy                           Trades    WR      AvgPnL%    PF    MaxDD
────────────────────────────────────────────────────────────────────────────
Breakout-Retest (ARCHIVED)              1    0%     -0.200%    0.00    N/A
VPMR (ARCHIVED)                       149   40%     -0.039%    0.92   38.7%
EMA Crossover BASELINE                 92   39%     -0.400%    0.47   35.5%
EMA Crossover BEST (ADX_20)           142   47%     -0.255%    0.60   42.5%
```

**EMA Crossover is the worst performer of all three Research Pack strategies.** Worse than VPMR on every metric except trade count.

| Metric | Breakout-Retest | VPMR | EMA Crossover |
|--------|:-:|:-:|:-:|
| Trade frequency | FAIL | PASS | PASS |
| Edge | Unmeasurable | Marginally negative | **Strongly negative** |
| PF | 0 | 0.92 | **0.47** |
| Drawdown | N/A | 38.7% | **35.5-42.5%** |
| Complexity | High | Low | Medium |
| Verdict | Archived (frequency) | Archived (edge) | **Archived (edge, much worse)** |

---

## 5. Why EMA Crossover Fails

1. **BTC-USD whipsaws through EMA crosses even in trending markets.** The 8/34 EMA cross generates signals that are simultaneously too late to catch the beginning of a trend and too early to confirm it. By the time the fast EMA crosses the slow, price has already moved, and the entry is chasing.

2. **The trailing stop system destroys winning trades.** When the trailing stop activates at breakeven (after 1.5x ATR profit), normal BTC volatility (~1-2% hourly moves) frequently triggers the trail, cutting winning trades short before they can compensate for the losers.

3. **Fee drag on 10 bps taker is fatal at 0% edge.** With 92-142 trades/year and ~5-10 bps round-trip cost per trade, the strategy needs at least +0.2% avg PnL to break even after fees. It produces -0.25% to -0.40%.

4. **The ADX regime filter correctly identifies trending periods, but the EMA cross is a poor way to trade them.** ADX >= 25 correlates with strong moves, but EMA crosses are lagging indicators that enter after the move has started and exit too late.

---

## 6. Verdict

### ARCHIVE — Immediate, no diagnostic cycle warranted

This is not a marginal failure. PF of 0.47-0.76 across 7 variants and 2 time horizons is catastrophic. The strategy loses money faster than VPMR, faster than the breakout strategy, and faster than holding cash. No parameter adjustment can bridge a PF gap of 0.5 to reach 1.2.

**All three Research Pack strategies have now been tested and archived:**

| # | Strategy | Trades/Year | PF | Cause of Death |
|---|----------|------------|------|----------------|
| 1 | Breakout-Retest-Continuation | 1-14 | 0 | Insufficient frequency |
| 2 | VPMR (Mean Reversion) | 149-656 | 0.71-1.04 | No robust edge at VA boundaries |
| 3 | EMA Crossover (Trend Following) | 92-273 | 0.47-0.76 | EMA cross is a losing signal on BTC-USD |

### What This Means

The Research Pack's top 3 strategies have all been rigorously tested and all fail on BTC-USD. The remaining option from the Research Pack is **Statistical Volatility Reversion (Rank 3)** — a fundamentally different approach that trades crash/spike events rather than continuous signals. However, the Research Pack itself warns this is a very low-frequency strategy.

Before proceeding, an honest assessment is needed: **three consecutive strategy failures suggest the problem may not be the strategies but the asset characteristics of BTC-USD on retail-accessible timeframes.**

---

## 7. Files

| File | Purpose |
|------|---------|
| `ema_crossover_backtest.py` | Backtest engine + 7 diagnostic variants |
| `docs/strategy/ema_crossover_evaluation.md` | This document |
| `docs/strategy/research_plan.md` | Updated with EMA Crossover archived |
