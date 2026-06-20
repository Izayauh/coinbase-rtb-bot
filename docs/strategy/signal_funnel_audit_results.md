# Signal Funnel Audit Results — BTC-USD 365d

> **Date**: 2026-04-04  
> **Data range**: 2025-04-04 to 2026-04-04  
> **Raw bars**: 8,755 1h → 2,190 4h  
> **Evaluation bars** (after warmup): 7,942

---

## 1. Funnel Hard Counts — Baseline (Current Strategy)

```
Stage                               Count    % of Total    % Survival
───────────────────────────────────────────────────────────────────────
1.  Total evaluation bars           7,942     100.000%         —
2.  Bullish regime pass             2,331      29.3%         29.3%
3.  Close > 20-bar high               116       1.46%         4.98% of regime
4.  Volume > 1.25× avg                 84       1.06%        72.4% of breakouts
5.  Close pct ≥ 0.70                   56       0.71%        66.7% of volume
6.  RSI in [56, 74]                    37       0.47%        66.1% of close-pct
7.  WAITING_RETEST transitions         37       0.47%           —
8.  RETEST_CONFIRMED transitions       22       0.28%        59.5% of waiting
9.  Continuation confirmed              1       0.013%        4.5% of retests
10. Trades executed                     1       0.013%           —
```

**The strategy produced exactly 1 trade in 365 days. That trade was a trailing stop loss for -0.195%.**

---

## 2. Where the Filter Kills Candidates

### Stage 2: Regime Filter (7,289 IDLE evaluations → 2,331 pass = 32.0%)

The regime filter eliminates 68% of all bars. Breakdown of the 4,958 rejections:

| Sub-condition | Fail count | Note |
|---------------|-----------|------|
| Close below EMA-200 | 4,442 | Most common — BTC spent ~60% of the year below its 200-period 4h EMA |
| EMA-50 below EMA-200 | 4,278 | Death cross condition — heavily correlated with above |
| EMA-200 slope negative | 4,442 | Essentially the same bars as close-below-200 |
| ATR/price < 0.5% | 0 | **Never triggers** — BTC always has sufficient volatility |

**Verdict**: The regime filter is functioning correctly. BTC-USD was in a bullish regime ~32% of the year, which is reasonable given the market topped near $130K in late 2025 and dropped to ~$70K by early 2026. **Not the bottleneck.**

### Stage 3: Close > 20-bar High (2,331 → 116 = 4.98%)

This is the first massive drop: 95% of bullish-regime bars do NOT set a new 20-hour high. This is mathematically expected — new highs are inherently rare events. **Not a bug, but this is the single largest filter by absolute kill count.**

### Stage 4-6: Volume, Close Percentile, RSI (116 → 37)

| Filter | Input | Output | Kill rate | Killed |
|--------|-------|--------|-----------|--------|
| Volume > 1.25× | 116 | 84 | 27.6% | 32 |
| Close pct ≥ 0.70 | 84 | 56 | 33.3% | 28 |
| RSI in [56, 74] | 56 | 37 | 33.9% | 19 |

Each filter independently kills about 1/3 of surviving candidates. Together they reduce 116 to 37 — a 68% compound kill rate. Individually reasonable. **But the RSI filter is the most concerning** because RSI naturally runs >74 during strong breakouts (the exact bars we're trying to catch).

**RSI distribution at breakout candidates (n=116):**
- 33 candidates (28.4%) have RSI ≥ 74 and are killed by the RSI filter
- 6 candidates (5.2%) have RSI < 56 and are killed
- Only 77/116 (66.4%) have RSI in the [56, 74] window

### Stage 7-8: Retest (37 → 22 RETEST_CONFIRMED)

Of 37 WAITING_RETEST events, over 131 evaluation bars:
- **15 expired** (window > 5 bars without retest)
- **83 zone misses** (bar low didn't fall into the retest zone)
- **7 close below breakout level** (retested but didn't close above)
- **4 close below midpoint**
- **22 confirmed** (59.5% success rate)

The 5-bar window is reasonably tight, but 59.5% is actually a decent conversion rate. The zone-miss count (83) is high because most bars after a breakout stay elevated.

### Stage 9: Continuation (22 → 1) ← **THE BOTTLENECK**

This is where the strategy collapses:

| Reason | Count |
|--------|-------|
| Close ≤ retest bar high | **496** |
| Chase too far (> 0.8 ATR) | 17 |
| Regime loss during wait | 4 |
| Same bar skip | 0 |
| **Actual entries** | **1** |

**518 evaluation bars after 22 RETEST_CONFIRMED events produced exactly 1 entry.** 

The problem: After RETEST_CONFIRMED, the strategy waits for `close > retest_bar.high`. But it stays in RETEST_CONFIRMED state indefinitely — there's no expiry window. So the engine evaluates hundreds of subsequent bars per retest, and most are at or below the retest bar's high. When price finally does exceed it, the chase filter (0.8 ATR) often kills it because price has drifted too far from the breakout level.

**This is the root cause.** The RETEST_CONFIRMED → Entry transition has an effective conversion rate of 1/22 = 4.5%.

---

## 3. Diagnostic Experiment Results

```
Metric                    BASELINE  NO_RSI  WIDE_RSI  NO_VOL  WIDE_RETEST  WIDE_CHASE  ALL_RELAXED
─────────────────────────────────────────────────────────────────────────────────────────────────────
WAITING_RETEST                  37      51       49      47          35          38           56
RETEST_CONFIRMED                22      25       24      29          24          21           32
Entries                          1       1        1       3           1           5           14
Trades                           1       1        1       3           1           5           14
Final equity              $9,994  $9,994  $9,994  $9,958    $9,994     $10,005      $10,003
Win rate                     0/1     0/1     0/1     0/3       0/1        3/5          7/14
Avg PnL%                  -0.20%  -0.20%  -0.20%  -0.79%    -0.20%     +0.25%       +0.09%
```

### Key Findings

1. **Removing the RSI filter (NO_RSI)**: WAITING_RETEST goes from 37 → 51, but trades still = 1. **RSI is not the bottleneck.** The continuation stage still kills everything.

2. **Removing the volume filter (NO_VOL)**: WAITING_RETEST → 47, trades = 3, but all 3 lose (-0.79% avg). Removing volume lets in lower-quality breakouts that fail.

3. **Widening the retest window (WIDE_RETEST, 5→10 bars)**: Still only 1 trade. The retest window is not the bottleneck either.

4. **Widening the chase limit (WIDE_CHASE, 0.8→1.5 ATR)**: **This is the most effective single change.** Trades go from 1 → 5, with a 3/5 win rate and +0.25% avg PnL. The chase filter was killing 17 continuation attempts.

5. **ALL_RELAXED (all filters eased)**: 14 trades, 7/14 win rate (50%), +0.09% PnL. **This is the first parameterization that even approaches viability**, though 14 trades is still below the 30-trade minimum floor.

---

## 4. Root-Cause Diagnosis

### Primary bottleneck: The continuation stage

The strategy enters RETEST_CONFIRMED 22 times but only converts 1 into a trade. The reasons:

1. **No expiry on RETEST_CONFIRMED**: Unlike WAITING_RETEST (which has a 5-bar window), RETEST_CONFIRMED has no timeout. The engine stays in this state indefinitely, evaluating hundreds of bars for a continuation that often never comes in the narrow form required.

2. **The chase filter (0.8 ATR) is too tight**: When price does eventually exceed the retest bar's high, it's often already > 0.8 ATR above the breakout level. This made sense conceptually (don't chase) but 0.8 ATR on BTC-USD at 1h resolution is a very small window — roughly $800-$2000 depending on current ATR.

3. **Structural contradiction confirmed**: The strategy requires a breakout (price moving up decisively), followed by a retest (price pulling back), followed by a continuation (price moving up again past the retest high) — BUT the continuation can't exceed 0.8 ATR above the original breakout level. On BTC-USD, this means the continuation must happen in a window of roughly $800-$2000, which is extremely narrow for an asset with average hourly ranges of $500-$1500.

### Secondary issue: Filter over-stacking

Even before the continuation bottleneck, the regime (32%) × price-break (5%) × volume/candle/RSI (32%) compound to only 37 WAITING_RETEST events per year. Each filter is individually defensible, but together they create a 0.47% pass rate from all evaluated bars.

### Not a bug (but a design flaw)

The code faithfully implements the documented strategy rules. The problem is the rules describe a price pattern that occurs ~1 time per year on BTC-USD at 1h resolution. **This is a frequency problem, not a correctness problem.**

---

## 5. Which Filter Kills the Most?

| Rank | Filter | Absolute kills | Impact |
|------|--------|---------------|--------|
| **1** | **Close ≤ retest high (continuation)** | 496 bars, 21/22 setups | **FATAL** — converts 22 retests into 1 entry |
| 2 | Close ≤ 20-bar high (price breakout) | 2,215 bars | Structural — new highs are inherently rare |
| 3 | Regime filter composite | 4,958 bars | Appropriate — BTC was bearish for most of the period |
| 4 | Chase filter (0.8 ATR) | 17 attempts | Tight but secondary to the retest-high gate |
| 5 | RSI [56, 74] | 19 candidates | Minor — 66% of candidates pass anyway |
| 6 | Volume 1.25× | 32 candidates | Minor — 72% of candidates pass anyway |
| 7 | Close percentile 0.70 | 28 candidates | Minor — 67% of candidates pass anyway |

---

## 6. Answers to the Posed Questions

### Which filter is eliminating almost everything?

**The RETEST_CONFIRMED → Entry continuation gate.** It has a 4.5% conversion rate (1/22). The secondary issue is the price-breakout gate (95% kill rate), but that's structural and unavoidable for a breakout strategy.

### Is the strategy merely too selective, or is there a likely logic bug?

**Both.** 

- **Too selective**: The compound filter chain passes 0.47% of bullish-regime bars to WAITING_RETEST, then 59% to RETEST_CONFIRMED, then only 4.5% to Entry.
- **Logic issue**: RETEST_CONFIRMED has no expiry window, so the engine can stay in that state for hundreds of bars, almost never meeting the `close > retest_bar.high AND (close - breakout_level) ≤ 0.8 * ATR` condition because the two constraints are contradictory when time elapses.

### Are the thresholds reasonable for BTC-USD over the tested year?

- **Regime filter**: Yes — 32% bullish is reasonable for a year that included a major drawdown
- **RSI [56, 74]**: Marginally tight — 66% of breakout candidates are in-range, which is acceptable
- **Volume 1.25×**: Reasonable — 72% of breakout candidates pass
- **Close pct 0.70**: Reasonable — 67% pass
- **5-bar retest window**: Reasonable — 59% convert to confirmed
- **0.8 ATR chase limit**: **Too tight** — this is the #1 problem parameter
- **No continuation expiry**: **Design gap** — should have a window like the retest stage

### What are the 2-3 most defensible changes to test next?

1. **Add a continuation expiry window (3-5 bars after RETEST_CONFIRMED)**: This prevents the engine from staying in RETEST_CONFIRMED indefinitely, which creates the 496-bar close-below-retest-high dead zone. Defensible because a genuine continuation pattern should manifest quickly.

2. **Widen the chase limit from 0.8 ATR to 1.5 ATR**: The experiment shows this alone increases trades from 1 to 5 with a positive average PnL (+0.25%). Defensible because 0.8 ATR is only $800-$2000 on BTC-USD, which is unrealistically tight for an hourly strategy.

3. **Widen the RSI band to [50, 80]**: This recovers ~30% of breakout candidates that are currently killed. The RSI band's purpose (avoid overbought entries) is valid, but 74 is too low for a breakout strategy — genuine breakouts commonly push RSI to 75-80.

---

---

## 7. Phase 1b Results & Final Verdict

Phase 1b experiments were run combining all three recommended fixes. Results:

```
Experiment              Trades  WR    Avg PnL   PF    Meets 30-floor?
─────────────────────────────────────────────────────────────────────
BASELINE                   1   0/1   -0.20%    0      NO
CHASE+EXP3                 2   1/2   -0.06%   0.58    NO
CHASE+EXP5                 2   1/2   -0.06%   0.58    NO
FULL_FIX_3                 3   2/3   +0.19%   2.99    NO
FULL_FIX_5                 3   2/3   +0.19%   2.99    NO
FULL_FIX_MAX               3   2/3   +0.35%   4.62    NO
MAX_ADDR (all relaxed)    11   3/11  -0.23%   0.54    NO
```

### Final Verdict

**ARCHIVE the breakout-retest-continuation strategy.**

- **Best trade count**: 11 (MAX_ADDR) — 37% of the 30-trade minimum floor, with negative expectancy
- **Best expectancy**: +0.35% (FULL_FIX_MAX) — on only 3 trades, statistically meaningless
- **The pattern simply does not occur frequently enough** on BTC-USD at 1h resolution to be viable as a strategy, under ANY reasonable parameterization
- **Relaxing filters to increase count degrades quality**: the 11-trade variant has PF 0.54 (losing proposition)

### Next: VPMR Strategy

Per the established research plan, evaluate the Volume Profile Mean Reversion (VPMR) strategy from the Research Pack using the same audit methodology and validation pipeline.
