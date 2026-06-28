# Crypto Trading — Coinbase BTC-USD research-and-trading spine

A fail-closed, single-process system for **discovering, testing, and (eventually)
executing** BTC-USD strategies on Coinbase Advanced Trade. It is intentionally
monolithic and async for full visibility into the edge logic. The end goal is a
verified end-to-end pipeline — data → feature → decision → risk → order → fill →
reconcile → guarded exit → accounting/learning — that can be promoted to small
live trades **only** when evidence and safety gates pass.

> This README is the canonical map of the repo. If something here disagrees with
> an older doc, this file and the wiki page **Crypto Trading Bot** win.

## Current state — 2026-06-28

- **Live trader:** running, but **entry-halted** under `KILL_SWITCH` (new BUYs
  blocked; risk-reducing SELL exits stay enabled). Caps: `$15` max order /
  `$30` max position / `$10` fixed test notional; product allowlist = `BTC-USD`.
- **Live trades to date: 0.** `live_journal.db` integrity `ok`; signals = orders
  = executions = positions = `0`.
- **Strategy authority:** none. The configured strategy
  (`btc_breakout_retest_continuation`) is **archived/disabled**; live BUYs require
  a separate, expiring, tamper-evident authorization linked to an
  `EVIDENCE_PASSED` artifact. None exists, so nothing can buy.
- **Research evidence:** `INSUFFICIENT_EVIDENCE` (cloud shards not yet eligible).
- **Active candidate:** `reversion_dislocation` (BTC cost-gated dislocation
  reversion, pre-registered v1) — enacted 2026-06-25 from the crossover debate;
  pending real shards, **not** authorized.
- **Shadow finding:** the EMA 8/34 crossover loses to fees (≈ **−1.82% net over
  77 days**); it runs only as a non-executing alerter.

## Repo map

### Live surface (running — do not move/rename casually)
| File | Role |
| --- | --- |
| `main.py` | The live bot orchestrator (data → state machine → execution → reconcile → exits). Launched via `start-live.ps1`. |
| `live_exit_watcher.py` | Every-minute risk-reducing SELL-exit watcher; reads `live_journal.db` read-only and returns early when no position is open. |
| `shadow_strategy_runner.py` | Non-executing EMA 8/34 alerter on live 1h bars; pushes "would-buy/would-sell + P&L" via Pushover. Never imports the execution path. State in `shadow_state.json`. |
| `candidate_advisory_bridge.py` | Validates the expiring research advisory before any candidate signal can stage. |
| `authorize_strategy.py` | Mints the one-writer live authorization (only after evidence passes). |
| `verify_coinbase.py`, `verify_live_ready.py`, `verify_tiny_live_ready.py` | Pre-flight readiness checks. |
| `config.yaml` | All runtime/risk/safety/live settings (caps, allowlist, kill-switch, gates). |
| `KILL_SWITCH` | Presence = entry halt. Delete to allow BUYs (only with authorization). |

### Packages
- **`bot/`** — the live engine (config, db, coinbase adapter, market data,
  aggregator, state machine, execution, safeguards, notifications, journal,
  strategy, strategy_authorization, readiness, learning, events). **Canonical.**
- **`research_pipeline/`** — the research spine (features incl. order math,
  advisory publish/validate, candidates, CLIs, symmetric-forward & live-shadow
  replay). **Canonical.** Candidates live in `research_pipeline/candidates/`.

### Data & artifacts
- `research_pipeline_data/` — **1.6 GB** of gitignored research shards/DB. Not litter; do not commit.
- `live_journal.db` (active live journal) · `journal.db` (pre-live snapshot) · `shadow_state.json` (shadow position/track record).
- `outputs/` — debate evidence (e.g. the 2026-06-25 crossover decision that produced `reversion_dislocation`). Untracked; preserved as provenance.
- `tiny_live_acceptance.json` — the latest acceptance receipt (must be < 5 min old at BUY).

### Docs & legacy
- `docs/` — architecture, strategy memos, runbooks, risk, paper-mode reports. Start at `docs/README.md`.
- `archive/` — **superseded code** (old `v1_src/`, old `research_lanes/`, April one-off scripts, old data caches). Kept for history, never run — see `archive/README.md`.

## How it runs

- **Launcher chain:** `start-live.ps1` → venv `python main.py` → re-exec under
  system Python (so PIDs appear as launcher → live child).
- **Hermes cron:** the shadow runner (`*/15`) and exit watcher (every minute) are
  driven by Hermes, not persistent processes. Both fail-closed and are idempotent
  per closed bar / skip when no position exists.
- **Kill switch:** create/keep `KILL_SWITCH` to block new BUY exposure
  immediately (checked every reconcile tick and right before each BUY). SELL
  exits are never blocked.

## Tests

```
venv/Scripts/python -m pytest          # bot/tests + research_pipeline/tests
```

`pyproject.toml` `testpaths` cover `bot/tests` and `research_pipeline/tests`. The
legacy `research/` lane suite was moved to `archive/research_lanes/` and is
intentionally no longer collected.

## Safety invariants (do not weaken without an explicit promotion decision)

1. Research evidence is **not** order authority — BUYs need a matching expiring authorization.
2. `KILL_SWITCH` halts entries only; automatic SELL exits stay live.
3. Hard USD caps + single-product allowlist are enforced before submission.
4. Live mode needs both `live_trading_confirmed: true` **and** env `LIVE_TRADING_CONFIRMED=true`.
