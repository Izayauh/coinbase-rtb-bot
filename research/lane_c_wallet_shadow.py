"""
research/lane_c_wallet_shadow.py — Minimal Lane C wallet-consensus shadow runner.

Research-only runner:
  - reads a pre-exported wallet_events.csv
  - filters to delayed long signals on Coinbase-listed spot assets only
  - builds simple consensus events from distinct wallets
  - reuses the existing backtest engine on cached Coinbase 1h bars

This is intentionally narrow. It is not a live trading path and does not try to
reconstruct on-chain execution.
"""
import argparse
import csv
import json
import math
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from research.backtest import run_backtest
from research.costs import ALTCOIN_SPREADS, FrictionModel
from research.data import load_bars
from research.types import Bar, Signal

RESULTS_DIR = os.path.join(os.path.dirname(__file__), "results")
DEFAULT_OUTPUT = os.path.join(RESULTS_DIR, "lane_c_wallet_shadow_results.json")

DEFAULT_SYMBOL_MAP = {
    "BTC": "BTC-USD",
    "ETH": "ETH-USD",
    "SOL": "SOL-USD",
    "XRP": "XRP-USD",
    "ADA": "ADA-USD",
    "LINK": "LINK-USD",
    "AVAX": "AVAX-USD",
    "DOGE": "DOGE-USD",
    "LTC": "LTC-USD",
    "BCH": "BCH-USD",
}


@dataclass
class WalletEvent:
    event_ts: int
    chain: str
    wallet: str
    token_symbol: str
    token_address: str
    action: str
    usd_notional: float
    price_usd: float
    liquidity_usd: float
    venue: str
    tx_hash: str
    coinbase_symbol: str
    wallet_score: float
    raw: dict


@dataclass
class ConsensusEvent:
    symbol: str
    signal_ts: int
    wallets: List[str]
    wallet_count: int
    consensus_score: float
    total_notional_usd: float
    source_event_count: int


REQUIRED_COLUMNS = {
    "event_ts", "chain", "wallet", "token_symbol", "token_address", "action",
    "usd_notional", "price_usd", "liquidity_usd", "venue", "tx_hash",
}


def parse_ts(value: str) -> int:
    value = str(value).strip()
    if not value:
        raise ValueError("empty timestamp")
    if value.isdigit():
        return int(value)
    iso = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _parse_boolish(value: object) -> bool:
    if value is None:
        return False
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "y"}


def _parse_float(value: object, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    return float(value)


def estimate_wallet_score(row: dict) -> float:
    hint = row.get("wallet_score_hint")
    if hint not in (None, ""):
        return max(float(hint), 0.0)

    realized = _parse_float(row.get("realized_pnl_30d"), 0.0)
    fwd_1d = _parse_float(row.get("forward_return_1d"), 0.0)
    fwd_7d = _parse_float(row.get("forward_return_7d"), 0.0)

    score = 1.0
    score += max(realized, 0.0) / 100.0
    score += max(fwd_1d, 0.0) / 20.0
    score += max(fwd_7d, 0.0) / 50.0
    return max(score, 0.0)


def map_coinbase_symbol(row: dict, symbol_map: Dict[str, str]) -> str:
    explicit = str(row.get("coinbase_symbol", "")).strip().upper()
    if explicit:
        return explicit
    token_symbol = str(row.get("token_symbol", "")).strip().upper()
    return symbol_map.get(token_symbol, "")


def load_wallet_events(
    csv_path: str,
    symbol_map: Optional[Dict[str, str]] = None,
    min_liquidity_usd: float = 1_000_000.0,
    min_notional_usd: float = 1_000.0,
) -> Tuple[List[WalletEvent], dict]:
    symbol_map = symbol_map or DEFAULT_SYMBOL_MAP
    events: List[WalletEvent] = []
    stats = defaultdict(int)

    with open(csv_path, "r", newline="") as f:
        reader = csv.DictReader(f)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"wallet event CSV missing required columns: {sorted(missing)}")

        for row in reader:
            stats["rows_seen"] += 1
            try:
                action = str(row["action"]).strip().lower()
                if action not in {"buy", "accumulate"}:
                    stats["drop_action"] += 1
                    continue

                usd_notional = float(row["usd_notional"])
                if usd_notional <= 0 or usd_notional < min_notional_usd:
                    stats["drop_notional"] += 1
                    continue

                liquidity_usd = float(row["liquidity_usd"])
                if liquidity_usd < min_liquidity_usd:
                    stats["drop_liquidity"] += 1
                    continue

                if _parse_boolish(row.get("is_router")) or _parse_boolish(row.get("is_exchange")) or _parse_boolish(row.get("is_contract")):
                    stats["drop_entity_flag"] += 1
                    continue

                coinbase_symbol = map_coinbase_symbol(row, symbol_map)
                if not coinbase_symbol:
                    stats["drop_unmapped_symbol"] += 1
                    continue

                event = WalletEvent(
                    event_ts=parse_ts(row["event_ts"]),
                    chain=str(row["chain"]).strip().lower(),
                    wallet=str(row["wallet"]).strip(),
                    token_symbol=str(row["token_symbol"]).strip().upper(),
                    token_address=str(row["token_address"]).strip(),
                    action=action,
                    usd_notional=usd_notional,
                    price_usd=float(row["price_usd"]),
                    liquidity_usd=liquidity_usd,
                    venue=str(row["venue"]).strip(),
                    tx_hash=str(row["tx_hash"]).strip(),
                    coinbase_symbol=coinbase_symbol,
                    wallet_score=estimate_wallet_score(row),
                    raw=row,
                )
                events.append(event)
                stats["rows_kept"] += 1
            except Exception:
                stats["drop_parse_error"] += 1

    events.sort(key=lambda e: (e.coinbase_symbol, e.event_ts, e.wallet, e.tx_hash))
    return events, dict(stats)


def build_consensus_events(
    events: Iterable[WalletEvent],
    delay_hours: int = 4,
    consensus_window_hours: int = 24,
    min_wallets: int = 2,
    min_consensus_score: float = 2.0,
) -> List[ConsensusEvent]:
    window_seconds = consensus_window_hours * 3600
    delay_seconds = delay_hours * 3600
    per_symbol: Dict[str, List[WalletEvent]] = defaultdict(list)
    for event in events:
        per_symbol[event.coinbase_symbol].append(event)

    output: List[ConsensusEvent] = []
    for symbol, sym_events in per_symbol.items():
        emitted = set()
        for idx, event in enumerate(sym_events):
            window_start = event.event_ts - window_seconds
            active = [e for e in sym_events[: idx + 1] if e.event_ts >= window_start]

            wallets: Dict[str, float] = {}
            total_notional = 0.0
            for candidate in active:
                wallets[candidate.wallet] = max(wallets.get(candidate.wallet, 0.0), candidate.wallet_score)
                total_notional += candidate.usd_notional

            wallet_count = len(wallets)
            consensus_score = sum(wallets.values())
            signal_ts = event.event_ts + delay_seconds
            bucket = (symbol, signal_ts)
            if wallet_count < min_wallets:
                continue
            if consensus_score < min_consensus_score:
                continue
            if bucket in emitted:
                continue

            emitted.add(bucket)
            output.append(ConsensusEvent(
                symbol=symbol,
                signal_ts=signal_ts,
                wallets=sorted(wallets),
                wallet_count=wallet_count,
                consensus_score=consensus_score,
                total_notional_usd=total_notional,
                source_event_count=len(active),
            ))

    output.sort(key=lambda e: (e.symbol, e.signal_ts))
    return output


def _find_signal_bar_index(bars: List[Bar], signal_ts: int) -> Optional[int]:
    for i, bar in enumerate(bars):
        if bar.ts >= signal_ts:
            if i >= len(bars) - 1:
                return None
            return i
    return None


def consensus_to_signals(bars: List[Bar], consensus_events: Iterable[ConsensusEvent]) -> List[Signal]:
    seen = set()
    signals: List[Signal] = []
    for event in consensus_events:
        idx = _find_signal_bar_index(bars, event.signal_ts)
        if idx is None:
            continue
        key = idx
        if key in seen:
            continue
        seen.add(key)
        signals.append(Signal(
            bar_index=idx,
            direction="long",
            rule_name="lane_c_wallet_shadow",
            params={
                "wallet_count": event.wallet_count,
                "consensus_score": round(event.consensus_score, 4),
                "signal_ts": event.signal_ts,
                "source_event_count": event.source_event_count,
            },
        ))
    return signals


def _json_safe(value):
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return None
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    return value


def run_shadow_backtest(
    wallet_csv: str,
    delay_hours: int = 4,
    consensus_window_hours: int = 24,
    min_wallets: int = 2,
    min_consensus_score: float = 2.0,
    min_liquidity_usd: float = 1_000_000.0,
    min_notional_usd: float = 1_000.0,
    timeframe: str = "1h",
    time_stop_bars: int = 24 * 7,
) -> dict:
    events, ingest_stats = load_wallet_events(
        wallet_csv,
        min_liquidity_usd=min_liquidity_usd,
        min_notional_usd=min_notional_usd,
    )
    consensus = build_consensus_events(
        events,
        delay_hours=delay_hours,
        consensus_window_hours=consensus_window_hours,
        min_wallets=min_wallets,
        min_consensus_score=min_consensus_score,
    )

    by_symbol: Dict[str, List[ConsensusEvent]] = defaultdict(list)
    for item in consensus:
        by_symbol[item.symbol].append(item)

    friction = FrictionModel(sensitivity=1.0, spread_overrides=ALTCOIN_SPREADS)
    results = []
    for symbol, symbol_events in sorted(by_symbol.items()):
        bars = load_bars(symbol, timeframe)
        if not bars:
            results.append({
                "symbol": symbol,
                "status": "missing_bars",
                "consensus_events": len(symbol_events),
                "signals": 0,
            })
            continue

        signals = consensus_to_signals(bars, symbol_events)
        result = run_backtest(
            bars=bars,
            signals=signals,
            friction=friction,
            symbol=symbol,
            timeframe=timeframe,
            rule_name="lane_c_wallet_shadow",
            params={
                "delay_hours": delay_hours,
                "consensus_window_hours": consensus_window_hours,
                "min_wallets": min_wallets,
                "min_consensus_score": min_consensus_score,
            },
            time_stop_bars=time_stop_bars,
        )
        results.append({
            "symbol": symbol,
            "status": "ok",
            "events_after_filter": sum(1 for e in events if e.coinbase_symbol == symbol),
            "consensus_events": len(symbol_events),
            "signals": len(signals),
            "trade_count": result.trade_count,
            "win_rate": result.win_rate,
            "profit_factor": result.profit_factor,
            "expectancy_pct": result.expectancy_pct,
            "total_return_pct": result.total_return_pct,
            "max_drawdown_pct": result.max_drawdown_pct,
        })

    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "wallet_csv": wallet_csv,
        "timeframe": timeframe,
        "parameters": {
            "delay_hours": delay_hours,
            "consensus_window_hours": consensus_window_hours,
            "min_wallets": min_wallets,
            "min_consensus_score": min_consensus_score,
            "min_liquidity_usd": min_liquidity_usd,
            "min_notional_usd": min_notional_usd,
            "time_stop_bars": time_stop_bars,
        },
        "ingest_stats": ingest_stats,
        "symbols_tested": sorted(by_symbol.keys()),
        "results": results,
    }
    return _json_safe(payload)


def main() -> None:
    parser = argparse.ArgumentParser(description="Lane C wallet shadow runner (research only)")
    parser.add_argument("--wallet-csv", required=True)
    parser.add_argument("--delay-hours", type=int, default=4)
    parser.add_argument("--consensus-window-hours", type=int, default=24)
    parser.add_argument("--min-wallets", type=int, default=2)
    parser.add_argument("--min-consensus-score", type=float, default=2.0)
    parser.add_argument("--min-liquidity-usd", type=float, default=1_000_000.0)
    parser.add_argument("--min-notional-usd", type=float, default=1_000.0)
    parser.add_argument("--time-stop-bars", type=int, default=24 * 7)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    payload = run_shadow_backtest(
        wallet_csv=args.wallet_csv,
        delay_hours=args.delay_hours,
        consensus_window_hours=args.consensus_window_hours,
        min_wallets=args.min_wallets,
        min_consensus_score=args.min_consensus_score,
        min_liquidity_usd=args.min_liquidity_usd,
        min_notional_usd=args.min_notional_usd,
        time_stop_bars=args.time_stop_bars,
    )

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(payload, f, indent=2)

    print(f"Saved results to {args.output}")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
