"""Derive idempotent features and executable labels from collected research data.

Usage:
    python -m research_pipeline.cli.derive
        [--db PATH] [--config PATH] [--step-seconds 60] [--max-points 20000]

This command reads only the research database. It never imports bot/ and has no
brokerage/order path.
"""
from __future__ import annotations

import argparse
import json
import math

from ..config import CostModel, load_config
from ..features import (
    REGISTRY,
    compute_order_math_series,
    compute_quote_feature_series,
    register_specs_as_variants,
)
from ..labeling import build_labels
from ..storage import ResearchStore
from .health import health_report


def _decision_times(lo: int, hi: int, step_us: int, max_points: int) -> list[int]:
    if lo is None or hi is None or hi < lo or step_us <= 0:
        return []
    first = math.ceil(lo / step_us) * step_us
    points = list(range(first, hi + 1, step_us))
    if max_points > 0 and len(points) > max_points:
        points = points[-max_points:]
    return points


def derive(
    store: ResearchStore,
    cfg: dict,
    *,
    step_seconds: int = 60,
    max_points: int = 20_000,
) -> dict:
    product = cfg["collector"]["product_ids"][0]
    span = store.conn.execute(
        "SELECT MIN(recv_time_us) AS lo, MAX(recv_time_us) AS hi "
        "FROM quotes WHERE product_id=?",
        (product,),
    ).fetchone()
    times = _decision_times(
        span["lo"],
        span["hi"],
        int(step_seconds * 1_000_000),
        max_points,
    )
    register_specs_as_variants(store)
    before_features = store.count("features")
    before_labels = store.count("labels")

    features = compute_quote_feature_series(
        store,
        product,
        times,
        cfg["freshness"]["max_quote_staleness_us"],
        max_trade_gap_us=cfg["freshness"]["max_trade_gap_us"],
        persist=True,
    )
    before_order_math = store.count("order_math")
    order_math = compute_order_math_series(
        store,
        product,
        times,
        cfg["freshness"]["max_book_staleness_us"],
        persist=True,
    )
    replay_counts = store.conn.execute(
        "SELECT "
        "COUNT(DISTINCT CASE WHEN update_kind='snapshot' THEN raw_id END) AS snapshots, "
        "SUM(CASE WHEN update_kind='update' THEN 1 ELSE 0 END) AS updates "
        "FROM l2_updates WHERE product_id=?",
        (product,),
    ).fetchone()
    replay = {
        "snapshots_applied": int(replay_counts["snapshots"] or 0),
        "updates_applied": int(replay_counts["updates"] or 0),
    }
    book_features = []
    latest_order_math = next(
        (record for record in reversed(order_math) if record["flags"] == "ok"),
        None,
    )
    if latest_order_math is not None:
        terminal_values = {
            "depth_imbalance_10bps": latest_order_math["depth_imbalance_10bps"],
            "depth_imbalance_25bps": latest_order_math["depth_imbalance_25bps"],
            "multilevel_imbalance": (
                latest_order_math["multilevel_depth_imbalance"]
            ),
        }
        for name, value in terminal_values.items():
            spec = REGISTRY[name]
            inserted = store.insert_feature(
                name,
                spec.version,
                product,
                latest_order_math["event_time_us"],
                value,
                0,
                "ok",
                f"book:{replay['snapshots_applied']}:{replay['updates_applied']}",
            )
            book_features.append({
                "name": name,
                "value": value,
                "flag": "ok",
                "inserted": bool(inserted),
            })
    labels = build_labels(
        store,
        product,
        times,
        cfg["labels"]["horizons"],
        CostModel.from_config(cfg),
        cfg["freshness"]["max_quote_staleness_us"],
        sensitivities=cfg["cost_model"]["sensitivity_sweep"],
        replay_version="replay_v1",
        persist=True,
    )
    return {
        "product": product,
        "decision_points": len(times),
        "feature_records_computed": len(features),
        "feature_rows_inserted": store.count("features") - before_features,
        "order_math_records_computed": len(order_math),
        "order_math_rows_inserted": store.count("order_math") - before_order_math,
        "book_replay": replay,
        "book_features": book_features,
        "label_records_computed": len(labels),
        "label_rows_inserted": store.count("labels") - before_labels,
        "health": health_report(store),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="derive research features and labels")
    ap.add_argument("--db", default=None)
    ap.add_argument("--config", default=None)
    ap.add_argument("--step-seconds", type=int, default=60)
    ap.add_argument("--max-points", type=int, default=20_000)
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    store = ResearchStore(args.db or cfg["storage"]["db_path"])
    try:
        result = derive(
            store,
            cfg,
            step_seconds=args.step_seconds,
            max_points=args.max_points,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
