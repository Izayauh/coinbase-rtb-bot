# Project Roadmap & Metrics

## Build Order

This sequence guarantees the shortest path to falsification safely. 

### Step 1
`bot/coinbase_adapter.py`
Get auth, balances, open orders, order placement, cancel, fills, and both WebSocket streams working effectively.

### Step 2
`bot/bars.py` and `bot/market_data.py`
Map out rolling 1m, 1h, and 4h boundaries incrementally.

### Step 3
`bot/db.py`, `bot/models.py`, `bot/journal.py`
Form persistence tables effectively capturing systemic logging traces.

### Step 4
`bot/strategy.py` and `bot/state_machine.py`
Connect filters directly applying regime validation alongside explicit retest progression thresholds.

### Step 5
`bot/execution.py` and `bot/risk.py`
Model explicitly aggressive IOC placement routing limits natively encapsulating drawdown logic. 

### Step 6
`bot/replay.py`
Emulate back-states securely demanding Walk-Forward constraints defensively effectively exposing curve-fit errors dynamically natively.

### Step 7
**Paper Mode Validation**
Live tape logic tracking seamlessly capturing signals correctly simulating exact placement constraints logging seamlessly independently to standard databases.

### Step 8
**Tiny Live Mode**
Strict BTC isolation utilizing active generic limits natively respecting tiny 0.20% margin scales effectively ensuring stable operation boundaries natively seamlessly.

## Validation Standard
The bot does not graduate because it possesses a beautiful mock equity graph natively. It graduates strictly if:
* Evaluates positively dynamically after exact pessimistic cost limits.
* Walk-forward validation succeeds independently.
* Paper trails mirror Replay trails dynamically closely natively safely.
* Mismatches explicitly exist understandably naturally.
