# Execution & Order Management

## Entry Assumptions
v0 must be strictly honest about execution.

Use:
* **IOC limit** generically for execution entries.
* **Exchange-side** native stop protections.
* **Local reconcile loop** constantly asserting protection persistence independently of exchange lag.

*Do extremely well to absolutely avoid market orders unless executing emergency scale outs. Post-only blocks break immediate-fill constraints, which we require under IOC structures natively.*

## Replay Validation Standard
Since exact execution environments deviate natively, execution logic implies mapping conservative traits effectively:
* Assume `Taker` logic strictly on entries unless actively modeled securely underneath.
* Assume `Taker` penalties directly explicitly on emergency exits.
* Always enforce pessimistic round-trip costs aggressively throughout simulation modeling constraints natively against Maker/Taker spread.

## Practical System Constraint Loop
1. Subscribe cleanly to `market-data` and `user-order` streams.
2. Aggregate localized bars.
3. Quantify regime/setup exclusively on close boundaries.
4. Scale one `IOC limit` strictly.
5. Immediately map generic Exchange `Stop` limits.
6. Reconcile structural integrity over rapid bursts (5 Seconds max intervals).
7. Execute flattening mechanisms defensively locally if exchange structures shift.
8. Store actively directly natively via generic DB logging interfaces.
