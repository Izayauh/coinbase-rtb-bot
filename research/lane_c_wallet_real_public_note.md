# Lane C first real-public wallet dataset note

## What this is
This is the first non-demo Lane C dataset for a narrow ETH-only shadow test:

- file: `research/examples/lane_c_wallet_events_real_public_eth_aug2025.csv`
- scope: 4 ETH accumulation-style wallet events
- chain: Ethereum
- asset: ETH -> `ETH-USD`
- source class: **real public wallet activity, hand-curated**

## Public source path used
Most realistic no-big-platform path found for a first tiny test:

1. public wallet-monitoring discovery post from Lookonchain
2. direct verification on the linked Etherscan address pages / internal transaction page
3. manual curation into the Lane C CSV contract

Primary discovery post:
- Lookonchain X post `1952906622692630557`
- human-readable fetch path used during research: `https://r.jina.ai/http://x.com/lookonchain/status/1952906622692630557`

The post described 4 new wallets accumulating 101,131 ETH from FalconX, Galaxy Digital OTC, and BitGo during 2025-08-04 through 2025-08-06.

## Exact wallet pages used
- `https://etherscan.io/address/0x8c6bbdeffbe8fc7c58e920934667c5b74debdc60`
- `https://etherscan.io/address/0x86f911deb6bb8ca5c36eddf9ef86a9dc1f694446`
- `https://etherscan.io/address/0x55cf01a87ba597ffa6772a0634c30ceec7fce679`
- `https://etherscan.io/address/0xf2a030cd953b4dcad9563f5a1d58bb3342fea458#internaltx`

## Important honesty note
This is **not** a clean historical DEX swap dataset.

These rows are real public wallet inflows into specific addresses, but they should be interpreted as:
- public accumulation-style wallet events
- likely institutional / custody routing
- **not proven open-market buys executed on-chain at that exact moment**

So this dataset is useful for a first shadow-run sanity check, but it is still a weak research truth layer.

## Field choices that are approximations
- `action=accumulate` is an interpretation from the observed inflows
- `venue=custody_transfer` is used because these are visible funding transfers, not decoded Uniswap/Jupiter swaps
- `liquidity_usd=1000000000` is a hand-set ETH liquidity proxy so the current runner can accept the rows
- `wallet_score_hint=1.0` is intentionally neutral; there is no claimed historical wallet alpha model here

## Verification commands used
Windows-side attempt:
- `py -3 research\lane_c_wallet_shadow.py ...` failed because `research/types.py` shadows the Python stdlib `types` module when run as a script
- `py -3 -m research.lane_c_wallet_shadow ...` got past that path issue but then failed because the Windows Python environment did not have `requests` installed

WSL-side run that produced the saved result:
- `python3 research/lane_c_wallet_shadow.py --wallet-csv research/examples/lane_c_wallet_events_real_public_eth_aug2025.csv --output research/results/lane_c_wallet_real_public_eth_aug2025_results.json`

## Assessment
Good enough for:
- proving the runner can consume real public, non-demo events
- seeing whether delayed ETH wallet-cluster events produce anything at all on Coinbase bars

Not good enough for:
- claiming a robust smart-wallet edge
- ranking wallets honestly over time
- scaling Lane C research without a better underlying source like Dune / decoded swaps
