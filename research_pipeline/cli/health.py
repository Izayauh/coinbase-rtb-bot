"""
Machine-readable health/status report for the research store.

Usage:
    python -m research_pipeline.cli.health [--db PATH] [--config PATH]
"""
from __future__ import annotations

import argparse
import json
from typing import Any, Dict

from ..config import load_config
from ..storage import ResearchStore


def health_report(store: ResearchStore) -> Dict[str, Any]:
    conn = store.conn
    counts = {t: store.count(t) for t in
              ("raw_events", "trades", "l2_updates", "quotes", "gaps",
               "labels", "features", "variant_registry", "context_events",
               "order_math", "ingestion_runs")}

    gap_kinds = {r["kind"]: r["c"] for r in conn.execute(
        "SELECT kind, COUNT(*) AS c FROM gaps GROUP BY kind")}
    label_validity = {("valid" if r["valid"] else "invalid"): r["c"] for r in conn.execute(
        "SELECT valid, COUNT(*) AS c FROM labels GROUP BY valid")}
    invalid_reasons = {r["invalid_reason"]: r["c"] for r in conn.execute(
        "SELECT invalid_reason, COUNT(*) AS c FROM labels "
        "WHERE valid=0 GROUP BY invalid_reason")}
    feature_flags = {r["flags"]: r["c"] for r in conn.execute(
        "SELECT flags, COUNT(*) AS c FROM features GROUP BY flags")}
    context_sources = {r["source_kind"]: r["c"] for r in conn.execute(
        "SELECT source_kind, COUNT(*) AS c FROM context_events GROUP BY source_kind")}
    last_run = conn.execute(
        "SELECT run_id, collector, status, raw_count, started_us, ended_us "
        "FROM ingestion_runs ORDER BY started_us DESC LIMIT 1").fetchone()

    quote_span = conn.execute(
        "SELECT MIN(recv_time_us) AS lo, MAX(recv_time_us) AS hi FROM quotes").fetchone()

    return {
        "db_path": store.db_path,
        "schema_version": conn.execute("PRAGMA user_version").fetchone()[0],
        "counts": counts,
        "gap_kinds": gap_kinds,
        "label_validity": label_validity,
        "label_invalid_reasons": invalid_reasons,
        "feature_flags": feature_flags,
        "context_sources": context_sources,
        "quote_recv_span_us": ({"lo": quote_span["lo"], "hi": quote_span["hi"],
                                "span_s": ((quote_span["hi"] - quote_span["lo"]) / 1e6)
                                if quote_span["lo"] else 0}),
        "last_run": dict(last_run) if last_run else None,
        "storage_bytes": store.storage_bytes(),
        "promotion_gate": (
            "BLOCKED until strategy trial matrix, all baselines, purged-WF folds, "
            "ESS, and operational gates pass; DSR/CSCV-PBO algorithms are implemented"
        ),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="research_pipeline health report")
    ap.add_argument("--db", default=None, help="research DB path (default from config)")
    ap.add_argument("--config", default=None, help="optional config override YAML")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    db_path = args.db or cfg["storage"]["db_path"]
    store = ResearchStore(db_path)
    try:
        print(json.dumps(health_report(store), indent=2, sort_keys=True))
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
