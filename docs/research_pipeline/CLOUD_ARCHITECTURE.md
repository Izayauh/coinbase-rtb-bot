# Cloud Research Architecture

## Decision

Use a hybrid, provider-neutral architecture:

1. An always-on Linux VM runs the public Coinbase collector, online feature
   derivation, paper strategy engine, and scheduled evaluation.
2. The VM keeps only a small hot working set locally.
3. Closed hourly partitions are exported as Zstandard-compressed Parquet,
   verified by exact row count and SHA-256, then uploaded to Google Cloud
   Storage or another S3-compatible object store.
4. DuckDB queries Parquet directly from object storage for backtests and
   research. A managed analytical database is not required at this stage.
5. The Windows PC receives reports and selected query results, not the full
   Level-2 corpus.

The live-order system remains separate and receives no authority from this
research deployment.

## Data tiers

| Tier | Contents | Retention |
|---|---|---|
| Hot VM | Open collector shard, current order book, recent quotes/trades, paper state | 24-48 hours |
| Warm object storage | Features, labels, paper decisions, evaluation reports | Indefinite |
| Cold object storage | Raw frames and normalized Level-2 replay evidence | Frozen experiment window, initially 90 days |
| Local PC | Code, manifests, summaries, selected samples | Small |

Raw WebSocket payloads and normalized Level-2 rows are separate evidence and
query layers. After remote upload and manifest verification exist, local
closed shards may be deleted as complete files. Rows are never deleted from an
active append-only shard.

## Archive contract

`python -m research_pipeline.cli.archive --output PATH` exports the current
research SQLite database into UTC-hour Parquet partitions.

Every archive includes:

- exact source and exported row counts per table;
- Zstandard compression;
- SHA-256 for every Parquet object;
- a machine-readable manifest;
- no source mutation or deletion.

`python -m research_pipeline.cli.upload_archive` uploads every Parquet object,
verifies remote size and SHA-256 metadata, publishes the manifest last, and
reads the remote manifest back before reporting success.

For Google Cloud Storage, use:

`python -m research_pipeline.cli.upload_gcs_archive --archive PATH --bucket BUCKET --prefix PREFIX --project PROJECT`

It follows the same publish-last and remote-readback contract using
Application Default Credentials or a VM service account. Gemini API keys are
not storage credentials and must not be copied into this repository.

Remote deletion of a local shard must remain blocked until:

1. every exported table has matching row counts;
2. every local Parquet file has a recorded SHA-256;
3. the remote object store confirms every uploaded object;
4. the remote manifest is read back and matches the local manifest.

## Recommended initial deployment

- One 2 GB Linux VM. The collector is light, but Level-2 replay and Parquet
  export need more headroom than a 512 MB instance.
- S3-compatible standard object storage.
- One product (`BTC-USD`) until the end-to-end storage, derivation, and paper
  loop has operated without gaps for at least seven days.
- Closed three-hour shards on a small VM. Each shard collects public data,
  exports accumulated candidate history, derives labels/features, runs
  diagnostic evaluation, exports verified
  Parquet, uploads to GCS, mirrors compact tables into stable table-first
  BigQuery query prefixes, runs the registered cross-shard derivatives-stress
  evidence evaluator, verifies candidate episodes, replay cost math, online
  order-math parity, and the explicit exit contract, uploads SHA-256-verified
  per-shard and stable-latest evidence JSON, publishes a short-lived
  research-only advisory, uploads its shard report, and only then removes the
  local closed database/archive.
- Run one shard with:

  `python -m research_pipeline.cli.run_cloud_shard --seconds 10800 --bucket BUCKET --project PROJECT`

Do not add products merely to collect more data. Add ETH-USD and SOL-USD only
after the BTC pipeline produces complete depth-feature time series and
verified paper decisions.

The VM service account has read-only BigQuery data access plus permission to
create query jobs. It has no trading credentials or order authority.

The advisory object is not an execution API. It is hash-verified, expires
quickly, and always declares `live_authority_granted: false`. Live authorization,
acceptance, caps, reconciliation, and final order submission remain on the
separate Windows runtime.
