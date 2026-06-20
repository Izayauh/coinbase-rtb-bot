# Lane B Investigation — Coinbase Perpetual Carry / Funding

> **Date**: 2026-04-05  
> **Project**: `C:\Users\isaia\Projects\ai-agents\crypto_trading`  
> **Status**: **INVESTIGATION STARTED** — technically researchable, operationally gated

---

## 1. Executive Read

Lane B is **not imaginary** and **not blocked on market data existence**.

Coinbase exposes a real perpetual-futures surface that we can already probe publicly:
- public perp product metadata exists (e.g. `BTC-PERP-INTX`, `ETH-PERP-INTX`)
- public perp candles are fetchable
- current funding rate, funding time, index price, open interest, and max leverage are visible in product metadata

But Lane B is **still gated** in two important ways:
1. **Account/onboarding access is unverified** in this environment
2. **Historical funding data is not yet proven available through the current repo path / SDK usage**

So the honest status is:
- **Research reconnaissance can start now**
- **A credible carry backtest is not ready yet**
- **Any actual execution or portfolio-aware validation is blocked until derivatives eligibility/onboarding is verified**

---

## 2. Why We Are Here

Lane A failed cleanly and decisively.

Per the existing repo docs:
- Lane A pilot failed on all 5 tested spot assets at honest friction
- The next serious path named in the docs is **Lane B: derivatives carry / funding**
- The same docs warned Lane B would require **derivatives access investigation first**

That warning was correct.

---

## 3. Repo / SDK Reality Check

### Current repo posture
The repo runtime is still fundamentally spot/live-paper oriented:
- `config.yaml` is still single-symbol spot (`BTC-USD`)
- runtime strategy logic is still tied to the archived breakout state machine
- the useful reusable piece is the **separate `research/` harness**, not the runtime bot

### Installed Coinbase SDK capabilities
The installed Coinbase Python package already contains distinct derivatives surfaces:

#### Perpetuals (`/intx/...`)
Found in `venv/Lib/site-packages/coinbase/rest/perpetuals.py`
- `get_perps_portfolio_summary`
- `list_perps_positions`
- `get_perps_position`
- `get_perps_portfolio_balances`
- `allocate_portfolio`
- `opt_in_or_out_multi_asset_collateral`

#### CFM futures (`/cfm/...`)
Found in `venv/Lib/site-packages/coinbase/rest/futures.py`
- `get_futures_balance_summary`
- `list_futures_positions`
- `get_futures_position`
- `get_current_margin_window`
- `set_intraday_margin_setting`
- sweep-related endpoints

Interpretation:
- Coinbase’s API surface is broad enough to support a real derivatives-aware path
- Lane B would be a **new research/product surface**, not a tiny extension of the spot bot

---

## 4. Public Market Surface We Confirmed

Using the installed Coinbase SDK without auth, we confirmed all of the following:

### Public perpetual products exist
Examples successfully resolved via public product lookup:
- `BTC-PERP-INTX`
- `ETH-PERP-INTX`
- `SOL-PERP-INTX`
- `XRP-PERP-INTX`
- `DOGE-PERP-INTX`
- `ADA-PERP-INTX`
- `LTC-PERP-INTX`
- `BCH-PERP-INTX`
- `LINK-PERP-INTX`
- `AVAX-PERP-INTX`

### Public product metadata includes current perp-specific fields
For example, on live public product responses we observed fields including:
- `contract_expiry_type = PERPETUAL`
- `funding_rate`
- `funding_time`
- `index_price`
- `open_interest`
- `max_leverage`
- `underlying_type = SPOT`
- `product_venue = INTX`

### Public candles work for perp products
Confirmed for at least:
- `BTC-PERP-INTX`
- `ETH-PERP-INTX`

So Lane B is **not blocked on basic perp OHLCV access**.

---

## 5. Exchange / Platform Constraints Confirmed

From Coinbase’s public docs, perpetual futures via Advanced Trade are subject to:
- **eligible region requirement**
- **successful onboarding in Advanced Trade UI**
- **minimum 10 USDC notional per perpetual order**
- **USDC collateral in the perpetuals portfolio**
- optional **multi-asset collateral** opt-in
- margin health / liquidation tracking as part of the operating model

The docs also explicitly point to perps-specific endpoints for:
- perp portfolio summary
- perp positions
- portfolio fund movement / collateral management

That means Lane B is fundamentally not “just another signal.”
It is a margin / collateral / liquidation-aware system.

---

## 6. What We Have NOT Yet Proven

### A. Account eligibility / onboarding
In the current environment:
- WSL process environment did **not** have `COINBASE_API_KEY` / `COINBASE_API_SECRET`
- Windows **user** environment also did **not** show those variables

So we have **not yet verified**:
- whether this Coinbase account is API-authenticated from the project environment
- whether the account is onboarded for perpetuals
- whether a perpetuals portfolio exists and is reachable
- whether region eligibility is satisfied in practice

### B. Historical funding-rate access
We verified **current** funding-rate visibility in product metadata.
We have **not yet verified** a clean historical funding-rate endpoint/path for backtesting through the current repo tooling.

This matters because a serious Lane B carry study needs more than current funding snapshots:
- historical funding series
- perp price series
- spot price series
- likely index / mark handling
- fees, collateral, and liquidation assumptions

Without historical funding data, a “carry backtest” turns into cosplay.

### C. Exact implementable Lane B variant
“Lane B” is still too broad unless we narrow it.
Possible variants include:
1. **Funding capture / perp carry monitor**
2. **Basis spread study (spot vs perp deviation)**
3. **Cash-and-carry style simulation** (long spot / short perp)
4. **Directional derivative strategy with funding-aware filters**

Right now, the most research-honest starting point is **basis/funding observation and data collection**, not pretending we already have an executable carry strategy.

---

## 7. What Lane B Actually Requires

A credible Lane B research pass needs these data/components:

### Minimum research dataset
- perp OHLCV candles
- spot OHLCV candles for the same underlyings
- funding-rate time series
- funding timestamps / interval alignment
- product metadata (contract size, leverage caps, venue)

### Minimum simulation model
- entry/exit fee model
- funding cashflows by interval
- collateral usage assumptions
- leverage assumptions
- liquidation / margin health simplification
- basis convergence / divergence behavior

### Minimum account validation
- API auth works
- perps portfolio exists or can be queried
- product tradability for the account is known
- region/onboarding restriction is resolved

---

## 8. Best Immediate Next Move

The best next move is **not** to bolt Lane B onto the live bot.
That would be clown behavior.

The right immediate sequence is:

### Phase B0 — Access + data proof
1. Verify Coinbase API credentials from the canonical Windows environment
2. Verify whether the account can query portfolios / perps endpoints
3. Build a public Lane B probe for perp products, candles, and current funding snapshots
4. Determine whether historical funding can be sourced from Coinbase directly or whether we need a collector

### Phase B1 — Research framing
5. Pre-register a narrow Lane B hypothesis:
   - likely **spot/perp basis + funding observation**, not full cash-and-carry yet
6. Pick a small universe:
   - BTC, ETH, SOL, XRP, DOGE
7. Build a dataset schema that can hold:
   - spot candles
   - perp candles
   - funding observations
   - index price snapshots

### Phase B2 — Honest falsification
8. Run descriptive stats first:
   - basis distribution
   - funding sign persistence
   - funding-vs-future-return relationship
9. Only then build a backtest if the raw mechanism survives first contact with data

---

## 9. Initial Verdict

### What is true right now
- Lane B is **plausible enough to investigate**
- Coinbase’s public and SDK surfaces support that claim
- The existing `research/` harness can be repurposed for a derivatives-aware study

### What is not true yet
- We do **not** have verified derivatives account access
- We do **not** yet have historical funding data wired up
- We do **not** yet have a defendable Lane B backtest design

### Bottom line
Lane B has moved from **“named idea in docs”** to **“real candidate lane with confirmed public perp surface”**.

But it is still in **investigation mode**, not **strategy mode**.

---

## 10. Probe Artifact Created

Created and ran:
- `research/lane_b_probe.py`
- output: `research/results/lane_b_probe.json`

The probe confirmed 10 public perpetual products and 24 hourly public candles for each sampled contract over the last 24h, along with live fields such as funding rate, funding time, index price, and open interest.

## 11. Suggested Next Artifact

Create a dedicated Phase B0 checklist and public data collector under `research/`:
- `research/lane_b_probe.py` ✅
- `research/lane_b_data.py`
- `research/lane_b_notes.md`

The first real deliverable should be a **data/access proof**, not a trading rule.
