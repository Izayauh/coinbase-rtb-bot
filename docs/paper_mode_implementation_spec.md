# Paper-Mode Integration Spec

## 1. Baseline and scope

**Baseline commit:** `6df27ce` on `stabilization/cleanup`
**Goal:** Wire all tested components into a runnable paper-mode process with operator safeguards and structured observability.

**In scope:** runtime orchestration, paper-mode adapter, config loader, bar aggregation bridge, safeguards, event logging, tests.

**Not in scope:** live order placement, multi-symbol support, new strategy features, shorts, optimization, external frameworks, changes to `src/`.

Paper mode is for **orchestration and state-machine validation**, not profitability validation.

---

## 2. Current gaps to close

| Gap | Detail |
|-----|--------|
| No runtime entrypoint | Root `main.py` uses orphaned `src/` stack. `bot/main.py` has broken imports and a stub callback. Neither runs end to end. |
| No bar aggregation | `BarBuilder` emits one bar at a time. `StateMachine.process_bars()` needs rolling lists (25x 1h, 205x 4h). Nothing bridges them. |
| State machine never invoked | `on_bar_close` callback is a log statement. No code calls `process_bars()`. |
| Signals never consumed | `ExecutionService` is never instantiated at runtime. NEW signals sit in the DB. |
| No paper fill path | `submit_order_intent()` creates a mock exchange ID, but `sync_get_fills()` returns `[]` on the next tick, so orders time out instead of filling. |
| No config system | No runtime mode toggle, no `trading_enabled` flag, no configurable timeouts. |
| No safeguards | No stale-stream detection, no daily loss guard, no stop invariant check. |
| No observability | No structured event logging. No session summary. |

---

## 3. Implementation phases

Implement and commit in this order. Existing 31 tests must pass after each commit.

### Phase 1 — Config loader

**New file:** `bot/config.py` — loads `config.yaml`, exposes typed accessors.

**Config additions to `config.yaml`:**

```yaml
runtime:
  mode: paper
  trading_enabled: true
  portfolio_value: 10000.0
  paper_db_path: paper_journal.db

symbols:
  - BTC-USD

execution:
  max_pending_order_age_sec: 60
```

Startup validation (fail fast with clear error message):

| Condition | Action |
|-----------|--------|
| `runtime.mode` key missing | default to `"paper"` |
| `runtime.mode` present but not `"paper"` or `"live"` | exit |
| `runtime.mode == "live"` | exit: `"Live mode is not implemented in this phase."` |
| `len(symbols) != 1` | exit: `"Exactly one symbol required. Got {n}: {symbols}"` |
| `portfolio_value <= 0` | exit |
| `reconcile_interval_sec <= 0` | exit |
| `max_pending_order_age_sec <= 0` | exit |

No silent fallbacks except the missing-key default above.

**Commit:** `feat(config): add runtime mode and config loader`

### Phase 2 — Bar aggregator

**New file:** `bot/aggregator.py`

`BarAggregator` bridges BarBuilder's single-bar callback to StateMachine's rolling-list interface:
- `deque` per timeframe: maxlen 210 for 4h, 30 for 1h.
- `add(bar)` appends to correct deque.
- `get_bars_1h()` / `get_bars_4h()` return lists.
- On init, warm deques from DB (`SELECT ... ORDER BY ts_open DESC LIMIT N`).

Single-symbol. Takes symbol string at init. Does not manage multiple symbols.

**Commit:** `feat(aggregator): bridge bar builder output to state machine`

### Phase 3 — Paper adapter

**New file:** `bot/adapters.py`

`PaperAdapter` wraps `CoinbaseAdapter`. Inherits WebSocket market data. Overrides only order/fill methods.

**Paper fill spec (exact, deterministic, non-negotiable):**

`submit_order_intent(order)` — inherited from `CoinbaseAdapter`, returns `{"exchange_order_id": f"cb_{order.order_id}", "submitted_at": now, "status": "OPEN"}`.

`sync_get_fills(order_id)` — on every call for an order that has an `exchange_order_id`:
- Look up order from DB by deriving `order_id` from `exchange_order_id` (strip `cb_` prefix).
- Return one fill: `{"trade_id": f"paper_fill_{order_id}", "price": order.price, "size": order.size - order.executed_size, "commission": 0.0}`.
- On subsequent ticks this returns the same fill. Local `handle_fill()` rejects it because `trade_id` already exists in executions table.

`sync_get_order(exchange_order_id)` — returns `{"status": "FILLED"}`.

No partial fills. No random pricing. No invented slippage. No live order placement.

Paper mode must use a **separate DB path** (`runtime.paper_db_path`, default `paper_journal.db`) so paper state never contaminates production data. The config loader sets `bot.db.db.db_path` to this value when `mode == "paper"`.

**Commit:** `feat(adapters): paper adapter with deterministic synthetic fills`

### Phase 4 — Safeguards

**New file:** `bot/safeguards.py`

`Safeguards` class. `can_trade()` returns False if any guard is tripped.

| Guard | Source | Behavior |
|-------|--------|----------|
| `trading_enabled` | config + `runtime_state` table | If False at startup or set False by another guard, blocks all new entries. Persisted across restarts. |
| `stale_stream` | `last_trade_ts` on `MarketDataProcessor` | If `now - last_trade_ts > ws_stale_timeout_sec`, disable trading. Recoverable when stream resumes (if no other guard tripped). |
| `max_pending_order_age` | `execution.max_pending_order_age_sec` | Passed as `timeout` to `reconcile_pending_orders()`. Orders exceeding age are expired. |
| `stop_required` | checked after every fill | If position has `stop_active != True` or `stop_price <= 0`, disable trading. Structural safety net. |
| `daily_loss` | `risk.max_daily_loss` | Wired structurally. Not economically meaningful this phase (no exit logic, no mark-to-market). Will trip once unrealized PnL tracking exists. |

**Stale-stream implementation:** Add `self.last_trade_ts = 0.0` to `MarketDataProcessor.__init__()`. Update to `time.time()` on every received trade inside the trade loop in `run()`. This is an allowed minimal core-module edit. The safeguard reads this field directly.

**Failure mode:** REST/WS failures fail closed for new entries. Retry with bounded backoff. No infinite retry loops.

**Commit:** `feat(safeguards): operator safety guards`

### Phase 5 — Event logging

**New file:** `bot/events.py` — `log_event(event_type, **kwargs)` calls `Journal.append_event(event_type, json.dumps(kwargs))`.

Events logged from the **signal consumer task** and **safeguard task** (not from core classes):

| Event | When logged | Key payload fields |
|-------|------------|-------------------|
| `SIGNAL_EMITTED` | Signal consumer observes NEW signal via `get_new_signals()` | signal_id, symbol, execution_price |
| `ORDER_PENDING` | After `process_signal()` creates PENDING order | order_id, signal_id, size, price |
| `ORDER_SUBMITTED` | After adapter returns exchange_order_id | order_id, exchange_order_id |
| `ORDER_FILLED` | After `handle_fill()` sets status FILLED | order_id, fill_price, fill_size |
| `ORDER_FAILED_EXCHANGE` | Remote status is CANCELLED/FAILED/EXPIRED | order_id, remote_status |
| `ORDER_TIMEOUT` | Order expired by age | order_id, age_seconds |
| `POSITION_OPENED` | First fill creates position | symbol, avg_entry, size, stop_price |
| `STOP_REQUIRED` | Stop invariant fails after fill | symbol, stop_price, stop_active |
| `TRADING_DISABLED` | Any safeguard trips | reason, guard_name |

`SIGNAL_EMITTED` is logged when `get_new_signals()` returns the signal in the consumer loop. Not from `on_bar_close`. Not by diffing DB state around `process_bars()`.

**Session summary** printed to stdout on clean shutdown (Ctrl+C): mode, duration, counts of signals/orders/fills/failures/positions from `event_log` since session start.

**Commit:** `feat(events): structured event logging and session summary`

### Phase 6 — Runtime orchestrator

**Rewrite:** `main.py` (repo root). **Delete:** `bot/main.py`.

Single-process, single event loop, three async tasks:

**Startup sequence:**
1. Load config, run fail-fast validation.
2. Set DB path (paper_db_path when mode is paper).
3. Init `CoinbaseAdapter` (for WS market data; credentials optional in paper mode).
4. Wrap in `PaperAdapter`.
5. Init `BarAggregator` (warm from DB).
6. Init `StateMachine` (loads persisted state from DB).
7. Init `ExecutionService(portfolio_value)`.
8. Init `Safeguards`.
9. Connect WebSocket to single configured symbol.
10. Launch tasks.

**Task 1 — Market data:** `MarketDataProcessor.run()` with `on_bar_close` callback:
```
Journal.upsert_bar(bar)
aggregator.add(bar)
if bar.timeframe == "1h" and enough bars accumulated:
    state_machine.process_bars(aggregator.get_bars_1h(), aggregator.get_bars_4h())
```

**Task 2 — Signal consumer** (every `reconcile_interval_sec`):
1. If `safeguards.can_trade()`: fetch NEW signals, process each through `ExecutionService.process_signal()`, update signal status, log events.
2. Always: call `reconcile_pending_orders(timeout=max_pending_order_age, adapter=paper_adapter)`.
3. After reconcile: check stop invariant on any open position. If violated, disable trading.

**Task 3 — Safeguard monitor** (every `reconcile_interval_sec`): evaluate all guards, log `TRADING_DISABLED` if any trips.

**Shutdown:** Cancel tasks, print session summary, exit 0.

**Import rules:** Root `main.py` uses `from bot.X import Y`. Does not import from `src/`.

**Commit:** `feat(runtime): paper-mode end-to-end orchestrator`

---

## 4. Hard constraints

These are non-negotiable. Any implementation violating them is rejected.

**Lifecycle integrity:**
- `process_signal()` creates a PENDING order. No shortcut to FILLED. No direct position creation.
- Fills happen only through `reconcile_pending_orders()` → `handle_fill()`.
- Paper and live modes use the identical pending → reconcile → fill path. Only adapter return values differ.

**Single symbol:**
- Exactly one symbol in config. Startup fails otherwise.
- One `StateMachine`, one `BarAggregator`, one `ExecutionService`.

**Single entrypoint:**
- `python main.py` from repo root. No PYTHONPATH hacks.
- `bot/main.py` deleted. `src/` not imported.

**Imports:**
- Tests: `from bot.X import Y`.
- New `bot/` files: relative imports (`from .db import db`).
- Root `main.py`: absolute package imports (`from bot.X import Y`).
- No `sys.path` hacks anywhere.

**Core module edits:**
- Allowed only when necessary (e.g., `last_trade_ts` on `MarketDataProcessor`).
- Must be minimal, justified, and test-covered.

**Paper isolation:**
- Paper mode uses a separate DB path (`paper_journal.db`). Paper state must not mix with production data.

---

## 5. Tests required

All in `bot/tests/`. Use existing `test_db` fixture from `conftest.py`.

| # | Test | File | Asserts |
|---|------|------|---------|
| 1 | Paper E2E: signal to position | `test_paper_mode.py` | Bars → aggregator → state machine → NEW signal → PENDING order → reconcile with PaperAdapter → FILLED → position OPEN with correct stop. Signal transitions: NEW → ORDER_PENDING → ORDER_FILLED. |
| 2 | trading_enabled=false blocks entries | `test_safeguards.py` | Insert NEW signal. Safeguard blocks. Signal stays NEW. No order created. Reconcile still runs for existing pending orders. |
| 3 | Stale order timeout logs event | `test_reconcile.py` (extend) | Existing timeout test + verify `ORDER_TIMEOUT` in `event_log`. |
| 4 | Duplicate reconcile idempotency | `test_paper_mode.py` | Run signal consumer twice with same state. No duplicate executions, no duplicate position size. |
| 5 | Restart resumes cleanly | `test_paper_mode.py` | Process signal → order pending → new StateMachine + ExecutionService (simulating restart) → reconcile continues → no duplicates. |

Existing 31 stabilization tests must continue to pass.

---

## 6. Acceptance gate

The phase is complete only when ALL of these are true:

1. `python main.py` from repo root starts the bot in paper mode.
2. No PYTHONPATH hacks required.
3. All 31 existing tests pass.
4. All new paper-mode and safeguard tests pass.
5. `bot/main.py` does not exist. One entrypoint only.
6. Paper-mode order lifecycle: PENDING first, then FILLED via reconcile. Never a direct shortcut.
7. Duplicate reconcile ticks produce no duplicate executions or positions.
8. Startup exits if `symbols` != 1, if mode is invalid, or if mode is `"live"`.
9. Ctrl+C prints session summary and exits cleanly.
10. No `sys.path` hacks anywhere in the codebase.
11. Paper mode uses a separate DB file from production.

---

## 7. Remaining non-goals / deferred risks

| Risk | Why deferred |
|------|-------------|
| No exit logic | Positions open but never close. Stop price is persisted but nothing acts on it. Required before live. |
| No mark-to-market | `unrealized_pnl` never updated. Daily loss guard is structural only until this exists. |
| No spread check | `max_spread_bps` config exists but nothing reads it. |
| Single-symbol only | Enforced this phase. Multi-symbol requires per-symbol StateMachine instances. |
| No order cancellation on shutdown | Not a problem in paper mode. Required for live. |
| No fee accounting in PnL | `Execution.fee` recorded but not subtracted from PnL. |
| SQLite single-connection | Fine for single-process paper mode. May need WAL for live. |
| Live mode not implemented | `mode: live` fails fast. Future phase must implement real order placement. |
| No heartbeat monitoring | Stale-stream guard uses trade timestamps, not heartbeat health. |
