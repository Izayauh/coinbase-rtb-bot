# Implementation Contract — Coinbase Research Pipeline (v1)

**Status:** Frozen defaults; implementation expanded on 2026-06-19 without changing live authority.
**Authority:** Read-only / shadow-only. This contract has **zero** order authority (§16).
**Provenance:** Derived from the frozen charter (`charter.frozen.json`, hash
`b3e1a8e5…efcf6f`) and `VERDICT.md`. Where the debate froze a value, it is marked **[frozen]**.
Where it did not, a conservative default is chosen, explained, versioned, and made configurable —
marked **[default-v1]**. Defaults must never be silently re-tuned after observing results (§15).

All identifiers below (`cost_model_v1`, `schema_v1`, `feature spec versions`) are immutable once data is
written under them. A change means a new version id and a new column/record, never an in-place edit.

---

## 1. Time semantics  [frozen: no-lookahead; defaults-v1: resolution]

| Concept | Definition | Storage |
|---|---|---|
| `event_time_us` | Exchange-stamped time the event happened. From Coinbase `time` (trades) / `event_time` (L2 updates) / message `timestamp`. UTC, microseconds. | INTEGER µs since epoch |
| `recv_time_us` | Local wall-clock (UTC) when our process finished parsing the frame. | INTEGER µs since epoch |
| `ingest_time_us` | When the row was committed to the research store. | INTEGER µs since epoch |
| `decision_time_us` | For a label: the event_time at which a hypothetical decision is taken. Only data with `event_time_us <= decision_time_us` may inform it. | INTEGER µs |
| `availability_time_us` | For context events: when the info was *publicly knowable* (release time), which may be **later** than the event it describes. Features key off availability, never event subject time. | INTEGER µs |

**Rule:** every feature and label carries source, `event_time_us`, `recv_time_us`, availability where
applicable, freshness, and confidence. No value may be computed from data whose
`availability_time_us`/`event_time_us` is after the decision time. Clock: `recv_time_us` is taken from a
single monotonic-anchored UTC reading per frame to avoid wall-clock jitter mid-batch.

---

## 2. Source identity & provenance  [frozen: §non_negotiables]

Every raw record stores: `source_id`, `source_kind` (`coinbase_ws` | `fred` | `bls` | `edgar` | `cftc` |
`funding` | `onchain`), `endpoint` (URL or `wss://…` + channel), `schema_version`, `retrieval_time_us`,
`payload_sha256`, and the verbatim `payload` (canonical JSON text). Normalized rows reference the raw row id.

`payload_sha256 = sha256(canonical_json(payload))` where canonical JSON = UTF-8, sorted keys, no
insignificant whitespace, `separators=(',',':')`. The hash is the dedup key for raw evidence (§3).

---

## 3. Deduplication & idempotency  [default-v1]

| Stream | Idempotency key | On duplicate |
|---|---|---|
| Raw frame | `payload_sha256` | Ignored (append-only store rejects exact re-insert; counted in ledger) |
| Normalized trade | `(product_id, trade_id)` | Upsert no-op |
| L2 update | `(connection_epoch, sequence_num, side, price_level)` | Ignored within epoch |
| Ticker | `(product_id, sequence_num)` | Latest wins (quotes are state) |
| Context event | `(source_id, native_id, vintage)` | New `vintage` = new row (revisions preserved, §9) |

`connection_epoch` increments on every (re)connect; sequence numbers are only comparable within an epoch.

---

## 4. Freshness & stale-data behavior  [default-v1]

| Parameter | Default | Meaning |
|---|---|---|
| `max_quote_staleness_us` | 2_000_000 (2 s) | A label's executable quote must be ≤ this old vs `decision_time_us`, else label `valid=false, reason=STALE_QUOTE`. |
| `max_book_staleness_us` | 5_000_000 (5 s) | No L2 update within this window ⇒ book `stale`; features depending on book emit `null` + `stale` flag. |
| `max_trade_gap_us` | 60_000_000 (60 s) | No trades within this window ⇒ `trade_intensity`-type features emit `null` + `quiet` flag (not zero). |

Stale ⇒ **emit null + flag**, never impute. Missing inputs never silently become `0`.

---

## 5. Outage / gap flags & recovery  [frozen: failure handling required]

- **Sequence gap:** within an epoch, `sequence_num` must increase by 1 per message of that channel where
  Coinbase provides per-message sequencing. A jump ⇒ `GAP` record `(channel, epoch, last_seq, new_seq)`;
  any L2 book is marked `invalid` until the next `snapshot` rebuilds it.
- **Reconnect:** new `connection_epoch`; the book is `invalid` until a fresh `snapshot` arrives.
- **Crossed book:** `best_bid >= best_ask` ⇒ book `invalid` (`CROSSED`); affected labels/features invalid.
- **Stale book:** see §4.
- **Recovery:** a book becomes `valid` only after a complete `snapshot` followed by contiguous updates.
  We **never** synthesize a "healthy" book after an unrepaired gap.

---

## 6. Retention & storage budget  [default-v1]

- Raw evidence is **append-only**. The retention tool may prune normalized/derived rows but **must refuse
  to delete raw rows that have not been exported** (export = written to an external Parquet/JSONL archive
  with its own manifest + hashes). Default: `allow_raw_prune=false`.
- `storage_warn_bytes` = 2 GB (warn), `storage_block_bytes` = 8 GB (collector refuses new raw writes,
  logs `STORAGE_BLOCKED`, exits cleanly). Both configurable.
- L2 stored as compact normalized updates (not per-tick full snapshots) to keep 90-day single-symbol
  capture within SQLite's comfortable range; interfaces allow a later Parquet/DuckDB backend.

---

## 7. Replay tolerances  [frozen: replay/live parity required]

- **Deterministic replay:** replaying a fixed event log must reproduce identical book states and identical
  feature/label rows. Prices/sizes compared exactly (they are reconstructed from the same integers/strings).
- **Live/replay parity:** features computed live-shadow vs via replay on the *same* event slice must match
  within `parity_abs_tol = 1e-9` for ratio/price features and exactly for integer/count features.
- A parity failure beyond tolerance is a **blocking** test failure.

---

## 8. Executable labels  [frozen: horizons & bid/ask-aware; default-v1: friction]

**Horizons [frozen]:** 5 min, 15 min, 1 hour, 4 hours.

For a long/flat spot account (no shorting — `charter.constraints`), each label at decision time `t` for
horizon `h` records:

| Field | Definition |
|---|---|
| `decision_time_us` | `t` |
| `horizon` | one of `5m,15m,1h,4h` |
| `entry_side` / `entry_price` | BUY at executable **ask** at `t` (+ slippage, §10) |
| `exit_side` / `exit_price` | SELL at executable **bid** at `t+h` (− slippage, §10) |
| `gross_return` | `exit_price/entry_price - 1` using mid-to-mid (diagnostic only) |
| `fee_component`, `slippage_component`, `adverse_selection_component`, `spread_component` | each friction term, in return units |
| `net_return` | executable bid-out vs ask-in, minus fees & adverse selection (spread already in the quotes) |
| `mfe` / `mae` | max favorable / adverse excursion over `[t, t+h]` where book coverage supports it, else `null` |
| `valid` / `invalid_reason` | gates from §4–§5 (STALE_QUOTE, CROSSED, GAP, NO_HORIZON, …) |
| `quote_source` | `book_top` \| `ticker` \| `fallback_mid` (lower confidence) |
| `cost_model_version` | `cost_model_v1` |
| `replay_version` | snapshot/replay id used |

A label is **only emitted when `t+h` data actually exists** (no horizon ⇒ no row, never a guessed value).

**Spread is not double-counted:** entering at the real ask and exiting at the real bid already pays the
spread. The `spread_component` is reported for diagnostics (ask−bid at `t`), not added again to `net_return`.
When no real quote exists, fall back to `mid ± half_spread_bps` and set `quote_source=fallback_mid`.

---

## 9. Context revisions / vintage  [frozen: no revised-data leakage]

Context series (CPI, etc.) are revised. Store every observation with `(native_id, vintage,
availability_time_us)`. A feature at decision time `t` may only read the vintage whose
`availability_time_us <= t`. The pipeline never overwrites an old vintage with a newer one.

---

## 10. Cost model `cost_model_v1`  [default-v1 — conservative, configurable]

Defaults reflect a ~$100 account permanently in Coinbase Advanced Trade's **lowest** 30-day-volume tier.
The repo's existing `research/costs.py` default of **8 bps taker is ~10× too optimistic** for this account
and must not be used for promotion math.

| Component | Default | Basis | Notes |
|---|---|---|---|
| `taker_fee_bps` | **60.0** per side | Coinbase Advanced lowest-tier taker ≈ 0.60% (some schedules cite up to 1.20%) | IOC limit orders that take liquidity pay taker |
| `slippage_bps` | **2.0** per side | conservative for a $10–$15 clip in a deep BTC book | applied on top of executable quote |
| `adverse_selection_bps` | **2.0** | short-horizon markout penalty on entries | |
| `latency_us` | **500_000** (0.5 s) | decision→executable-quote allowance | interacts with §4 staleness |
| `half_spread_bps` (fallback only) | **1.0** | used only when no real quote (`fallback_mid`) | real spread comes from quotes |
| `sensitivity_sweep` | **{0.5×, 1.0×, 1.5×, 2.0×}** | 2.0× ≈ 120 bps taker = lowest-tier worst case | **promotion must hold at the binding stress level, not just 1.0×** |

Round-trip floor at 1.0× ≈ `2×(60+2)` + adverse ≈ **126 bps**; at 2.0× ≈ **250 bps**. Any candidate whose
edge is smaller than this after purged-WF is rejected (this is the debate's central dissent: microstructure
alpha may be smaller than Coinbase friction).

---

## 11. Feature registry & variant budget  [frozen: count all variants]

First microstructure family is **pre-registered and compact** (the spine ships exactly one; the rest are
registered specs, not yet computed). Each spec declares: `name`, `version`, required inputs, event-time
window, freshness rule, missing-data behavior, output unit.

| # | Feature | Inputs | Status in spine |
|---|---|---|---|
| 1 | `quoted_spread_bps` | best bid/ask | **implemented** |
| 2 | `top_of_book_imbalance` | bid/ask sizes L1 | spec |
| 3 | `depth_imbalance_10bps` / `25bps` | L2 bands | spec |
| 4 | `multilevel_imbalance` | L2 N levels | spec |
| 5 | `signed_trade_flow` | trades side | spec |
| 6 | `trade_intensity` | trade count/Δt | spec |
| 7 | `orderbook_pressure_change` | L2 Δ | spec |
| 8 | `liquidity_shock` | depth Δ | spec |
| 9 | `realized_volatility` | mid returns | spec |
| 10 | `short_horizon_adverse_selection` | quote + markout | spec |

**Variant budget [default-v1]:** Track-1 microstructure ≤ **12** registered variants; Track-2 context ≤ **12**.
Every variant ever evaluated (including discarded parameterizations) is recorded in the variant registry and
counted for multiple-testing (§13). The registry is the denominator; you cannot un-count a variant.

**Baselines [frozen]:** `no_trade`, `buy_and_hold`, `price_volatility_only`, `archived_breakout`.

---

## 12. Purged walk-forward & embargo  [frozen]

- **Purged** splits: training observations whose label horizon overlaps any test observation's window are
  **dropped** (purged), preventing horizon leakage.
- **Embargo:** an additional embargo of `embargo = ceil(horizon × embargo_mult)` bars after each test block is
  excluded from training. `embargo_mult` **[default-v1] = 1.0**.
- Splits are time-ordered, never shuffled. Min test fraction and number of folds are configurable
  (`n_folds` **[default-v1] = 6**).

---

## 13. Effective sample size & multiple testing  [frozen requirement]

- **ESS [default-v1]:** because labels at horizon `h` overlap, report both raw `N` and a non-overlapping
  `ESS ≈ T_span / h`, plus an autocorrelation-adjusted `ESS = N / (1 + 2·Σ ρ_k)` over the overlap window.
  A track needs `ESS ≥ ess_floor` (**[default-v1] = 50** non-overlapping observations per horizon) before
  any inferential claim.
- **DSR (Deflated Sharpe Ratio):** implemented from Bailey & López de Prado (2014): non-annualized
  observed Sharpe, expected maximum Sharpe across trials, sample length, skewness, and kurtosis.
- **PBO via CSCV:** implemented with contiguous symmetric half/half partitions and OOS rank logits.
  Threshold `PBO < 0.40` **[default-v1]**.

---

## 14. Promotion / demotion / failure gates  [frozen: separate authorization]

**Promotion (ALL must pass):**
1. Positive expectancy after `cost_model_v1` at **1.0× and at the binding stress (2.0×)**.
2. Incremental value over **all four** baselines (§11).
3. Purged-WF OOS positive across folds.
4. `ESS ≥ ess_floor` per horizon.
5. `DSR > 0` **and** `PBO < 0.40`.
6. Replay parity, freshness, outage/storage gates all green.

DSR/PBO availability no longer blocks mechanically. Promotion remains **HARD-BLOCKED** whenever
the trial matrix, all baselines, purged-WF folds, ESS, binding-stress returns, or operational gates
are missing or fail. Clearing every evidence gate emits `EVIDENCE_PASSED`, never live authority.
BH-FDR alone never authorizes promotion.

**Promotion still requires a separate, human-authorized, one-writer implementation step** (charter
`non_negotiables`): clearing the gate produces *evidence*, not a live change.

**Demotion:** a track that clears data/replay gates but fails edge gates is demoted to
annotation/suppression only — never a live order source.

**Failure:** any unrepaired gap/crossed/stale book invalidates affected labels/features for that window;
they are excluded from evaluation, not patched.

---

## 15. Anti-optimization discipline  [frozen: §working style]

Defaults in §1–§14 are versioned. Changing one after observing results requires a new version id and a
written rationale in the variant registry; the old results remain attributed to the old version. No silent
re-tuning toward a desired answer.

---

## 16. Separation from live order authority  [frozen non-negotiable]

The pipeline:
- imports **no** `bot/` module and never instantiates `bot.db.db` (avoids the import-time journal side
  effect, ARCHITECTURE_REVIEW F-C1);
- uses its **own** SQLite database, never `journal.db` / `live_journal.db` / `paper_journal.db`;
- contains **no** brokerage/order adapter and no path to `rest.create_order`;
- is enforced by automated **boundary tests** (Phase 3) that fail if research code can import a live
  execution/journal module or open a journal database.

Context, in this test-off, is **annotations / suppressors / regime tags only**. It cannot create orders.

---

## 17. Channel availability  [verified 2026-06-19 — empirical]

Coinbase Advanced Trade WS, `wss://advanced-trade-ws.coinbase.com`:

| Channel | Public? | Used by spine | Notes |
|---|---|---|---|
| `market_trades` | yes | yes | `trade_id/price/size/side/time` |
| `ticker` | yes | yes | carries `best_bid/best_ask(+qty)` — the executable-quote source |
| `heartbeats` | yes | yes | liveness; connection `sequence_num` continuity |
| `level2` | **yes** | **yes** | Initial BTC snapshot is ~4–5 MB. The prior apparent auth failure was the client’s 1 MB message limit. Collector now allows 16 MB and batch-inserts snapshot rows. Messages arrive with `channel:"l2_data"`. |

`sequence_num` is a **single per-connection counter shared across channels**; gap detection is
connection-level. Public Level2 now supports depth-band and multilevel imbalance features without
credentials. The quoted-spread feature and executable labels continue to use `ticker` top-of-book.
