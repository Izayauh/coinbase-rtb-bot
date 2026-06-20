"""
research/data.py — Coinbase candle downloader + local cache + manifest.

Downloads OHLCV from the public Coinbase Advanced Trade API.
Handles pagination (max 350 candles/request).
Caches to CSV in research/datasets/.
Maintains a manifest.json for coverage tracking.

No dependency on bot/.
"""
import csv
import json
import os
import time
from datetime import datetime, timezone
from typing import List, Optional, Tuple

import requests

from .types import Bar

# -----------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------
BASE_URL = "https://api.exchange.coinbase.com"
MAX_CANDLES_PER_REQUEST = 300   # conservative (API says 350 max)
DATASETS_DIR = os.path.join(os.path.dirname(__file__), "datasets")
MANIFEST_PATH = os.path.join(DATASETS_DIR, "manifest.json")

GRANULARITY_MAP = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}


# -----------------------------------------------------------------------
# Downloader
# -----------------------------------------------------------------------
def download_candles(
    symbol: str,
    timeframe: str,
    start_ts: int,
    end_ts: int,
    rate_limit_pause: float = 0.35,
) -> List[Bar]:
    """
    Download OHLCV candles from Coinbase public API with pagination.

    Args:
        symbol: e.g. "BTC-USD"
        timeframe: one of GRANULARITY_MAP keys
        start_ts: unix timestamp for start
        end_ts: unix timestamp for end
        rate_limit_pause: seconds between requests

    Returns:
        List of Bar sorted by timestamp ascending.
    """
    granularity = GRANULARITY_MAP.get(timeframe)
    if granularity is None:
        raise ValueError(f"Unknown timeframe: {timeframe}. Use: {list(GRANULARITY_MAP)}")

    all_bars: List[Bar] = []
    cursor = start_ts
    chunk_size = MAX_CANDLES_PER_REQUEST * granularity
    request_count = 0

    print(f"  Downloading {symbol} {timeframe} from "
          f"{_ts_str(start_ts)} to {_ts_str(end_ts)} ...")

    while cursor < end_ts:
        chunk_end = min(cursor + chunk_size, end_ts)
        url = f"{BASE_URL}/products/{symbol}/candles"
        params = {
            "start": datetime.fromtimestamp(cursor, tz=timezone.utc).isoformat(),
            "end": datetime.fromtimestamp(chunk_end, tz=timezone.utc).isoformat(),
            "granularity": granularity,
        }

        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            candles = resp.json()
            request_count += 1
        except requests.exceptions.HTTPError as e:
            if resp.status_code == 404:
                print(f"    WARNING: {symbol} not found (404), skipping")
                return []
            print(f"    WARNING: HTTP {resp.status_code} for {symbol}: {e}")
            cursor = chunk_end
            time.sleep(1)
            continue
        except Exception as e:
            print(f"    WARNING: Request error for {symbol}: {e}")
            cursor = chunk_end
            time.sleep(1)
            continue

        for c in candles:
            try:
                # Coinbase format: [timestamp, low, high, open, close, volume]
                all_bars.append(Bar(
                    symbol=symbol,
                    timeframe=timeframe,
                    ts=int(c[0]),
                    open=float(c[3]),
                    high=float(c[2]),
                    low=float(c[1]),
                    close=float(c[4]),
                    volume=float(c[5]),
                ))
            except (IndexError, ValueError, TypeError):
                continue

        cursor = chunk_end
        if request_count % 5 == 0:
            time.sleep(rate_limit_pause)

    # Deduplicate and sort
    seen = {}
    for b in all_bars:
        seen[b.ts] = b
    result = [seen[k] for k in sorted(seen)]

    print(f"    Fetched {len(result)} bars ({request_count} requests)")
    return result


# -----------------------------------------------------------------------
# Cache
# -----------------------------------------------------------------------
def _cache_path(symbol: str, timeframe: str) -> str:
    """Return canonical cache file path."""
    safe_sym = symbol.replace("/", "_").replace("-", "_")
    return os.path.join(DATASETS_DIR, f"{safe_sym}_{timeframe}.csv")


def save_bars(bars: List[Bar], symbol: str, timeframe: str) -> str:
    """Save bars to CSV cache. Returns the file path."""
    os.makedirs(DATASETS_DIR, exist_ok=True)
    path = _cache_path(symbol, timeframe)

    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "timeframe", "ts", "open", "high", "low", "close", "volume"])
        for b in bars:
            w.writerow([b.symbol, b.timeframe, b.ts, b.open, b.high, b.low, b.close, b.volume])

    print(f"    Saved {len(bars)} bars to {path}")
    return path


def load_bars(symbol: str, timeframe: str) -> Optional[List[Bar]]:
    """Load bars from CSV cache. Returns None if file doesn't exist."""
    path = _cache_path(symbol, timeframe)
    if not os.path.isfile(path):
        return None

    bars = []
    with open(path, "r") as f:
        for row in csv.DictReader(f):
            bars.append(Bar(
                symbol=row["symbol"],
                timeframe=row["timeframe"],
                ts=int(row["ts"]),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
            ))

    return bars


def ensure_data(
    symbol: str,
    timeframe: str,
    days: int,
    force_redownload: bool = False,
) -> List[Bar]:
    """
    Download data if not cached, otherwise load from cache.
    Updates the manifest after download.
    """
    if not force_redownload:
        cached = load_bars(symbol, timeframe)
        if cached:
            print(f"  Loaded {len(cached)} cached bars for {symbol} {timeframe}")
            return cached

    now = int(time.time())
    start = now - days * 86400
    bars = download_candles(symbol, timeframe, start, now)

    if bars:
        save_bars(bars, symbol, timeframe)
        _update_manifest(symbol, timeframe, bars)

    return bars


# -----------------------------------------------------------------------
# Manifest
# -----------------------------------------------------------------------
def _load_manifest() -> dict:
    if os.path.isfile(MANIFEST_PATH):
        with open(MANIFEST_PATH, "r") as f:
            return json.load(f)
    return {}


def _save_manifest(manifest: dict):
    os.makedirs(DATASETS_DIR, exist_ok=True)
    with open(MANIFEST_PATH, "w") as f:
        json.dump(manifest, f, indent=2)


def _update_manifest(symbol: str, timeframe: str, bars: List[Bar]):
    manifest = _load_manifest()
    key = f"{symbol}_{timeframe}"

    granularity = GRANULARITY_MAP.get(timeframe, 3600)
    expected_bars = (bars[-1].ts - bars[0].ts) // granularity + 1 if bars else 0
    actual_bars = len(bars)
    missing = expected_bars - actual_bars
    coverage = actual_bars / expected_bars if expected_bars > 0 else 0

    manifest[key] = {
        "symbol": symbol,
        "timeframe": timeframe,
        "start_ts": bars[0].ts if bars else 0,
        "end_ts": bars[-1].ts if bars else 0,
        "start_date": _ts_str(bars[0].ts) if bars else "",
        "end_date": _ts_str(bars[-1].ts) if bars else "",
        "bar_count": actual_bars,
        "expected_bars": expected_bars,
        "missing_bars": max(0, missing),
        "coverage_pct": round(coverage * 100, 2),
        "file": _cache_path(symbol, timeframe),
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
    }

    _save_manifest(manifest)


def print_manifest():
    """Print a summary of all cached datasets."""
    manifest = _load_manifest()
    if not manifest:
        print("  No datasets in manifest.")
        return

    print(f"\n  {'Symbol':<12} {'TF':<5} {'Bars':>7} {'Expected':>8} {'Cov%':>6} "
          f"{'Start':>12} {'End':>12}")
    print("  " + "-" * 68)
    for key in sorted(manifest):
        m = manifest[key]
        print(f"  {m['symbol']:<12} {m['timeframe']:<5} {m['bar_count']:>7} "
              f"{m['expected_bars']:>8} {m['coverage_pct']:>5.1f}% "
              f"{m.get('start_date',''):>12} {m.get('end_date',''):>12}")


# -----------------------------------------------------------------------
# Aggregation (build 4h from 1h etc.)
# -----------------------------------------------------------------------
def aggregate_bars(
    bars: List[Bar],
    target_tf: str,
    symbol: str,
) -> List[Bar]:
    """
    Aggregate bars into a larger timeframe.
    E.g., 1h → 4h. Assumes input is sorted ascending.
    """
    target_granularity = GRANULARITY_MAP.get(target_tf)
    if target_granularity is None:
        raise ValueError(f"Unknown target timeframe: {target_tf}")

    buckets: dict = {}
    for b in bars:
        boundary = (b.ts // target_granularity) * target_granularity
        if boundary not in buckets:
            buckets[boundary] = Bar(
                symbol=symbol, timeframe=target_tf, ts=boundary,
                open=b.open, high=b.high, low=b.low, close=b.close, volume=b.volume,
            )
        else:
            agg = buckets[boundary]
            agg.high = max(agg.high, b.high)
            agg.low = min(agg.low, b.low)
            agg.close = b.close
            agg.volume += b.volume

    return [buckets[k] for k in sorted(buckets)]


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------
def _ts_str(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
