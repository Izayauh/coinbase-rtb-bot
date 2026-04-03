# Paper-Mode End-to-End Integration Plan

**Baseline:** commit `6df27ce` on `stabilization/cleanup`
**Scope:** Make the bot runnable end to end in paper mode. Add operator safeguards and minimal observability.
**Not in scope:** New strategy features, more symbols, shorts, optimization, live trading rollout, external frameworks.

---

## Current State Summary

The stabilized repo has all the pieces — BarBuilder, StateMachine, ExecutionService, Journal, CoinbaseAdapter — tested in isolation but never wired together into a runnable process.

### What exists and works (unit-tested):
- `bot/bars.py` — builds 1m/1h/4h bars from streaming trades
- `bot/state_machine.py` — IDLE → WAITING_RETEST → RETEST_CONFIRMED → SIGNAL_EMITTED lifecycle
- `bot/execution.py` — signal → order → fill → position lifecycle with reconciliation
- `bot/journal.py` — all persistence (bars, signals, orders, executions, positions, runtime state)
- `bot/risk.py` — position sizing and IOC limit calculation
- `bot/coinbase_adapter.py` — WebSocket loop, REST wrappers, `submit_order_intent()` (paper stub)

### What does NOT work:
- **`bot/main.py`** — `on_bar_close` callback is a stub (just logs). StateMachine is never called. Signal consumer has stale bare imports.
- **Root `main.py`** — only starts the `src/` MarketDataService (Phase 1 prototype). Does not touch `bot/` stack at all.
- **No bar aggregation** — BarBuilder emits individual completed bars via callback, but nobody collects them into the rolling `List[Bar]` that `StateMachine.process_bars(bars_1h, bars_4h)` requires.
- **No paper/live mode toggle** — `submit_order_intent()` is always paper, `place_order()` is always live. No config controls which path is taken.
- **No operator safeguards** — no `trading_enabled` flag, no daily loss guard, no stale-stream detection.

### Two entrypoints, two stacks:
| | Root `main.py` | `bot/main.py` |
|--|----------------|---------------|
| Uses | `src/` package (incomplete Phase 1) | `bot/` package (tested core) |
| Does | Market data only (no strategy, no execution) | Has full wiring skeleton, but broken imports and stub callback |

**Decision for this phase:** Replace root `main.py` with a new orchestrator that uses the `bot/` package exclusively. The `src/` directory is orphaned Phase 1 code and is not touched.

---

## Architecture for Paper Mode

Single-process, single-event-loop. Three concurrent async tasks:

```
┌──────────────────────────────────────────────────┐
│  main.py (repo root)                              │
│                                                    │
│  1. Load config.yaml (+ runtime mode, safeguards) │
│  2. Init CoinbaseAdapter (WS credentials)         │
│  3. Init BarAggregator (collects bars per symbol)  │
│  4. Init StateMachine (loads persisted state)       │
│  5. Init ExecutionService (portfolio value)        │
│  6. Init Safeguards (guards object)                │
│  7. Launch three tasks:                            │
│                                                    │
│  ┌─ market_data_task ──────────────────────────┐   │
│  │ MarketDataProcessor.run()                   │   │
│  │  → on_bar_close callback:                   │   │
│  │    1. Journal.upsert_bar(bar)               │   │
│  │    2. aggregator.add(bar)                   │   │
│  │    3. if 1h bar and enough data:            │   │
│  │       sm.process_bars(agg.bars_1h, agg.4h)  │   │
│  └─────────────────────────────────────────────┘   │
│                                                    │
│  ┌─ signal_consumer_task (every N seconds) ────┐   │
│  │ 1. Check safeguards → if tripped, skip      │   │
│  │ 2. Journal.get_new_signals()                │   │
│  │ 3. For each: exec_service.process_signal()  │   │
│  │ 4. Update signal status                     │   │
│  │ 5. exec_service.reconcile_pending_orders()  │   │
│  │ 6. Post-fill: verify stop invariant         │   │
│  │ 7. Log structured events                    │   │
│  └─────────────────────────────────────────────┘   │
│                                                    │
│  ┌─ safeguard_task (every N seconds) ──────────┐   │
│  │ 1. Check stale stream (last trade age)      │   │
│  │ 2. Check daily loss                         │   │
│  │ 3. Check max pending order age              │   │
│  │ 4. If any trips → set trading_enabled=False │   │
│  │ 5. Log events                               │   │
│  └─────────────────────────────────────────────┘   │
│                                                    │
│  Ctrl+C → graceful shutdown, print session summary │
└──────────────────────────────────────────────────┘
```

---

## Phase A — Runtime mode config

### Config changes (`config.yaml`)

Add a top-level `runtime` section:

```yaml
runtime:
  mode: paper          # paper | live
  trading_enabled: true
  portfolio_value: 10000.0
```

### Behavior
- **`paper`** (default): adapter uses `submit_order_intent()` for all order submissions. No real exchange interaction for orders. WebSocket market data still streams live from Coinbase for realistic signal generation.
- **`live`**: reserved config value for a future phase. In this phase, if `runtime.mode == "live"`, startup must fail immediately with a clear message: `"Live mode is not implemented in this phase."` Do not implement live order placement in this phase.
- If `runtime.mode` key is missing, default to `paper`.
- If `runtime.mode` is present but not `paper` or `live`, exit immediately with a clear error.

### New file: `bot/config.py`

Thin config loader. Reads `config.yaml`, exposes typed accessors. No external dependency beyond PyYAML (already in requirements.txt).

Key accessors:
- `config.runtime_mode` → `"paper"` or `"live"`
- `config.trading_enabled` → bool
- `config.portfolio_value` → float
- `config.reconcile_interval_sec` → int (from `execution.reconcile_interval_sec`)
- `config.ws_stale_timeout_sec` → int
- `config.max_daily_loss` → float (from `risk.max_daily_loss`)
- `config.symbols` → list of str

### Files changed
- `config.yaml` — add `runtime` section
- `bot/config.py` — new file, config loader

---

## Phase B — Main runtime integration

### Replace root `main.py`

Rewrite as the single orchestrator that wires `bot/` components together. The existing root `main.py` (which uses `src/`) is replaced entirely.

### New component: `bot/aggregator.py`

The gap between BarBuilder and StateMachine is that BarBuilder emits one completed bar at a time via callback, but `StateMachine.process_bars()` expects rolling lists of the last N bars (25 for 1h, 205 for 4h).

`BarAggregator` bridges this:
- Maintains `deque` of recent bars per symbol per timeframe (maxlen = 210 for 4h, 30 for 1h)
- `add(bar)` appends to the correct deque
- `get_bars_1h(symbol)` / `get_bars_4h(symbol)` returns current lists
- On startup, loads existing bars from DB to warm the deques (so restarts don't lose history)

### The `on_bar_close` callback

This is the critical integration point. Currently a stub in `bot/main.py`. The new version:

```python
def on_bar_close(bar: Bar):
    Journal.upsert_bar(bar)
    aggregator.add(bar)
    
    if bar.timeframe == "1h":
        bars_1h = aggregator.get_bars_1h(bar.symbol)
        bars_4h = aggregator.get_bars_4h(bar.symbol)
        if len(bars_1h) >= 25 and len(bars_4h) >= 205:
            state_machine.process_bars(bars_1h, bars_4h)
```

Synchronous. Called from within `MarketDataProcessor.run()` (which is async but uses sync callback path — already supported at `market_data.py:46-47`).

### Signal consumer task

Runs on a configurable interval (`reconcile_interval_sec`, default 5s). Each tick:

1. Check `safeguards.can_trade()` — if false, skip signal processing (still runs reconcile for pending orders).
2. Fetch `Journal.get_new_signals()`.
3. For each signal: call `exec_service.process_signal(signal)`, update signal status, log `SIGNAL_EMITTED` / `ORDER_PENDING` events.
4. Call `exec_service.reconcile_pending_orders(timeout=max_pending_age, adapter=adapter)`.
5. After reconcile: for any newly filled orders, verify stop invariant (position must have `stop_active=True` and `stop_price > 0`). If not, log `STOP_REQUIRED` event and disable trading.
6. Log any status transitions as structured events.

### Adapter mode selection

This phase is paper-mode only. Live order placement is not implemented.

In the signal consumer, when calling `reconcile_pending_orders`, pass the `PaperAdapter`. The adapter overrides only the order/fill methods while keeping the real WebSocket market data stream from `CoinbaseAdapter`.

Paper adapter behavior is defined precisely in Addendum 3. Summary:
- `submit_order_intent(order)` — returns mock exchange_order_id (already exists on CoinbaseAdapter, PaperAdapter inherits it)
- `sync_get_fills(order_id)` — returns one deterministic synthetic fill (trade_id = `f"paper_fill_{order_id}"`, price = order limit price, size = full remaining)
- `sync_get_order(exchange_order_id)` — returns `{"status": "FILLED"}`

### Graceful shutdown

On Ctrl+C:
1. Cancel all tasks
2. Print session summary (signals generated, orders placed, fills, positions opened, PnL)
3. Exit cleanly

### Files changed
- `main.py` (repo root) — rewritten as orchestrator
- `bot/aggregator.py` — new file, bar aggregation bridge
- `bot/adapters.py` — new file, `PaperAdapter` class

### Deleted file
- `bot/main.py` — removed. Root `main.py` is the only supported runtime entrypoint.

---

## Phase C — Operator safeguards

### New file: `bot/safeguards.py`

A `Safeguards` class that evaluates a set of hard invariants each tick. If any trips, `can_trade()` returns False and new entries are blocked.

### Guard 1: `trading_enabled` flag
- Source: `config.runtime.trading_enabled`
- Persisted in `runtime_state` table (key: `"trading_enabled"`) so it survives restarts
- If False at startup, bot logs `TRADING_DISABLED` and blocks entries from the start
- Can be set to False by any other guard tripping

### Guard 2: `max_daily_loss`
- Source: `config.risk.max_daily_loss` (currently 0.015 = 1.5%)
- Calculation: sum of `realized_pnl` from all positions closed today + unrealized PnL of open positions
- Compare against `portfolio_value * max_daily_loss`
- If exceeded: set `trading_enabled = False`, log `TRADING_DISABLED` with reason `"max_daily_loss"`
- **For Phase 1 paper mode:** realized_pnl is 0 since we don't yet have exit logic. This guard is wired but will only trip on unrealized loss if we add mark-to-market later. Still worth wiring now for structural correctness.

### Guard 3: `stale_stream`
- Source: `config.execution.ws_stale_timeout_sec` (currently 15s)
- Check: time since last trade received by MarketDataProcessor
- If exceeded: set `trading_enabled = False`, log `TRADING_DISABLED` with reason `"stale_market_stream"`
- Recoverable: if stream resumes, re-enable (only if no other guard is tripped)

### Guard 4: `max_pending_order_age`
- Already partially implemented via `reconcile_pending_orders(timeout=60)`
- Make the timeout configurable via a new config key: `execution.max_pending_order_age_sec` (default 60)
- When an order is expired by timeout, log `ORDER_TIMEOUT` event

### Guard 5: `stop_required` invariant
- After any fill creates or updates a position, assert: `position.stop_active == True` and `position.stop_price > 0`
- If this invariant fails (code bug, not expected in normal operation): set `trading_enabled = False`, log `STOP_REQUIRED`
- This is a structural safety net, not a runtime condition

### Safeguard evaluation cadence
- Evaluated every tick of the safeguard task (runs every `reconcile_interval_sec`)
- Also evaluated before each signal processing in signal consumer task

### Files changed
- `bot/safeguards.py` — new file
- `config.yaml` — add `execution.max_pending_order_age_sec`

---

## Phase D — Observability

### Structured event logging

Use the existing `Journal.append_event(event_type, message)` which writes to the `event_log` table. The `message` field will be a JSON string for structured data.

Events to log:

| Event | When | Payload |
|-------|------|---------|
| `SIGNAL_EMITTED` | StateMachine inserts signal to DB | `{signal_id, symbol, execution_price, atr, rsi}` |
| `ORDER_PENDING` | ExecutionService creates order | `{order_id, signal_id, symbol, size, price}` |
| `ORDER_SUBMITTED` | Adapter returns exchange_order_id | `{order_id, exchange_order_id}` |
| `ORDER_FILLED` | handle_fill completes full fill | `{order_id, signal_id, fill_price, fill_size, fee}` |
| `ORDER_FAILED_EXCHANGE` | Remote status is CANCELLED/FAILED/EXPIRED | `{order_id, remote_status}` |
| `ORDER_TIMEOUT` | Pending order expired by age | `{order_id, age_seconds}` |
| `POSITION_OPENED` | First fill creates new position | `{symbol, avg_entry, size, stop_price}` |
| `STOP_REQUIRED` | Stop invariant violated after fill | `{symbol, stop_price, stop_active}` |
| `TRADING_DISABLED` | Any safeguard trips | `{reason, guard_name}` |

### Where to add logging calls

Most events are logged from the **signal consumer task** and **safeguard task**, not from the core classes themselves. This keeps the core classes (ExecutionService, Journal) unchanged and testable without logging side effects.

`SIGNAL_EMITTED` is logged inside the signal consumer loop when `Journal.get_new_signals()` returns a new signal. See Addendum 5 for the binding rule — do not attempt to log it from `on_bar_close`.

### Session summary

On shutdown (Ctrl+C or SIGTERM), print to stdout:

```
=== Session Summary ===
Runtime mode: paper
Duration: 2h 14m
Signals generated: 1
Orders placed: 1
Orders filled: 1
Orders failed: 0
Orders timed out: 0
Positions opened: 1
Trading disabled events: 0
```

Data sourced from `event_log` table counts for the current session (events since bot start time).

### Files changed
- `bot/events.py` — new file, thin helper: `log_event(event_type, **kwargs)` that calls `Journal.append_event` with JSON-serialized kwargs
- Root `main.py` — session summary on shutdown
- Signal consumer task — event logging calls at each state transition

---

## Phase E — Tests

All new tests go in `bot/tests/`. Use the existing `test_db` fixture from `conftest.py`.

### Test 1: Paper mode end-to-end signal to position flow

**File:** `bot/tests/test_paper_mode.py`

Exercises the full chain without a real WebSocket:
1. Create a `PaperAdapter` (no credentials)
2. Manually push bars through `BarAggregator` and `StateMachine` to produce a signal
3. Run signal consumer logic: process_signal → reconcile with PaperAdapter → fill → position
4. Assert: signal transitions NEW → ORDER_PENDING → ORDER_FILLED, order is FILLED, position is OPEN with correct stop

### Test 2: `trading_enabled=false` blocks new entries

1. Init `Safeguards` with `trading_enabled=False`
2. Insert a NEW signal into DB
3. Run signal consumer logic with safeguard check
4. Assert: signal stays NEW, no order created

### Test 3: Stale pending order is expired and logged

Already covered by existing `test_stale_pending_order_timeout`. Extend to verify:
- `ORDER_TIMEOUT` event is written to `event_log`

### Test 4: Duplicate reconcile ticks do not duplicate fills or positions

Already covered by existing `test_duplicate_fill_not_double_credited`. Extend to verify:
- Running the signal consumer twice with same state produces no extra executions or position size changes

### Test 5: Restart resumes paper-mode state cleanly

1. Set up: signal consumer processes a signal, order is pending, adapter submitted
2. Simulate restart: create new StateMachine, new ExecutionService (both load from DB)
3. Run signal consumer again
4. Assert: no duplicate orders, state machine resumes from persisted state, pending order continues reconciliation

### Files changed
- `bot/tests/test_paper_mode.py` — new file
- `bot/tests/test_safeguards.py` — new file
- `bot/tests/test_reconcile.py` — extend existing tests with event log assertions

---

## File Change Summary

### New files:
| File | Purpose |
|------|---------|
| `bot/config.py` | Config loader for config.yaml |
| `bot/aggregator.py` | BarAggregator: bridges BarBuilder output to StateMachine input |
| `bot/adapters.py` | PaperAdapter: wraps CoinbaseAdapter with synthetic fills |
| `bot/safeguards.py` | Operator safety guards |
| `bot/events.py` | Structured event logging helper |
| `bot/tests/test_paper_mode.py` | End-to-end paper mode tests |
| `bot/tests/test_safeguards.py` | Safeguard tests |

### Expected files to change:

At minimum:
- `main.py` (root) — rewritten as orchestrator using bot/ package
- `config.yaml` — add `runtime` section, `execution.max_pending_order_age_sec`
- `bot/tests/test_reconcile.py` — extend with event log assertions

Minimal changes to existing core modules are allowed only when required by the binding addendum (see Addendum 7). Likely candidates include:
- `bot/market_data.py` — add `last_trade_ts` field for stale-stream guard (Addendum 8)
- `bot/coinbase_adapter.py` — only if needed for adapter boundary compatibility with PaperAdapter

Any core module change must be minimal, justified in the final report, and covered by tests.

### Deleted file:
- `bot/main.py` — removed. Root `main.py` is the only supported runtime entrypoint (Addendum 4).

### Not touched:
- `src/` directory — orphaned Phase 1 code. Not wired, not deleted, not imported by new code. Deletion is a future cleanup task.

---

## Exact Behavior Added

1. `python main.py` from repo root starts the bot in paper mode
2. WebSocket connects to Coinbase (live market data, no order interaction)
3. Trades build bars → bars feed state machine → signals generate orders → paper adapter fills orders → positions open
4. If `trading_enabled` is false (config or guard trip), no new entries are placed
5. If market stream goes stale for >15s, trading stops automatically
6. Daily loss guard is wired and enforced structurally, but meaningful triggering is deferred until exit logic and/or mark-to-market are implemented
7. Pending orders older than 60s are expired with `FAILED_TIMEOUT`
8. Every fill is followed by a stop invariant check
9. All lifecycle events are logged to `event_log` table as structured JSON
10. Ctrl+C prints session summary and exits cleanly
11. Restart loads persisted state (state machine, pending orders) and resumes

---

## Tests Added

| Test | What it proves |
|------|----------------|
| Paper mode end-to-end | Full lifecycle: bar → signal → order → paper fill → position |
| trading_enabled=false | Guard blocks new entries, existing reconciliation continues |
| Stale order timeout + event | Timeout path works and logs ORDER_TIMEOUT |
| Duplicate reconcile idempotency | No double fills, no duplicate positions |
| Restart resume | Persisted state machine and orders survive process restart |

---

## Remaining Risks Before Live Trading

These are known issues that must be addressed in a future phase before enabling `mode: live`:

1. **No exit logic.** Positions open but never close. No stop-loss execution, no take-profit, no trailing stop. The `stop_price` is calculated and persisted but nothing acts on it.

2. **No mark-to-market.** `unrealized_pnl` is never updated. The daily loss guard cannot trip on unrealized losses until mark-to-market is implemented.

3. **No spread check.** Config has `max_spread_bps: 8` but nothing reads it. Live IOC orders could fill at worse prices than intended if spread is wide.

4. **Single-symbol only.** This phase enforces exactly one symbol. Multi-symbol support (one StateMachine per symbol, parallel aggregators) is deferred to a future phase.

5. **No order cancellation on shutdown.** If the bot stops while a live order is pending on the exchange, nothing cancels it. Paper mode doesn't have this problem.

6. **No fee accounting in PnL.** `Execution.fee` is recorded but not subtracted from realized PnL.

7. **SQLite concurrency.** Single-connection, no WAL mode. Acceptable for single-process paper mode, may need WAL or connection pooling for live.

8. **Live mode not implemented.** `mode: live` is a reserved config value that fails fast in this phase. A future phase must implement real order placement, credential validation, and the `submit_order_intent` vs `place_order` routing boundary.

9. **No heartbeat monitoring.** The WS loop receives heartbeats but doesn't track missed ones. The stale-stream guard checks last trade time, not heartbeat health.

---

## Implementation Order

Phases should be implemented and committed in this order:

| # | Phase | Commit message |
|---|-------|----------------|
| 1 | A: Config loader + runtime mode in config.yaml | `feat(config): add runtime mode and config loader` |
| 2 | B.1: BarAggregator | `feat(aggregator): bridge bar builder output to state machine` |
| 3 | B.2: PaperAdapter | `feat(adapters): paper adapter with synthetic fills` |
| 4 | C: Safeguards | `feat(safeguards): operator safety guards` |
| 5 | D: Event logging | `feat(events): structured event logging` |
| 6 | B.3: Root main.py rewrite (wires everything) | `feat(runtime): paper-mode end-to-end orchestrator` |
| 7 | E: Tests | `test(paper): end-to-end paper mode and safeguard tests` |

Each commit should be independently verifiable: existing tests must still pass after each commit.

---

# Binding Addendum — Hard Constraints

The following amendments are mandatory. They override any softer language above. Any implementation that violates these constraints is rejected.

---

## Addendum 1: Single-symbol only for this phase

The runtime must support exactly one trading symbol. This is not a preference — it is a hard constraint.

- `config.yaml` `symbols` list must contain exactly one entry.
- At startup, if `symbols` length != 1, the program must log a clear error and exit immediately. No fallback to the first symbol, no silent truncation.
- Do not add multi-symbol support in this phase.
- Do not create one StateMachine per symbol in this phase.
- Default symbol is `BTC-USD`.
- The `BarAggregator`, `StateMachine`, and `ExecutionService` all operate on a single symbol.

This prevents quiet scope broadening.

---

## Addendum 2: Paper mode must preserve the pending -> reconcile -> fill lifecycle

The existing order lifecycle must be preserved exactly in paper mode. This is the exact regression we already had once. Do not let it happen again.

**Hard rules:**

- `ExecutionService.process_signal()` must still create a PENDING order. No shortcut.
- `PaperAdapter.submit_order_intent()` may attach synthetic exchange metadata (exchange_order_id, submitted_at). That is its only job during signal processing.
- Fills must **only** be applied through `reconcile_pending_orders()` calling `handle_fill()`.
- No code may mark an order FILLED directly during signal processing.
- No code may open a position directly from signal processing.
- The pending -> reconcile -> fill path must be **identical** in paper and live modes. The only difference is which adapter methods return real vs synthetic data.

---

## Addendum 3: Paper fill behavior — explicit specification

"Synthetic fill" must not be vague. The exact behavior for this phase is:

1. On the first reconcile tick after a pending order has an `exchange_order_id`, `PaperAdapter.sync_get_fills(order_id)` returns one synthetic fill:
   - `trade_id`: deterministic per order. Use `f"paper_fill_{order_id}"` so duplicate reconcile ticks do not create duplicate executions.
   - `price`: equals the order's stored limit price.
   - `size`: equals the full remaining order size (i.e., `order.size - order.executed_size`).
   - `commission`: `0.0` for this phase.
2. On that same reconcile tick, `PaperAdapter.sync_get_order(exchange_order_id)` returns `{"status": "FILLED"}`.
3. On subsequent reconcile ticks, the adapter returns the same fill again (same deterministic `trade_id`). Local execution handling ignores it because `trade_id` already exists in the executions table.

No partial-fill simulation. No randomized pricing. No invented slippage.

To implement this, the `PaperAdapter` must be able to look up the pending order's `size` and `price`. The simplest correct approach: `PaperAdapter` reads from the Journal/DB to get order details when `sync_get_fills` is called. The adapter receives the `exchange_order_id` (which is `f"cb_{order_id}"`), derives the local `order_id`, and queries the orders table.

---

## Addendum 4: Single runtime entrypoint — no ambiguity

**Hard rules:**

- `python main.py` from repo root is the **only** supported runtime entrypoint after this phase.
- `bot/main.py` must be **deleted**. Not "left as dead code." Not "replaced with a wrapper." Deleted.
- The `src/` directory remains untouched but must not be imported or referenced by the new runtime or any new code.
- No duplicate entrypoints. If someone runs `python bot/main.py`, it should not exist.

---

## Addendum 5: Signal logging must not depend on guessing after process_bars()

`StateMachine.process_bars()` does not return the emitted signal. Inferring signal emission by diffing opaque state is brittle.

**Hard rules:**

- `SIGNAL_EMITTED` must be logged when the signal is first observed in `Journal.get_new_signals()` inside the signal consumer loop. This is the natural point: the signal consumer already iterates over NEW signals.
- Do not add "query DB before and after `process_bars()` and diff" logic.
- If a future phase needs exact-at-emission logging, the state machine may be modified minimally to return or expose the emitted signal ID. That is out of scope for this phase.
- The `on_bar_close` callback must NOT attempt to log `SIGNAL_EMITTED`. It does not know whether a signal was emitted.

---

## Addendum 6: Source import policy — explicit and binding

**Hard rules:**

- Tests must use package-style imports: `from bot.X import Y`.
- Source files inside `bot/` currently use relative imports (`from .db import db`). This was established in the stabilization phase and must remain consistent.
- New source files added in this phase (config.py, aggregator.py, adapters.py, safeguards.py, events.py) must use relative imports when importing sibling `bot/` modules.
- Root `main.py` must use absolute package imports: `from bot.X import Y`.
- Do not mix relative and absolute imports within the same file.
- Do not add `sys.path` hacks anywhere.
- If any import breaks, fix it consistently. Do not leave the repo in a mixed state.

---

## Addendum 7: Core module changes — allowed if minimal and justified

The original plan says core modules remain unchanged. That is a **preference, not a law**.

**Hard rules:**

- Prefer not changing tested core modules.
- But if a minimal change is required for:
  - runtime mode routing
  - deterministic paper fills
  - startup fail-fast behavior
  - clean event logging
  - stale-stream tracking (e.g., adding `last_trade_ts` to MarketDataProcessor)
  - then it is **allowed**.
- Any such change must be:
  - Minimal (smallest possible diff).
  - Directly justified in the final report (state what changed and why).
  - Covered by tests if behavior changed.
- Do not use "core modules unchanged" as an excuse to invent parallel infrastructure when a one-line addition to an existing module would suffice.

---

## Addendum 8: Stale-stream guard — concrete timestamp source

**Hard rules:**

- Maintain a single authoritative `last_trade_ts` field.
- Location: on the `MarketDataProcessor` instance (or a shared object the safeguard can read). This is the natural place because MarketDataProcessor already processes every trade.
- Update it on **every received trade** inside the market data processing path (inside the trade loop in `MarketDataProcessor.run()`).
- The safeguard must read that exact field. No estimation from bar close times, event log timestamps, or any other indirect source.
- If this requires adding one field to `MarketDataProcessor`, that is an allowed minimal core module change per Addendum 7.

---

## Addendum 9: Startup fail-fast conditions

At startup, the program must **exit immediately with a clear error message** if any of the following are true:

| Condition | Error message pattern |
|-----------|----------------------|
| `runtime.mode` is not `"paper"` or `"live"` | `"Invalid runtime mode: {mode}. Must be 'paper' or 'live'."` |
| `symbols` list length != 1 | `"Exactly one symbol required. Got {n}: {symbols}"` |
| `runtime.portfolio_value` <= 0 | `"portfolio_value must be > 0. Got {value}"` |
| `execution.reconcile_interval_sec` <= 0 | `"reconcile_interval_sec must be > 0"` |
| `execution.max_pending_order_age_sec` <= 0 | `"max_pending_order_age_sec must be > 0"` |

**No silent fallbacks.** If mode is unrecognized, do not default to paper. Fail.

The original plan's Phase A said "if mode is missing or unrecognized, default to paper and log a warning." That is overridden. For this phase: fail fast. Silent fallbacks hide bugs.

Exception: if `runtime.mode` key is entirely absent from config.yaml, default to `"paper"` (this is the only acceptable default, and only for the missing-key case, not for invalid values).

---

## Addendum 10: Final acceptance gate

The phase is **not complete** unless ALL of the following are true:

1. `python main.py` from repo root starts the bot in paper mode.
2. No `PYTHONPATH` hacks required.
3. Existing full test suite (31 tests from stabilization) still passes.
4. New paper-mode and safeguard tests pass.
5. There is exactly one supported runtime entrypoint (`main.py` at repo root). `bot/main.py` does not exist.
6. A paper-mode order becomes PENDING first, then FILLED via `reconcile_pending_orders`, not directly.
7. Duplicate reconcile ticks do not create duplicate executions or positions.
8. Single-symbol constraint is enforced at startup (exit if symbols != 1).
9. Ctrl+C exits cleanly and prints a session summary.
10. No `sys.path` hacks exist anywhere in the codebase.

If any of the above is false, the phase is incomplete.
