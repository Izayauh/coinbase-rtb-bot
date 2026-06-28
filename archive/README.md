# archive/ — superseded code (kept for history, not run)

Nothing in this folder is imported, scheduled, or executed by the live system.
It is retained so the project's earlier generations stay auditable. The live
code is `bot/` + `research_pipeline/` (see the root `README.md`). Moved here on
**2026-06-28**; full history is preserved via `git mv`.

| Path | What it was | Superseded by |
| --- | --- | --- |
| `v1_src/` | Original **v1 architecture** — first single-process design (`connectors/coinbase_ws`, `core/config`, `db/database` + `schema.sql`, `services/journal_service`, `services/md_service`). | `bot/` package |
| `research_lanes/` | Original **lane-A/B/C research framework** — spot/breakout probes, wallet-consensus shadow (lane C), `option_a_spot`, `pilot`, `multiple_testing`, `walkforward`, plus `datasets/` (BTC/ETH/SOL/XRP/DOGE 1h CSVs) and `results/`. | `research_pipeline/` |
| `scripts/ema_crossover_backtest.py` | EMA-crossover study. | `docs/strategy/ema_crossover_evaluation.md` (and now the live `shadow_strategy_runner.py`) |
| `scripts/vpmr_backtest.py`, `scripts/vpmr_diagnostic.py` | Volume-profile mean-reversion study. | `docs/strategy/vpmr_evaluation.md`, `docs/strategy/vpmr_diagnostic_results.md` |
| `scripts/signal_funnel_audit.py` | Signal-funnel audit harness. | `docs/strategy/signal_funnel_audit_results.md` |
| `scripts/diagnose_key.py` | Coinbase JWT/PEM key diagnostic. | superseded by `verify_coinbase.py` (which still hints at this file) |
| `scripts/test_jwt.py` | JWT signing smoke test. | one-off; folded into `verify_coinbase.py` |
| `scripts/inspect_journal.py` | Inspector for the old paper-mode `event_log` schema (`paper_journal.db`). | live journal uses a different schema; use `bot/` tooling |
| `data_caches/cache_BTC-USD_*.csv` | Orphan April 365d/730d 1h OHLC caches (no live code references them). | live data flows through `bot/` + `research_pipeline_data/` |

> Note: the root `backtest.py` was **not** archived — `bot/tests/test_backtest_engine.py`
> imports `BacktestEngine`/`Trade` from it, so it stays at the repo root as a
> test helper. It does not read the archived caches.

To revive any item, `git mv` it back and re-wire its imports. Reviving
`research_lanes/tests` would also need its path re-added to `pyproject.toml`
`testpaths`.
