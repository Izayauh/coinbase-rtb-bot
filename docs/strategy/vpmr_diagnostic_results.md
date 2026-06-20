# VPMR Diagnostic Cycle — Final Results

> **Date**: 2026-04-04  
> **Status**: ARCHIVED — No variant meets acceptance criteria  
> **Next**: Dynamic EMA Crossover (Rank 2 from Research Pack)

---

## 1. What Was Tested

4 variants × 2 time horizons = 8 runs.

| Variant | Change from Baseline |
|---------|---------------------|
| **BASELINE** | Current VPMR (RSI 30/70, 1.5× ATR stop, direct entry) |
| **CONFIRM** | Require reversal bar after boundary touch before entry |
| **TIGHT_STOP** | 1.0× ATR stop instead of 1.5× |
| **CONFIRM+TIGHT** | Both changes combined |

---

## 2. Results

```
Variant          | ── 365-DAY ──────────────────────────────── | ── 730-DAY ──────────────────
                 | Trades   WR        AvgPnL%   PF    MaxDD   | Trades   WR        AvgPnL%   PF    MaxDD
─────────────────|─────────────────────────────────────────────|────────────────────────────────────────────
BASELINE         |   149   59/149 40%  -0.039%  0.92  38.70%  |   303   116/303 38%  -0.060%  0.89  62.94%
CONFIRM          |    16    9/16  56%  -0.099%  0.71   5.81%  |    23    12/23  52%  -0.093%  0.77   8.51%
TIGHT_STOP       |   181   58/181 32%  -0.022%  0.95  52.87%  |   362   105/362 29%  -0.074%  0.84  80.85%
CONFIRM+TIGHT    |    17    9/17  53%  -0.030%  0.90   8.45%  |    24    11/24  46%  -0.041%  0.88   8.45%
```

### Exit Breakdown (365d)

| Variant | STOP_LOSS | TAKE_PROFIT | TIME_STOP |
|---------|-----------|-------------|-----------|
| BASELINE | 80 (54%) | 19 (13%) | 50 (34%) |
| CONFIRM | 5 (31%) | 6 (38%) | 5 (31%) |
| TIGHT_STOP | 120 (66%) | 21 (12%) | 40 (22%) |
| CONFIRM+TIGHT | 7 (41%) | 7 (41%) | 3 (18%) |

### Confirmation Statistics

Out of 202 setups (boundary touch + RSI pass), only **19 confirmed** (9.4% pass rate). Confirmation massively filters entries — from 149 down to 16 — but the surviving trades still have negative expectancy.

---

## 3. Diagnostic Analysis

### Did confirmation improve trade quality?

**Partially, but not enough.**

- Win rate improves: 40% → 56% (365d), 38% → 52% (730d)
- TP rate improves: 13% → 38% of exits are take-profits
- Drawdown collapses: 38.7% → 5.8% (huge improvement)
- **But expectancy is still negative**: -0.099% (365d), -0.093% (730d)
- **And trade count drops to 16-23**: below the 30-trade floor

The confirmation bar works as a quality filter — but the surviving trades are still, on average, losers. The strategy concept itself does not produce positive expectancy at VA boundaries on BTC-USD.

### Did tighter stops reduce DD without destroying expectancy?

**No. Made everything worse.**

- TIGHT_STOP: DD goes UP to 52.9% (365d), 80.9% (730d)
- Win rate drops from 40% to 32% (stops trigger more easily, killing good trades)
- PF slightly improves (0.92 → 0.95) but expectancy is still negative
- On 730d: equity drops to $2,141 (79% loss). Catastrophic.

Tighter stops do not help because the strategy's edge is non-existent — cutting losses faster just means you lose slightly less per trade but stop out of more trades that would have recovered.

### Which component helped, if any?

**Confirmation is the only one that helped**, and it helped only one metric: drawdown. It does this by trading extremely rarely (9.4% of setups pass), which limits exposure. But the trades it takes are still negative EV. A strategy that loses money slowly is not better than one that doesn't trade at all.

### Do results collapse on 730d?

**Yes, for everything.**

| Variant | 365d PF | 730d PF | Collapsed? |
|---------|---------|---------|------------|
| BASELINE | 0.92 | 0.89 | Yes — worse |
| CONFIRM | 0.71 | 0.77 | Stable but negative |
| TIGHT_STOP | 0.95 | 0.84 | Yes — much worse |
| CONFIRM+TIGHT | 0.90 | 0.88 | Stable but negative |

No variant improves with more data. Every variant has PF < 1.0 on both horizons.

---

## 4. Acceptance Criteria Check

| Criterion | BASELINE | CONFIRM | TIGHT_STOP | CONFIRM+TIGHT |
|-----------|----------|---------|------------|---------------|
| PF >= 1.2 | 0.92 FAIL | 0.71 FAIL | 0.95 FAIL | 0.90 FAIL |
| DD < 25% | 38.7% FAIL | 5.8% PASS | 52.9% FAIL | 8.5% PASS |
| AvgPnL > 0.03% | -0.04% FAIL | -0.10% FAIL | -0.02% FAIL | -0.03% FAIL |
| 730d stable | PF degrades FAIL | PF negative FAIL | PF collapses FAIL | PF negative FAIL |
| Trades >= 30 | 149 PASS | 16 FAIL | 181 PASS | 17 FAIL |
| **ALL PASS** | **NO** | **NO** | **NO** | **NO** |

**Zero variants pass.** Not a single criterion is simultaneously satisfied by any variant.

---

## 5. Verdict

### ARCHIVE VPMR

The VPMR strategy is **dead on arrival** for BTC-USD. Across 4 variants × 2 time horizons:

- **Every variant has negative expectancy** on both 365d and 730d
- **Every variant has PF < 1.0** (losing money faster than making it)
- The only configuration with acceptable drawdown (CONFIRM) has **16-23 trades** — below the 30-trade floor AND still negative EV
- **The concept fails on 730d worse than 365d** — there is no hidden edge that needs a longer sample to reveal

### Why VPMR Fails on BTC-USD

1. **BTC-USD does not mean-revert at Volume Area boundaries.** When price reaches VAL/VAH with RSI confirmation, it continues through more often than it reverts. The fundamental assumption of the strategy is empirically wrong for this asset.

2. **The 24-hour rolling Volume Profile is unstable.** BTC trades 24/7 with no session boundaries, so the "daily" profile shifts continuously. Traditional Volume Profile works on assets with defined sessions (futures, equities) where POC/VAH/VAL represent genuine institutional accumulation zones. On BTC-USD, these levels are noise.

3. **The ADX regime filter correctly identifies ranging periods, but ranging BTC still trends within the range.** ADX < 25 means "no strong trend," not "mean-reverting." BTC can move 1-3% in a ranging market, which is enough to stop out a 1-1.5 ATR stop.

### What Comes Next

Per the Research Pack, the next strategy to evaluate is **Dynamic EMA Crossover with ATR/RSI Filters** (Rank 2):
- Trend-following (not mean-reverting)
- 8/34 EMA cross on 1h, gated by ADX >= 25
- Higher minimum excursion target
- Trailing stop system
- Same validation pipeline and standards

### What We Preserve

- All execution infrastructure (strategy-agnostic)
- Data pipeline and caching (public API, CSV cache for both 365d and 730d)
- Funnel audit methodology
- Validation standards and decision criteria

---

## 6. Files

| File | Status |
|------|--------|
| `vpmr_backtest.py` | CREATED — VPMR backtest engine + Phase 1 experiments |
| `vpmr_diagnostic.py` | CREATED — Phase 2 diagnostic cycle (this test) |
| `docs/strategy/vpmr_evaluation.md` | CREATED — Phase 1 evaluation |
| `docs/strategy/vpmr_diagnostic_results.md` | THIS DOCUMENT — final verdict |
| `cache_BTC-USD_730d_1h.csv` | CREATED — 2-year cached data (reusable) |

---

## 7. Strategy Graveyard

| # | Strategy | Trades/Year | Edge | Cause of Death |
|---|----------|------------|------|----------------|
| 1 | Breakout-Retest-Continuation | 1-14 | Unmeasurable | Insufficient frequency; pattern too rare on BTC-USD |
| 2 | VPMR (Volume Profile Mean Reversion) | 16-656 | Negative | BTC-USD does not mean-revert at VA boundaries |
| 3 | Dynamic EMA Crossover (pending) | TBD | TBD | Next candidate |
