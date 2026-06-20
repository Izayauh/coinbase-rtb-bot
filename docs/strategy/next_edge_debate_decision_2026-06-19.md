# Next Crypto Edge Decision — 2026-06-19

## Decision

The research-first Claude/Codex tournament selected Proposal A, a BTC
derivatives-stress exhaustion rebound, by **10 votes to 3**. This is a
provisional shadow-research priority, not evidence of profitability and not
authorization to trade.

The strategy hypothesis is:

1. Detect downside leverage stress using BTC perpetual open interest, funding,
   mark/spot basis, and aggressive derivatives flow.
2. Wait for the Coinbase BTC-USD spot order book to show exhaustion rather
   than blindly buying a falling market.
3. Require bid replenishment, improving order-flow imbalance, and a recovering
   microprice before a hypothetical long entry.
4. Evaluate 1h–4h outcomes after conservative executable friction.

## Immediate implementation

- Added a versioned one-minute `order_math` series containing spread,
  microprice, queue imbalance, 5/10/25/50 bps depth, multilevel depth
  imbalance, OFI, MLOFI, additions/depletion, replenishment ratios, and book
  shape.
- Exact individual queue position and cancellation-versus-execution remain
  unobservable from aggregate public Level2 and are explicitly treated as
  estimates.
- Added public Coinbase International BTC-PERP collection:
  minute-cadence open interest plus official hourly realized funding and mark
  price observations.
- Binance Futures returned HTTP 451 from the U.S. Google Cloud VM, so it is
  excluded from the primary probe unless the infrastructure and legal-access
  constraints change.
- Published the initial 360 order-math observations to Google Cloud Storage
  and BigQuery as `bitwise-trader.crypto_research.order_math_external`.

## Seven-day feasibility gates

The strategy proceeds to a registered 30-day shadow test only if:

- the Coinbase INTX source remains stable and timestamp/provenance checks pass;
- enough distinct stress episodes occur to test the mechanism;
- derivatives stress precedes or coincides with spot-book exhaustion rather
  than arriving after the move;
- the hypothetical edge survives the binding fee/spread/slippage model;
- results remain stable across purged walk-forward folds and do not depend on
  one event.

If those gates fail, the bounded challenger becomes the primary next test:
ETH/SOL volatility-shock failed-breakdown reversion with the same replenishment
and execution-realism requirements.

## Preserved dissent

The strongest objections were operational: public aggregate L2 cannot reveal
exact queue position; maker fills need conservative queue-ahead/depletion
models; cross-venue feeds add alignment and outage risk; and liquidation
cascades may be too rare for fast statistical confidence. These objections are
acceptance criteria, not footnotes.

## Authority boundary

No live strategy, cap, product, credential policy, or order authority changed.
No order was placed.
