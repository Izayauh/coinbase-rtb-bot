# Lane A Binding Spec — v1

> Date: 2026-04-05. Governs all research/ implementation.

## A. Binding Constraints

1. Research harness in `research/`, decoupled from `bot/`. No imports from bot.
2. No modifications to `bot/state_machine.py`, `bot/risk.py`, `config.yaml`, or runtime modules.
3. 15-30 Coinbase spot pairs, not single-symbol BTC. Pilot uses top 5 by volume.
4. Pre-registered rule families + parameter bounds. Cap at 465 configs for pilot.
5. Walk-forward: 180d train / 60d test / 60d step. Pre-registered.
6. Multiple-testing control is mandatory, not optional.
7. Lane D is a post-harness sanity check only.

## B. Implementation Sequence

```
Phase 1 — Data:     types.py → costs.py → data.py → universe.py → download
Phase 2 — Engine:   rules.py → backtest.py → tests/
Phase 3 — WF+MT:    walkforward.py → multiple_testing.py → pilot.py
Phase 4 — Run:      pilot experiment → analysis → falsification report
Phase 5 — Lane D:   stat vol reversion as one config in same engine
```

## C. File Plan

```
research/
├── __init__.py
├── types.py              # Bar, Signal, Trade, BacktestResult
├── costs.py              # FrictionModel
├── data.py               # CoinbaseDownloader + cache + manifest
├── universe.py           # Liquidity-filtered spot pair list
├── rules.py              # Rule families (stateless signal generators)
├── backtest.py           # No-lookahead bar engine + MAE/MFE
├── walkforward.py        # Rolling train/test window runner
├── multiple_testing.py   # Stage-1 BH-FDR, Stage-2 stub
├── pilot.py              # Wires everything, outputs CSV
├── datasets/             # Cached OHLCV
│   └── manifest.json
└── tests/
    ├── __init__.py
    └── test_backtest.py  # Synthetic determinism + no-lookahead
```

## D. Immediate Task Queue

| # | File | Lines | Deps | Status |
|---|------|-------|------|--------|
| 1 | `research/__init__.py` | 1 | — | NOW |
| 2 | `research/types.py` | ~50 | — | NOW |
| 3 | `research/costs.py` | ~60 | — | NOW |
| 4 | `research/data.py` | ~200 | types | NOW |
| 5 | `research/universe.py` | ~80 | data | NOW |
| 6 | Download pilot data | — | 4,5 | NEXT |
| 7 | `research/rules.py` | ~150 | types | NEXT |
| 8 | `research/backtest.py` | ~250 | types,costs,rules | NEXT |
| 9 | `research/tests/test_backtest.py` | ~120 | backtest | NEXT |
| 10 | `research/walkforward.py` | ~150 | backtest | THEN |
| 11 | `research/multiple_testing.py` | ~60 | — | THEN |
| 12 | `research/pilot.py` | ~120 | all | THEN |

## E. Friction Model

```
ENTRY COST = taker_fee_bps + half_spread_bps + slippage_bps
EXIT COST  = taker_fee_bps + half_spread_bps + slippage_bps
ROUND-TRIP = ENTRY + EXIT

Base assumptions (conservative):
  taker_fee_bps  = 8.0   (Coinbase Advanced Trade taker)
  half_spread_bps = 1.5  (BTC-USD typical; wider for altcoins)
  slippage_bps   = 3.0   (market order estimate)
  ───────────────────
  ONE-WAY        = 12.5 bps
  ROUND-TRIP     = 25.0 bps

Sensitivity runs: 0.5× (12.5 bps RT), 1.0× (25 bps RT), 1.5× (37.5 bps RT)

Implementation:
  - FrictionModel stores base components separately
  - apply_entry_cost(price, side) and apply_exit_cost(price, side) methods
  - sensitivity_multiplier scales ALL components uniformly
  - Per-asset spread override supported (altcoins wider)
```

## F. Multiple-Testing Plan

### Stage 1 (MVP — pilot screen)
- Method: Benjamini-Hochberg FDR on OOS per-window t-test p-values
- Threshold: adjusted p < 0.10
- Applied: per walk-forward window, then aggregate
- Purpose: cheap screen to remove noise configs

### Stage 2 (post-selection — reserved for survivors)
- Method: CSCV/PBO (Combinatorially Symmetric Cross-Validation / Probability of Backtest Overfitting)
- Or: White's Reality Check bootstrap
- Threshold: PBO < 0.40 (fewer than 40% of path permutations overfit)
- Applied: only to Stage-1 survivors
- Purpose: confirm surviving configs are not artifacts of path-dependence
- Status: **PLANNED, not built in Phase 1**. Build only if Stage-1 produces survivors.

## G. Pilot Viability Thresholds (harness works)

These prove the harness is functional, NOT that Lane A passes:

| Metric | Threshold |
|--------|-----------|
| Data coverage | ≥ 95% of expected bars for ≥ 4/5 pilot assets |
| Backtest runs | All 465 configs complete without error |
| Walk-forward windows | ≥ 7 non-overlapping OOS windows |
| Trade frequency | ≥ 50% of configs produce ≥ 10 trades per OOS window |
| Determinism | Same input → same output (verified by test) |

## H. Final Lane A Pass Thresholds

| Criterion | Threshold |
|-----------|-----------|
| Survivor count after Stage-1 FDR | ≥ 3 configs |
| Cross-asset presence | Survivors span ≥ 2 different assets |
| OOS expectancy after 1.0× friction | > 0 in ≥ 3/9 windows |
| OOS PF | ≥ 1.1 median across windows |
| Max DD per window | < 20% median |
| Friction robustness | Positive at 1.5× friction in ≥ 2 windows |
| Portfolio-form check | Equal-weight ensemble of survivors is positive OOS after 1.0× friction |
| Stage-2 (if reached) | PBO < 0.40 or Reality Check not rejected at 5% |

**Lane A passes** = all of the above.
**Lane A is promising** = meets first 6 but portfolio check is marginal.
**Lane A fails** = fewer than 3 survivors or all confined to 1 asset.

## I. Two-Week Kill Criteria

| Kill | Trigger |
|------|---------|
| K1 | Cannot download 730d OHLCV for ≥ 10 pairs (API/data infeasible) |
| K2 | Zero configs show OOS expectancy > 0 before FDR (no raw edge at all) |
| K3 | All positive configs flip negative at 1.0× friction (edge < costs) |
| K4 | All survivors are BTC-USD only (cross-asset thesis fails) |
| K5 | All IS winners collapse in OOS (pure overfitting) |

If killed: archive OHLCV rule-library premise entirely.

## J. What NOT to Build Yet

- Lane B/C infrastructure
- Lane D (until harness exists)
- Runtime changes to bot/
- Portfolio optimizers (equal-weight only if survivors exist)
- Stage-2 multiple-testing (planned, built only if needed)
- CI/CD, GitHub Actions
- Equity curve visualization
- Regime tagging (stretch goal)
