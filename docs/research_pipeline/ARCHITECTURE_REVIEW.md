# Architecture Review — Coinbase RTB Bot & Research Pipeline

**Reviewer:** Claude (Opus 4.8), independent pass
**Date:** 2026-06-18
**Scope:** The live `crypto_trading` runtime and the proposed read-only research pipeline.
**Method:** Read of actual entrypoints, imports, data flow, SQLite ownership, and the
`research/` package. Every architectural claim below is cited to a file and line that
was inspected, not to `docs/system/architecture.md` (which is stale — see F-H2).

---

## 1. Executive verdict

The live bot is **small, legible, and operationally hardened** — single-instance lock,
WAL + busy-retry SQLite, public Coinbase market-data socket, REST reconciliation, sell-only
exit watcher, hard kill switch, and tiny notional caps ($10 test / $15 order / $30 position).
For what it is — a $100 long/flat BTC-USD execution lane — the monolithic async design is
**appropriate and should not be re-architected now.**

The honest problems are not in execution mechanics; they are in **governance and provenance**:

1. **The live lane is armed in `mode: live` running a strategy its own research has formally
   archived** (`config.yaml:5`, `config.yaml:71`, `config.yaml:79`). It is safe only because the
   archived breakout strategy emits essentially no signals. The *authority* is live even though
   the *edge* was retired. This is a governance mismatch, not an execution bug. (F-C2)

2. **A module-level DB singleton (`bot/db.py:200`) creates and initializes a journal database
   at import time.** Importing almost any `bot/` module (`journal`, `events`, `execution`,
   `state_machine`) has the side effect of opening/initializing `journal.db` in the current
   working directory. This is the single most dangerous coupling for *any* new subsystem in this
   repo, and it dictates the research pipeline's first hard rule: **the research package must not
   import the live `bot/` data/execution modules at all.** (F-C1)

3. **The original statistical governance did not exist.** `research/multiple_testing.py`
   has a real Benjamini-Hochberg FDR screen but its `cscv_pbo_check()` still raises
   `NotImplementedError`. The isolated `research_pipeline` now implements DSR and CSCV/PBO;
   promotion remains blocked whenever the required evidence matrix, baselines, folds, ESS,
   stress returns, or operational gates are incomplete. (F-M3)

The requested read-only research pipeline **can be built safely** as a separate package with its
own SQLite database and its own entrypoints, **provided** it never imports the live journal/execution
modules and never instantiates `bot.db.db`. Nothing in the safe subsystem requires touching live
execution. **Proceed.**

Verdict on the debate's question: the microstructure-first **shared spine** (immutable executable-
labeling layer + provenance store) is the correct first build regardless of which track ultimately
wins, because both tracks consume it. Build the spine; keep both tracks as pre-registered consumers.

---

## 2. Current-state component & data-flow diagram

```text
                         ┌──────────────────────────────────────────────┐
   Coinbase Advanced     │  wss://advanced-trade-ws.coinbase.com         │
   Trade WS (PUBLIC)     │  channels: market_trades, heartbeats          │  (coinbase_adapter.py:46,265-266)
                         └───────────────┬──────────────────────────────┘
                                         │ JSON frames
                                         ▼
                          CoinbaseAdapter.ws_loop  (coinbase_adapter.py:235)
                                         │ asyncio.Queue (market_queue)
                                         ▼
                          MarketDataProcessor.run  (market_data.py:22)
                            trade → BarBuilder.process_trade  (market_data.py:43)
                                         │ closed bars
                                         ▼
                          StateMachine  (state_machine.py)  ── emits ──► signals table
                                         │
   ┌─────────────────────────────────────┼───────────────────────────────────────────┐
   │ main.py async tasks (main.py:45-57 imports; loop orchestrates)                    │
   │                                                                                   │
   │  _process_new_signals ─► ExecutionService.process_signal (execution.py:22)        │
   │        │                         creates PENDING order → Journal.insert_order     │
   │        ▼                                                                          │
   │  reconcile_pending_orders (execution.py:184)                                      │
   │        └─► adapter.submit_order_intent (coinbase_adapter.py:143)                  │
   │                  └─► rest.create_order(limit_limit_ioc)  ◄── ONLY real-money call │
   │                       kill-switch checked here (coinbase_adapter.py:167)          │
   │                                                                                   │
   │  Safeguards (safeguards.py): order/position size caps, can_trade()                │
   └───────────────────────────────────────────────────────────────────────────────┘
                                         │
                                         ▼
            bot.db.db  (singleton, bot/db.py:200; path rebound at main.py:501)
            ┌──────────────────────────────────────────────────────────────┐
            │ live_journal.db  (mode=live)  /  paper_journal.db (mode=paper) │
            │   tables: bars, event_log, runtime_state, signals, orders,    │
            │           executions, positions, equity_snapshots             │
            │ journal.db  ← created at IMPORT TIME as a side effect (F-C1)   │
            └──────────────────────────────────────────────────────────────┘

   Out-of-process companions:
     live_exit_watcher.py   (root) — sell-only exit monitor, reads same journal, never buys
     start-live.ps1         (root) — launcher
     .cb_rtb_bot.lock       — single-instance guard (single_instance.py)

   Offline / not in the live path:
     research/   — candle backtest harness (walkforward, costs, multiple_testing, lane probes)
     src/        — DEAD. Not imported by main.py (main.py:8 header: "src/ is not imported")
```

**DB selection trace (verified):** `bot/db.py:200` runs `db = Database()` → default `journal.db`
is created and `_init_db()` executes at import. `main.py:500-501` then sets
`db.db_path = config.live_db_path()` (`live_journal.db`, since `config.yaml:5` is `mode: live`).
Net effect: the live process *operates* on `live_journal.db`, but `journal.db` is still
materialized in the CWD purely from importing the module.

---

## 3. Strengths worth preserving (do not "improve" these)

- **Single-instance OS lock** (`single_instance.py`, wired at `main.py:26-43`) — prevents the classic
  double-bot disaster. Keep.
- **WAL + busy-timeout + bounded retry** (`bot/db.py:30-45,139-140`) — correct concurrency posture for a
  long-running writer plus the sell-only exit watcher reading concurrently.
- **Defense-in-depth kill switch** checked *immediately before the REST order call*
  (`coinbase_adapter.py:167-172`), not only at the strategy layer.
- **Tiny, explicit notional caps** enforced in two places — order cap pre-submit, position cap on fill
  (`execution.py:65-67`, `execution.py:177-178`; `config.yaml:64-65`).
- **Idempotent order creation** keyed on `signal_id` (`execution.py:27-30`) and idempotent bar/position
  upserts via `ON CONFLICT` (`journal.py:13-26,159-179`).
- **Public-only market socket** — no JWT churn, no private socket required for data
  (`coinbase_adapter.py:235-266`). The research collector should follow the same public-only posture.
- **Reusable friction model** — `research/costs.py` `FrictionModel` (bps fee/half-spread/slippage with a
  uniform sensitivity multiplier) is a clean, conservative base the labeler can reuse for the fee/slippage
  components. The labeler must add *executable bid/ask* on top (F-L1).

---

## 4. Findings (ranked)

### Critical

**F-C1 — Import-time global DB singleton writes to the CWD.**
`bot/db.py:200` instantiates `Database()` at import, which creates and `_init_db()`s `journal.db`
in the process CWD. Any transitive import of `bot.journal` / `bot.events` / `bot.execution` /
`bot.state_machine` triggers this. *Impact on research:* a research module that innocently
`import`s a bot helper would materialize/mutate a journal DB. *Mitigation (enforced in Phase 3):*
the `research_pipeline/` package imports **zero** `bot/` modules; an automated boundary test asserts
this and asserts that importing the package creates no `*journal*.db`.

**F-C2 — Archived strategy remains armed in live mode (governance mismatch).**
`config.yaml:5` `mode: live`, `config.yaml:71` `live_trading_confirmed: true`, `config.yaml:79`
`test_order_notional_usd: 10.0`. The 1h breakout/retest/continuation strategy was formally archived
by the repo's own research (one trade in 365 days). It is *operationally* safe today only because it
almost never emits a signal — but the order authority is live and the position cap is real money.
This review **does not change it** (outside the safety boundary for this task) but flags it as an
explicit decision Isaiah should make: either (a) consciously keep the armed-but-silent lane, or
(b) set `mode: paper` / `trading_enabled: false` until a promoted strategy exists. Recommended: (b),
because an armed lane running a retired strategy is a latent surprise.

### High

**F-H1 — Test discovery excludes research tests.**
`pyproject.toml` is exactly `testpaths = ["bot/tests"]`. `research/tests` and any future
`research_pipeline/tests` are **not** collected by a bare `pytest`. Fix in Phase 3: widen discovery to
include research suites without weakening bot coverage (add paths, keep `bot/tests` first).

**F-H2 — `docs/system/architecture.md` is materially stale.** Concrete inaccuracies:
- Claims a "single centralized SQLite instance" — there are at least three journals
  (`journal.db`, `live_journal.db`, `paper_journal.db`) plus per-test DBs.
- Lists `bot/main.py` and `bot/config.yaml` as the entrypoint/parameters — the real runtime is the
  **root** `main.py` (`main.py:7-8` states `bot/main.py has been deleted`) and the **root** `config.yaml`
  (loaded via `bot/config.py:13`, `../config.yaml`).
- Says "0.20% risk"; `config.yaml:19` is `risk_per_trade: 0.0035` (0.35%).
- Omits `src/` (dead), `single_instance`, `notifications`, `readiness`, `backfill`, `safeguards`,
  `live_exit_watcher.py`, and the entire `research/` package.
Treat that doc as untrusted until rewritten; this review supersedes it for the data path.

**F-H3 — Synchronous REST inside the async runtime.**
`submit_order_intent` / `sync_get_fills` / `sync_get_order` are blocking calls invoked from the
reconcile path (`execution.py:199,211,231`). Fine for tiny execution, but it means **research collection
must not be colocated** in this process — a blocking research call (or a heavy L2 reconstruction) could
stall reconciliation. Argues for a **separate research process** (see §5).

**F-H4 — Dirty, partly-untracked worktree.**
The entire `research/` package, `live_exit_watcher.py`, `notifications.py`, `single_instance.py`, and a
large set of `test_*.db` artifacts are untracked or modified (branch `feat/live-plumbing`). This is
valuable, unpushed work. *This task preserves all of it.* Recommendation (defer, with Isaiah's
consent): add `test_*.db`, `*.db-wal`, `*.db-shm` to `.gitignore` and commit `research/` deliberately so
it stops being at risk.

### Medium

**F-M1 — Dead `src/` tree.** `src/{connectors,core,db,services}` is not imported (`main.py:8`).
It duplicates concepts (adapters, db, services) and invites confusion / accidental edits. Defer removal,
but quarantine mentally: it is not part of any live or research path.

**F-M2 — Journal DB ownership is non-obvious.** The import-time singleton binds `journal.db`; the live
process rebinds to `live_journal.db` only at `main.py:501`. A reader cannot tell from `bot/db.py` which
file is authoritative. The research store sidesteps this entirely by using its **own** database file and
**never** importing `bot.db`.

**F-M3 — Statistical governance was a stub presented as staged.** The original
`cscv_pbo_check()` still raises `NotImplementedError`, but the isolated pipeline now implements
DSR and CSCV/PBO. It never emits evidence-passed status from BH-FDR alone.

**F-M4 — Live event timestamps are 1-second integers.** `event_log.ts`, executions `ts`, etc. are
`int(time.time())` seconds (`journal.py:34,147`; `execution.py:120`). That resolution is fine for 1h-bar
execution but **insufficient for microstructure research**, which needs millisecond event-time *and* a
separate receive-time. The research store must define its own high-resolution, dual-timestamp schema
rather than reuse the journal's.

### Low

**F-L1 — `FrictionModel` half-spread is a constant, not executable.** `research/costs.py:25` uses a fixed
`half_spread_bps`. Reusable for the fee + slippage components, but executable labels must derive the
spread from real best-bid/best-ask (the `ticker` channel provides `best_bid`/`best_ask` directly).

**F-L2 — Backtest types are bar-index based.** `research/types.py` `Trade`/`BacktestResult` index by bar,
not event-time. Good for the offline candle harness; the new label/replay layer must be event-time native
for live/replay parity.

---

## 5. Recommended target architecture

```text
research_pipeline/                  ← NEW, separate package, zero imports from bot/
  storage/        immutable raw-event store + normalized tables (own SQLite, WAL, migrations)
  collectors/     Coinbase PUBLIC ws collector (market_trades, ticker, heartbeats; see note)
  book/           deterministic L2 order-book reconstruction + health (gaps/crossed/stale)
  labeling/       executable bid/ask-aware forward labels @ 5m/15m/1h/4h + friction + replay
  features/       pre-registered microstructure features (versioned, freshness-aware)
  context/        adapter interfaces for FOMC/CPI/EDGAR/CFTC/funding/on-chain (annotations only)
  governance/     baselines, purged walk-forward + embargo, variant registry, DSR/PBO (BLOCKED)
  cli/            entrypoints: collect / smoke / health  (never import bot/)
  tests/          migrations, append-only, dedup, hashing, gap, replay parity, BOUNDARY tests
  config/         conservative defaults (YAML), separate from bot config.yaml
```

**Process topology:** a **separate process** from the live bot (F-H3). The research collector and the
live bot share *nothing* at runtime except read-only access to the same public Coinbase socket (each
opens its own connection). No shared DB, no shared lock, no shared config.

**Storage:** start with a dedicated **SQLite** DB (`research_pipeline.db` or similar) with WAL and explicit
migrations. SQLite is adequate for a 90-day local single-symbol L2 capture *if* L2 is stored as compact
normalized updates, not full snapshots per tick. Design the read interfaces so the backing store can move
to **Parquet/DuckDB** later without changing callers if L2 volume proves heavy. Do **not** introduce
distributed infra now.

**Why a shared spine first:** both debate tracks (microstructure, context) consume the same
executable-labeling layer and the same provenance store. Building the spine is the highest-leverage,
lowest-risk move and is track-agnostic.

---

## 6. Migration / build sequence

1. **Spine (complete).** Immutable ingestion store + boundary safety tests → public Coinbase
   trades/Level2/quotes → deterministic replay + executable labeler (5m/15m/1h/4h) → eight
   microstructure features → live smoke/benchmark → machine-readable health.
2. **Context adapters (partially complete).** Federal Reserve/BLS/EDGAR/CFTC are implemented with
   provenance and conservative availability; funding/OI and on-chain remain explicit access gaps.
   Context is **annotations only**, with no order path.
3. **Governance (algorithms complete, evidence incomplete).** Purged walk-forward + embargo +
   variant registry + ESS + DSR/PBO are real code. Three baselines and complete candidate evidence
   remain unfinished, so incomplete evidence stays explicitly BLOCKED.
4. **(Defer, separately authorized)** 90-day shadow run; one-writer promotion path; any change to the
   live lane; removal of `src/`; `.gitignore` hygiene for `test_*.db`.

---

## 7. Safe to implement now  ✅

- A new `research_pipeline/` package that imports **no** `bot/` modules and never instantiates `bot.db.db`.
- A **separate** research SQLite database with its own migrations.
- A **public-only** Coinbase WS collector (`market_trades`, `level2`, `ticker`, `heartbeats`) on its
  own connection. The initial Level2 snapshot needs a larger WebSocket message limit; `ticker`
  supplies the executable best bid/ask.
- Deterministic replay + executable bid/ask-aware labels + ≥1 microstructure feature.
- Automated **boundary tests** proving research code cannot place orders or open/mutate any journal DB.
- A bounded **smoke test** writing only to research storage; a machine-readable health report.
- The four documents (this review, the implementation contract, runbook, implementation report).
- Widening `pyproject.toml` test discovery to include research suites (additive only).

## 8. Defer until separately authorized  ⛔

- Any change to `config.yaml`, env vars, credentials, caps, thresholds, cron, launchers, exit watcher, or
  kill switch — including resolving the F-C2 governance mismatch (flagged, not actioned).
- Any import of research decisions into `main.py` / `state_machine.py` / `execution.py` / order path.
- Any brokerage/order adapter inside `research_pipeline/`.
- Installing or modifying scheduled tasks / long-running collectors.
- Deleting the dead `src/` tree or rewriting `docs/system/architecture.md` beyond noting its staleness.
- Editing the wiki.
- Implementing the live promotion (one-writer) path.

---

## 9. Reconciliation notes (post-implementation, 2026-06-19)

- **`level2` is public.** The earlier 0-frame result was a client-side
  `PayloadTooBig` failure: BTC-USD's initial snapshot is about 4.6 MB, above the WebSocket
  library's 1 MB default. With a 16 MB limit, public Level2 streams normally and depth features work.
- **Coinbase `sequence_num` is per-connection (shared across channels), not per-channel.** Gap
  detection is connection-level; an early per-channel implementation produced ~70% false-positive
  gaps and was corrected.
- **Level2 messages arrive with `channel: "l2_data"`** (you subscribe with `"level2"`). The
  collector dispatch handles both.

*Where implementation and review disagreed, this document was updated so the final docs describe
the system that exists.*
