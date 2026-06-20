# Strategy Research Plan & Backtesting Standards

> **Status**: The current strategy is **unvalidated**. One trade in 365 days is not a strategy — it is a rounding error. This document establishes what "validated" means and how to get there.

---

## Table of Contents

1. [Materials Inventory & Ranked Reading Order](#1-materials-inventory--ranked-reading-order)
2. [Key Takeaways to Extract Per Source](#2-key-takeaways-to-extract-per-source)
3. [Research Standards & Checklists](#3-research-standards--checklists)
4. [Next Engineering Tasks](#4-next-engineering-tasks)
5. [Blunt Conclusions](#5-blunt-conclusions)

---

## 1. Materials Inventory & Ranked Reading Order

### Available Materials

| # | File | Type | Size |
|---|------|------|------|
| A | Crypto Trading Strategy Research Pack.txt | Custom research synthesis | 52 KB |
| B | Kevin Davey — Building Winning Algorithmic Trading Systems | Book (PDF) | 6.3 MB |
| C | Ernie Chan — Quantitative Trading (2008) | Book (PDF) | 3.7 MB |
| D | Ernie Chan — Algorithmic Trading (2013) | Book (PDF) | 9.2 MB |
| E | Ernie Chan — Machine Trading (2017) | Book (PDF) | 1.5 MB |
| F | Ruppert & Matteson — Statistics and Data Analysis for Financial Engineering | Book (PDF) | 12 MB |
| G | Larry Harris — Trading and Exchanges (2003) | Book (PDF) | 666 KB |
| H | O'Hara — Market Microstructure Theory (1998) | Book (PDF) | 9.8 MB |
| I | O'Hara OCR version | Duplicate of H | 11.5 MB |
| J | Khandani & Lo — What Happened to the Quants in August 2007 | Paper (PDF) | 728 KB |

### Ranked Reading Order (for current stage)

#### Tier 1 — Read Immediately (this week)

| Priority | Source | Why Now |
|----------|--------|---------|
| **1** | **A — Research Pack** | Already read. Contains backtesting protocol (section 4), paper validation protocol (section 5), kill-switch criteria, minimum sample sizes (300 trades), Walk-Forward schedule, PBO methodology. This is the single most operationally relevant document. |
| **2** | **B — Davey: Building Winning Algorithmic Trading Systems** | The only book in the collection written specifically for individual algo traders building and validating systems from scratch. Covers: when to kill a strategy, minimum trade counts, walk-forward analysis mechanics, Monte Carlo validation, and the psychology of trusting backtests. **This is the #1 book for this stage.** |
| **3** | **C — Chan: Quantitative Trading (2008)** | Covers the full pipeline from idea to backtest to paper to live for a solo quant. Chapter focus: realistic backtesting, common pitfalls, transaction cost modeling, Sharpe ratio interpretation, and the "how do I know if this is real" question. |

#### Tier 2 — Read Next (within 2 weeks)

| Priority | Source | Why Now |
|----------|--------|---------|
| **4** | **D — Chan: Algorithmic Trading (2013)** | Deeper treatment of mean reversion vs. momentum classification, regime detection, Kalman filters, cointegration. Useful once the basic backtesting harness is credible. |
| **5** | **E — Chan: Machine Trading (2017)** | Covers machine learning for trading, but more importantly: strategy decay detection, live vs. backtest divergence measurement, and when to stop trading a strategy. Relevant for paper-to-live transition gates. |

#### Tier 3 — Defer (reference only, not urgent)

| Priority | Source | Why Now |
|----------|--------|---------|
| **6** | **F — Ruppert: Statistics for Financial Engineering** | Academic statistics reference. Useful for understanding specific tests (Sharpe significance, bootstrap methods) but too broad to read cover-to-cover now. Use as reference when implementing PBO or White's Reality Check. |
| **7** | **G — Harris: Trading and Exchanges** | Market microstructure overview. Relevant for understanding order book dynamics, but not the immediate bottleneck. Revisit when optimizing execution. |
| **8** | **H/I — O'Hara: Market Microstructure Theory** | Deep academic theory. Not useful until execution optimization phase. |
| **9** | **J — Khandani & Lo: Quant Meltdown 2007** | Historical case study of systematic strategy failure. Educationally valuable for understanding crowded trades and liquidity crises, but not actionable right now. |

### What's Missing From This Library

> **WARNING**: None of these books contain a worked example of diagnosing a signal funnel that produces near-zero trades. That is a strategy design problem, not a backtesting methodology problem. The books will tell you *how to validate* a strategy that generates enough trades — they will not fix a strategy that doesn't fire.

The immediate problem (1 trade in 365 days) is a **strategy design failure**, not a statistical validation failure. The books become useful *after* the strategy generates enough signals to measure.

---

## 2. Key Takeaways to Extract Per Source

### A — Research Pack (already read)

| Concept | Section | Directly Applicable |
|---------|---------|-------------------|
| Minimum 300 independent trades for statistical confidence | Section 4 Overfitting Checks | YES — We have 1. |
| Walk-Forward Analysis: 24-month in-sample, 6-month OOS, 12-month holdback | Section 4 Data Split | YES — Need to implement |
| Expectancy > 0.15% after fees to be viable | Section 4 Optimization Metrics | YES — We are negative |
| OOS Sharpe > 1.2, Sortino > 1.5, MAR > 1.0 | Section 4 Optimization Metrics | YES — Cannot measure with 1 trade |
| Parameter sensitivity: plus/minus 15% performance band | Section 4 Overfitting Checks | YES — Need to implement |
| White's Reality Check bootstrap | Section 4 Overfitting Checks | Defer — need trades first |
| Paper trading: 4 weeks minimum, 3 quantitative gates | Section 5 | Defer — need a passing backtest first |
| Kill-switch: SPRT, 10% drawdown, 2x friction breach | Section 5 | Already partially implemented |

### B — Davey (to extract)

| Concept | Expected Chapter | Why |
|---------|-----------------|-----|
| "When to abandon a strategy idea" framework | Early chapters | We need this **right now** |
| Minimum number of trades for significance | Walk-forward chapters | Cross-reference with Research Pack's 300-trade floor |
| Monte Carlo permutation testing | Validation chapters | Alternative to PBO for small sample sizes |
| Walk-forward mechanics (rolling vs. anchored) | Core methodology | Implementation guide |
| Out-of-sample degradation thresholds | Validation chapters | Define what "acceptable" OOS decay looks like |
| Curve-fitting red flags checklist | Throughout | Directly applicable to our 5-filter breakout chain |

### C — Chan: Quantitative Trading (to extract)

| Concept | Expected Chapter | Why |
|---------|-----------------|-----|
| "Does my strategy have a positive expected return?" | Early chapters | The fundamental question we cannot answer |
| Transaction cost modeling best practices | Backtesting chapters | Verify our 10bps + 5bps model is realistic for Coinbase |
| Sharpe ratio: what values are meaningful | Risk chapters | Establish whether our target Sharpe is realistic |
| Information ratio and signal decay | Performance chapters | Framework for measuring live vs. backtest divergence |
| Common backtesting pitfalls checklist | Backtesting chapters | Cross-reference against our current setup |

### D, E — Chan: Algorithmic Trading & Machine Trading (to extract later)

- Mean reversion vs. momentum regime classification
- Half-life of mean reversion (relevant if we switch strategy classes)
- Strategy decay detection framework
- Kalman filter for adaptive parameters

### F — Ruppert (reference only)

- Bootstrap confidence intervals (when implementing PBO)
- Time series stationarity tests (for regime filter validation)
- Distribution fitting for trade PnL

---

## 3. Research Standards & Checklists

### 3.1 Backtesting Standard

Every backtest run must satisfy ALL of the following before results are considered meaningful:

#### Data Integrity

| # | Requirement | Status |
|---|-------------|--------|
| D1 | Minimum 365 days of 1h bar data, deduplicated and gap-checked | Done |
| D2 | 4h bars aggregated from 1h (not independently fetched) | Done |
| D3 | No lookahead bias — indicators computed only on data available at decision time | Verified in backtest.py |
| D4 | Data must include at least 1 full bull regime AND 1 full bear/sideways regime | Verified — bullish May-Oct 2025 (32% of bars), bearish Nov 2025-Mar 2026 |

#### Execution Realism

| # | Requirement | Current | Standard |
|---|-------------|---------|----------|
| E1 | Fee model (round-trip) | 10 bps | Coinbase taker is ~12 bps RT. **Slightly optimistic.** |
| E2 | Slippage model | 5 bps entry | Acceptable for 1h bars on BTC-USD |
| E3 | Fill assumption | Close price + slippage | Acceptable — not assuming fills at limit |
| E4 | No partial fills modeled | Not modeled | Acceptable for 1h timeframe |

#### Statistical Minimums

| # | Requirement | Threshold | Current |
|---|-------------|-----------|---------|
| S1 | Minimum trade count | >= 30 (absolute floor) | **1 — FAIL** |
| S2 | Statistical confidence target | >= 100 trades (moderate confidence) | **1 — FAIL** |
| S3 | Research-grade confidence | >= 300 trades (Research Pack standard) | **1 — FAIL** |
| S4 | Minimum evaluation period | >= 365 days | Pass |
| S5 | Walk-forward validation | Required before any trust | Not implemented |
| S6 | Parameter sensitivity sweep | +/-20% on each param, < 15% perf change | Not implemented |

> **CRITICAL**: With 1 trade, NONE of the statistical standards can be evaluated. The strategy does not generate enough data to distinguish from random noise. This is the #1 problem.

#### Metric Thresholds (when trade count is sufficient)

| Metric | Minimum | Target | Disqualifying |
|--------|---------|--------|---------------|
| Trade count (365d) | >= 30 | >= 100 | < 30 |
| Win rate | >= 35% | >= 45% | < 25% |
| Profit factor | >= 1.2 | >= 1.5 | < 1.0 |
| Expectancy per trade | > 0 after fees | > 0.15% after fees | <= 0 |
| Sharpe (annualized, OOS) | >= 0.8 | >= 1.2 | < 0.5 |
| Max drawdown | < 15% | < 10% | > 20% |
| Avg bars held | 2-20 | 4-12 | > 50 or < 1 |

---

### 3.2 Paper Trading Go/No-Go Criteria

A strategy MUST pass ALL of these before entering paper trading:

| Gate | Requirement | Evidence Required |
|------|-------------|-------------------|
| P1 | Backtest produces >= 30 trades over 365 days | Backtest report showing trade count |
| P2 | Positive expectancy after realistic fees (>= 0.10%) | Backtest report |
| P3 | Profit factor >= 1.1 | Backtest report |
| P4 | Win rate >= 30% | Backtest report |
| P5 | Max drawdown < 20% | Equity curve analysis |
| P6 | Parameter sensitivity sweep shows < 25% degradation at +/-20% on each param | Sensitivity report |
| P7 | Walk-forward OOS performance >= 60% of in-sample performance | Walk-forward report |
| P8 | Strategy logic has been independently reviewed (not just the author) | Code review artifact |

**Current status: FAIL on P1 through P7. Cannot evaluate P8.**

---

### 3.3 Live Trading Go/No-Go Criteria

A strategy MUST pass ALL of these before receiving real capital:

| Gate | Requirement | Evidence Required |
|------|-------------|-------------------|
| L1 | All Paper Trading gates (P1-P8) passed | Paper gate report |
| L2 | >= 4 weeks of paper trading completed | Paper trading log |
| L3 | Paper trading daily returns correlate >= 0.7 with backtest projection | Statistical comparison |
| L4 | Realized slippage <= 1.5x modeled slippage | Execution log analysis |
| L5 | Zero critical infrastructure failures in trailing 14 days | System log audit |
| L6 | Kill-switch tested and confirmed functional | Kill-switch test report |
| L7 | Position sizing validated at minimum capital (e.g., $10 notional) | First-trade verification |
| L8 | Operator has explicitly signed off after reviewing all evidence | Written sign-off |

**Current status: BLOCKED by Paper Trading gates. Cannot evaluate.**

---

## 4. Next Engineering Tasks

### Phase 0: Signal Funnel Audit — COMPLETED 2026-04-04

> Full results: [signal_funnel_audit_results.md](signal_funnel_audit_results.md)

**Data**: 8,755 1h bars (2025-04-04 to 2026-04-04) → 2,190 4h bars → 7,942 evaluated bars.

#### Actual funnel (hard counts):

```
Stage                               Count    % of Total
───────────────────────────────────────────────────────────
1.  Total evaluation bars           7,942     100.0%
2.  Bullish regime pass             2,331      29.3%
3.  Close > 20-bar high               116       1.46%
4.  Volume > 1.25x avg                 84       1.06%
5.  Close pct >= 0.70                   56       0.71%
6.  RSI in [56, 74]                    37       0.47%
7.  WAITING_RETEST transitions         37       0.47%
8.  RETEST_CONFIRMED transitions       22       0.28%
9.  Continuation confirmed              1       0.013%
10. Trades executed                     1       0.013%
```

#### Pre-audit predictions vs. reality:

| Prediction | Reality |
|-----------|---------|
| "RSI is the single largest killer" | **WRONG.** RSI passes 66% of candidates (77/116). It kills only 19 bars. |
| "Volume filter creates a contradiction" | **PARTIALLY WRONG.** Volume passes 72% of breakout candidates (84/116). Minor filter. |
| "Compound selectivity is the problem" | **PARTIALLY RIGHT.** The breakout filters compound to 0.47%, but the real bottleneck is downstream. |
| "Final signals per year: 0.1-10" | **CORRECT.** Actual: 1. |

#### Actual root cause:

**The RETEST_CONFIRMED → Entry continuation stage has a 4.5% conversion rate (1/22).** This is the fatal bottleneck. After 22 confirmed retests, 496 bars fail the `close > retest_bar.high` test, and 17 attempts are killed by the 0.8 ATR chase filter. The continuation stage has no expiry window, so the engine lingers in RETEST_CONFIRMED for hundreds of bars, almost never finding a bar that is both above the retest high AND within 0.8 ATR of the breakout level.

### Phase 1: Diagnostic Experiments — COMPLETED 2026-04-04

All 6 experiments from the plan were run:

```
Metric                    BASELINE  NO_RSI  WIDE_RSI  NO_VOL  WIDE_RETEST  WIDE_CHASE  ALL_RELAXED
─────────────────────────────────────────────────────────────────────────────────────────────────────
WAITING_RETEST                  37      51       49      47          35          38           56
RETEST_CONFIRMED                22      25       24      29          24          21           32
Entries                          1       1        1       3           1           5           14
Trades                           1       1        1       3           1           5           14
Win rate                       0/1     0/1     0/1     0/3         0/1        3/5          7/14
Avg PnL%                    -0.20%  -0.20%  -0.20%  -0.79%      -0.20%     +0.25%       +0.09%
```

**Key findings:**
- Removing RSI or widening RSI: still 1 trade. **RSI is not the bottleneck.**
- Removing volume filter: 3 trades, all losers. Worse performance.
- Widening retest window (5→10 bars): still 1 trade. **Retest window is not the bottleneck.**
- **Widening chase limit (0.8→1.5 ATR): 5 trades, 3/5 wins, +0.25% avg.** This is the single most effective change.
- **ALL_RELAXED: 14 trades, 7/14 wins (50%), +0.09% avg.** First parameterization approaching viability, but still below the 30-trade minimum floor.

### Phase 1b: Targeted Continuation Fix — COMPLETED 2026-04-04

Ran 6 targeted experiments combining the three recommended structural fixes:

```
Experiment              Changes Applied                                    Trades  WR    Avg PnL   PF
────────────────────────────────────────────────────────────────────────────────────────────────────────
BASELINE                (current strategy)                                    1    0/1   -0.20%    0
G: CHASE+EXP3           chase 1.5 ATR + 3-bar cont. expiry                   2    1/2   -0.06%   0.58
H: CHASE+EXP5           chase 1.5 ATR + 5-bar cont. expiry                   2    1/2   -0.06%   0.58
I: FULL_FIX_3           chase 1.5 + exp 3 + RSI [50,80]                      3    2/3   +0.19%   2.99
J: FULL_FIX_5           chase 1.5 + exp 5 + RSI [50,80]                      3    2/3   +0.19%   2.99
K: FULL_FIX_MAX         chase 1.5 + exp 5 + RSI [50,80] + retest 10          3    2/3   +0.35%   4.62
L: MAX_ADDR             all above + no volume + close_pct 0.55               11   3/11  -0.23%   0.54
```

**Key findings:**
- **Continuation expiry + wider chase** alone: only 2 trades. The expiry *helps* (prevents lingering) but the fundamental pattern frequency is too low.
- **Adding wider RSI [50, 80]**: 3 trades, positive expectancy (+0.19%), PF 2.99. Directionally correct but 3 trades is statistically meaningless.
- **FULL_FIX_MAX** (all three recommended changes + wider retest): still only 3 trades. Best unit economics (+0.35%, PF 4.62) but 3 trades in 365 days is not a strategy.
- **MAX_ADDR** (every possible relaxation): 11 trades, but expectancy goes negative (-0.23%, PF 0.54). Relaxing volume filter + close-pct lets in low-quality breakouts that fail.

> **VERDICT: The breakout-retest-continuation concept does NOT meet the 30-trade floor under ANY reasonable parameterization on BTC-USD at 1h resolution.**
>
> - Best count: 11 trades (37% of minimum floor), with negative expectancy
> - Best expectancy: +0.35% on 3 trades (not statistically distinguishable from random)
> - Maximum addressable setups: 49 RETEST_CONFIRMED events, but only 11 convert to entries even with all filters maximally relaxed
>
> **Decision: ARCHIVE the breakout-retest-continuation concept. Proceed to VPMR strategy from the Research Pack.**

### Phase 2: Backtest Harness Improvements

| Task | Priority | Description |
|------|----------|-------------|
| Walk-forward engine | High | Implement rolling 6-month in-sample / 2-month OOS windows |
| Parameter sweep framework | High | Systematic grid search with results logging |
| Equity curve export | Medium | CSV output for external analysis |
| Regime breakdown logging | Medium | Track what % of backtest period is "bullish regime" |
| Multi-timeframe support | Low | Test whether 4h signal timeframe produces more trades |

### Phase 3: Strategy Alternatives

The Research Pack identifies three ranked alternatives. Status:

1. ~~**Volume Profile Mean Reversion (VPMR)**~~ — **ARCHIVED 2026-04-04.** See [vpmr_diagnostic_results.md](vpmr_diagnostic_results.md).

2. ~~**Dynamic EMA Crossover with ADX gate**~~ — **ARCHIVED 2026-04-04.** See [ema_crossover_evaluation.md](ema_crossover_evaluation.md).

3. ~~**Statistical Volatility Reversion**~~ — **SUPERSEDED by Lane A pilot** which tested Bollinger+RSI mean reversion across 5 assets at 3 friction levels. Zero raw edge.

### Phase 4: Lane A Cross-Asset Rule Library Pilot (2026-04-05)

**COMPLETE — LANE A FAILS. Kill trigger K2: zero raw edge.**

| Parameter | Value |
|-----------|-------|
| Universe | BTC-USD, ETH-USD, SOL-USD, XRP-USD, DOGE-USD |
| Configs | 93 (81 momentum EMA cross + 12 mean reversion BBands) |
| Walk-forward | 180d train / 60d test / 60d step → 9 OOS windows |
| Friction | 0.5x, 1.0x, 1.5x sensitivity |
| Total runs | **12,555 backtests** |

Results at 1.0x friction (honest cost):

| Asset | OOS Trades | PF Med | Expectancy | DD Med |
|-------|:-:|:-:|:-:|:-:|
| BTC-USD | 133 | 0.51 | -0.0044 | 7.25% |
| ETH-USD | 152 | 0.56 | -0.0072 | 6.54% |
| SOL-USD | 135 | 0.82 | -0.0025 | 6.20% |
| XRP-USD | 184 | 0.51 | -0.0096 | 6.79% |
| DOGE-USD | 108 | 0.78 | -0.0043 | 6.34% |

> All 5 assets have negative OOS expectancy at all friction levels except DOGE-USD at 0.5x (which flips to negative at 1.0x — cost-killed).

See [lane_a_pilot_report.md](lane_a_pilot_report.md).

> **Meta-observation (confirmed)**: Standard technical indicators (EMA crossovers, Bollinger Bands) with RSI confirmation produce no detectable positive conditional drift on any of 5 major Coinbase spot pairs over 730 days, across 93 parameter combinations and 9 walk-forward windows. The failure is in the signal source, not the infrastructure.

Any future research must use a different approach than indicator-defined boundary strategies.

---

## 5. Final Conclusions (Phase 0 + 1 + 1b Complete)

### Strategy Status: ARCHIVED

The breakout-retest-continuation strategy has been **formally archived** after exhaustive diagnostic testing.

**Evidence summary across 13 experiments on 365 days of BTC-USD data (2025-04-04 to 2026-04-04):**

| Configuration | Trades | Win Rate | Avg PnL | Profit Factor |
|--------------|--------|----------|---------|---------------|
| BASELINE (current params) | 1 | 0% | -0.20% | 0 |
| Best single-parameter fix (WIDE_CHASE) | 5 | 60% | +0.25% | 2.12 |
| All Phase 1 filters relaxed | 14 | 50% | +0.09% | ~1.0 |
| Best Phase 1b (FULL_FIX_MAX, 3 targeted fixes) | 3 | 67% | +0.35% | 4.62 |
| Maximum addressable (all filters maximally relaxed) | 11 | 27% | -0.23% | 0.54 |

**No parameterization produces >= 30 trades/year.** The maximum addressable signal count is ~11 trades/year with negative expectancy. The pattern simply does not occur frequently enough on BTC-USD at 1h resolution to be a viable strategy.

### Root Cause (Data-Confirmed, Final)

1. **The breakout-retest-continuation pattern occurs ~22-49 times/year** (confirmed retests range) on BTC-USD at 1h resolution — below the minimum sample size under the most generous counting.
2. **The continuation stage converts only 4.5-22% of retests to entries**, depending on chase filter tightness. Even with all filters maximally relaxed, the conversion rate caps at ~22%.
3. **Relaxing filters to increase trade count degrades quality**: MAX_ADDR (11 trades) has negative expectancy because the loosened filters admit low-quality breakouts.
4. **There is a structural contradiction in the concept**: the strategy requires strong breakouts that then retrace gently and then continue promptly — a very specific price behavior that is inherently rare on a trending/volatile asset like BTC.

### What This Means For the Project

- **The execution infrastructure is sound.** Coinbase adapter, persistence layer, state machine, risk management — all functional and strategy-agnostic.
- **The backtesting/audit tooling is now proven.** The signal funnel audit successfully diagnosed the exact failure mode, the comparison table format enables rapid experiment iteration, and the public API data pipeline works without credentials.
- **The strategy was the weak link.** It was designed from a textbook pattern without empirical frequency validation. This is the exact failure mode the research materials warned about.

### Next Action: VPMR Strategy

Per the Research Pack and the established research plan, the next strategy to evaluate is **Volume Profile Mean Reversion (VPMR)**:

- **Expected advantages**: Higher frequency (mean reversion generates more trades than trend-following), passive limit orders (lower fees), regime-gated (ADX < 25 for mean reversion, ADX >= 25 for trend following)
- **Key requirement**: Must pass the same validation pipeline (>= 30 trades, positive expectancy, walk-forward, parameter sensitivity)
- **Timeline**: Prototype in the existing backtest harness, run the same funnel audit methodology

The execution infrastructure will serve this new strategy without modification.

---

## Appendix: What the Research Pack Already Told Us

The Research Pack (section 4) explicitly states:

> *"A strategy must generate a minimum of 300 independent trade occurrences across the full sample duration before any confidence can be placed in its metrics. Strategies with fewer trades are statistically indistinguishable from noise."*

We adopted this standard in previous conversations but never enforced it as a hard gate before proceeding to paper/live testing. **That was the process failure.** This document fixed it, and the standard was enforced correctly — the strategy was archived because it could not meet even the relaxed 30-trade absolute floor, let alone the 300-trade confidence target.
