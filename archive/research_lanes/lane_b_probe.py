"""
Lane B public probe for Coinbase perpetual products.

Purpose:
- prove public perpetual product metadata is reachable
- show current funding-rate / index-price snapshot fields
- prove perp candle downloads work without auth

This is deliberately public-data only.
It does NOT place orders and does NOT require API credentials.

Usage (Windows venv examples):
    .\\venv\\Scripts\\python.exe -m research.lane_b_probe
    .\\venv\\Scripts\\python.exe research\\lane_b_probe.py
"""
from __future__ import annotations

import os
import sys

# When executed directly as research/lane_b_probe.py, Python puts the
# research/ directory on sys.path first, which makes research/types.py shadow
# the stdlib types module. Fix that before importing stdlib modules that may
# transitively import `types`.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
if SCRIPT_DIR in sys.path:
    sys.path.remove(SCRIPT_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import json
import time
from pathlib import Path
from typing import Iterable

from coinbase.rest import RESTClient

BASES = ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "LTC", "BCH", "LINK", "AVAX"]
OUT_DIR = Path(SCRIPT_DIR) / "results"
OUT_PATH = OUT_DIR / "lane_b_probe.json"


def iter_known_perps(client: RESTClient, bases: Iterable[str]):
    for base in bases:
        product_id = f"{base}-PERP-INTX"
        try:
            product = client.get_public_product(product_id)
        except Exception:
            continue
        yield product


def main() -> int:
    client = RESTClient()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    now = int(time.time())
    start = now - 24 * 3600

    rows = []
    for product in iter_known_perps(client, BASES):
        details = getattr(product, "future_product_details", None) or {}
        perp = details.get("perpetual_details", {}) if isinstance(details, dict) else {}
        product_id = getattr(product, "product_id", "")

        candle_count = 0
        sample_candle = None
        try:
            candles_resp = client.get_public_candles(
                product_id=product_id,
                start=str(start),
                end=str(now),
                granularity="ONE_HOUR",
            )
            candles = getattr(candles_resp, "candles", None) or []
            candle_count = len(candles)
            if candles:
                c = candles[0]
                sample_candle = {
                    "start": getattr(c, "start", None),
                    "open": getattr(c, "open", None),
                    "high": getattr(c, "high", None),
                    "low": getattr(c, "low", None),
                    "close": getattr(c, "close", None),
                    "volume": getattr(c, "volume", None),
                }
        except Exception as exc:
            sample_candle = {"error": f"{type(exc).__name__}: {exc}"}

        rows.append(
            {
                "product_id": product_id,
                "display_name": getattr(product, "display_name", None),
                "product_type": getattr(product, "product_type", None),
                "product_venue": getattr(product, "product_venue", None),
                "price": getattr(product, "price", None),
                "mid_market_price": getattr(product, "mid_market_price", None),
                "contract_expiry_type": details.get("contract_expiry_type") if isinstance(details, dict) else None,
                "funding_interval": details.get("funding_interval") if isinstance(details, dict) else None,
                "index_price": details.get("index_price") if isinstance(details, dict) else None,
                "open_interest": details.get("open_interest") if isinstance(details, dict) else None,
                "funding_rate": details.get("funding_rate") if isinstance(details, dict) else None,
                "funding_time": details.get("funding_time") if isinstance(details, dict) else None,
                "max_leverage": perp.get("max_leverage") if isinstance(perp, dict) else None,
                "underlying_type": perp.get("underlying_type") if isinstance(perp, dict) else None,
                "candle_count_last_24h": candle_count,
                "sample_candle": sample_candle,
            }
        )

    payload = {
        "timestamp": now,
        "count": len(rows),
        "products": rows,
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(f"Lane B probe wrote {OUT_PATH}")
    print(f"Products confirmed: {len(rows)}")
    for row in rows:
        print(
            f"- {row['product_id']}: funding={row['funding_rate']} "
            f"index={row['index_price']} candles24h={row['candle_count_last_24h']}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
