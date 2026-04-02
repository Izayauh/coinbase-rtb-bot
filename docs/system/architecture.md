# System Architecture (v0)

## Core Foundation
This bot operates as a single-process BTC-only codebase. It relies on one centralized SQLite instance. All computations use pure Python async routines. The core goal of v0 is to strip all abstraction logic and map execution against Coinbase Advanced semantics.

### Implementation Hierarchy (`bot/`)

```text
bot/
  main.py                # Bootstraps the primary async loop and connects queues.
  config.yaml            # Declarative parameters specifying 0.20% risk on BTC-USD.
  coinbase_adapter.py    # Thin wrapper for the official Coinbase Advanced SDK exposing asyncio Queues.
  market_data.py         # Pulls from adapter queues and extracts timestamps for the BarBuilder.
  bars.py                # Constructs 1m, 1h, and 4h boundaries incrementally. Processes 4h before 1h.
  strategy.py            # Dependency-free indicator logic matching Wilder's smoothing for ATR/RSI.
  state_machine.py       # Strict explicit evaluation sequence determining execution states.
  db.py                  # Lean SQLite schema establishing minimal storage models.
  journal.py             # Interfaces matching precise operations to DB transactions without crash overlaps.
  models.py              # Strongly typed Dataclasses defining Bars, Signals, and Positions.
  tests/                 # Holds precise simulation limits ensuring logical restarts behave accurately.
```

### State Machine Lifecycle
The generic states utilized in `state_machine.py` directly track:

* `IDLE`: Baseline standby awaiting a bullish 4h regime and 1h breakout.
* `WAITING_RETEST`: Activated upon volume-confirmed 1h breakout. Retest must occur within 5 bars.
* `RETEST_CONFIRMED`: Retest bound valid. Requires continuation confirmation to proceed.
* `SIGNAL_EMITTED`: Emits signal tracking logic into generic persistence tables.
* `COOLDOWN`: Reserved exclusively for structural failures or stops.
* `DISABLED`: Used explicitly if timestamp gaps exceed expected bounds.

## Persistence Operations (Step 3) 
The DB architecture avoids complex object-relational mapping. Restart-safety is driven by `ON CONFLICT` constraints, allowing identical bars or events to overwrite or aggregate volume correctly instead of throwing duplicate exception errors.
