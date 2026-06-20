# Lane C Wallet Event Data Contract

Minimal input schema for the first wallet-consensus experiment.

## File
`wallet_events.csv`

## Required columns
- `event_ts` — UTC unix timestamp or ISO8601 timestamp
- `chain` — e.g. `ethereum`, `base`, `solana`
- `wallet` — wallet address / public key
- `token_symbol` — raw token symbol if known
- `token_address` — contract / mint address when applicable
- `action` — `buy`, `sell`, or `accumulate`
- `usd_notional` — estimated USD size at event time
- `price_usd` — estimated token price at event time
- `liquidity_usd` — estimated pool / market liquidity near event time
- `venue` — source venue / protocol, e.g. `uniswap_v3`, `jupiter`
- `tx_hash` — transaction id

## Optional enrichment columns
- `wallet_score_hint`
- `label`
- `is_router`
- `is_exchange`
- `is_contract`
- `is_funder_wallet`
- `realized_pnl_30d`
- `forward_return_1d`
- `forward_return_7d`
- `coinbase_symbol` — if pre-mapped; e.g. `ETH-USD`

## Research-side derived fields
The strategy code should derive, not require:
- eligible wallet flag
- rolling wallet score
- asset consensus score
- distinct wallet count
- delayed entry timestamp
- executable Coinbase symbol

## Hard exclusions for first pass
Drop rows when:
- `action` is not buy/accumulate
- `usd_notional <= 0`
- token mapping is ambiguous
- liquidity is missing or clearly tiny
- wallet is known exchange/router/contract

## First-pass scope
The first pass is intentionally narrow:
- long-only
- delayed entries only
- Coinbase-listed assets only
- no attempt to reconstruct partial fills or exact DEX execution quality
