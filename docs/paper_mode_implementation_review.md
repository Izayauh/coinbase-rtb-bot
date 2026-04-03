# Implementation Review: Commit `5e7c991`

**Reviewer:** Claude Opus 4.6 (adversarial pass)  
**Scope:** Correctness, minimality, new bugs, test quality, go/no-go  
**Commit message:** "fix(audit): resolve four confirmed bugs from paper-mode adversarial review"

---

## Issue 4 — `_tripped` restore

### Verdict: Correct, minimal, no new bugs

The fix at `safeguards.py:50-53` adds:
```python
if persisted and "tripped" in persisted:
    self._tripped: set = set(persisted["tripped"])
else:
    self._tripped: set = set()
```

This is exactly right. The old `_persist()` already wrote `tripped` into the JSON payload — the key was always there; it just wasn't read back. The new `else` branch handles pre-existing persisted state from old code gracefully.

**Minor cosmetic issue:** both branches annotate `self._tripped: set`. Python allows this at runtime without error, but type checkers may warn about the duplicate annotation.

**Safety-critical test confirmed correct.** `test_stale_stream_recovery_blocked_when_stop_required_tripped` traces the exact failure mode:
1. Session 1: `stop_required` trips, persisted
2. Restart: new instance loads `_tripped={"stop_required"}`
3. Session 2: `stale_stream` trips, then recovers
4. Recovery code checks `self._tripped == {"stale_stream"}` — it's `{"stop_required", "stale_stream"}` — condition False — trading stays disabled ✓

---

## Issue 3 — STOP_REQUIRED false positive

### Verdict: Stated fix is correct. One pre-existing bug not addressed.

The change from checking `not safeguards.trading_enabled` to using the return value of `check_stop_invariant` is correct and minimal.

**Pre-existing bug not fixed by this commit:** `_check_fills_and_positions` is called on every reconcile tick (every 5 seconds). If a position exists with `stop_active=0`, `check_stop_invariant` returns False on every tick, and `log_event("STOP_REQUIRED")` writes to `event_log` on every tick. STOP_REQUIRED fires indefinitely until the position is manually resolved.

This was also true with the old code (different trigger, same outcome). The fix was an opportunity to add idempotency and didn't. No test covers the multi-fire behavior.

**Also:** `_check_fills_and_positions` fetches the position at line 81, then `check_stop_invariant` fetches it again at line 97. Two DB reads for the same row. Pre-existing inefficiency, not introduced here.

---

## Issue 2 — ORDER_SUBMITTED snapshot timing

### Verdict: Correct for all scenarios. One fragility introduced.

Moving the snapshot after `_process_new_signals` and expanding it to include `exchange_order_id` fixes both the missed emission (paper mode) and the duplicate emission (live mode).

**All scenarios trace correctly:**

| Scenario | Before fix | After fix |
|----------|-----------|-----------|
| Paper mode tick 1 (submit) | ORDER_SUBMITTED: not logged (order not in snapshot) | ORDER_SUBMITTED: logged (None→present) ✓ |
| Paper mode tick 2 (fill) | ORDER_SUBMITTED: not logged (PENDING→FILLED skips condition) | ORDER_SUBMITTED: not logged (prev_exch_id set) ✓ |
| Live mode tick 2..N-1 (pending) | ORDER_SUBMITTED: logged on every tick (duplicate) | ORDER_SUBMITTED: not logged (prev_exch_id set) ✓ |
| Restart (order already submitted) | No regression | ORDER_SUBMITTED not re-emitted ✓ |

**Fragility introduced:** The `before_orders` format changed from `{order_id: str}` to `{order_id: dict}`. `_collect_reconcile_events` now expects `prev["status"]` and `prev["exchange_order_id"]`. If any future caller passes the old format, the function raises `TypeError` with a confusing message. There is currently only one call site (updated correctly), but the function's type annotation (`before_orders: dict`) does not enforce the new contract. This is a breaking interface change with no type enforcement.

**Exception ordering:** The move of `_process_new_signals` before the snapshot does not change exception semantics — all operations are in the same try/except block. An exception anywhere still aborts the full iteration.

---

## Issue 1 — `ws_connect` credential guard removal

### Verdict: Architecturally justified, empirically unverified. One overstated claim in the commit message.

### The reasoning is sound

`_ws_payload` conditionally adds JWT when `_enabled=True`, producing a valid unauthenticated message when `_enabled=False`. `ws_loop` skips the `user` channel subscription when `_enabled=False`. The code was designed to support credential-less public channel subscriptions. The guard in `ws_connect` was a contradiction with the rest of the class's own design.

### What exact evidence is still missing

Whether Coinbase Advanced Trade WebSocket server accepts a `subscribe` message without a `jwt` field on the `market_trades` channel. The payload sent without credentials:
```json
{"type": "subscribe", "product_ids": ["BTC-USD"], "channel": "market_trades"}
```
This has not been empirically tested. The commit message states "market_trades and heartbeats are public channels" as fact. It is an assumption.

### What happens if the assumption is wrong

1. `ws_loop` connects successfully at the TCP level
2. Sends unauthenticated subscription message
3. Coinbase responds with an error or closes the connection
4. `except Exception` catches it, logs `"WS Exception dynamically isolating: <error>"`
5. `asyncio.sleep(5)` → reconnect → repeat every 5 seconds
6. `market_queue` never receives data
7. `md_processor.run()` blocks forever on `await self.adapter.market_queue.get()`
8. `last_trade_ts` stays `0.0`
9. Stale-stream guard exempts `0.0` → `can_trade()` returns `True`
10. Bot runs signal processing with no market data, logs WS errors every 5 seconds

**This is not silent** — the log spam is unmistakable. It is more observable than the pre-fix behavior (which was a completely silent no-op). But it is also not safe to call harmless: the bot would consume resources and process any pre-seeded signals while generating no useful output.

### Net assessment of the fix

The fix is an improvement even if the empirical assumption turns out to be wrong: it replaces a completely silent failure mode with a noisy, diagnosable one. The architectural reasoning is correct. The single outstanding requirement is a five-minute empirical test.

---

## Test Quality

### Strong

- `test_stale_stream_recovery_blocked_when_stop_required_tripped` — proves the exact safety-critical path across a simulated restart. The most important test in this commit.
- `test_tripped_set_persists_across_restart` — minimal, direct, verifies the exact fix.
- `test_stop_required_not_logged_when_other_guard_disabled_trading` — isolates the false-positive case precisely.
- `test_order_submitted_logged_exactly_once` — covers both emission and non-duplication across 3 ticks.

### Weak or brittle

**`_import_helpers()` in `test_main_events.py`**

Loads `main.py` via `importlib.util.spec_from_file_location` using a hardcoded relative path. Problems:
1. Re-executes `main.py` module-level code (including `logging.basicConfig`) on every call — called multiple times per test
2. Path breaks if test file or main.py moves
3. Failure mode is an `ImportError`, not a clear test assertion failure

The correct fix is to extract `_check_fills_and_positions`, `_collect_reconcile_events`, and `_process_new_signals` to a proper submodule (`bot/consumer.py`) that is cleanly importable. This is the right architectural move and would eliminate `_import_helpers()` entirely.

**`test_stop_required_not_logged_when_stop_is_valid`**

Would pass even without the fix. A position with `stop_active=1, stop_price=48000` passes `check_stop_invariant` in both old and new code, and in the old code `trading_enabled` was True anyway. This test verifies a case that was never broken.

**Config tests — unused `monkeypatch` parameter**

All nine config tests accept `monkeypatch` as a parameter but never use it. The patching is done by directly mutating `cfg._raw` in a try/finally. This works but misleads readers into thinking pytest's monkeypatching is involved.

### Important paths still untested (not introduced by this commit)

| Path | Status |
|------|--------|
| STOP_REQUIRED fires on every tick with persistent invariant failure | Untested |
| `safeguard_task` TRADING_DISABLED emission | Zero coverage |
| POSITION_OPENED event emission | Zero coverage |
| `ORDER_SUBMITTED` when `can_trade()` is False (no new orders) | Untested |
| `on_bar_close` callback wiring | Zero coverage |
| Unauthenticated WebSocket subscription accepted by Coinbase | Empirically untested |

---

## Go / No-Go

### Safe to merge as-is

**No.** Issue 1's claim is stated as fact but is empirically unverified.

### Merge only after X

**After empirically verifying that Coinbase Advanced Trade WebSocket accepts unauthenticated `market_trades` subscriptions.**

Test: run `python main.py` without `COINBASE_API_KEY` and `COINBASE_API_SECRET`. Watch logs for 30 seconds.
- Pass: `"Direct Advanced WS connected."` followed by trade data
- Fail: `"WS Exception dynamically isolating:"` repeating every 5 seconds

This is the single blocking prerequisite. Five minutes to resolve.

### Must change before merge

Nothing is technically broken in the production code paths.

**Strong recommendation (not a blocker):** Extract `_process_new_signals`, `_collect_reconcile_events`, and `_check_fills_and_positions` from `main.py` into `bot/consumer.py`. This eliminates the `_import_helpers()` hack in `test_main_events.py`, makes the functions cleanly importable, and follows the existing package structure. The current test pattern works but locks in a fragile dependency on `main.py`'s location and module-level side effects.

**Known issue to track:** STOP_REQUIRED multi-fire — the event is logged on every reconcile tick while the stop invariant fails, not once per failure. This is pre-existing behavior that survived all four audit passes.
