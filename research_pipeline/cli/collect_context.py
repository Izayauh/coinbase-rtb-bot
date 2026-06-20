"""Collect authoritative context annotations into the research store.

Wired sources:
  * Federal Reserve monetary-policy RSS
  * BLS headline CPI-U API
  * SEC EDGAR Coinbase Global submissions
  * Coinbase official status incidents
  * CoinDesk attributed publication RSS

Context is annotation-only and cannot create orders.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json

from ..config import load_config
from ..context import (
    AccessGap,
    BLSCPIAdapter,
    CFTCBitcoinCOTAdapter,
    CoinbaseIntxDerivativesAdapter,
    CoinbaseStatusRSSAdapter,
    CoinDeskRSSAdapter,
    EdgarCoinbaseAdapter,
    FederalReserveRSSAdapter,
    NotWiredAdapter,
)
from ..storage import ResearchStore


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="collect authoritative context annotations")
    ap.add_argument("--db", default=None)
    ap.add_argument("--config", default=None)
    ap.add_argument("--lookback-days", type=int, default=120)
    ap.add_argument(
        "--sec-user-agent",
        default="Isaiah crypto research isaiah@example.invalid",
    )
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    store = ResearchStore(args.db or cfg["storage"]["db_path"])
    now = datetime.now(timezone.utc)
    since_us = int((now - timedelta(days=args.lookback_days)).timestamp() * 1_000_000)
    # Network retrieval occurs after this timestamp is captured. A small future
    # bound includes the actual retrieval time without changing event vintages.
    until_us = int((now + timedelta(minutes=5)).timestamp() * 1_000_000)
    adapters = [
        FederalReserveRSSAdapter(),
        BLSCPIAdapter(),
        EdgarCoinbaseAdapter(user_agent=args.sec_user_agent),
        CFTCBitcoinCOTAdapter(),
        CoinbaseIntxDerivativesAdapter(),
        CoinbaseStatusRSSAdapter(),
        CoinDeskRSSAdapter(),
        NotWiredAdapter(
            "onchain", "onchain",
            "node/indexer source not selected",
        ),
    ]
    result = {"inserted": {}, "duplicates": {}, "access_gaps": {}}
    try:
        for adapter in adapters:
            try:
                records = adapter.fetch(since_us, until_us)
            except AccessGap as exc:
                result["access_gaps"][adapter.source_id] = str(exc)
                continue
            inserted = duplicates = 0
            for record in records:
                if store.insert_context(record.to_row()):
                    inserted += 1
                else:
                    duplicates += 1
            result["inserted"][adapter.source_id] = inserted
            result["duplicates"][adapter.source_id] = duplicates
        result["context_rows"] = store.count("context_events")
        print(json.dumps(result, indent=2, sort_keys=True))
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
