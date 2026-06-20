"""Run the diagnostic microstructure policy tournament."""
from __future__ import annotations

import argparse
import json

from ..config import load_config
from ..governance import evaluate_policy_variants
from ..storage import ResearchStore


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="evaluate derived microstructure policies")
    ap.add_argument("--db", default=None)
    ap.add_argument("--config", default=None)
    ap.add_argument("--horizon", default="5m")
    ap.add_argument("--cscv-slices", type=int, default=8)
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    horizon_seconds = int(cfg["labels"]["horizons"][args.horizon])
    store = ResearchStore(args.db or cfg["storage"]["db_path"])
    try:
        result = evaluate_policy_variants(
            store,
            product_id=cfg["collector"]["product_ids"][0],
            horizon=args.horizon,
            horizon_us=horizon_seconds * 1_000_000,
            cscv_slices=args.cscv_slices,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
