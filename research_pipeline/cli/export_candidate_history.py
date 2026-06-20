"""Export a bounded prior-only candidate history checkpoint from BigQuery."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

from .evaluate_derivatives_stress import _bq_query, _decision_sql


HISTORY_US = 48 * 60 * 60 * 1_000_000


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default="bitwise-trader")
    parser.add_argument("--dataset", default="crypto_research")
    parser.add_argument("--location", default="us-east1")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    rows = _bq_query(
        _decision_sql(args.project, args.dataset),
        location=args.location,
    )
    if rows:
        high = max(int(row["event_time_us"]) for row in rows)
        rows = [
            row for row in rows
            if int(row["event_time_us"]) >= high - HISTORY_US
        ]
    payload = {
        "schema_version": 1,
        "generated_at": int(time.time()),
        "project": args.project,
        "dataset": args.dataset,
        "location": args.location,
        "rows": rows,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "status": "EXPORTED",
        "rows": len(rows),
        "output": str(output),
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
