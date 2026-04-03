# Paper-Mode Integration Build Report

**Branch:** `stabilization/cleanup`  
**Baseline commit:** `6df27ce` (stabilization phase complete, 31 tests passing)  
**Final commit:** `319100e`  
**Test count:** 31 → 45 (all passing)

---

## Overview

This report documents every change made during the paper-mode integration phase,
following `docs/paper_mode_implementation_spec.md`. The goal was to make the bot
runnable end-to-end from `python main.py` in paper mode — live Coinbase WebSocket
data, deterministic synthetic fills, operator safeguards, and structured event
logging — without touching real order routing or the `src/` directory.

Seven commits were made, one per phase.

---

## Commits

| Commit | Message |
|--------|---------|
| `e0c3170` | feat(config): add runtime mode and config loader |
| `7d31852` | feat(aggregator): bridge bar builder output to state machine |
| `1c04fd1` | feat(adapters): paper adapter with deterministic synthetic fills |
| `8842d80` | feat(safeguards): operator safety guards |
| `615caad` | feat(events): structured event logging and session summary |
| `f8a6584` | feat(runtime): paper-mode end-to-end orchestrator |
| `319100e` | test(paper): add paper-mode, safeguard, and event-log tests (45 total) |

---

## Phase 1 — Config loader (`e0c3170`)

**Files changed:** `config.yaml`, `bot/config.py` (new)

### `config.yaml`
Added a `runtime` section and `max_pending_order_age_sec` under `execution`:

```yaml
runtime:
  mode: paper
  trading_enabled: true
  portfolio_value: 10000.0
  paper_db_path: paper_journal.db

execution:
  max_pending_order_age_sec: 60
```

### `bot/config.py`
New typed config accessor module with a fail-fast `validate()` function.

**Accessors added:**
- `runtime_mode()` — defaults to `"paper"` if not set
- `trading_enabled()` — bool from `runtime.trading_enabled`
- `portfolio_value()` — float, must be > 0
- `paper_db_path()` — path string for the paper-mode SQLite DB
- `symbol()` — single symbol string (validates count == 1)
- `symbols()` — list of symbols
- `reconcile_interval_sec()` — int, must be > 0
- `max_pending_order_age_sec()` — int, must be > 0
- `ws_stale_timeout_sec()` — int
- `max_daily_loss()` — float

**`validate()` exits on:**
- `mode` set to anything other than `"paper"` or `"live"`
- `mode == "live"` (not implemented)
- `symbols` count != 1
- `portfolio_value` <= 0
- `reconcile_interval_sec` <= 0
- `max_pending_order_age_sec` <= 0

---

## Phase 2 — Bar aggregator (`7d31852`)

**Files changed:** `bot/aggregator.py` (new)

`BarAggregator` bridges the single-bar-at-a-time output of `BarBuilder` to the
rolling-list interface expected by `StateMachine.process_bars()`.

**Key design decisions:**
- Two `deque` instances: `_bars_1h` (maxlen=30) and `_bars_4h` (maxlen=210)
- `_warm_from_db()` runs on `__init__` so restarts don't lose accumulated bar history
- `ready()` returns `True` only when `len(_bars_1h) >= 25` and `len(_bars_4h) >= 205`
  (StateMachine needs 205 4h bars for the EMA-200 calculation)
- `add(bar)` routes by `bar.timeframe`

---

## Phase 3 — Paper adapter (`1c04fd1`)

**Files changed:** `bot/adapters.py` (new)

`PaperAdapter` inherits `CoinbaseAdapter` so the real WebSocket market-data stream
continues to work in paper mode. Only the three order/fill methods are overridden.

**Overridden methods:**

| Method | Behaviour |
|--------|-----------|
| `submit_order_intent(order)` | Returns `{"exchange_order_id": f"cb_{order.order_id}", ...}` — no real order sent |
| `sync_get_fills(order_id)` | Reads `orders` table for `price/size/executed_size`, returns one deterministic fill with `trade_id = f"paper_fill_{local_order_id}"` and `commission = 0.0` |
| `sync_get_order(exchange_order_id)` | Always returns `{"status": "FILLED"}` |

**Idempotency guarantee:** `sync_get_fills` returns the same `trade_id` on every
call. `ExecutionService.handle_fill` rejects duplicate `execution_id` inserts, so
repeat reconcile ticks are harmless.

---

## Phase 4 — Safeguards (`8842d80`)

**Files changed:** `bot/safeguards.py` (new)

`Safeguards` is the operator safety layer. `can_trade()` returns `False` if any
guard is tripped.

**Guards:**

| Guard | Condition | Recoverable |
|-------|-----------|-------------|
| `trading_enabled` | Config flag; persisted in `runtime_state` | No |
| `stale_stream` | `time.time() - md_processor.last_trade_ts > ws_stale_timeout_sec` | Yes — re-enables when stream resumes and was the sole tripped guard |
| `stop_required` | `stop_active=0` or `stop_price=0` on open position | No |
| `daily_loss` | Structural only — always returns False until exit logic exists | N/A |

**Persistence:** `_disable()` writes `{trading_enabled, tripped}` to the
`runtime_state` table under key `"safeguards"`. New instances load this on init,
so a disabled bot stays disabled across restarts.

**`can_trade()` fix applied in test phase:** The initial implementation returned
`False` immediately when `_trading_enabled=False`, which made the stale-stream
recovery path unreachable. Fixed to always call `_check_stale_stream()` first so
recovery can fire.

---

## Phase 5 — Event logging (`615caad`)

**Files changed:** `bot/events.py` (new)

Thin wrapper around `Journal.append_event`. All structured events are emitted
from the signal consumer and safeguard tasks in `main.py`, not from core classes.

**Event types:**

| Event | When |
|-------|------|
| `SIGNAL_EMITTED` | `get_new_signals()` returns a NEW signal |
| `ORDER_PENDING` | `process_signal()` creates a PENDING order |
| `ORDER_SUBMITTED` | Reconcile attaches an `exchange_order_id` |
| `ORDER_FILLED` | `handle_fill()` sets status FILLED |
| `ORDER_FAILED_EXCHANGE` | Remote order is CANCELLED/FAILED/EXPIRED |
| `ORDER_TIMEOUT` | Pending order exceeds `max_pending_order_age_sec` |
| `POSITION_OPENED` | First fill opens a new position |
| `STOP_REQUIRED` | Stop invariant violated after fill |
| `TRADING_DISABLED` | A safeguard trips |

---

## Phase 6 — Runtime orchestrator (`f8a6584`)

**Files changed:** `main.py` (root, fully rewritten), `bot/main.py` (deleted via `git rm`)

The root `main.py` is the only supported entrypoint. `bot/main.py` is gone.

### Startup sequence (inside `run()`)

1. `config.validate()` — exits on any invalid condition
2. Set `db.db_path` to `paper_db_path` and call `db._init_db()`
3. Init `CoinbaseAdapter` and `PaperAdapter`
4. Init `BarAggregator(sym)` — warms from DB
5. Init `StateMachine()` — loads persisted state
6. Init `ExecutionService(portfolio_value=pv)`
7. Init `Safeguards(...)` with config values
8. Build `on_bar_close` callback: upserts bar → aggregator.add → state_machine.process_bars when `aggregator.ready()` and `bar.timeframe == "1h"`
9. Init `MarketDataProcessor(coinbase_adapter, on_bar_close_callback=on_bar_close)`
10. Wire `safeguards.set_md_processor(md_processor)`
11. `coinbase_adapter.ws_connect([sym])`
12. Launch 3 async tasks

### Async tasks

| Task | Responsibility |
|------|----------------|
| `market_data_task` | Runs `md_processor.run()` — WebSocket trade stream + bar building |
| `signal_consumer_task` | Every `reconcile_interval` seconds: snapshot pending orders, process NEW signals (if `can_trade()`), reconcile, log transitions, check stop invariant |
| `safeguard_task` | Every `reconcile_interval` seconds: calls `can_trade()` to evaluate guards, logs `TRADING_DISABLED` if newly tripped |

### Session summary

Printed on exit to stdout. Reads `event_log` counts since session start for:
signals generated, orders placed/filled/failed/timed-out, positions opened,
trading-disabled events.

### `bot/market_data.py` edit

Added `self.last_trade_ts: float = 0.0` to `__init__` and
`self.last_trade_ts = time.time()` inside the trade loop. This gives `Safeguards`
a concrete staleness timestamp without any other changes to the class.

---

## Phase 7 — Tests (`319100e`)

**Files changed:**
- `bot/tests/conftest.py` — coinbase SDK stub
- `bot/tests/test_paper_mode.py` (new) — 5 paper-mode end-to-end tests
- `bot/tests/test_safeguards.py` (new) — 8 safeguard unit tests
- `bot/tests/test_reconcile.py` — 1 new event-log assertion

### conftest.py — coinbase SDK stub

`PaperAdapter` inherits `CoinbaseAdapter`, which imports `coinbase.rest.RESTClient`
at module level. Since the `coinbase-advanced-py` SDK is not installed in the test
environment, a minimal stub is injected into `sys.modules` before any test import
can fail. The stub provides a no-op `RESTClient` class and a stub
`jwt_generator.build_ws_jwt` function.

### `test_paper_mode.py` — 5 tests

| Test | What it verifies |
|------|-----------------|
| `test_paper_e2e_signal_to_position` | Tick 1 submits order (PENDING + exchange_order_id). Tick 2 fills it. Position opens with `stop_price = retest_level - atr = 48000.0`. |
| `test_duplicate_reconcile_ticks_are_idempotent` | 3 extra ticks after fill produce no new executions and no change to position size. |
| `test_restart_resumes_paper_mode` | New `ExecutionService` instance after tick 1 fills the existing PENDING order without creating duplicates. |
| `test_aggregator_warms_from_db` | 5 bars of each timeframe inserted to DB; new `BarAggregator` loads all 10. `ready()` returns False (not enough bars yet). |
| `test_paper_adapter_deterministic_fill` | `sync_get_fills` returns the same `trade_id` on two consecutive calls. Commission is 0.0. |

### `test_safeguards.py` — 8 tests

| Test | What it verifies |
|------|-----------------|
| `test_trading_disabled_blocks_can_trade` | `Safeguards(trading_enabled=False)` → `can_trade()` is False. |
| `test_stale_stream_disables_trading` | `last_trade_ts` 20s old with 10s threshold trips the guard. |
| `test_stale_stream_no_fire_when_ts_is_zero` | Unstarted stream (`last_trade_ts=0`) does not trip. |
| `test_stale_stream_recovers_when_fresh` | After trip, updating `last_trade_ts` to now causes next `can_trade()` to return True. |
| `test_stop_invariant_passes_when_no_position` | No position → `check_stop_invariant` returns True. |
| `test_stop_invariant_fails_missing_stop` | Position with `stop_active=0` → guard trips, `trading_enabled=False`. |
| `test_stop_invariant_passes_with_valid_stop` | Position with `stop_active=1, stop_price=48000` → passes. |
| `test_disabled_state_persists_across_restart` | After `sg1.disable()`, new `Safeguards()` instance loads `trading_enabled=False` from DB. |

### `test_reconcile.py` addition

`test_timeout_event_logged_to_event_log` — verifies that after a PENDING order
expires, calling `log_event("ORDER_TIMEOUT", ...)` writes a correctly-structured
JSON payload to `event_log`.

---

## Test suite totals

| Category | Count |
|----------|-------|
| Original (stabilization baseline) | 31 |
| Paper mode E2E | 5 |
| Safeguards | 8 |
| Event log (reconcile extension) | 1 |
| **Total** | **45** |

All 45 pass with `python -m pytest bot/tests/ -q`.

---

## What was intentionally NOT changed

- `src/` directory — untouched throughout
- `bot/execution.py` — no changes in this phase
- `bot/state_machine.py` — no changes in this phase  
- `bot/journal.py` — no changes in this phase
- `bot/risk.py` — no changes in this phase
- No live order routing was added or modified
- No credentials or API keys are referenced in any new test
