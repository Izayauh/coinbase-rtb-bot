"""Upload a verified Parquet archive to Google Cloud Storage."""
from __future__ import annotations

import argparse
import json
import os

from ..archive import upload_archive_to_gcs


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="upload and verify a research archive in Google Cloud Storage"
    )
    parser.add_argument("--archive", required=True)
    parser.add_argument("--bucket", default=os.getenv("RESEARCH_GCS_BUCKET"))
    parser.add_argument("--prefix", required=True)
    parser.add_argument(
        "--project",
        default=os.getenv("GOOGLE_CLOUD_PROJECT"),
    )
    args = parser.parse_args(argv)
    if not args.bucket:
        parser.error("--bucket or RESEARCH_GCS_BUCKET is required")

    result = upload_archive_to_gcs(
        args.archive,
        bucket=args.bucket,
        prefix=args.prefix,
        project=args.project,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
