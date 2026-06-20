# BTC Derivatives-Stress Exhaustion Candidate v1

Status: pre-registered, shadow-only, no order authority.

- Strategy id: `btc_derivatives_stress_exhaustion`
- Version: `1.0.0`
- Product: `BTC-USD`
- Primary outcomes: executable 1-hour and 4-hour long returns
- Costs: `cost_model_v1` at 1.0x and binding 2.0x stress
- Exit contract: `derivatives_stress_exit_v1`

## Frozen mechanism

A downside spot shock must be present. Derivatives stress is measured from
prior-only rolling distributions of:

1. 15-minute open-interest change;
2. official realized funding;
3. Coinbase INTX mark/spot basis.

Spot-book exhaustion/recovery is measured by:

1. bid replenishment exceeding one and ask replenishment;
2. positive 60-second OFI;
3. positive microprice displacement;
4. improving 10-bps depth imbalance versus five minutes earlier.

Frozen tails:

- spot-return z-score <= -1.5;
- open-interest-change z-score <= -1.5;
- funding z-score <= -1.0 and funding negative;
- basis z-score <= -1.0 and basis negative.

Minute-derived statistics need 360 prior observations. Funding needs 24 unique
prior hourly observations. Current observations are never included in their
own z-score.

## Fixed variants

- `stress_only_v1`: spot shock plus at least two derivatives-stress flags.
- `book_only_exhaustion_v1`: spot shock plus at least three book flags.
- `combined_balanced_v1`: spot shock, two derivatives flags, three book flags.
- `combined_strict_v1`: spot shock, all three derivatives flags, all four book flags.

Only the combined variants may promote. Signals are episode starts, not every
minute a condition remains true, with a four-hour cooldown.

## Frozen baselines and cross-shard labels

The evaluation opportunity set is the union of candidate episodes and the
pre-registered 15-minute downside-volatility baseline. Every fixed variant is
compared with:

- no trade;
- buy every registered opportunity;
- price-volatility-only episode starts;
- the archived BTC breakout OOS expectancy from
  `research/results/pilot_results.json`.

Executable 1h and 4h labels are computed across shard boundaries from the
latest non-crossed Coinbase quote at or before each target, with the same
two-second staleness limit and first-order fee/slippage/adverse-selection math
used by the replay labeler. Open interest and funding are joined only when
their recorded availability time is no later than the spot decision.

## Promotion additions

Alongside every frozen research-pipeline gate:

- at least 30 completed signals;
- at least 15 distinct episodes;
- both 1h and 4h gates pass;
- the same combined variant wins both horizons;
- no single positive event supplies more than 50% of total positive P&L;
- cloud replay/parity, freshness, outage, and verified-storage gates pass.

The same winning combined variant must also pass the frozen path-dependent exit
contract:

- stop distance: one-half of the absolute 15-minute spot decline, clamped to
  0.5%-2.0%;
- target: 1.5R;
- time stop: four hours;
- stop/target resolution: first eligible Coinbase bid hit, otherwise the
  four-hour bid;
- both 1x and binding 2x cost cases.

The evaluator emits only `INSUFFICIENT_EVIDENCE`, `DEMOTED`, or
`EVIDENCE_PASSED`. It cannot create authorization or alter the live strategy.

## Online shadow path

The collector computes the same one-minute order-math record online that the
closed-shard replay computes offline. Runtime verification requires exact
parity between those two paths.

Each completed minute may update a short-lived, hash-verified advisory at:

`coinbase/BTC-USD/advisory/btc_derivatives_stress_exhaustion/1.0.0/latest.json`

The advisory is research-only and always carries
`live_authority_granted: false`. Candidate mode disables the breakout state
machine. The local bridge may stage a matching signal only after strategy
authorization, the entry halt, and the final acceptance receipt all pass.
The bridge never submits an order.

## Continuous evaluation

Each completed cloud shard now:

1. runs a dependency-free runtime verifier covering candidate episode logic,
   exact cross-shard/replay cost parity, online order-math parity, and the exit
   contract;
2. mirrors compact Parquet tables into the BigQuery external-table prefixes;
3. evaluates all accumulated no-lookahead rows;
4. uploads a SHA-256 and size-verified per-shard evidence artifact; and
5. replaces the stable latest artifact at
   `coinbase/BTC-USD/shards/strategy_evidence/`
   `btc_derivatives_stress_exhaustion/1.0.0/latest.json`.

A non-passing evidence result is an expected successful shard outcome. It does
not fail collection and never grants live authority.
