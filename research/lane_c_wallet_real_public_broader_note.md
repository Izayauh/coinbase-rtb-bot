# Lane C broader real-public wallet dataset note

## What this is
A slightly broader real-public Lane C dataset for the wallet-shadow runner:

- file: `research/examples/lane_c_wallet_events_real_public_eth_broader.csv`
- scope: 6 ETH accumulation-style wallet events across 2 public wallet clusters
- chain: Ethereum
- asset coverage: ETH -> `ETH-USD`
- source class: **real public wallet activity, still hand-curated and still weak as research truth**

This expands the first tiny 4-row ETH dataset with one extra public cluster so the runner sees more than a single anecdote.

## Public source paths used
### Cluster A — Aug 2025 institutional-style ETH accumulation
Discovery source:
- Lookonchain X post `1952906622692630557`
- described 4 newly created wallets accumulating 101,131 ETH from FalconX, Galaxy Digital OTC, and BitGo during 2025-08-04 through 2025-08-06

Verification path:
- direct Etherscan wallet pages / internal-transactions page
- same path already documented in `research/lane_c_wallet_real_public_note.md`

### Cluster B — Mar 2026 ETH withdrawals into 2 fresh wallets
Discovery source:
- OnchainLens X post `2036601365611553167`
- text surfaced by Brave search: 2 newly created wallets withdrew 67,111 ETH worth $144.73M from Kraken, likely linked to Bitmine
- addresses shown in the public snippet:
  - `0xD7711559879aB70E0D0727cef9d7C7D1dBBcA7Bb`
  - `0x7c485F1659e068928E78a87f0DF80f8F6D907134`

Verification path:
- public Ethplorer address-transactions endpoint for each wallet, which exposed the large incoming ETH transfers and tx hashes on 2026-03-24:
  - `0x3d8992eda94382cae48a7c9251c3cf6c37c1c99012c05a235efb9734b2b35bf5`
  - `0xdc7b7275977a246866475396f378479c78bad75e36395ae205bf350d2e77511d`
- direct public Etherscan wallet pages remain linkable from the CSV rows

## Important honesty note
This is still **not** a clean historical DEX swap dataset.

These rows are real public on-chain wallet funding / withdrawal events into visible addresses, but they should be interpreted as:
- public accumulation-style wallet events
- likely exchange / custody / treasury routing into fresh wallets
- **not proven open-market buys executed on-chain at that exact moment**

So the dataset is usable for narrow shadow testing, but still weak for any strong alpha claim.

## Why this is still limited
- still only one executable asset (`ETH-USD`)
- still hand-curated from public monitoring posts, not a systematic decoded-swap history
- wallet selection is discovery-driven, so survivorship / narrative bias is still very real
- the second cluster uses an intermediate public data API (Ethplorer) for tx extraction because explorer HTML is awkward to scrape directly

## Field choices that are approximations
- `action=accumulate` remains an interpretation
- `venue` is `custody_transfer` or `exchange_withdrawal`, not a DEX venue
- `liquidity_usd=1000000000` is a hand-set ETH liquidity proxy so the current runner accepts the rows
- `wallet_score_hint=1.0` remains intentionally neutral

## Bottom line
This is an honest broadening of the first real-public test, but it is still **evidence of public accumulation narratives**, not evidence of a robust copyable smart-wallet edge.
