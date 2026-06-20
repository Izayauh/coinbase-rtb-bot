# Implementation Report — Research Pipeline Spine (Phase 3)

**Date:** 2026-06-19
**Scope delivered:** Architecture review + frozen implementation contract + a working, tested,
read-only/shadow-only Coinbase microstructure and context research pipeline. It now collects public
Level2/trade/quote data, derives executable labels and eight features, ingests four authoritative
context feeds, and runs diagnostic DSR/CSCV-PBO policy evaluation without any order path.

## 2026-06-19 expansion

- Completed a second research-first strategy tournament. A BTC
  derivatives-stress/order-book-exhaustion hypothesis won 10–3, but advances
  only to a seven-day feasibility probe.
- Implemented Deflated Sharpe Ratio and CSCV/PBO from the primary papers.
- Corrected Level2 diagnosis: Coinbase documents it as public; the real failure was the WebSocket
  client’s 1 MB message limit. A 16 MB limit captures the ~4.6 MB / ~42k-level initial snapshot.
- Batch-inserts each L2 frame atomically.
- Implemented eight microstructure features: quoted spread, top-of-book imbalance, 10/25 bps depth
  imbalance, multilevel imbalance, signed aggressive flow, trade intensity, and realized volatility.
- Added versioned per-decision `order_math` state: microprice, queue imbalance,
  60-second OFI, sampled 5/10-level MLOFI, 5/10/25/50 bps depth, book shape,
  additions/depletion, and replenishment ratios. Public aggregate Level2 does
  not reveal exact cancellations or individual queue position, so those remain
  explicitly estimated rather than asserted.
- Backfilled 360 one-minute order-math rows from the completed six-hour BTC
  archive, exported them as verified ZSTD Parquet, and published
  `bitwise-trader.crypto_research.order_math_external` in BigQuery.
- Wired public Coinbase International BTC-PERP context: minute-cadence open
  interest plus official hourly realized funding and mark prices. Binance
  Futures is not used because the U.S. cloud VM receives HTTP 451.
- Added idempotent `derive`, diagnostic `evaluate`, and a one-writer collector lock.
- Added primary-source context adapters for Federal Reserve, BLS CPI, SEC EDGAR Coinbase filings,
  and CFTC Bitcoin futures positioning; 219 context records were ingested.
- Started a bounded six-hour shadow capture with automatic post-capture derive + policy evaluation.

---

## 1. Exactly what works now

- **Immutable ingestion store** (`research_pipeline/storage/`): own SQLite (WAL), explicit
  forward-only migrations through `schema_v4`, append-only raw evidence enforced by **DB triggers**,
  sha256 payload hashing with canonical JSON, hash-based dedup, source registry, ingestion-run
  ledger, normalized `trades`/`l2_updates`/`quotes`, `gaps`, `labels`, `features`,
  `variant_registry`, and `context_events`.
  Refuses to open any `*_journal.db`. Retention prunes derived rows only and **refuses to delete
  unexported raw evidence**.
- **Public Coinbase collector** (`research_pipeline/collectors/coinbase.py`): bounded async WS
  collection of `market_trades`, `level2`, `ticker`, and `heartbeats`; a 16 MB message ceiling for
  the ~4–5 MB initial BTC-USD depth snapshot; atomic batch insertion of L2 frames; raw-dedup-gated
  normalization; reconnect epochs; connection-level `sequence_num` gap detection; robust ISO→µs
  timestamp parsing; storage warning/block limits; and a per-database single-writer lock.
  **Verified live today without credentials.**
- **Order-book reconstruction** (`research_pipeline/book/orderbook.py`): snapshot/absolute-qty/
  removal semantics, preservation of snapshot boundaries during replay, best bid/ask, mid, spread,
  depth-within-bps, and explicit health
  (OK/NO_SNAPSHOT/GAP/RECONNECT/CROSSED/STALE). Never synthesizes a healthy book after a gap.
- **Executable labeler + deterministic replay** (`research_pipeline/labeling/labeler.py`): bid/ask-
  aware long/flat labels at **5m/15m/1h/4h**, transparent first-order friction decomposition
  (`net = gross + spread + slippage + fee + adverse`), MFE/MAE, validity flags
  (STALE_QUOTE/CROSSED/NO_QUOTE / no-row-when-horizon-unavailable), cost-model + replay versions.
  Same engine drives live-shadow and replay → parity by construction.
- **Eight microstructure features** (`features/microstructure.py`): quoted spread, top-of-book
  imbalance, 10/25 bps depth imbalance, multilevel imbalance, signed aggressive trade flow, trade
  intensity, and realized volatility. Missing/stale/crossed inputs become `null + flag`, never an
  imputed value.
- **Context adapters** (`context/base.py`): provenance/vintage-aware collection from Federal Reserve
  RSS, BLS CPI, SEC EDGAR Coinbase filings, official CFTC Bitcoin/Micro Bitcoin futures files, and
  public Coinbase International BTC-PERP open interest/funding/mark data. On-chain remains an
  explicit `AccessGap`.
- **Governance** (`governance/gates.py`): purged walk-forward + embargo, ESS, DSR, CSCV/PBO, exact
  no-trade baseline, variant counting, and a fail-closed evidence gate. Missing trial matrices,
  baselines, OOS folds, stress evidence, ESS, or operational evidence keeps promotion **BLOCKED**.
- **Diagnostic policy tournament** (`governance/evaluation.py`): seven fixed policies evaluated
  against aligned executable labels with DSR, PBO, ESS, expectancy, and trade rate. Output is always
  marked `DIAGNOSTIC_ONLY`; it cannot authorize a live strategy.
- **CLI**: `collect`, `collect_context`, `derive`, `evaluate`, `smoke`, and `health`. No `bot/`
  imports or order APIs anywhere.
- **Test discovery fixed** (F-H1): `pyproject.toml` now collects `bot/tests`, `research/tests`,
  `research_pipeline/tests` (additive; bot coverage unchanged).

## 2. Exactly what remains blocked (honest)

- **Promotion evidence is incomplete.** The algorithms for DSR and CSCV/PBO are implemented, but a
  candidate still needs its complete registered trial matrix, all four baselines, purged
  walk-forward OOS folds, sufficient non-overlapping ESS, binding-stress returns, and all
  operational gates. The current diagnostic policies are not pre-authorized strategy candidates.
- **Three baselines remain unfinished:** buy-and-hold, price/volatility-only, and archived breakout.
  They need a continuous aligned evaluation series. No-trade is implemented exactly.
- **Liquidation-event and on-chain adapters remain access gaps.** Coinbase INTX funding and open
  interest are now live, but the selected strategy still lacks a direct public liquidation stream.
- **Long-duration evidence remains immature.** Verified hourly ZSTD Parquet archives, GCS query
  mirrors, and BigQuery external tables work, but the system still needs multi-day operational
  history and retention monitoring.
- **No profitability claim exists.** The active collection window must finish and be evaluated before
  any hypothesis can even be considered for a larger shadow test.

## 3. Sources actually contacted and freshness

| Source | How | Freshness | Result |
|---|---|---|---|
| Coinbase Advanced Trade public WS | live collect | **today, real-time** | `market_trades` + public `level2` + `ticker` + `heartbeats`; snapshot and updates verified |
| Coinbase WS channel docs | official docs + live verification | current | `level2` is public; messages arrive as `l2_data`; the earlier failure was the client's 1 MB message limit |
| Coinbase fee schedule | WebSearch | current | lowest tier taker ≈ 0.60–1.20% → `cost_model_v1` taker default 60 bps + stress to 120 bps |
| Federal Reserve RSS | official RSS | current at retrieval | 15 provenance/vintage-aware records ingested |
| BLS CPI | official public API | current at retrieval | 29 observations ingested; availability conservatively set to retrieval time |
| SEC EDGAR | official submissions API | current at retrieval | 77 Coinbase filing records ingested |
| CFTC COT | official annual TFF archive | current at retrieval | 98 Bitcoin/Micro Bitcoin futures records ingested; availability conservatively set to retrieval time |
| Coinbase INTX funding/OI | public REST | minute OI / hourly funding | live and provenance-aware; no credentials |
| Binance Futures | public REST probe | current | HTTP 451 from the U.S. cloud VM; excluded |
| On-chain | not wired | n/a | explicit `AccessGap` |

## 4. Live collection evidence (today)

Clean 20 s enhanced smoke:

```
frames 1,441   trades 173   quotes 60   l2_updates 53,160   gaps 0
feature_rows 253   valid reconstructed book and depth-band features
```

- A separate 60 s benchmark captured 451 frames, 43,309 L2 rows, 148 quotes, 268 trades, and zero
  detected sequence gaps.
- The bounded six-hour collector is running in the shadow database with an automatic post-capture
  `derive` + `evaluate` waiter. At the final verification point for this report it was healthy, had
  zero logged errors, and had already accumulated more than 193,000 L2 updates, 3,000 quotes,
  2,900 trades, 20,000 raw frames, and 219 context records.
- **No strategy-edge claim is made from this smoke.**

## 5. Databases & storage

- `research_pipeline_data/research.db` — main store (created on demand by `collect`).
- `research_pipeline_data/smoke_v2.db` — enhanced Level2 smoke store.
- `research_pipeline_data/l2_benchmark.db` — 60-second Level2 benchmark.
- `research_pipeline_data/.gitignore` — ignores all data artifacts.
- Append-only raw; storage warns at 2 GB, blocks new raw at 8 GB.

## 6. Tests run and exact results

```
python -m pytest research_pipeline/tests -q   -> 87 passed
```

Coverage includes schema v1→v3 migrations, append-only enforcement, hash deduplication, batched L2
inserts, idempotent derivation, single-writer locking, storage limits, order-book replay, executable
friction labels, all eight features, public-context adapters, DSR, CSCV/PBO, diagnostic evaluation,
purged WF, ESS, fail-closed promotion gates, and boundary proofs showing that research code cannot
import `bot`, call an order method, or open a journal database.

## 7. All files changed

**New package** `research_pipeline/`: configuration, schema/store, Coinbase collector, order-book
replay, executable labeling, microstructure features, authoritative context adapters, statistical
governance, diagnostic evaluation, CLIs, and tests.
**New docs** `docs/research_pipeline/`: `ARCHITECTURE_REVIEW.md`, `IMPLEMENTATION_CONTRACT.md`,
`RUNBOOK.md`, `IMPLEMENTATION_REPORT.md`.
**New** `research_pipeline_data/.gitignore`.
**Edited (tracked):** `pyproject.toml` — test discovery only (additive).

## 8. Safety confirmation

- The research expansion did not change the live BTC-USD strategy, products, caps, thresholds, or
  order authority. The test notional remains $10, max order $15, and max position $30.
- No research code writes to `journal.db`, `live_journal.db`, or `paper_journal.db`.
- The six-hour process is research-only and writes only to `research_pipeline_data/research.db`.
  Its post-capture waiter only derives features/labels and runs diagnostic evaluation.
- No credentials are needed for the public Coinbase channels. **No order path exists in the package
  (AST-proven), and no order was placed during this work.**

## 9. Next smallest safe steps toward the 90-day shadow run

1. Let the bounded six-hour collection finish and inspect the automatic derive/evaluate report.
2. Implement the three missing baselines and complete purged-WF candidate selection over the fixed
   policy/trial registry.
3. Run the seven-day BTC-PERP funding/OI + BTC-USD order-math feasibility probe and predeclare
   qualifying stress episodes.
4. Add a lawful, region-accessible liquidation source or reject the cascade strategy for lack of
   observable mechanism data.
5. Keep all results shadow-only. Even `EVIDENCE_PASSED` requires a separate human-authorized,
   one-writer implementation before any live strategy change.

## 10. Durable project record

The project wiki records the expanded state: public Level2 is working, DSR/CSCV-PBO are implemented,
eight features and four authoritative context feeds are live, the bounded six-hour shadow capture is
running, and promotion remains blocked on complete candidate evidence rather than missing algorithms.
