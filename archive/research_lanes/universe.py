"""
research/universe.py — Trading universe definition for Lane A.

Selects 15-30 Coinbase spot pairs by 24h volume.
Freezes the list for reproducibility.
"""
import json
import os
import time
from typing import List, Tuple

import requests

from .data import DATASETS_DIR

UNIVERSE_PATH = os.path.join(DATASETS_DIR, "universe.json")


def fetch_top_spot_pairs(
    min_count: int = 15,
    max_count: int = 30,
    quote: str = "USD",
) -> List[Tuple[str, float]]:
    """
    Fetch Coinbase spot products sorted by 24h USD volume (descending).

    Returns list of (symbol, volume_usd) tuples.
    """
    url = "https://api.exchange.coinbase.com/products"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        products = resp.json()
    except Exception as e:
        print(f"  WARNING: Cannot fetch products: {e}")
        return []

    # Filter to USD-quoted, non-stablecoin, traded products
    stablecoins = {"USDT", "USDC", "DAI", "BUSD", "TUSD", "USDP", "GUSD", "PYUSD"}
    candidates = []
    for p in products:
        pid = p.get("id", "")
        if not pid.endswith(f"-{quote}"):
            continue
        base = p.get("base_currency", "")
        if base in stablecoins:
            continue
        if p.get("trading_disabled", False):
            continue
        if p.get("status", "") != "online":
            continue

        # Get 24h stats for volume
        try:
            stats_url = f"https://api.exchange.coinbase.com/products/{pid}/stats"
            sr = requests.get(stats_url, timeout=10)
            sr.raise_for_status()
            stats = sr.json()
            vol_usd = float(stats.get("volume", 0)) * float(stats.get("last", 0))
            candidates.append((pid, vol_usd))
            time.sleep(0.15)  # rate limit
        except Exception:
            continue

    # Sort by volume descending
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[:max_count]


def get_pilot_universe(n: int = 5) -> List[str]:
    """
    Return the top N symbols for the pilot experiment.
    Uses cached universe if available, otherwise fetches live.
    """
    universe = load_universe()
    if universe:
        return [s for s, _ in universe[:n]]

    print("  No cached universe. Fetching from Coinbase...")
    universe = fetch_top_spot_pairs()
    if universe:
        save_universe(universe)
    return [s for s, _ in universe[:n]]


def get_full_universe() -> List[str]:
    """Return all symbols in the frozen universe."""
    universe = load_universe()
    if not universe:
        print("  No cached universe. Run freeze_universe() first.")
        return []
    return [s for s, _ in universe]


def freeze_universe(max_count: int = 30):
    """Fetch and freeze the universe for this experiment run."""
    print("  Freezing universe (top Coinbase spot pairs by 24h volume)...")
    universe = fetch_top_spot_pairs(max_count=max_count)
    save_universe(universe)
    print(f"  Frozen {len(universe)} pairs.")
    for i, (sym, vol) in enumerate(universe, 1):
        print(f"    {i:2d}. {sym:<12} vol=${vol:>15,.0f}")
    return universe


def save_universe(universe: List[Tuple[str, float]]):
    """Save universe to JSON."""
    os.makedirs(DATASETS_DIR, exist_ok=True)
    data = {
        "frozen_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "pairs": [{"symbol": s, "volume_usd_24h": v} for s, v in universe],
    }
    with open(UNIVERSE_PATH, "w") as f:
        json.dump(data, f, indent=2)


def load_universe() -> List[Tuple[str, float]]:
    """Load universe from JSON. Returns empty list if not found."""
    if not os.path.isfile(UNIVERSE_PATH):
        return []
    with open(UNIVERSE_PATH, "r") as f:
        data = json.load(f)
    return [(p["symbol"], p["volume_usd_24h"]) for p in data.get("pairs", [])]
