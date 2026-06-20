# Research Pipeline — Runbook

Read-only / shadow-only Coinbase microstructure research spine. **No order authority.** Running
anything here cannot touch the live bot, its journals, its config, or place an order.

All commands run from the repo root `C:\Users\isaia\Projects\ai-agents\crypto_trading` using the
existing venv interpreter. (PowerShell shown; the `python` calls are identical under bash.)

```powershell
$py = ".\venv\Scripts\python.exe"
```

## 1. Run the test suite

```powershell
& $py -m pytest research_pipeline/tests -q          # research spine only
& $py -m pytest -q                                  # everything: bot + research + spine
```

## 2. Bounded public-data smoke test (writes only to a separate smoke DB)

```powershell
& $py -m research_pipeline.cli.smoke --seconds 25
```

Collects real public Coinbase data (`market_trades`, `level2`, `ticker`, `heartbeats`), reconstructs
the full book, computes microstructure feature rows, and attempts executable labels (emitted
only when the horizon is available — a short window yields 0, by design), and prints a compact JSON
health report. Writes to `research_pipeline_data/smoke.db` only.

## 3. Bounded collector (writes to the main research DB)

```powershell
& $py -m research_pipeline.cli.collect --seconds 60
```

Collects for a bounded window into `research_pipeline_data/research.db`. It is **bounded** and exits.
An OS lock prevents duplicate collectors from writing to the same database.

## 4. Collect authoritative context

```powershell
& $py -m research_pipeline.cli.collect_context --lookback-days 180
```

Wired primary sources: Federal Reserve monetary-policy RSS, BLS CPI-U, SEC EDGAR Coinbase filings,
and CFTC Bitcoin/Micro Bitcoin positioning.

## 5. Derive features and labels

```powershell
& $py -m research_pipeline.cli.derive --step-seconds 60 --max-points 50000
```

Repeated runs are idempotent.

## 6. Run the diagnostic policy tournament

```powershell
& $py -m research_pipeline.cli.evaluate --horizon 5m --cscv-slices 8
```

Reports expectancy, trade rate, ESS, DSR, and CSCV/PBO for seven fixed policies. It cannot promote
or execute a strategy.

## 7. Health / status (machine-readable)

```powershell
& $py -m research_pipeline.cli.health --db research_pipeline_data/research.db
```

Emits JSON: row counts, gap kinds, label validity + invalid-reason breakdown, feature flags, quote
time span, last ingestion run, storage bytes, and `promotion_gate` (always `BLOCKED` here).

## 8. Configuration

Defaults live in `research_pipeline/config/default.yaml` (cost model, freshness, horizons,
governance, storage budgets). Override with `--config path/to/override.yaml` on any command; only the
keys you set are merged over the defaults. Do **not** silently re-tune cost-model values after seeing
results (contract §15) — bump the version instead.

## 9. Databases & storage

| File | Purpose |
|---|---|
| `research_pipeline_data/research.db` | main research store (WAL) |
| `research_pipeline_data/smoke.db` | smoke-test store |
| `research_pipeline_data/.gitignore` | ignores all data artifacts (never committed) |

The store **refuses** to open `journal.db` / `live_journal.db` / `paper_journal.db`. Raw evidence is
append-only (enforced by DB triggers). Storage warns at 2 GB and blocks new raw writes at 8 GB.

## 10. Export a verified compressed archive

```powershell
& $py -m research_pipeline.cli.archive `
  --db research_pipeline_data/research.db `
  --output research_pipeline_data/archive/research-2026-06-19
```

This creates UTC-hour, Zstandard-compressed Parquet partitions plus a manifest
containing exact row counts and SHA-256 hashes. It does not delete or mutate
the SQLite source. See `CLOUD_ARCHITECTURE.md`.

Upload to an S3-compatible provider after setting standard AWS credentials plus
the provider-specific endpoint:

```powershell
$env:RESEARCH_S3_BUCKET = "crypto-research"
$env:RESEARCH_S3_ENDPOINT_URL = "https://ACCOUNT_ID.r2.cloudflarestorage.com"
& $py -m research_pipeline.cli.upload_archive `
  --archive research_pipeline_data/archive/research-2026-06-19 `
  --prefix coinbase/BTC-USD/2026-06-19
```

The uploader verifies every remote object and reads the manifest back. It does
not delete local data.

## 11. Safety reminders

- Public channels only; no Coinbase credentials are used or required.
- `level2` is public. Its initial snapshot exceeds the websocket client’s default message limit, so
  the collector uses a 16 MB limit and one transaction per frame.
- Nothing here imports `bot/` or can submit an order (proven by `tests/test_boundary.py`).
- No scheduled task, cron, or watcher is installed by any command above.

## 12. Next step toward the 90-day shadow run

Let the current bounded six-hour capture complete, then inspect the automatic derive/evaluate output.
Add archive/compaction before attempting multi-day continuous depth capture. Promotion remains
blocked until every evidence gate passes and a separate one-writer authorization is granted.
