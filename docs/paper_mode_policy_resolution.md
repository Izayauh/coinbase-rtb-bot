# Paper-Mode Policy Resolution

**Source:** Adversarial review of `paper_mode_audit_plan.md`  
**Commit reviewed:** `319100e`  
**Purpose:** Resolve the four open policy questions before implementation begins. Correct one factual error in the original audit plan.

---

## What This Document Is

The `paper_mode_audit_plan.md` identified five bugs and left four policy questions open. This document answers those questions using evidence from the actual code paths, corrects one significant mistake in the audit plan, and closes with binding constraints the implementation must follow.

---

## Correction to the Audit Plan

### Issue 1 recommended the wrong fix

The audit plan recommended: *add a startup guard in `main.py` — if credentials are absent, log CRITICAL and `sys.exit(1)`.*

**This is wrong.** The WebSocket loop in `coinbase_adapter.py` was explicitly designed to work without credentials:

- `_ws_payload()` (lines 123–132): builds subscription messages with JWT only if `self._enabled`. Without credentials, it produces a valid unauthenticated message.
- `ws_loop()` (lines 144–147): subscribes to `market_trades` and `heartbeats` unconditionally. Only the `user` channel (private order events) requires auth.
- `ws_connect()` (line 173): `if not self._enabled: return` — this is the contradiction. The loop supports unauthenticated connections; the connect guard prohibits them.

The correct fix is to remove the guard from `ws_connect`, not to force a hard exit. Paper mode does not need REST API access — only the public WebSocket feed for bar-building.

---

## Policy Question 1 — Credential-absent paper mode

### Should startup hard-exit, run degraded, or allow credential-less WebSocket?

**Decision: Allow credential-less WebSocket.**

The code already supports it. `market_trades` and `heartbeats` are public Coinbase channels that do not require authentication. The loop was written to handle this case. The connect guard is a bug in `ws_connect`, not a policy.

**What needs to happen:** Remove `if not self._enabled: return` from `ws_connect` for the credential-less path.

**One empirical prerequisite:** Verify that Coinbase Advanced Trade WebSocket accepts unauthenticated `market_trades` subscriptions. The code assumes yes; this has not been tested. If Coinbase rejects unauthenticated subscriptions, the entire paper-mode WebSocket strategy needs rethinking. This can be confirmed in under five minutes.

**What hard-exit would break:** It would prevent paper mode from running in its most common use case — testing without live credentials. That contradicts the purpose of paper mode.

---

## Policy Question 2 — Safeguard persistence and recoverability

### Which guards are sticky? Which recover? What survives restart?

**Root fact:** `_persist()` saves `{"trading_enabled": ..., "tripped": [...]}`. `__init__` restores `trading_enabled` but always initializes `_tripped = set()`. The `tripped` list is persisted but never read back.

### Per-guard decisions

| Guard | Recoverable? | Survives restart? | Basis |
|-------|-------------|-------------------|-------|
| `trading_enabled` | No | Yes (already works) | Config flag, always sticky |
| `stale_stream` | Yes | Yes (once bug is fixed) | Comment in code: "Recoverable: if stream resumes and no other guard is tripped, re-enable" |
| `stop_required` | No | Yes (once bug is fixed) | No recovery path exists anywhere in the code |
| `daily_loss` | N/A | N/A | Structural stub, always returns False |

### The safety-critical failure mode

Without restoring `_tripped`, this sequence is possible:

1. `stop_required` trips → `trading_enabled=False`, `_tripped={"stop_required"}`
2. Bot restarts → `trading_enabled=False` loaded, `_tripped=set()` (empty)
3. `stale_stream` trips in new session → `_tripped={"stale_stream"}`
4. Stream recovers → recovery code checks `self._tripped == {"stale_stream"}` → True → **re-enables trading**
5. Trading re-enabled even though `stop_required` was previously tripped

A position without a stop existed, trading was correctly halted, but a restart plus stream recovery accidentally cleared that halt. This is the highest-severity failure mode in the codebase.

**Fix:** Restore `_tripped` from persisted state in `__init__`. Two lines. Must be done first, before any other fix.

### Should stop_required ever be auto-cleared?

No. There is no code path suggesting auto-clearance. Clearing it requires manual intervention (editing the `runtime_state` table directly). This is correct — a position without a stop is a human problem requiring human resolution.

---

## Policy Question 3 — ORDER_SUBMITTED event semantics

### The audit plan's diagnosis was incomplete

The audit plan said the bug is *"duplicate logging on later pending ticks."* That is correct for live mode but misses the paper-mode case.

### Exact tick trace in paper mode

**Tick 1:**
- `before_orders` snapshot: `{}` (order does not exist yet)
- `_process_new_signals` creates Order (PENDING, no `exchange_order_id`)
- `reconcile_pending_orders` submits it, sets `exchange_order_id`, then `continue`s
- `_collect_reconcile_events({})` — empty dict — nothing fires
- **ORDER_SUBMITTED: not logged**

**Tick 2:**
- `before_orders` snapshot: `{"ord_1": "PENDING"}` (already has `exchange_order_id`)
- `reconcile_pending_orders` fetches fill, fills the order → FILLED
- `_collect_reconcile_events`: `prev=PENDING`, `cur=FILLED` → ORDER_FILLED logged
- ORDER_SUBMITTED condition requires `cur == "PENDING"` — it's FILLED — **not logged**

**Result in paper mode: ORDER_SUBMITTED is never logged.** The order skips the only window where the condition can fire.

### In live mode (multi-tick fills)

- Tick 1: New order, not in snapshot → not logged
- Tick 2: In snapshot as PENDING; still PENDING after reconcile; `exchange_order_id` present → **logged (correct)**
- Tick 3: Same state → **logged again (duplicate)**
- Tick N: PENDING→FILLED → ORDER_FILLED

### Summary of the actual bug

| Mode | Behaviour | Classification |
|------|-----------|----------------|
| Paper mode | ORDER_SUBMITTED never fired | Missed emission |
| Live mode (slow fill) | ORDER_SUBMITTED fired every tick after submission | Duplicate emission |

The audit plan only identified the live-mode duplicate. The paper-mode miss is the more fundamental problem and is caused by snapshot timing: the snapshot is taken before `_process_new_signals`, so orders created on that tick are invisible to the event detector.

### Should ORDER_SUBMITTED exist in paper mode?

Yes. Paper mode simulates the full operational event sequence. An operator using paper logs to validate event pipeline behaviour needs to see ORDER_SUBMITTED, or the pipeline validation is incomplete.

### Correct fix

The snapshot must be taken after `_process_new_signals` but before `reconcile_pending_orders`, so newly created orders are in scope. The detection condition must then fire only on the None→present transition of `exchange_order_id` — not on every tick where exchange_order_id is already set. This closes both the miss (order now in snapshot when submitted) and the duplicate (condition requires transition, not presence).

---

## Policy Question 4 — End-to-end acceptance criteria

### Is passing the current component tests enough?

No.

The three confirmed bugs (Issues 2, 3, 4) all live in code with zero automated test coverage:
- `main.py:_collect_reconcile_events` — no test
- `main.py:_check_fills_and_positions` — no test
- `bot/safeguards.py:__init__` (`_tripped` restore) — no test
- `bot/config.py:validate()` — no test

The component tests passing is consistent with all three bugs being present. Tests-passing does not mean runtime-correct.

### Minimum acceptable proof for "paper mode is ready"

**Automated (required before merge):**
1. Test for `_tripped` restoration — trip a guard, new instance, verify `_tripped` contains the guard name
2. Test for the safety-critical interaction — trip `stop_required` + restart + `stale_stream` recovery, verify trading stays disabled
3. Test for ORDER_SUBMITTED — exactly one emission per order, paper adapter, 3 ticks
4. Test for STOP_REQUIRED false positive — trading disabled by stale_stream, valid position stop, verify STOP_REQUIRED not logged
5. Tests for `config.validate()` — live mode exits, invalid portfolio exits, paper mode passes

**Manual (required but not automatable at unit level):**
- Run `python main.py` with and without credentials
- Observe WebSocket connects and `last_trade_ts` updates in logs
- Confirm `event_log` rows appear for at least one bar-close cycle
- Confirm no unhandled exceptions after 60 seconds of operation

---

## Final Decisions Summary

| Question | Decision |
|----------|----------|
| Credential-absent startup | Allow — remove `ws_connect` credential guard; verify Coinbase accepts unauthenticated `market_trades` first |
| stale_stream across restart | Recoverable — restore `_tripped` from DB on init |
| stop_required clearance | Never auto-cleared — sticky until manual DB intervention |
| ORDER_SUBMITTED in paper mode | Must exist — fix requires snapshot timing change + None→present transition detection |
| Acceptance criteria | 5 targeted tests + documented manual smoke test |

---

## Implementation Constraints (non-negotiable)

1. **Fix order:** Issue 4 (`_tripped` restore) must be implemented and tested before Issues 2 and 3. It is a precondition for safely reasoning about any other guard interaction.

2. **No hard exit for missing credentials.** Fix `ws_connect`, do not add a `sys.exit` in `main.py`.

3. **Event logging stays in the consumer/task layer.** Do not move ORDER_SUBMITTED emission into `ExecutionService` or `Safeguards`. The current design constraint — events logged from orchestration, not from core classes — must be preserved.

4. **ORDER_SUBMITTED fix must address snapshot timing**, not just the detection condition. A fix that only adds `exchange_order_id` to the snapshot without moving the snapshot past `_process_new_signals` closes the duplicate but not the miss. Both must be closed.

5. **Every bug fix requires a failing test first.** Write the test, confirm it fails, apply the fix, confirm it passes. No fix without a prior failing test.

6. **Test the `stop_required` + restart + `stale_stream` recovery interaction explicitly** before merging the `_tripped` restore. This is the safety-critical path and must be verified to close.

---

## Remaining Uncertainty

One question is empirical and cannot be resolved by code reading alone:

> Does Coinbase Advanced Trade WebSocket accept unauthenticated subscriptions on the `market_trades` channel?

If yes: remove the `ws_connect` guard and paper mode works without credentials.  
If no: the loop's unauthenticated-payload path is dead code, and paper mode requires credentials to function. In that case, the hard-exit recommendation from the original audit plan becomes correct.

This must be tested before implementation begins on Issue 1.
