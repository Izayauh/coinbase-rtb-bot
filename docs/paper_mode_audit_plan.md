# Paper-Mode Audit Plan

**Commit audited:** `319100e`  
**Audited by:** Claude Opus 4.6 (adversarial review pass)  
**Scope:** Five suspected issues in the paper-mode integration build

---

## Issue 1 — Silent no-WebSocket paper mode

**Classification:** Design inconsistency  
**Severity:** Medium-High

### What happens

`CoinbaseAdapter.__init__` sets `self._enabled = False` when `COINBASE_API_KEY` / `COINBASE_API_SECRET` are absent. `ws_connect` silently no-ops when `_enabled=False`. `market_data_task` then blocks forever on `await self.adapter.market_queue.get()`. `last_trade_ts` stays `0.0`. The stale-stream guard exempts `0.0` (`safeguards.py:115`), so it never fires. The signal consumer loop calls `can_trade()` → True and processes signals normally — against zero real market data.

### Open question requiring a decision

> **Should credential-absent startup be a hard exit or a degraded-mode warning?**
> The existing code was designed to tolerate missing credentials ("Running disconnected"). Is there a use case (DB replay, offline testing) where running without WebSocket data is valid? If paper mode *always* means live market data, a hard exit is correct. If not, a warning with trading blocked is more appropriate.

### Resolutions

**A. (Recommended / minimal)** Add a guard in `main.py:run()` after `CoinbaseAdapter()` is constructed. If `coinbase_adapter._enabled` is False, log CRITICAL and `sys.exit(1)`. Enforces the paper-mode contract at startup.

**B. (Defence-in-depth, additive)** Modify `_check_stale_stream()` to trip after a configurable grace period even when `last_trade_ts == 0.0`. Catches both credential-missing and WebSocket-connect-failure cases at runtime.

### Files to change
- `main.py:run()` — add credential check (resolution A)
- `bot/safeguards.py:_check_stale_stream()` — optional grace-period (resolution B)

### Tests to add
- `test_startup_exits_without_credentials` — monkeypatch env vars to empty, assert startup exits
- `test_stale_stream_trips_after_grace_period` (if B is implemented)

---

## Issue 2 — ORDER_SUBMITTED event logged on every reconcile tick, not just once

**Classification:** Confirmed bug  
**Severity:** Medium

### What happens

`main.py:_collect_reconcile_events()` detects ORDER_SUBMITTED with:
```
prev_status == "PENDING" and cur_status == "PENDING" and row.get("exchange_order_id")
```

On Tick 1, the order is submitted and gets an `exchange_order_id`. The order stays PENDING. On Tick 2, `before_orders` snapshots the order as PENDING. After reconcile it's still PENDING (fill not yet applied). `exchange_order_id` is already set. Condition passes → ORDER_SUBMITTED logged again. This repeats every tick until the order fills.

### Resolution

**A. (Recommended / minimal)** Expand `before_orders` snapshot to also capture `exchange_order_id`. Change the detection condition to:
```
prev had no exchange_order_id AND cur has exchange_order_id
```
This fires exactly once — the tick the exchange_order_id first appears.

**B.** Add a `submitted` boolean column to the orders table or dedup against the event_log. More complex, schema change required.

### Files to change
- `main.py:signal_consumer_task()` — expand snapshot dict to include `exchange_order_id`
- `main.py:_collect_reconcile_events()` — condition on None→present transition

### Tests to add
- `test_order_submitted_logged_exactly_once` — paper adapter, 3 ticks, assert exactly 1 ORDER_SUBMITTED in event_log
- `test_order_submitted_not_logged_before_submission`

---

## Issue 3 — STOP_REQUIRED event is a false positive when another guard disabled trading

**Classification:** Confirmed bug  
**Severity:** Medium

### What happens

`main.py:_check_fills_and_positions()`:
```python
safeguards.check_stop_invariant(symbol)
if not safeguards.trading_enabled:
    log_event("STOP_REQUIRED", ...)
```

If trading was already disabled for any other reason (stale_stream, manual disable), `not safeguards.trading_enabled` is True even when `check_stop_invariant` returned True (stop is fine). STOP_REQUIRED is logged as a false positive.

### Resolution

**A. (Recommended / minimal)** Use the return value of `check_stop_invariant` directly:
```python
ok = safeguards.check_stop_invariant(symbol)
if not ok:
    log_event("STOP_REQUIRED", ...)
```
`check_stop_invariant` already returns `False` only when the invariant is actually violated.

**B.** Check both the return value and `"stop_required" in safeguards._tripped`. More coupled to internals, not necessary.

### Files to change
- `main.py:_check_fills_and_positions()` — capture return value, log only on False

### Tests to add
- `test_stop_required_not_logged_when_other_guard_tripped` — disable via stale_stream, open position with valid stop, call `_check_fills_and_positions`, assert no STOP_REQUIRED in event_log
- `test_stop_required_logged_when_invariant_fails`

---

## Issue 4 — `_tripped` set is persisted but never restored on restart

**Classification:** Confirmed bug  
**Severity:** High (safety-critical)

### What happens

`Safeguards._persist()` writes `{"trading_enabled": ..., "tripped": [...]}` to `runtime_state`.

`Safeguards.__init__` reads only `trading_enabled` from the persisted state. `_tripped` is always initialized as an empty set:
```python
self._tripped: set = set()  # always empty on restart
```

Three failure modes result:

1. **Blocked stale_stream recovery after restart:** Recovery code at `safeguards.py:126` checks `"stale_stream" in self._tripped`. Since `_tripped` is empty after restart, this condition is never entered. Trading stays permanently disabled even after the stream recovers.

2. **Duplicate guard log messages after restart:** `_disable` logs only when `guard_name not in self._tripped`. Empty set means re-logging on first evaluation.

3. **Safety override (worst case):** If `stop_required` had tripped, then after restart `_tripped` is empty. If `stale_stream` then trips and recovers, the recovery code sees `self._tripped == {"stale_stream"}` and re-enables trading — silently overriding the previously tripped non-recoverable `stop_required` guard.

### Open question requiring a decision

> **Should stale_stream be recoverable after a restart, or only within a session?**
> Restoring `_tripped` means a restarted bot that previously tripped `stale_stream` can recover when the stream comes back. Is that desired? Or should any restart require manual intervention to re-enable trading?

### Resolutions

**A. (Recommended / minimal)** In `Safeguards.__init__`, restore `_tripped` from persisted state:
```python
if persisted and "tripped" in persisted:
    self._tripped = set(persisted["tripped"])
```
Closes all three failure modes in ~2 lines.

**B. (Belt-and-suspenders, additive after A)** Persist `stop_required` as a separate `runtime_state` key, independent of `_tripped`, so it can never be accidentally cleared by the tripped-set mechanism.

### Files to change
- `bot/safeguards.py:__init__()` — restore `_tripped` from persisted state

### Tests to add
- `test_tripped_set_persists_across_restart` — trip stop_required, new Safeguards instance, assert "stop_required" in `_tripped`
- `test_stale_stream_recovery_blocked_when_stop_required_tripped` — trip both, restart, recover stale_stream, assert trading still disabled
- Update `test_disabled_state_persists_across_restart` to also assert `_tripped` contents

---

## Issue 5 — Test coverage gaps in orchestrator layer

**Classification:** Design inconsistency  
**Severity:** Medium

### What is not tested

| Gap | Notes |
|-----|-------|
| `main.py:_collect_reconcile_events()` | Zero test coverage — this is where Issues 2 and 3 live |
| `main.py:_process_new_signals()` | Status mapping and safeguard integration untested |
| `main.py:_check_fills_and_positions()` | The STOP_REQUIRED false positive (Issue 3) is untested |
| `bot/config.py:validate()` | No test exercises the fail-fast startup validation |
| `on_bar_close` callback wiring | Aggregator + StateMachine integration via callback untested |
| `_print_session_summary` | Not tested |
| Credential-absent startup | See Issue 1 |
| ORDER_SUBMITTED idempotency | See Issue 2 |
| STOP_REQUIRED false positive | See Issue 3 |
| `_tripped` restoration | See Issue 4 |

### Resolutions

**A. (Recommended)** Add targeted tests for Issues 2, 3, 4 plus config validation. Do not attempt to test the async orchestrator. The async orchestrator is better verified by a manual smoke test (`python main.py` with credentials, observe logs for 60 seconds).

**B. (Ambitious, later milestone)** Integration test that runs a truncated event loop with a fake WebSocket adapter pushing trade data into the queue, verifying bars are built, signals emitted, orders filled, events logged.

### Tests to add (from A, in addition to per-issue tests above)
- `test_config_validate_exits_on_live_mode`
- `test_config_validate_exits_on_invalid_portfolio_value`
- `test_config_validate_passes_for_paper_mode`
- `test_config_validate_exits_on_zero_reconcile_interval`

---

# Ranked Issues by Severity

| Rank | Issue | Severity |
|------|-------|----------|
| 1 | Issue 4: `_tripped` not restored from persisted state | **High** — safety-critical |
| 2 | Issue 1: Silent no-WebSocket paper mode | **Medium-High** — violates paper-mode contract |
| 3 | Issue 3: STOP_REQUIRED false positives | **Medium** — corrupts event log |
| 4 | Issue 2: ORDER_SUBMITTED duplicate logging | **Medium** — corrupts event log |
| 5 | Issue 5: Orchestrator test coverage gaps | **Medium** — advisory |

---

# Minimal Safe Implementation Sequence

1. **Fix Issue 4** (`bot/safeguards.py:__init__`) — restore `_tripped` from persisted state. Write `test_tripped_set_persists_across_restart` and `test_stale_stream_recovery_blocked_when_stop_required_tripped`.

2. **Fix Issue 3** (`main.py:_check_fills_and_positions`) — use return value of `check_stop_invariant`. Write `test_stop_required_not_logged_when_other_guard_tripped`.

3. **Fix Issue 2** (`main.py:signal_consumer_task` + `_collect_reconcile_events`) — expand snapshot, condition on None→present transition. Write `test_order_submitted_logged_exactly_once`.

4. **Fix Issue 1** (`main.py:run()`) — add startup guard for missing credentials after answering the policy question above.

5. **Add config validation tests** — `test_config_validate_*` against `bot/config.py`.

6. Run full suite, commit, push.

---

# Open Questions That Must Be Answered Before Coding

1. **Credential-absent startup: hard exit or blocked-trading degraded mode?**  
   Context: the existing `CoinbaseAdapter` was designed to tolerate absent credentials. Is there a valid use case for running without WebSocket data (DB replay, offline dev)? Or does paper mode always require live data?

2. **Stale-stream recoverability after restart: within-session only or across restarts?**  
   Context: fixing Issue 4 means a restarted bot that previously tripped `stale_stream` can recover. Is manual intervention required after any restart, or should recovery be automatic?

3. **Is ORDER_SUBMITTED worth logging in paper mode?**  
   Context: PaperAdapter submission is synchronous and deterministic — it never fails. The event is only meaningful for live mode where network latency or exchange rejection can occur. Should it be suppressed in paper mode or kept for consistency?

4. **What is the acceptance criterion for "end-to-end ready"?**  
   Context: all component tests pass, but no test exercises the wired orchestrator. Is "all tests pass" sufficient, or is a manual smoke test against live WebSocket data required before this is considered production-ready paper mode?
