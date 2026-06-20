#!/usr/bin/env python
"""
signal_funnel_audit.py — Instrumented signal funnel diagnostic.

Runs the EXACT same strategy logic as backtest.py but counts how many bars
pass each filter stage. Produces hard counts for root-cause diagnosis.

Uses the public Coinbase Exchange API (no auth required) for historical data.
Caches fetched data to CSV to avoid re-fetching.

Usage:
    python signal_funnel_audit.py --days 365
    python signal_funnel_audit.py --days 365 --experiments   # run diagnostic experiments
"""
import argparse
import csv
import os
import sys
import time
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Dict, Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot.models import Bar
from bot.strategy import Indicators, is_bullish_regime

# -----------------------------------------------------------------------
# Strategy params (must match backtest.py / state_machine.py / config.yaml)
# -----------------------------------------------------------------------
DEFAULT_PARAMS = {
    "breakout_lookback": 20,
    "breakout_volume_mult": 1.25,
    "breakout_rsi_min": 56,
    "breakout_rsi_max": 74,
    "retest_window_bars": 5,
    "continuation_chase_atr_max": 0.8,
    "continuation_expiry_bars": 0,  # 0 = no expiry (baseline behavior)
    "close_pct_min": 0.70,
    "use_rsi_filter": True,
    "use_volume_filter": True,
}

SLIPPAGE_BPS = 5
FEE_BPS = 10
PORTFOLIO_VALUE = 10000.0
RISK_PER_TRADE = 0.002
TIME_STOP_BARS = 12
TRAIL_TRIGGER_ATR = 1.0
TRAIL_DISTANCE_ATR = 1.8


# -----------------------------------------------------------------------
# Data fetching — public Coinbase Exchange API (NO AUTH REQUIRED)
# -----------------------------------------------------------------------
def fetch_1h_candles_public(symbol: str, days: int, cache_dir: str = ".") -> List[Bar]:
    """Fetch historical 1h candles from the public Coinbase Exchange API.
    
    No API keys required. Caches to CSV for re-use.
    """
    import requests

    cache_file = os.path.join(cache_dir, f"cache_{symbol}_{days}d_1h.csv")

    # Check cache
    if os.path.isfile(cache_file):
        bars = _load_cache(cache_file, symbol)
        if bars:
            print(f"  Loaded {len(bars)} cached 1h bars from {cache_file}")
            return bars

    # Fetch from public API
    # Coinbase Exchange API: GET /products/{id}/candles
    # Max 300 candles per request
    base_url = "https://api.exchange.coinbase.com"
    product_id = symbol
    granularity = 3600  # 1 hour

    now = int(time.time())
    start = now - days * 86400

    all_bars: List[Bar] = []
    cursor = start
    chunk_size = 300 * 3600  # 300 candles * 1h

    print(f"Fetching {days} days of 1h candles for {symbol} (public API, no auth) ...")
    request_count = 0

    while cursor < now:
        chunk_end = min(cursor + chunk_size, now)

        url = f"{base_url}/products/{product_id}/candles"
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
        except Exception as e:
            print(f"  Warning: API error at cursor {cursor}: {e}")
            cursor = chunk_end
            time.sleep(1)
            continue

        # Coinbase Exchange returns: [timestamp, low, high, open, close, volume]
        for c in candles:
            try:
                ts = int(c[0])
                lo = float(c[1])
                hi = float(c[2])
                o = float(c[3])
                cl = float(c[4])
                v = float(c[5])
                all_bars.append(Bar(symbol, "1h", ts, o, hi, lo, cl, v))
            except (IndexError, ValueError, TypeError):
                continue

        cursor = chunk_end
        # Rate limiting: Coinbase public API allows ~10 req/sec
        if request_count % 5 == 0:
            time.sleep(0.5)

    # Deduplicate + sort
    seen = {}
    for b in all_bars:
        seen[b.ts_open] = b
    all_bars = [seen[k] for k in sorted(seen)]
    print(f"  Fetched {len(all_bars)} 1h bars ({request_count} API requests).")

    # Cache to CSV
    _save_cache(cache_file, all_bars)
    print(f"  Cached to {cache_file}")

    return all_bars


def _save_cache(path: str, bars: List[Bar]):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "timeframe", "ts_open", "open", "high", "low", "close", "volume"])
        for b in bars:
            w.writerow([b.symbol, b.timeframe, b.ts_open, b.open, b.high, b.low, b.close, b.volume])


def _load_cache(path: str, symbol: str) -> List[Bar]:
    bars = []
    try:
        with open(path, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                bars.append(Bar(
                    symbol=row["symbol"],
                    timeframe=row["timeframe"],
                    ts_open=int(row["ts_open"]),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                ))
    except Exception:
        return []
    return bars


def build_4h_bars(bars_1h: List[Bar], symbol: str) -> List[Bar]:
    buckets: dict = {}
    for b in bars_1h:
        boundary = (b.ts_open // 14400) * 14400
        if boundary not in buckets:
            buckets[boundary] = Bar(symbol, "4h", boundary,
                                    b.open, b.high, b.low, b.close, b.volume)
        else:
            agg = buckets[boundary]
            agg.high = max(agg.high, b.high)
            agg.low = min(agg.low, b.low)
            agg.close = b.close
            agg.volume += b.volume
    return [buckets[k] for k in sorted(buckets)]


# -----------------------------------------------------------------------
# Regime sub-filter breakdown
# -----------------------------------------------------------------------
def regime_breakdown(bars_4h: List[Bar]) -> dict:
    """Return which sub-conditions of is_bullish_regime pass/fail."""
    if len(bars_4h) < 201:
        return {"enough_bars": False}

    closes = [b.close for b in bars_4h]
    ema_50 = Indicators.calc_ema(closes, 50)
    ema_200 = Indicators.calc_ema(closes, 200)
    atr = Indicators.calc_atr(bars_4h, 14)

    if ema_50[-1] is None or ema_200[-1] is None or atr[-1] is None:
        return {"enough_bars": True, "indicators_ready": False}

    current_close = closes[-1]
    close_above_200 = current_close > ema_200[-1]
    ema_50_above_200 = ema_50[-1] > ema_200[-1]
    slope_200_positive = ema_200[-1] > ema_200[-2]
    atr_ratio = atr[-1] / current_close
    high_vol = atr_ratio > 0.005

    return {
        "enough_bars": True,
        "indicators_ready": True,
        "close_above_200": close_above_200,
        "ema_50_above_200": ema_50_above_200,
        "slope_200_positive": slope_200_positive,
        "atr_ratio": atr_ratio,
        "high_vol": high_vol,
        "all_pass": close_above_200 and ema_50_above_200 and slope_200_positive and high_vol,
    }


# -----------------------------------------------------------------------
# Instrumented funnel engine
# -----------------------------------------------------------------------
class FunnelAuditEngine:
    IDLE = "IDLE"
    WAITING_RETEST = "WAITING_RETEST"
    RETEST_CONFIRMED = "RETEST_CONFIRMED"
    IN_POSITION = "IN_POSITION"

    def __init__(self, params: dict = None):
        self.p = {**DEFAULT_PARAMS, **(params or {})}
        self.state = self.IDLE
        self.breakout_bar: Optional[Bar] = None
        self.breakout_level = 0.0
        self.bars_since_breakout = 0
        self.bars_since_retest = 0
        self.retest_bar: Optional[Bar] = None
        self.position_active = False
        self.bars_in_position = 0
        self.trailing_active = False
        self.trail_stop = 0.0
        self.position_entry_price = 0.0
        self.position_stop = 0.0
        self.position_atr = 0.0
        self.equity = PORTFOLIO_VALUE

        # COUNTERS
        self.total_eval_bars = 0
        self.skipped_insufficient_4h = 0
        self.idle_evals = 0

        # Regime sub-breakdown
        self.regime_not_enough_bars = 0
        self.regime_indicators_not_ready = 0
        self.regime_close_below_200 = 0
        self.regime_ema50_below_200 = 0
        self.regime_slope_negative = 0
        self.regime_low_vol = 0
        self.regime_pass = 0

        # Breakout sub-filters
        self.breakout_candidate_total = 0
        self.breakout_close_above_high = 0
        self.breakout_volume_pass = 0
        self.breakout_close_pct_pass = 0
        self.breakout_rsi_pass = 0
        self.breakout_all_pass = 0

        # Retest stage
        self.retest_evals = 0
        self.retest_regime_fail = 0
        self.retest_window_expired = 0
        self.retest_zone_miss = 0
        self.retest_close_below_level = 0
        self.retest_close_below_mid = 0
        self.retest_confirmed = 0

        # Continuation stage
        self.continuation_evals = 0
        self.continuation_regime_fail = 0
        self.continuation_same_bar = 0
        self.continuation_close_below_retest_high = 0
        self.continuation_chase_too_far = 0
        self.continuation_bad_stop = 0
        self.continuation_expiry = 0
        self.continuation_entry = 0

        # Trades
        self.trades_executed = 0
        self.trade_exits = Counter()
        self.trade_pnls = []

        # Breakout events log
        self.breakout_events = []

        # Distributions
        self.rsi_values_at_breakout_candidate = []
        self.vol_ratios_at_breakout_candidate = []
        self.close_pcts_at_breakout_candidate = []

    def run(self, bars_1h: List[Bar], bars_4h: List[Bar]):
        if len(bars_1h) < 25 or len(bars_4h) < 205:
            print(f"ERROR: Not enough bars. Have {len(bars_1h)} 1h, {len(bars_4h)} 4h.")
            return

        for i in range(25, len(bars_1h)):
            current_1h = bars_1h[i]
            context_1h = bars_1h[max(0, i - 29):i + 1]

            current_4h_boundary = (current_1h.ts_open // 14400) * 14400
            context_4h = [b for b in bars_4h if b.ts_open <= current_4h_boundary]
            if len(context_4h) < 205:
                self.skipped_insufficient_4h += 1
                continue
            context_4h = context_4h[-210:]

            self.total_eval_bars += 1

            if self.position_active:
                self._check_exit(current_1h, context_1h)

            if not self.position_active:
                if self.state == self.IDLE:
                    self.idle_evals += 1
                    self._eval_breakout_instrumented(context_1h, context_4h, current_1h)
                elif self.state == self.WAITING_RETEST:
                    self.retest_evals += 1
                    self._eval_retest_instrumented(context_1h, context_4h)
                elif self.state == self.RETEST_CONFIRMED:
                    self.continuation_evals += 1
                    self._eval_continuation_instrumented(context_1h, context_4h, current_1h)

    def _eval_breakout_instrumented(self, bars_1h: List[Bar], bars_4h: List[Bar], current: Bar):
        breakdown = regime_breakdown(bars_4h)

        if not breakdown.get("enough_bars", False):
            self.regime_not_enough_bars += 1
            return
        if not breakdown.get("indicators_ready", False):
            self.regime_indicators_not_ready += 1
            return

        if not breakdown["close_above_200"]:
            self.regime_close_below_200 += 1
        if not breakdown["ema_50_above_200"]:
            self.regime_ema50_below_200 += 1
        if not breakdown["slope_200_positive"]:
            self.regime_slope_negative += 1
        if not breakdown["high_vol"]:
            self.regime_low_vol += 1

        if not breakdown["all_pass"]:
            return

        self.regime_pass += 1
        self.breakout_candidate_total += 1

        latest = bars_1h[-1]
        past_20 = bars_1h[-21:-1]
        if len(past_20) < 20:
            return

        highest_20 = max(b.high for b in past_20)
        avg_vol_20 = sum(b.volume for b in past_20) / 20

        rsi = Indicators.calc_rsi([b.close for b in bars_1h])
        if rsi[-1] is None:
            return

        # Filter 1: close > highest_20
        if latest.close <= highest_20:
            return
        self.breakout_close_above_high += 1

        vol_ratio = latest.volume / avg_vol_20 if avg_vol_20 > 0 else 0
        candle_range = latest.high - latest.low
        close_pct = (latest.close - latest.low) / candle_range if candle_range > 0 else 0

        # Record distributions
        self.rsi_values_at_breakout_candidate.append(rsi[-1])
        self.vol_ratios_at_breakout_candidate.append(vol_ratio)
        self.close_pcts_at_breakout_candidate.append(close_pct)

        # Filter 2: volume
        if self.p["use_volume_filter"] and latest.volume <= self.p["breakout_volume_mult"] * avg_vol_20:
            return
        self.breakout_volume_pass += 1

        # Filter 3: close percentile
        if candle_range == 0:
            return
        if close_pct < self.p["close_pct_min"]:
            return
        self.breakout_close_pct_pass += 1

        # Filter 4: RSI
        if self.p["use_rsi_filter"] and not (self.p["breakout_rsi_min"] <= rsi[-1] <= self.p["breakout_rsi_max"]):
            return
        self.breakout_rsi_pass += 1
        self.breakout_all_pass += 1

        ts_str = datetime.fromtimestamp(latest.ts_open, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        self.breakout_events.append({
            "ts": ts_str,
            "price": latest.close,
            "highest_20": highest_20,
            "vol_ratio": vol_ratio,
            "close_pct": close_pct,
            "rsi": rsi[-1],
        })

        self.state = self.WAITING_RETEST
        self.breakout_bar = latest
        self.breakout_level = highest_20
        self.bars_since_breakout = 0

    def _eval_retest_instrumented(self, bars_1h: List[Bar], bars_4h: List[Bar]):
        if not is_bullish_regime(bars_4h):
            self.retest_regime_fail += 1
            self._reset()
            return

        self.bars_since_breakout += 1
        if self.bars_since_breakout > self.p["retest_window_bars"]:
            self.retest_window_expired += 1
            self._reset()
            return

        latest = bars_1h[-1]
        bo_midpoint = (self.breakout_bar.high + self.breakout_bar.low) / 2
        atr = Indicators.calc_atr(bars_1h, 14)[-1]
        if atr is None:
            return

        upper = self.breakout_level + atr * 0.2
        lower = self.breakout_level - atr * 0.5

        touches = lower <= latest.low <= upper
        closes_above = latest.close > self.breakout_level
        closes_above_mid = latest.close > bo_midpoint

        if not touches:
            self.retest_zone_miss += 1
            return
        if not closes_above:
            self.retest_close_below_level += 1
            return
        if not closes_above_mid:
            self.retest_close_below_mid += 1
            return

        self.retest_confirmed += 1
        self.state = self.RETEST_CONFIRMED
        self.retest_bar = latest
        self.bars_since_retest = 0

    def _eval_continuation_instrumented(self, bars_1h: List[Bar], bars_4h: List[Bar], current: Bar):
        if not is_bullish_regime(bars_4h):
            self.continuation_regime_fail += 1
            self._reset()
            return

        self.bars_since_retest += 1

        # Continuation expiry window (0 = disabled)
        if self.p["continuation_expiry_bars"] > 0 and self.bars_since_retest > self.p["continuation_expiry_bars"]:
            self.continuation_expiry += 1
            self._reset()
            return

        if current.ts_open == self.retest_bar.ts_open:
            self.continuation_same_bar += 1
            return

        if current.close <= self.retest_bar.high:
            self.continuation_close_below_retest_high += 1
            return

        atr = Indicators.calc_atr(bars_1h, 14)[-1]
        if atr is None:
            self._reset()
            return

        if (current.close - self.breakout_level) > (self.p["continuation_chase_atr_max"] * atr):
            self.continuation_chase_too_far += 1
            self._reset()
            return

        # ENTRY
        entry_price = current.close * (1 + SLIPPAGE_BPS / 10000)
        stop_price = self.retest_bar.low - atr
        if stop_price >= entry_price:
            self.continuation_bad_stop += 1
            self._reset()
            return

        self.continuation_entry += 1
        self.trades_executed += 1
        self.position_active = True
        self.position_entry_price = entry_price
        self.position_stop = stop_price
        self.position_atr = atr
        self.bars_in_position = 0
        self.trailing_active = False
        self.trail_stop = 0.0

        risk_per_unit = entry_price - stop_price
        dollars_at_risk = self.equity * RISK_PER_TRADE
        size = dollars_at_risk / risk_per_unit
        fee = entry_price * size * (FEE_BPS / 10000 / 2)
        self.equity -= fee
        self._position_size = size

        self.state = self.IN_POSITION

    def _check_exit(self, bar: Bar, bars_1h: List[Bar]):
        self.bars_in_position += 1
        exit_price = 0.0
        reason = ""

        if bar.low <= self.position_stop:
            exit_price = self.position_stop
            reason = "STOP_LOSS"
        elif self.trailing_active and bar.low <= self.trail_stop:
            exit_price = self.trail_stop
            reason = "TRAILING_STOP"
        elif self.bars_in_position >= TIME_STOP_BARS:
            exit_price = bar.close
            reason = "TIME_STOP"

        if not self.trailing_active:
            trigger = self.position_entry_price + TRAIL_TRIGGER_ATR * self.position_atr
            if bar.high >= trigger:
                self.trailing_active = True
                self.trail_stop = self.position_entry_price + (TRAIL_TRIGGER_ATR - TRAIL_DISTANCE_ATR) * self.position_atr
                self.trail_stop = max(self.trail_stop, self.position_stop)

        if self.trailing_active and not reason:
            new_trail = bar.high - TRAIL_DISTANCE_ATR * self.position_atr
            if new_trail > self.trail_stop:
                self.trail_stop = new_trail

        if exit_price > 0:
            size = getattr(self, "_position_size", 0)
            fee = exit_price * size * (FEE_BPS / 10000 / 2)
            pnl = (exit_price - self.position_entry_price) * size - fee
            pnl_pct = (exit_price - self.position_entry_price) / self.position_entry_price * 100
            self.equity += pnl
            self.trade_exits[reason] += 1
            self.trade_pnls.append(pnl_pct)
            self.position_active = False
            self._reset()

    def _reset(self):
        self.state = self.IDLE
        self.breakout_bar = None
        self.breakout_level = 0.0
        self.bars_since_breakout = 0
        self.bars_since_retest = 0
        self.retest_bar = None


# -----------------------------------------------------------------------
# Reporting
# -----------------------------------------------------------------------
def print_funnel_report(engine: FunnelAuditEngine, symbol: str, days: int,
                        total_1h: int, total_4h: int, label: str = "BASELINE"):
    e = engine
    print()
    print("=" * 70)
    print(f"  SIGNAL FUNNEL AUDIT — {symbol} — {days}d — [{label}]")
    print("=" * 70)
    print()
    print(f"  Raw data: {total_1h} 1h bars -> {total_4h} 4h bars")
    print(f"  Skipped (not enough 4h context): {e.skipped_insufficient_4h}")
    print()

    print("-" * 70)
    print("  STAGE 1: Total evaluation bars")
    print(f"    Bars evaluated:                    {e.total_eval_bars}")
    print(f"    IDLE evaluations (breakout check): {e.idle_evals}")
    print()

    print("-" * 70)
    print("  STAGE 2: Bullish regime filter (4h EMA cross + volatility)")
    print(f"    Not enough bars:                   {e.regime_not_enough_bars}")
    print(f"    Indicators not ready:              {e.regime_indicators_not_ready}")
    print(f"    Close below EMA-200:               {e.regime_close_below_200}")
    print(f"    EMA-50 below EMA-200:              {e.regime_ema50_below_200}")
    print(f"    EMA-200 slope negative:            {e.regime_slope_negative}")
    print(f"    ATR/price ratio too low (<0.5%):   {e.regime_low_vol}")
    print(f"    ---")
    print(f"    PASS (bullish regime):             {e.regime_pass}")
    if e.idle_evals > 0:
        print(f"    Pass rate:                         {e.regime_pass / e.idle_evals * 100:.1f}%")
    print()

    print("-" * 70)
    print("  STAGE 3-6: Breakout sub-filters (sequential)")
    print(f"    Regime pass (candidates):          {e.breakout_candidate_total}")
    print(f"    3. Close > 20-bar high:            {e.breakout_close_above_high}")
    print(f"    4. Volume > {e.p['breakout_volume_mult']}x avg:           {e.breakout_volume_pass}")
    print(f"    5. Close pct >= {e.p['close_pct_min']}:            {e.breakout_close_pct_pass}")
    print(f"    6. RSI in [{e.p['breakout_rsi_min']}, {e.p['breakout_rsi_max']}]:             {e.breakout_rsi_pass}")
    print(f"    ---")
    print(f"    ALL PASS -> WAITING_RETEST:        {e.breakout_all_pass}")
    print()

    if e.breakout_candidate_total > 0:
        print("    Filter attrition:")
        killed_price = e.breakout_candidate_total - e.breakout_close_above_high
        killed_vol = e.breakout_close_above_high - e.breakout_volume_pass
        killed_pct = e.breakout_volume_pass - e.breakout_close_pct_pass
        killed_rsi = e.breakout_close_pct_pass - e.breakout_rsi_pass
        total_candidates = e.breakout_candidate_total
        print(f"      Killed by close <= highest_20:   {killed_price:>5} ({killed_price/total_candidates*100:>5.1f}%)")
        print(f"      Killed by volume filter:         {killed_vol:>5} ({killed_vol/total_candidates*100:>5.1f}%)")
        print(f"      Killed by close-percentile:      {killed_pct:>5} ({killed_pct/total_candidates*100:>5.1f}%)")
        print(f"      Killed by RSI range:             {killed_rsi:>5} ({killed_rsi/total_candidates*100:>5.1f}%)")
    print()

    print("-" * 70)
    print("  STAGE 7: WAITING_RETEST -> RETEST_CONFIRMED")
    print(f"    Retest evaluations:                {e.retest_evals}")
    print(f"    Killed by regime loss:             {e.retest_regime_fail}")
    print(f"    Killed by window expiry (>{e.p['retest_window_bars']} bars): {e.retest_window_expired}")
    print(f"    Zone miss (low outside zone):      {e.retest_zone_miss}")
    print(f"    Close below breakout level:        {e.retest_close_below_level}")
    print(f"    Close below midpoint:              {e.retest_close_below_mid}")
    print(f"    ---")
    print(f"    RETEST_CONFIRMED transitions:      {e.retest_confirmed}")
    print()

    print("-" * 70)
    print("  STAGE 8-9: RETEST_CONFIRMED -> Entry")
    print(f"    Continuation evaluations:          {e.continuation_evals}")
    print(f"    Killed by regime loss:             {e.continuation_regime_fail}")
    print(f"    Same bar skip:                     {e.continuation_same_bar}")
    if e.p["continuation_expiry_bars"] > 0:
        print(f"    Continuation expired (>{e.p['continuation_expiry_bars']} bars):  {e.continuation_expiry}")
    print(f"    Close <= retest bar high:          {e.continuation_close_below_retest_high}")
    print(f"    Chase too far (>{e.p['continuation_chase_atr_max']} ATR):         {e.continuation_chase_too_far}")
    print(f"    Bad stop (stop >= entry):          {e.continuation_bad_stop}")
    print(f"    ---")
    print(f"    ENTRIES (trades executed):         {e.continuation_entry}")
    print()

    print("-" * 70)
    print("  STAGE 10-11: Trades & outcomes")
    print(f"    Trades executed:                   {e.trades_executed}")
    print(f"    Final equity:                      ${e.equity:,.2f}")
    print(f"    Total return:                      {(e.equity/PORTFOLIO_VALUE - 1)*100:+.2f}%")
    if e.trade_exits:
        for reason, count in e.trade_exits.most_common():
            print(f"      {reason}: {count}")
    if e.trade_pnls:
        avg_pnl = sum(e.trade_pnls) / len(e.trade_pnls)
        wins = [p for p in e.trade_pnls if p > 0]
        losses = [p for p in e.trade_pnls if p <= 0]
        print(f"    Avg PnL%:                          {avg_pnl:+.3f}%")
        print(f"    Win rate:                          {len(wins)}/{len(e.trade_pnls)} ({len(wins)/len(e.trade_pnls)*100:.0f}%)")
        if wins:
            print(f"    Avg win:                           {sum(wins)/len(wins):+.3f}%")
        if losses:
            print(f"    Avg loss:                          {sum(losses)/len(losses):+.3f}%")
    print()

    # Distributions
    if e.rsi_values_at_breakout_candidate:
        rsi_vals = e.rsi_values_at_breakout_candidate
        print("-" * 70)
        print(f"  RSI DISTRIBUTION at breakout candidates (n={len(rsi_vals)})")
        print(f"    Min: {min(rsi_vals):.1f}  Max: {max(rsi_vals):.1f}  Median: {sorted(rsi_vals)[len(rsi_vals)//2]:.1f}")
        in_range = sum(1 for r in rsi_vals if e.p["breakout_rsi_min"] <= r <= e.p["breakout_rsi_max"])
        print(f"    In [{e.p['breakout_rsi_min']}, {e.p['breakout_rsi_max']}]: {in_range}/{len(rsi_vals)} ({in_range/len(rsi_vals)*100:.1f}%)")
        buckets = [(0,30), (30,40), (40,50), (50,56), (56,60), (60,65), (65,70), (70,74), (74,80), (80,100)]
        for lo, hi in buckets:
            cnt = sum(1 for r in rsi_vals if lo <= r < hi)
            bar = "#" * min(cnt, 50)
            marker = " <-- target" if lo >= 56 and hi <= 74 else ""
            print(f"      [{lo:2d}-{hi:2d}): {cnt:4d} {bar}{marker}")
        print()

    if e.vol_ratios_at_breakout_candidate:
        vr = e.vol_ratios_at_breakout_candidate
        print(f"  VOLUME RATIO DISTRIBUTION at breakout candidates (n={len(vr)})")
        print(f"    Min: {min(vr):.2f}x  Max: {max(vr):.2f}x  Median: {sorted(vr)[len(vr)//2]:.2f}x")
        above = sum(1 for v in vr if v > e.p["breakout_volume_mult"])
        print(f"    Above {e.p['breakout_volume_mult']}x: {above}/{len(vr)} ({above/len(vr)*100:.1f}%)")
        print()

    if e.close_pcts_at_breakout_candidate:
        cp = e.close_pcts_at_breakout_candidate
        print(f"  CLOSE PERCENTILE DISTRIBUTION at breakout candidates (n={len(cp)})")
        print(f"    Min: {min(cp):.2f}  Max: {max(cp):.2f}  Median: {sorted(cp)[len(cp)//2]:.2f}")
        above = sum(1 for c in cp if c >= e.p["close_pct_min"])
        print(f"    Above {e.p['close_pct_min']}: {above}/{len(cp)} ({above/len(cp)*100:.1f}%)")
        print()

    # Breakout events log
    if e.breakout_events:
        print("-" * 70)
        print(f"  ALL BREAKOUT EVENTS ({len(e.breakout_events)} total):")
        print(f"  {'#':>3}  {'Timestamp':>19}  {'Price':>10}  {'H20':>10}  {'Volx':>7}  {'ClPct':>6}  {'RSI':>6}")
        print("  " + "-" * 75)
        for i, ev in enumerate(e.breakout_events, 1):
            print(f"  {i:3d}  {ev['ts']:>19}  ${ev['price']:>9,.2f}  ${ev['highest_20']:>9,.2f}  "
                  f"{ev['vol_ratio']:>6.2f}  {ev['close_pct']:>5.2f}  {ev['rsi']:>5.1f}")
        print()

    # Summary funnel
    print("=" * 70)
    print("  FUNNEL SUMMARY")
    print("=" * 70)
    stages = [
        ("1.  Total eval bars", e.total_eval_bars),
        ("2.  Bullish regime pass", e.regime_pass),
        ("3.  Close > 20-bar high", e.breakout_close_above_high),
        ("4.  Volume filter pass", e.breakout_volume_pass),
        ("5.  Close pct filter pass", e.breakout_close_pct_pass),
        ("6.  RSI filter pass", e.breakout_rsi_pass),
        ("7.  WAITING_RETEST", e.breakout_all_pass),
        ("8.  RETEST_CONFIRMED", e.retest_confirmed),
        ("9.  Continuation confirmed", e.continuation_entry),
        ("10. Trades executed", e.trades_executed),
    ]
    max_label = max(len(s[0]) for s in stages)
    for label, count in stages:
        pct = count / e.total_eval_bars * 100 if e.total_eval_bars > 0 else 0
        bar_len = int(count / max(e.total_eval_bars, 1) * 40)
        bar = "#" * max(bar_len, 1) if count > 0 else "."
        print(f"  {label:<{max_label}}  {count:>6}  ({pct:>7.3f}%)  {bar}")
    print("=" * 70)


def run_experiment(bars_1h, bars_4h, symbol, days, label, params):
    """Run a single experiment with the given parameters."""
    engine = FunnelAuditEngine(params)
    engine.run(bars_1h, bars_4h)
    print_funnel_report(engine, symbol, days, len(bars_1h), len(bars_4h), label)
    return engine


def print_comparison_table(results: Dict[str, FunnelAuditEngine]):
    """Print a side-by-side comparison of all experiments."""
    print()
    print("=" * 100)
    print("  EXPERIMENT COMPARISON TABLE")
    print("=" * 100)

    headers = list(results.keys())
    header_str = f"  {'Metric':<35}" + "".join(f"  {h:>10}" for h in headers)
    print(header_str)
    print("  " + "-" * (35 + 12 * len(headers)))

    metrics = [
        ("Total eval bars", lambda e: e.total_eval_bars),
        ("Regime pass", lambda e: e.regime_pass),
        ("Close > H20", lambda e: e.breakout_close_above_high),
        ("Volume pass", lambda e: e.breakout_volume_pass),
        ("Close pct pass", lambda e: e.breakout_close_pct_pass),
        ("RSI pass", lambda e: e.breakout_rsi_pass),
        ("WAITING_RETEST", lambda e: e.breakout_all_pass),
        ("RETEST_CONFIRMED", lambda e: e.retest_confirmed),
        ("Entries", lambda e: e.continuation_entry),
        ("Trades", lambda e: e.trades_executed),
        ("Final equity", lambda e: f"${e.equity:,.0f}"),
        ("Win rate", lambda e: f"{len([p for p in e.trade_pnls if p > 0])}/{len(e.trade_pnls)}" if e.trade_pnls else "N/A"),
        ("Avg PnL%", lambda e: f"{sum(e.trade_pnls)/len(e.trade_pnls):+.2f}%" if e.trade_pnls else "N/A"),
    ]

    for name, fn in metrics:
        vals = []
        for key in headers:
            v = fn(results[key])
            if isinstance(v, (int, float)):
                vals.append(f"{v:>10}")
            else:
                vals.append(f"{v:>10}")
        print(f"  {name:<35}" + "".join(f"  {v}" for v in vals))

    print("=" * 100)
    print()


def main():
    parser = argparse.ArgumentParser(description="Signal funnel audit")
    parser.add_argument("--days", type=int, default=365, help="Days of history")
    parser.add_argument("--symbol", type=str, default="BTC-USD", help="Product ID")
    parser.add_argument("--experiments", action="store_true",
                        help="Run diagnostic experiments (A-F) after baseline")
    parser.add_argument("--phase1b", action="store_true",
                        help="Run Phase 1b targeted experiments (continuation fixes)")
    args = parser.parse_args()

    # Fetch data
    bars_1h = fetch_1h_candles_public(args.symbol, args.days)
    bars_4h = build_4h_bars(bars_1h, args.symbol)

    print(f"  Built {len(bars_4h)} 4h bars from {len(bars_1h)} 1h bars.")
    print(f"  Date range: {datetime.fromtimestamp(bars_1h[0].ts_open, tz=timezone.utc).strftime('%Y-%m-%d')} "
          f"to {datetime.fromtimestamp(bars_1h[-1].ts_open, tz=timezone.utc).strftime('%Y-%m-%d')}")
    print(f"  Evaluation window: {len(bars_1h) - 25} bars after warmup.")

    results = {}

    # ── BASELINE: exact current strategy params ──
    baseline = run_experiment(bars_1h, bars_4h, args.symbol, args.days,
                              "BASELINE", DEFAULT_PARAMS)
    results["BASELINE"] = baseline

    if args.experiments:
        print("\n\n" + "=" * 70)
        print("  RUNNING DIAGNOSTIC EXPERIMENTS (Phase 1)")
        print("=" * 70)

        # Experiment A: No RSI filter
        results["NO_RSI"] = run_experiment(
            bars_1h, bars_4h, args.symbol, args.days,
            "A: NO RSI FILTER",
            {**DEFAULT_PARAMS, "use_rsi_filter": False})

        # Experiment B: Wider RSI
        results["WIDE_RSI"] = run_experiment(
            bars_1h, bars_4h, args.symbol, args.days,
            "B: WIDE RSI [45,82]",
            {**DEFAULT_PARAMS, "breakout_rsi_min": 45, "breakout_rsi_max": 82})

        # Experiment C: No volume filter
        results["NO_VOL"] = run_experiment(
            bars_1h, bars_4h, args.symbol, args.days,
            "C: NO VOLUME FILTER",
            {**DEFAULT_PARAMS, "use_volume_filter": False})

        # Experiment D: Wider retest window
        results["WIDE_RETEST"] = run_experiment(
            bars_1h, bars_4h, args.symbol, args.days,
            "D: RETEST 10 BARS",
            {**DEFAULT_PARAMS, "retest_window_bars": 10})

        # Experiment E: Wider chase limit
        results["WIDE_CHASE"] = run_experiment(
            bars_1h, bars_4h, args.symbol, args.days,
            "E: CHASE 1.5 ATR",
            {**DEFAULT_PARAMS, "continuation_chase_atr_max": 1.5})

        # Experiment F: All relaxed
        results["ALL_RELAXED"] = run_experiment(
            bars_1h, bars_4h, args.symbol, args.days,
            "F: ALL RELAXED",
            {
                **DEFAULT_PARAMS,
                "use_rsi_filter": False,
                "use_volume_filter": False,
                "retest_window_bars": 10,
                "continuation_chase_atr_max": 1.5,
                "close_pct_min": 0.55,
            })

        # Print comparison
        print_comparison_table(results)

    if args.phase1b:
        print("\n\n" + "=" * 70)
        print("  RUNNING PHASE 1b — TARGETED CONTINUATION FIX EXPERIMENTS")
        print("  Goal: >= 30 trades/year with non-negative expectancy")
        print("=" * 70)

        phase1b_results = {"BASELINE": baseline}

        # G: Chase 1.5 ATR + 3-bar continuation expiry (minimal structural fix)
        phase1b_results["CHASE+EXP3"] = run_experiment(
            bars_1h, bars_4h, args.symbol, args.days,
            "G: CHASE 1.5 ATR + EXPIRY 3",
            {
                **DEFAULT_PARAMS,
                "continuation_chase_atr_max": 1.5,
                "continuation_expiry_bars": 3,
            })

        # H: Chase 1.5 ATR + 5-bar continuation expiry
        phase1b_results["CHASE+EXP5"] = run_experiment(
            bars_1h, bars_4h, args.symbol, args.days,
            "H: CHASE 1.5 ATR + EXPIRY 5",
            {
                **DEFAULT_PARAMS,
                "continuation_chase_atr_max": 1.5,
                "continuation_expiry_bars": 5,
            })

        # I: Chase 1.5 ATR + 3-bar expiry + wider RSI [50, 80]
        phase1b_results["FULL_FIX_3"] = run_experiment(
            bars_1h, bars_4h, args.symbol, args.days,
            "I: CHASE+EXP3+RSI[50,80]",
            {
                **DEFAULT_PARAMS,
                "continuation_chase_atr_max": 1.5,
                "continuation_expiry_bars": 3,
                "breakout_rsi_min": 50,
                "breakout_rsi_max": 80,
            })

        # J: Chase 1.5 ATR + 5-bar expiry + wider RSI [50, 80]
        phase1b_results["FULL_FIX_5"] = run_experiment(
            bars_1h, bars_4h, args.symbol, args.days,
            "J: CHASE+EXP5+RSI[50,80]",
            {
                **DEFAULT_PARAMS,
                "continuation_chase_atr_max": 1.5,
                "continuation_expiry_bars": 5,
                "breakout_rsi_min": 50,
                "breakout_rsi_max": 80,
            })

        # K: Full fix + wider retest window (10 bars)
        phase1b_results["FULL_FIX_MAX"] = run_experiment(
            bars_1h, bars_4h, args.symbol, args.days,
            "K: FULL FIX + RETEST 10",
            {
                **DEFAULT_PARAMS,
                "continuation_chase_atr_max": 1.5,
                "continuation_expiry_bars": 5,
                "breakout_rsi_min": 50,
                "breakout_rsi_max": 80,
                "retest_window_bars": 10,
            })

        # L: Maximum addressable (all fixes + no volume filter)
        phase1b_results["MAX_ADDR"] = run_experiment(
            bars_1h, bars_4h, args.symbol, args.days,
            "L: MAX ADDRESSABLE",
            {
                **DEFAULT_PARAMS,
                "continuation_chase_atr_max": 1.5,
                "continuation_expiry_bars": 5,
                "breakout_rsi_min": 50,
                "breakout_rsi_max": 80,
                "retest_window_bars": 10,
                "use_volume_filter": False,
                "close_pct_min": 0.55,
            })

        print_comparison_table(phase1b_results)

        # ── VERDICT ──
        print("\n" + "=" * 70)
        print("  PHASE 1b VERDICT")
        print("=" * 70)
        for label, engine in phase1b_results.items():
            if label == "BASELINE":
                continue
            n = engine.trades_executed
            wr = f"{len([p for p in engine.trade_pnls if p > 0])}/{n}" if n else "N/A"
            avg = f"{sum(engine.trade_pnls)/n:+.3f}%" if n else "N/A"
            pf_num = sum(p for p in engine.trade_pnls if p > 0) if n else 0
            pf_den = abs(sum(p for p in engine.trade_pnls if p <= 0)) if n else 0
            pf = f"{pf_num/pf_den:.2f}" if pf_den > 0 else "inf" if pf_num > 0 else "N/A"
            meets_floor = "YES" if n >= 30 else "NO"
            print(f"  {label:<20}  trades={n:<4}  WR={wr:<6}  avgPnL={avg:<10}  PF={pf:<6}  meets_30_floor={meets_floor}")

        best = max(
            ((k, v) for k, v in phase1b_results.items() if k != "BASELINE" and v.trades_executed > 0),
            key=lambda x: x[1].trades_executed,
            default=None
        )
        if best:
            bk, bv = best
            print(f"\n  Best: {bk} with {bv.trades_executed} trades")
            if bv.trades_executed >= 30:
                avg = sum(bv.trade_pnls) / len(bv.trade_pnls)
                if avg >= 0:
                    print("  >>> PASSES 30-trade floor with non-negative expectancy.")
                    print("  >>> PROCEED to walk-forward validation.")
                else:
                    print(f"  >>> Meets trade count but expectancy is NEGATIVE ({avg:+.3f}%).")
                    print("  >>> ARCHIVE concept. Move to VPMR strategy.")
            else:
                print(f"  >>> Does NOT meet 30-trade floor ({bv.trades_executed} < 30).")
                print("  >>> ARCHIVE concept. Move to VPMR strategy.")
        print("=" * 70)

    print("\nDone.")


if __name__ == "__main__":
    main()
