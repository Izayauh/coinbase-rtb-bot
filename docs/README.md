# CB-RTB v0 (Coinbase Retest Breakout bot, v0)

Welcome to the documentation for the **CB-RTB v0** trading system. 

It is designed as an explicitly **monolithic, single-process, async layout** trading natively on **BTC-USD**. We actively avoid generic bulky external orchestration modules to maintain pure visibility on our strictly designed edge logic.

## Current System State
> [!IMPORTANT]
> **To get the most accurate, detailed explanation of how everything fits together right now, please read:**
> => **[System Architecture](./system/architecture.md)**

## Documentation Index
- **[System Architecture](./system/architecture.md)** 
  *Detailed mapping of the v0 `bot/` hierarchy, minimal Data models, async threading, state machine progressions, and database interactions.*
- **[Trading Rules](./strategy/trading_rules.md)**
  *Mathematical specifications defining precisely how Breakouts and Retests validate locally.*
- **[Risk Management](./risk/risk_management.md)**
  *Margin boundaries capping risk completely dynamically.*
- **[Execution Limits](./execution/order_management.md)**
  *Rigorous IOC logic strictly bounding taker/maker costs aggressively.*
- **[Build Roadmap](./project/roadmap_and_metrics.md)** 
  *Status of the 8-step build pipeline securely.*
- **[Config Defaults](./project/config_defaults.yaml)**
  *Raw definitions defining limits globally.*
