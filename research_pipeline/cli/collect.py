"""
Bounded public-data collector.

Usage:
    python -m research_pipeline.cli.collect [--seconds 30] [--max-frames 10000]
                                            [--db PATH] [--config PATH]

Writes ONLY to the research store. Public Coinbase channels only; no credentials.
Does not install any scheduler or long-running service.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys

from ..config import load_config
from ..storage import ResearchStore
from ..collectors import CoinbaseCollector, CoinbaseIntxPoller
from ..advisory import (
    AdvisoryUploadPoller,
    CandidateAdvisoryPublisher,
    load_history,
)


@contextmanager
def _collector_lock(db_path: str):
    """One collector writer per research DB."""
    lock_path = Path(str(db_path) + ".collector.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"0\n")
        handle.flush()
    handle.seek(0)
    try:
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise RuntimeError(f"collector already owns {lock_path}") from exc
    try:
        yield
    finally:
        try:
            handle.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="research_pipeline bounded collector")
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--max-frames", type=int, default=10_000_000)
    ap.add_argument("--db", default=None)
    ap.add_argument("--config", default=None)
    ap.add_argument("--candidate-history", default=None)
    ap.add_argument("--advisory-output", default=None)
    ap.add_argument("--advisory-bucket", default=None)
    ap.add_argument("--advisory-object", default=None)
    ap.add_argument("--project", default=None)
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    db_path = args.db or cfg["storage"]["db_path"]
    try:
        with _collector_lock(db_path):
            store = ResearchStore(db_path)
            try:
                advisory = (
                    CandidateAdvisoryPublisher(
                        store,
                        history_rows=load_history(args.candidate_history),
                        output_path=args.advisory_output,
                    )
                    if args.advisory_output else None
                )
                collector = CoinbaseCollector(
                    store, product_ids=cfg["collector"]["product_ids"],
                    channels=cfg["collector"]["channels"],
                    ws_url=cfg["collector"]["ws_url"],
                    max_message_bytes=cfg["collector"]["max_message_bytes"],
                    storage_warn_bytes=cfg["storage"]["storage_warn_bytes"],
                    storage_block_bytes=cfg["storage"]["storage_block_bytes"],
                    max_book_staleness_us=cfg["freshness"][
                        "max_book_staleness_us"
                    ],
                    on_order_math=advisory.observe if advisory else None,
                )
                intx = CoinbaseIntxPoller(store)
                uploader = (
                    AdvisoryUploadPoller(
                        args.advisory_output,
                        bucket=args.advisory_bucket,
                        object_name=args.advisory_object,
                        project=args.project,
                    )
                    if (
                        args.advisory_output
                        and args.advisory_bucket
                        and args.advisory_object
                        and args.project
                    ) else None
                )

                async def run_collectors():
                    tasks = [
                        collector.run(
                            max_seconds=args.seconds,
                            max_frames=args.max_frames,
                        ),
                        intx.run(max_seconds=args.seconds),
                    ]
                    if uploader:
                        tasks.append(uploader.run(max_seconds=args.seconds))
                    results = await asyncio.gather(*tasks)
                    output = {
                        "coinbase_spot": results[0],
                        "coinbase_intx_derivatives": results[1],
                    }
                    if uploader:
                        output["candidate_advisory_upload"] = results[2]
                    return output

                totals = asyncio.run(run_collectors())
                print(json.dumps(totals, indent=2, sort_keys=True))
            finally:
                store.close()
    except RuntimeError as exc:
        print(json.dumps({"status": "ALREADY_RUNNING", "detail": str(exc)}))
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
