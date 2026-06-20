# Lane A Execution Plan — Cross-Asset Rule Library Research Harness

> **Governing document**: Strategy-Decision Memo (2026-04-04)  
> **Date**: 2026-04-05  
> **Status**: Ready for implementation  
> **Scope**: Build-only. No live trading. No strategy promotion.

---

## A. Binding Constraints (Extracted from Memo + Repo Audit)

### From the Decision Memo

| ID | Constraint | Source |
|----|-----------|--------|
| M1 | Lane A is the primary research lane: cross-asset spot-only rule library with multiple-testing control | Memo §D, §I |
| M2 | No strategy promotes without walk-forward OOS validation + multiple-testing correction | Memo §E, §G |
| M3 | Must test 15-30 Coinbase spot pairs, not just BTC-USD | Memo §G.1 |
| M4 | Pre-register rule families, parameter bounds, and cap total configurations before searching | Memo §G.2 |
| M5 | Walk-forward design: 180d train / 60d test / 60d step (pre-registered) | Memo §G.3 |
| M6 | Portfolio/ensemble evaluation of survivors, not single-rule promotion | Memo §G.6 |
| M7 | Two-week kill: OOS expectancy ≤ 0 after friction, or sign-flip with small param changes = stop | Memo §E (Lane D kills) |
| M8 | Treat multiple-testing control as first-order compliance, not optional | Analytical Review §Strategy research integrity |

### From the Repo Audit

| ID | Mismatch | File | Fix Required |
|----|----------|------|-------------|
| R1 | `risk.py` hardcodes `MAX_RISK_PERCENT = 0.002` and `MAX_SLIPPAGE = 0.005`; config.yaml has `risk_per_trade: 0.0035` | `bot/risk.py`, `config.yaml` | Make research layer read friction from config, not hardcoded constants |
| R2 | `adapters.py` PaperAdapter returns `commission: 0.0` on all fills | `bot/adapters.py` L68 | Research backtest must inject realistic fees; do not fix PaperAdapter (it's runtime) |
| R3 | `state_machine.py` still embeds archived breakout-retest logic | `bot/state_machine.py` | Do not touch — this is runtime code. Research harness is separate. |
| R4 | No backtest engine anywhere in the repo | — | Build `research/backtest.py` |
| R5 | No walk-forward runner | — | Build `research/walkforward.py` |
| R6 | No multiple-testing control | — | Build `research/multiple_testing.py` |
| R7 | No multi-symbol candle downloader with manifest | — | Build `research/data.py` |
| R8 | Existing `signal_funnel_audit.py`, `vpmr_backtest.py`, `ema_crossover_backtest.py` are single-strategy single-symbol scripts with inline data fetching | repo root | Do not reuse; build clean research layer |

### Architectural Boundary

The research harness lives in `research/` and is **completely decoupled** from `bot/`. It imports nothing from `bot/` except possibly the `Bar` dataclass if convenient (or defines its own). It does not use `config.yaml`, `db.py`, `journal.py`, or any runtime module.

---

## B. Exact Implementation Sequence

### Phase 1: Data Layer (blocks everything)

| Step | Module | Description | Depends On |
|------|--------|-------------|-----------|
| 1.1 | `research/data.py` | Coinbase candle downloader + paginator. Handles 350-candle/request limit. Downloads OHLCV for arbitrary products × timeframes × date ranges. | — |
| 1.2 | `research/data.py` | Local cache in `research/datasets/`. Parquet or CSV per product-timeframe. Manifest file: `research/datasets/manifest.json` tracks symbol, timeframe, start, end, bar count, missing bars. | 1.1 |
| 1.3 | `research/universe.py` | Universe definition: frozen list of 15-30 Coinbase spot pairs selected by liquidity proxy (24h volume from public endpoint). | 1.1 |
| 1.4 | Run initial download | Download 730d of 1h data for the frozen universe. Verify manifest. | 1.1-1.3 |

### Phase 2: Backtest Engine (blocks walk-forward)

| Step | Module | Description | Depends On |
|------|--------|-------------|-----------|
| 2.1 | `research/types.py` | Minimal data types: `Bar`, `Trade`, `Signal`, `BacktestResult`. No lookahead. | — |
| 2.2 | `research/costs.py` | Friction model: fee_bps, slippage_bps, spread_bps. Configurable. Must support 0.5×, 1×, 1.5× sensitivity multiplier. | — |
| 2.3 | `research/rules.py` | Rule interface: `def generate_signals(bars, params) -> List[Signal]`. A Signal has: bar_index, direction (long/short/flat), stop, target. Rules are stateless functions. | 2.1 |
| 2.4 | `research/backtest.py` | Bar-based backtest engine. Takes bars + signals + cost model → `BacktestResult`. No lookahead (signal on bar[i] enters at bar[i+1] open). Reports: trades, win rate, PF, expectancy, max DD, avg hold, equity curve, per-trade MAE/MFE. | 2.1-2.3 |
| 2.5 | Unit tests | Synthetic 20-bar dataset → deterministic trade result. Verify no lookahead. Verify fee deduction. | 2.4 |

### Phase 3: Walk-Forward + Multiple Testing (blocks pilot)

| Step | Module | Description | Depends On |
|------|--------|-------------|-----------|
| 3.1 | `research/walkforward.py` | Rolling window runner. Config: train_days, test_days, step_days. For each window: run param grid on train → select best by metric → evaluate on test. Returns per-window OOS results. | 2.4 |
| 3.2 | `research/multiple_testing.py` | FDR-BH step: given N tested configurations per window, correct reported significance. Start with Benjamini-Hochberg on p-values from bootstrap or t-test of OOS returns. Minimal viable version. | 3.1 |
| 3.3 | `research/pilot.py` | Pilot experiment runner: wires universe subset × timeframes × rules × walk-forward. Outputs per-config OOS metrics in a flat CSV for analysis. | 3.1, 3.2, 1.4 |

### Phase 4: Pilot Experiment (the actual falsification test)

| Step | Description | Depends On |
|------|-------------|-----------|
| 4.1 | Pre-register pilot design (see Section D below) | 3.3 |
| 4.2 | Run pilot | 4.1 |
| 4.3 | Analyze: any survivors after costs + FDR correction? | 4.2 |
| 4.4 | Write falsification report | 4.3 |

### Phase 5: Lane D sanity check (only after pilot)

| Step | Description | Depends On |
|------|-------------|-----------|
| 5.1 | Run Statistical Volatility Reversion as one config in the same backtest engine (not a separate script) | 2.4, 1.4 |
| 5.2 | Compare against pilot survivors (if any) | 4.3 |

---

## C. File/Module Plan

```
research/
├── __init__.py
├── types.py           # Bar, Trade, Signal, BacktestResult dataclasses
├── costs.py           # FrictionModel: fee, slippage, spread, sensitivity multiplier
├── data.py            # CoinbaseDownloader: paginated candle fetch + cache + manifest
├── universe.py        # Universe definition + liquidity filter
├── rules.py           # Rule interface + rule library (EMA cross, mean reversion, etc.)
├── backtest.py        # BarBacktester: no-lookahead engine with MAE/MFE
├── walkforward.py     # WalkForwardRunner: rolling train/test windows
├── multiple_testing.py # FDR correction (Benjamini-Hochberg)
├── pilot.py           # Pilot experiment harness
├── datasets/          # Cached OHLCV data
│   ├── manifest.json
│   └── *.csv
└── tests/
    ├── test_backtest.py   # Synthetic bar determinism tests
    ├── test_costs.py      # Fee invariant tests
    └── test_walkforward.py # Window overlap/leakage tests
```

Nothing in `bot/` is modified. Nothing in `research/` imports from `bot/`.

---

## D. First Pilot Experiment Design (Pre-Registered)

### Universe

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Assets | 5 (pilot subset from frozen 15-30 list) | Cheapest honest test |
| Selection | Top 5 by 30-day avg USD volume on Coinbase spot | Objective, reproducible |
| Expected members | BTC-USD, ETH-USD, SOL-USD, XRP-USD, DOGE-USD (verify at download time) | — |

### Timeframes

| Timeframe | Usage |
|-----------|-------|
| 1h | Signal generation + backtest |
| 4h | Regime classification (optional, if rule uses it) |

### Rule Families (2 families, capped)

| Family | Signal Logic | Parameters | Grid Size |
|--------|-------------|------------|-----------|
| **Momentum** | EMA cross (fast/slow) + RSI band confirmation | fast∈{5,8,13}, slow∈{21,34,55}, RSI_lo∈{45,50,55}, RSI_hi∈{60,65,70} | 3×3×3×3 = 81 |
| **Mean Reversion** | Bollinger Band touch (2σ, 2.5σ) + RSI extreme | bb_period∈{14,20}, bb_sigma∈{2.0,2.5}, RSI_thresh∈{25,30,35} | 2×2×3 = 12 |
| **TOTAL** | | | **93 configs per asset × timeframe** |

> With 5 assets × 1 timeframe = **465 total configurations**. This is the pre-registered cap.

### Walk-Forward Design

| Parameter | Value |
|-----------|-------|
| Train window | 180 days |
| Test window | 60 days |
| Step size | 60 days |
| Total data | 730 days |
| Number of OOS windows | ~9 (depends on exact data coverage) |
| In-sample selection metric | Sharpe ratio (after friction) |
| OOS evaluation metrics | Expectancy, PF, max DD, trade count |

### Cost Model

| Parameter | Base Value |
|-----------|-----------|
| Fee (round-trip) | 10 bps maker, 20 bps taker |
| Slippage | 5 bps |
| Spread | 3 bps |
| **Total one-way friction** | ~14 bps (conservative) |
| Sensitivity test | Run at 0.5×, 1.0×, 1.5× friction |

### Exit Rules (Fixed, Not Optimized)

| Exit | Rule |
|------|------|
| Stop loss | 2× ATR(14) from entry |
| Take profit | 3× ATR(14) from entry (1.5 R:R) |
| Time stop | 24 bars |

> Exits are NOT part of the grid search. They are fixed to prevent overfitting exit parameters.

---

## E. Explicit Success Criteria

A configuration "survives" if it meets ALL of:

| Criterion | Threshold | Rationale |
|-----------|----------|-----------|
| OOS trade count (per window) | ≥ 10 | Statistical minimum per window |
| OOS expectancy after friction | > 0 | Must be profitable after costs |
| OOS profit factor | ≥ 1.1 | Not just break-even |
| OOS max drawdown | < 20% | Risk-acceptable |
| Survives FDR correction | adjusted p < 0.10 | Not a multiple-testing artifact |
| Stable across ≥ 3 OOS windows | No sign-flip | Not a single-regime fluke |
| Friction sensitivity | Positive at 1.5× friction | Edge must survive higher costs |

**Lane A passes** if ≥ 1 configuration survives all criteria on ≥ 2 assets.

**Lane A fails** if zero configurations survive, or all survivors are confined to one asset/one window.

---

## F. Two-Week Kill Criteria

Lane A is killed if any of these occurs within 2 weeks of starting implementation:

| Kill Trigger | Description |
|-------------|------------|
| K1: Data infeasible | Cannot download 730d of reliable OHLCV for ≥ 10 Coinbase spot pairs (API rate limits, data gaps > 5% of bars) |
| K2: Zero raw edge | Pilot shows 0 configurations with OOS expectancy > 0 before FDR correction (not even false positives) |
| K3: Cost-killed | All positive-expectancy configs flip to negative at 1.0× friction (edge exists but is too small) |
| K4: Single-asset confinement | All survivors are BTC-USD only (cross-asset thesis is false) |
| K5: Overfit collapse | All IS-selected configs collapse to negative PF in OOS (classic overfitting) |

If killed: **Archive the entire OHLCV rule-library premise. Reassess whether to pursue Lane B (derivatives carry) or exit trading entirely.**

---

## G. What NOT to Work On Yet

| Item | Why Deferred |
|------|-------------|
| Lane B (perp carry/funding) | Requires derivatives access investigation; only revisit if Lane A fails |
| Lane C (microstructure/L2 book) | Requires WebSocket infrastructure work; orthogonal to Lane A |
| Lane D (Statistical Volatility Reversion) | Small sanity check AFTER pilot harness exists; not a strategic bet |
| Touching `bot/state_machine.py` | Runtime code; archived strategy stays in place |
| Touching `bot/risk.py` constants | Runtime code; research harness has its own cost model |
| Touching `config.yaml` | Runtime config; research harness has its own config |
| Paper trading or live testing | Nothing trades until Lane A produces a surviving configuration |
| Regime tagging, MAE/MFE export, equity curve export | Phase 2 stretch goals; implement only if core engine works |
| Portfolio/ensemble construction | Phase 4+; only relevant if survivors exist |
| CI/CD, GitHub Actions, test infrastructure | Nice-to-have; deferred until research harness is stable |

---

## H. Implementation Order — Concrete Task Queue

```
TASK 1: research/types.py          ~30 lines   (dataclasses, no deps)
TASK 2: research/costs.py          ~40 lines   (friction model, no deps)
TASK 3: research/data.py           ~150 lines  (downloader + cache + manifest)
TASK 4: research/universe.py       ~60 lines   (liquidity filter + frozen list)
TASK 5: Download pilot data        (run script, verify manifest)
TASK 6: research/rules.py          ~120 lines  (2 rule families, stateless)
TASK 7: research/backtest.py       ~200 lines  (bar engine + MAE/MFE + no lookahead)
TASK 8: research/tests/            ~100 lines  (synthetic bar determinism, fee check)
TASK 9: research/walkforward.py    ~120 lines  (rolling windows, param grid, select)
TASK 10: research/multiple_testing.py ~50 lines (BH-FDR correction)
TASK 11: research/pilot.py         ~100 lines  (wire everything, output CSV)
TASK 12: Run pilot experiment
TASK 13: Analyze + write falsification report
TASK 14: (conditional) Lane D sanity check via same engine
```

**Estimated total new code**: ~1,000 lines across 8 modules + tests.

**Dependencies**: Tasks 1-2 are parallel. Task 3 requires Task 1. Tasks 6-7 require 1-2. Task 9 requires 7. Task 11 requires 9-10. Sequential from there.
