"""Upload a verified Parquet archive to S3-compatible object storage."""
from __future__ import annotations

import argparse
import json
import os

from ..archive import upload_archive


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="upload and verify a research archive in object storage"
    )
    parser.add_argument("--archive", required=True)
    parser.add_argument("--bucket", default=os.getenv("RESEARCH_S3_BUCKET"))
    parser.add_argument("--prefix", required=True)
    parser.add_argument(
        "--endpoint-url",
        default=os.getenv("RESEARCH_S3_ENDPOINT_URL"),
        help="S3-compatible endpoint; omit for AWS S3",
    )
    parser.add_argument(
        "--region",
        default=os.getenv("AWS_DEFAULT_REGION") or "auto",
    )
    args = parser.parse_args(argv)
    if not args.bucket:
        parser.error("--bucket or RESEARCH_S3_BUCKET is required")

    result = upload_archive(
        args.archive,
        bucket=args.bucket,
        prefix=args.prefix,
        endpoint_url=args.endpoint_url,
        region_name=args.region,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
