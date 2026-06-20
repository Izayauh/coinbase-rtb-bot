# Tiny Live Acceptance Contract

`python verify_tiny_live_ready.py` is the only readiness decision surface for
the first capped live trial. It is read-only at Coinbase and writes a JSON
report. It never submits an order.

Required gates:

1. One live BTC-USD product, $10 test notional, no more than $15 per order and
   $30 total position.
2. One managed runtime process is alive.
3. The entry-halt file is absent. Its presence blocks BUYs but never SELL exits.
4. The configured strategy is a registered, implementation-ready strategy.
5. The configured strategy has a matching, unexpired authorization file linked
   by SHA-256 to a real `EVIDENCE_PASSED` artifact with matching strategy id,
   version, and product.
6. Coinbase account lookup and non-executing BUY preview pass.
7. Live-journal integrity, pending-order, execution, and position invariants pass.
8. Live bars are fresh and the required 1h/4h history is contiguous.
9. Automatic stop, target, and time exits are enabled.
10. Reconciled trade outcomes and learning reviews are available. Learning never
    changes parameters or grants order authority.
11. Market, regulatory, derivatives, official Coinbase status, and attributed
    CoinDesk provenance are populated without collection gaps.
12. Targeted adversarial strategy/execution/reconciliation/exit/safety tests pass.

The report is also written as a tamper-evident activation receipt. A candidate
BUY requires a matching `READY` receipt no more than five minutes old. The
advisory bridge and final Coinbase BUY call validate it independently.

## Activation sequence

1. Run the candidate evaluation. It must write a machine-readable evidence
   artifact with `evidence_status: EVIDENCE_PASSED`.
   The cloud research loop continuously publishes the current registered
   candidate result under the strategy/version `latest.json` prefix; a result
   of `INSUFFICIENT_EVIDENCE` or `DEMOTED` is not authorizable.
2. Update `config.yaml` to the exact strategy id/version that produced it.
   Selecting the derivatives-stress candidate disables the archived breakout
   state machine; candidate signals can come only from the external advisory
   bridge.
3. Run:

   `python authorize_strategy.py --evidence <file> --authorized-by <operator>`

4. Keep `KILL_SWITCH` present and rerun acceptance. Only the `entry_halt` gate
   may remain red.
5. Remove `KILL_SWITCH`.
6. Rerun acceptance. Proceed only when the report says `READY`; this creates
   the short-lived receipt.
7. Allow only a fresh, hash-valid advisory for the exact candidate
   id/version to stage a signal. The advisory itself has no order authority.

An authorization expires automatically and cannot expand the configured caps.
Removing the entry halt without passed evidence does not arm BUYs.
