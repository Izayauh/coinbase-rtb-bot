"""
Bounded public-data smoke test (writes ONLY to a separate smoke DB).

It (1) collects a bounded sample of real public Coinbase data, (2) reconstructs order-book
state and computes at least one valid book-derived feature, (3) computes quoted_spread_bps
feature rows, (4) attempts executable labels at 5m/15m/1h/4h — emitting label rows only when
the horizon is actually available, (5) demonstrates gap/staleness reporting, and (6) prints a
compact health report.

Usage:
    python -m research_pipeline.cli.smoke [--seconds 25] [--db PATH] [--config PATH]

Does NOT install any scheduler or long-running collector.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any, Dict, List, Optional, Tuple

from ..config import load_config, CostModel
from ..storage import ResearchStore
from ..collectors import CoinbaseCollector
from ..book import OrderBook, replay_l2_rows
from ..features import (
    REGISTRY,
    book_feature_values,
    compute_quote_feature_series,
    register_specs_as_variants,
)
from ..labeling import build_labels
from .health import health_report


def reconstruct_book(store: ResearchStore, product_id: str) -> Tuple[OrderBook, Dict[str, Any]]:
    """Replay stored l2_updates deterministically through an OrderBook."""
    rows = list(store.conn.execute(
        "SELECT * FROM l2_updates WHERE product_id=? ORDER BY id ASC", (product_id,)))
    book, replay = replay_l2_rows(product_id, rows)
    h = book.health()
    return book, {
        "snapshot_applied": replay["snapshots_applied"] > 0,
        "snapshots_applied": replay["snapshots_applied"],
        "updates_applied": replay["updates_applied"],
        "valid": h.valid, "reason": h.reason, "best_bid": h.best_bid, "best_ask": h.best_ask,
        "spread_bps": book.spread_bps(),
        "depth_bid_10bps": book.depth_within_bps("bid", 10),
        "depth_ask_10bps": book.depth_within_bps("ask", 10),
    }


def _sample(times: List[int], k: int) -> List[int]:
    if len(times) <= k:
        return times
    step = len(times) / k
    return [times[int(i * step)] for i in range(k)]


def run_smoke(db_path: str, cfg: Dict[str, Any], seconds: float) -> Dict[str, Any]:
    store = ResearchStore(db_path)
    try:
        product = cfg["collector"]["product_ids"][0]
        register_specs_as_variants(store)

        collector = CoinbaseCollector(
            store, product_ids=cfg["collector"]["product_ids"],
            channels=cfg["collector"]["channels"],
            ws_url=cfg["collector"]["ws_url"],
            max_message_bytes=cfg["collector"]["max_message_bytes"],
            storage_warn_bytes=cfg["storage"]["storage_warn_bytes"],
            storage_block_bytes=cfg["storage"]["storage_block_bytes"],
        )
        collected = asyncio.run(collector.run(max_seconds=seconds, max_frames=100_000))

        # Book reconstruction from the public Level2 feed.
        _book, book_summary = reconstruct_book(store, product)
        if book_summary["updates_applied"] == 0:
            latest_q = store.conn.execute(
                "SELECT * FROM quotes WHERE product_id=? ORDER BY recv_time_us DESC LIMIT 1",
                (product,)).fetchone()
            top = None
            if latest_q and latest_q["best_bid"] and latest_q["best_ask"]:
                bb, ba = latest_q["best_bid"], latest_q["best_ask"]
                top = {"best_bid": bb, "best_ask": ba,
                       "spread_bps": (ba - bb) / ((bb + ba) / 2) * 10_000}
            book_summary = {
                "l2_status": "missing",
                "note": "No Level2 updates were captured; ticker top-of-book remains available",
                "top_of_book_from_ticker": top,
                "l2_replay": book_summary,
            }

        # Feature rows from real quotes and trades.
        quote_times = [r["recv_time_us"] for r in store.conn.execute(
            "SELECT recv_time_us FROM quotes WHERE product_id=? ORDER BY recv_time_us ASC",
            (product,))]
        decision_times = _sample(quote_times, 50)
        feats = compute_quote_feature_series(
            store,
            product,
            decision_times,
            cfg["freshness"]["max_quote_staleness_us"],
            max_trade_gap_us=cfg["freshness"]["max_trade_gap_us"],
        )
        if book_summary.get("valid") and _book.last_update_us is not None:
            for name, (value, flag) in book_feature_values(
                _book,
                _book.last_update_us,
                cfg["freshness"]["max_book_staleness_us"],
            ).items():
                spec = REGISTRY[name]
                store.insert_feature(
                    name,
                    spec.version,
                    product,
                    _book.last_update_us,
                    value,
                    0,
                    flag,
                    f"book:{book_summary['snapshots_applied']}:{book_summary['updates_applied']}",
                )
        valid_feats = [f for f in feats if f["value"] is not None]

        # Executable labels — emitted only when the horizon is actually available.
        cm = CostModel.from_config(cfg)
        labels = build_labels(
            store, product, decision_times, cfg["labels"]["horizons"], cm,
            cfg["freshness"]["max_quote_staleness_us"],
            sensitivities=[1.0, 2.0], replay_version="smoke_v1")

        report = health_report(store)
        return {
            "ok": True,
            "product": product,
            "collected": collected,
            "book_reconstruction": book_summary,
            "feature_rows": len(feats),
            "feature_rows_valid": len(valid_feats),
            "label_rows": len(labels),
            "label_note": ("labels emit only when t+horizon is available; a short smoke "
                           "window typically yields 0 (shortest horizon is 5m)"),
            "health": report,
        }
    finally:
        store.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="research_pipeline smoke test")
    ap.add_argument("--seconds", type=float, default=25.0)
    ap.add_argument("--db", default="research_pipeline_data/smoke.db")
    ap.add_argument("--config", default=None)
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    result = run_smoke(args.db, cfg, args.seconds)
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
