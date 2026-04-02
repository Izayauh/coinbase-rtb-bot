# Risk Management

## Position Sizing
* **Risk per trade:** 0.20% of sleeve equity
* **Max open positions:** 1 (since we only map BTC-USD)
* **Position calculation:** `risk_dollars / stop_distance`
* **Stop distance:** `max(1.4 * ATR_1h, structure_stop_distance)`

This tight structure enforces absolute selectivity over random spraying.

## Stop Placement
Initial stop targets the lower of:
* Retest low minus 0.25 ATR
* Entry minus 1.4 ATR

Coinbase Advanced stop logic hinges inherently strictly to last-trade prints, and TPs/SL brackets hold conditional execution risks during heightened volatility natively. Therefore:
1. Native `exchange-side` stops are posted.
2. A fast localized daemon reconciliation script loops persistently confirming protection and flattening aggressively if synchronization fails.

## Profit-Taking Management
* at **+1R**: scale out 20% & move to breakeven + fees gap.
* at **+2R**: scale out 30% & trail actively loosely tracking high variance constraints.
* Runner 50% left against continuous trailed protection.

### Time Stop Requirement
* If after 12 completed 1h bars the trade has not hit +0.75R structurally, forcefully flatten. 

## Daily & Structural Drawdown Limits
Hard limits:
* Max daily realized loss: **1.0%**
* Max weekly realized loss: **2.5%**
* Complete bot structural shutdown: **5.0% drawdown**
