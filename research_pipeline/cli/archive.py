"""Export a research SQLite database to verified compressed Parquet."""
from __future__ import annotations

import argparse
import json

from ..archive import export_sqlite_to_parquet
from ..config import load_config


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="export research data to verified hourly Parquet partitions"
    )
    parser.add_argument("--db", default=None)
    parser.add_argument("--config", default=None)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--tables",
        default=None,
        help="comma-separated table names; default exports every research table",
    )
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    tables = (
        [item.strip() for item in args.tables.split(",") if item.strip()]
        if args.tables else None
    )
    manifest = export_sqlite_to_parquet(
        args.db or cfg["storage"]["db_path"],
        args.output,
        tables=tables,
        overwrite=args.overwrite,
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
