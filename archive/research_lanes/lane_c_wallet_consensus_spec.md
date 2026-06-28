# Lane C Spec — Wallet Consensus Shadowing

## Blunt verdict
Pursue **with constraints**.

This is **not** a clean extension of the current Coinbase spot-only lane.
A real wallet-following edge naturally lives in **on-chain / DEX-first universes** where wallets actually express the behavior you want to study.
If we force it into Coinbase spot only, the lane becomes a **filtered secondary shadow** of on-chain accumulation, not true copy-trading.

That filtered version is still worth testing because it is more executable and much more honest than blind wallet mirroring.

## Chosen lane name
**Lane C: Wallet Consensus Shadowing**

## Core idea
Do **not** copy a single "smart" wallet.
Instead:
1. score wallets on realized behavior,
2. require multi-wallet agreement,
3. only act on assets that are liquid and tradable in the target venue,
4. assume delayed entry after the wallet activity is already visible,
5. size small and treat this as a cross-sectional event signal, not guru worship.

## Target universe
### Primary research universe
- **Base + Ethereum + Solana wallet activity** for discovery
- Focus on spot token accumulation events only

### First executable trading universe
- **Coinbase USD spot products only**
- Prefer majors / liquid large caps that can be traded without fantasy slippage
- Initial shortlist: BTC, ETH, SOL, XRP, ADA, LINK, AVAX, DOGE, LTC, BCH

### Honest implication
If wallet activity is mostly in microcaps / memecoins / newly launched tokens, that does **not** transfer cleanly to Coinbase spot.
In that case the lane should be marked **interesting but not executable in current venue**.

## Candidate data sources
### Useful / realistic ground-truth sources
1. **Dune API / DuneSQL**
   - Best realistic source for historical decoded DEX trades, wallet-level aggregation, labels, and multi-chain SQL workflows
   - Good for building wallet scoring and consensus features
   - Needs account/API key and query work, but this is a real path

2. **Etherscan / Basescan-style APIs**
   - Usable for address-level token transfer history, balances, and some labels/name tags
   - Good supporting source for wallet inspection and enrichment
   - Weak as sole ground truth for swap reconstruction because transfers are not enough to infer all trade intent honestly

3. **GeckoTerminal / CoinGecko Onchain endpoints**
   - Useful for token/pool liquidity, pair metadata, and on-chain market context
   - Better for liquidity filtering and price sanity checks than for full wallet trade history

4. **Coinbase public product endpoints**
   - Use only to define the executable spot universe and later price/backtest execution layer

### Useful but not sufficient as backtest truth
- Arkham / Nansen / Dexscreener dashboards
- Whale alert / social feeds / wallet leaderboards

These are fine for **idea generation** or wallet discovery, but not strong enough as the sole historical truth layer for honest research.

## What must be true for an honest backtest
We need, at minimum:
1. **Historical wallet activity timestamps** at the transaction / swap level
2. **Token identifiers and chain mapping**
3. **Wallet-level realized behavior** over time (not just current PnL screenshots)
4. **Historical price + liquidity** for the traded token around signal time
5. **A tradable mapping** from token -> Coinbase spot asset when testing the Coinbase-executable version
6. **Visibility delay assumptions**
   - e.g. act 1h, 4h, or next bar after a wallet cluster becomes observable
7. **Execution assumptions**
   - fees, spread, slippage, max position, minimum liquidity
8. **Survivorship controls**
   - wallets must be selected using only information available before each test window

Without these, the whole thing turns into screenshot cosplay.

## Biggest traps
1. **Hidden hedges**
   - Wallet may buy spot on-chain while hedging elsewhere or OTC. The visible address is not the full book.

2. **Insider / launch access contamination**
   - A wallet that farms seed rounds, private allocations, team flow, or launchpad access is not copyable alpha.

3. **Airdrop farming noise**
   - Many active wallets optimize for points / farming, not directional edge.

4. **Latency illusion**
   - By the time a wallet buy is visible and clustered, the move may already be gone.

5. **Illiquidity / market impact**
   - Wallets can enter tiny pools that are impossible to shadow honestly, especially after discovery delay.

6. **Survivorship bias**
   - Picking wallets because they look brilliant today is the classic trap.

7. **Selection leakage**
   - Ranking wallets on full-period PnL and then backtesting earlier periods is fake.

8. **Address fragmentation**
   - Good actors often split activity across many wallets; bad actors do too.

9. **Transfer != buy**
   - Raw token transfers can be bridge moves, internal routing, LP operations, or OTC settlement.

10. **Microcap dependency**
   - If the apparent edge only exists in tiny on-chain tokens, it probably dies when restricted to Coinbase spot.

## Recommended MVP design
### Strategy form
**Wallet-scoring + consensus accumulation + liquid tradable filter + delayed entry**

### Signal unit
An asset-level event, not a wallet-level copy.

### Wallet eligibility score (rolling, train-window only)
For each wallet over a trailing lookback window (for example 60-120 days), compute:
- number of distinct buys
- median holding time
- win rate after 1d / 3d / 7d from first accumulation event
- average forward return after buy
- hit rate in liquid assets only
- max drawdown / tail loss after buys
- concentration penalty (too dependent on one token)
- microcap penalty
- freshness penalty if activity is stale

Exclude wallets with:
- too few observations
- extreme dependence on one token
- mostly illiquid / untradable tokens
- obvious farming / router / exchange / team / deployer behavior

### Asset consensus score
For each asset and bar:
- sum wallet scores for wallets with net positive accumulation in the last X hours
- require at least **N distinct eligible wallets**
- require buys from **independent wallets**, not same cluster if identifiable
- optionally require accumulation spread over multiple blocks / hours, not one burst

### Coinbase-executable filter
Only keep signals where:
- asset has a live Coinbase USD spot pair
- asset passes minimum 30d Coinbase volume threshold
- signal occurs after wallet accumulation, then execute on Coinbase bars with delay

### Entry / exit assumptions
Initial honest version:
- signal finalization window: 4h accumulation window
- enter at **next 1h or 4h bar open after signal becomes observable**
- long only
- max holding period: 3d / 7d / 14d variants
- optional exit on consensus decay or trailing stop

### Why this MVP is better than blind copy-trading
Because it avoids the dumb version:
- no single-wallet hero worship
- no pretending we can mirror memecoin fills
- no instantaneous execution fantasy
- no dependence on private wallet labels

## Backtest assumptions
- Walk-forward only
- Wallet scores frozen inside each train/test split
- Delays tested at 1h, 4h, 24h
- Friction at the Coinbase spot layer, not fantasy DEX fills
- Only tradable Coinbase assets in the executable variant
- Signals dropped if asset liquidity / listing / mapping is ambiguous
- Portfolio cap: top 3 concurrent names, equal weight
- Hard cap on per-asset turnover to avoid signal-chasing

## Reuse from current repo
The current harness is actually reusable for the **execution and validation** side:
- `research/backtest.py` — reusable with event-derived long signals
- `research/walkforward.py` — reusable for train/test wallet scoring and OOS evaluation
- `research/costs.py` — reusable for conservative Coinbase execution assumptions
- `research/types.py` — mostly reusable; may need wallet-event types later
- `research/data.py` — reusable only for Coinbase price history / execution bars, not wallet discovery
- `research/universe.py` — reusable for Coinbase tradable universe filtering

What is missing:
- wallet event ingestion
- wallet scoring
- token/chain mapping
- consensus signal construction
- honest discovery-delay handling

## First implementation steps
1. Build a **read-only data note + schema** for wallet events and consensus signals
2. Add a small wallet-lane module that expects pre-exported wallet events (CSV/JSON) rather than overbuilding a full pipeline
3. Run the first study on a **narrow executable universe**:
   - ETH, SOL, LINK, AVAX, XRP, ADA
4. Test only delayed long signals from wallet consensus events, not full copy-trading
5. Kill fast if signals disappear once delayed and filtered to Coinbase names

## Exact next implementation step
Create a minimal ingestion contract and first experiment runner that accepts a prebuilt `wallet_events.csv` and converts it into delayed long signals on Coinbase bars.

That means the very next code task should be:
- add `research/lane_c_wallet_data_contract.md`
- add a tiny `research/lane_c_wallet_shadow.py` that loads wallet event CSV, maps asset -> Coinbase symbol, builds consensus events, and reuses `walkforward.py` / `backtest.py`

## Recommendation
**PURSUE WITH CONSTRAINTS**

Green light only for this narrower claim:
> "Can delayed consensus accumulation by historically effective on-chain wallets predict forward returns in liquid Coinbase-listed spot assets?"

Do **not** green-light this stronger claim yet:
> "We can profitably copy smart wallets directly."

That stronger claim is mostly bullshit unless we move into full on-chain execution and much richer data infrastructure.
