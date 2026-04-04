# Live Verification Audit — feat/live-plumbing

**Date:** 2026-04-04  
**Branch:** `feat/live-plumbing`  
**Prior test count:** 96  
**Final test count:** 108 (+12)

---

## What Was Audited

The branch was audited adversarially: do not assume the current implementation
is ready, prove it or disprove it. The question was: *"Is this bot capable of
placing a live buy order right now?"*

---

## What Was Proven Before This Session

| Claim | Status |
|---|---|
| Config double-gate works (flag + env var) | Proven — tested |
| Kill switch file blocks order submission | Proven — tested in both `can_trade()` and `submit_order_intent` |
| `CoinbaseAdapter` selected in live mode | Proven by inspection |
| Fixed notional ($10) sizing works | Proven — tested |
| WebSocket connects to Coinbase | Proven by live run (public + authenticated channels) |

---

## What Was Wrong / Unproven Before This Session

### 1. Banner lied about portfolio value
The live startup banner printed `$10,000` from `config.runtime.portfolio_value`.  
This is a configured number, not a fetched Coinbase balance.  
No REST call was ever made at startup to verify credentials or fetch real balance.

### 2. Missing credentials = silent paper mode in live mode
If `COINBASE_API_KEY` or `COINBASE_API_SECRET` were absent, `CoinbaseAdapter._enabled`
was `False`. `submit_order_intent()` with `_enabled=False` returns a synthetic
`cb_ord_xxx` ID — identical to paper mode behavior — with no error. The bot would
run in live mode config while placing fake orders silently.

### 3. Persisted kill_switch mismatch
If the kill switch file was created (tripping the `kill_switch` guard, persisting
`trading_enabled=False`), then the file was deleted, the banner would say
`kill switch not present` but the bot could not trade. The stale persisted state
was never cleared and no honest error was shown.

### 4. No REST auth verification at startup
The bot could start fully in live mode without ever calling a Coinbase API endpoint.
The first real credential test would be the first order submission — after a signal
fires, during live trading.

### 5. No preflight script
No way to answer "can this bot trade right now?" without starting the bot and
waiting for a signal to fire.

---

## Changes Made

### `bot/safeguards.py`
Added stale `kill_switch` auto-clear in `__init__`.

When `kill_switch` is in the persisted `_tripped` set but the kill switch file
no longer exists, the guard is cleared on startup and trading is re-enabled (if
no other guards are tripped). This is safe because deleting the file IS the
operator's explicit signal to clear it. Non-recoverable guards (`stop_required`,
`daily_loss`, `position_size_exceeded`) are NOT auto-cleared.

**Design choice:** auto-clear rather than abort-with-error, because the prior
behavior (silently disabled while banner said "not present") was actively
misleading. The auto-clear is logged visibly.

### `main.py`
Three additions to live startup:

1. **`_abort_if_live_creds_missing(adapter)`** — hard `sys.exit(1)` if
   `adapter._enabled=False`. No silent degradation to synthetic fills.

2. **`_fetch_live_balances_for_banner(adapter)`** — async fetch of real Coinbase
   account balances before the abort window. Exits on auth failure. Proves
   credentials work before any operator countdown.

3. **`_print_live_banner()`** updated — now takes `live_balances: dict` and
   displays a clearly labelled "Live Coinbase Account (fetched this startup)"
   section. The configured portfolio value line is now explicitly labelled
   `Portfolio (config)  <- configured, NOT live balance`.

The `CoinbaseAdapter` is now constructed before the banner (previously it was
constructed after the abort window).

### `bot/readiness.py` (new)
Core readiness logic, importable as a module.

`check_readiness()` → `(ready: bool, blockers: list[str], live_balances: dict)`

Checks in order:
1. `runtime.mode == live`
2. `safety.live_trading_confirmed == true`
3. `LIVE_TRADING_CONFIRMED` env var set
4. `COINBASE_API_KEY` present
5. `COINBASE_API_SECRET` present
6. Kill switch file absent
7. No stale non-recoverable guards in live DB
8. Symbol in product allowlist
9. Coinbase REST auth works and returns accounts

`parse_coinbase_balances(response)` handles SDK response objects and dicts
defensively; used by both `main.py` and `verify_coinbase.py`.

### `verify_coinbase.py` (new)
Standalone credential + account verification script.

```
python verify_coinbase.py
```

Reads `COINBASE_API_KEY` and `COINBASE_API_SECRET`, calls `REST.get_accounts()`,
prints all currency balances. Exit 0 on success, exit 1 on any failure. Run this
first to prove credentials map to the intended account.

### `verify_live_ready.py` (new)
Comprehensive preflight script. Answers: **"Can this bot place a live buy order right now?"**

```
python verify_live_ready.py
```

Prints `LIVE ORDER PATH ARMED: YES` or lists every blocker. Calls
`check_readiness()` which includes the live Coinbase REST auth test.

### `start-live.ps1` (new)
Windows operator helper for live sessions.

Sets `LIVE_TRADING_CONFIRMED=true` for the PowerShell session only.  
Clears it in a `finally` block after the bot exits (even on crash/Ctrl+C).  
**Never persists `LIVE_TRADING_CONFIRMED`.**

One-time credential setup instructions are in the file comments.

---

## Tests Added (108 total, was 96)

### `bot/tests/test_readiness.py` (10 new tests)
| Test | What it proves |
|---|---|
| `test_readiness_no_when_api_key_missing` | COINBASE_API_KEY absent → blocker |
| `test_readiness_no_when_api_secret_missing` | COINBASE_API_SECRET absent → blocker |
| `test_readiness_no_when_kill_switch_file_exists` | Kill switch file present → blocker |
| `test_readiness_no_when_live_confirmed_env_not_set` | LIVE_TRADING_CONFIRMED unset → blocker |
| `test_readiness_no_when_stale_non_recoverable_guard` | `stop_required` in live DB → blocker |
| `test_readiness_stale_kill_switch_not_blocker_when_file_gone` | Stale kill_switch (file gone) is NOT a blocker |
| `test_readiness_no_when_rest_auth_fails` | REST 401 → blocker |
| `test_readiness_no_when_rest_returns_no_accounts` | REST returns empty accounts → blocker |
| `test_abort_if_live_creds_missing_raises_exit` | `_enabled=False` → `sys.exit(1)` |
| `test_abort_if_live_creds_missing_no_raise_when_enabled` | `_enabled=True` → no exit |

### `bot/tests/test_safeguards.py` (2 new tests)
| Test | What it proves |
|---|---|
| `test_stale_kill_switch_auto_cleared_on_init` | kill_switch persisted, file deleted → cleared + trading re-enabled on next init |
| `test_stale_kill_switch_cleared_but_other_guard_keeps_trading_disabled` | kill_switch cleared but `stop_required` keeps trading disabled |

---

## Commands to Run

### Prove credentials map to your Coinbase account
```
python verify_coinbase.py
```
Pass = key resolves to a real portfolio with currency balances printed.

### Prove live order path is armed (no order placed)
```powershell
$env:LIVE_TRADING_CONFIRMED = "true"
python verify_live_ready.py
```
Expected output: `LIVE ORDER PATH ARMED: YES` with live Coinbase balances shown.

### Standard live session launch
```powershell
.\start-live.ps1
```
Sets `LIVE_TRADING_CONFIRMED` for session only; clears on exit.

---

## Final Verdict

**Live buy capability: NOT fully proven.**

| Evidence for | Evidence against |
|---|---|
| Config gate, env gate, kill switch all wired and tested | `verify_coinbase.py` not yet run against real exchange |
| `submit_order_intent` calls `rest.create_order` — unit tested with mocks | No signal has ever fired in live mode; order path never end-to-end exercised |
| JWT builds successfully (confirmed in prior sessions) | Fill/reconcile path untested against real fills |
| Startup now aborts on missing creds (not silently degraded) | |

**The gap:** one signal firing and being executed against the real exchange.  
Running `verify_coinbase.py` and seeing real account balances closes the
account-mapping gap. The rest is structure — proven by tests and inspection.
