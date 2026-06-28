#!/usr/bin/env python
"""
vpmr_backtest.py — Volume Profile Mean Reversion strategy backtest & funnel audit.

Implements VPMR from the Research Pack (Rank 1 recommendation):
  - Daily Volume Profile: POC, VAH, VAL
  - Mean reversion at Value Area boundaries
  - ADX < 25 regime filter (range-bound only)
  - RSI confirmation (oversold/overbought at entry)
  - ATR-based stop loss (1.5× ATR)
  - Take-profit at POC (mean reversion target)
  - Time stop (12 bars = 12 hours)

Reuses the same data pipeline (public Coinbase API, CSV cache, Bar model)
from signal_funnel_audit.py.

Usage:
    python vpmr_backtest.py --days 365
    python vpmr_backtest.py --days 365 --experiments
"""
import argparse
import csv
import math
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from typing import List, Optional, Dict, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot.models import Bar
from bot.strategy import Indicators

# -----------------------------------------------------------------------
# VPMR Strategy Parameters
# -----------------------------------------------------------------------
DEFAULT_PARAMS = {
    # Volume Profile
    "vp_lookback_bars": 24,        # 24 1h bars = 1 day rolling window
    "value_area_pct": 0.70,        # 70% of volume defines the Value Area

    # Regime filter
    "adx_period": 14,
    "adx_max": 25,                 # ADX must be < 25 (range-bound)
    "adx_timeframe": "4h",         # evaluated on 4h bars

    # Entry
    "rsi_period": 14,
    "rsi_oversold": 30,            # RSI < 30 for long
    "rsi_overbought": 70,          # RSI > 70 for short
    "entry_proximity_pct": 0.001,  # price within 0.1% of VA boundary

    # Exit
    "stop_atr_mult": 1.5,          # stop at 1.5× ATR from entry
    "time_stop_bars": 12,          # exit after 12 bars if not hit

    # Risk
    "risk_per_trade": 0.015,       # 1.5% of equity per trade
    "slippage_bps": 3,             # maker orders → lower slippage
    "fee_bps": 4,                  # maker fee (lower than taker)

    # Trade direction
    "allow_longs": True,
    "allow_shorts": True,
}

PORTFOLIO_VALUE = 10000.0

# -----------------------------------------------------------------------
# Data fetching — reuse from signal_funnel_audit.py
# -----------------------------------------------------------------------
def fetch_1h_candles_public(symbol: str, days: int, cache_dir: str = ".") -> List[Bar]:
    import requests
    cache_file = os.path.join(cache_dir, f"cache_{symbol}_{days}d_1h.csv")
    if os.path.isfile(cache_file):
        bars = _load_cache(cache_file, symbol)
        if bars:
            print(f"  Loaded {len(bars)} cached 1h bars from {cache_file}")
            return bars

    base_url = "https://api.exchange.coinbase.com"
    granularity = 3600
    now = int(time.time())
    start = now - days * 86400
    all_bars: List[Bar] = []
    cursor = start
    chunk_size = 300 * 3600
    print(f"Fetching {days} days of 1h candles for {symbol} (public API) ...")
    request_count = 0
    while cursor < now:
        chunk_end = min(cursor + chunk_size, now)
        url = f"{base_url}/products/{symbol}/candles"
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
            print(f"  Warning: API error: {e}")
            cursor = chunk_end
            time.sleep(1)
            continue
        for c in candles:
            try:
                all_bars.append(Bar(symbol, "1h", int(c[0]), float(c[3]),
                                    float(c[2]), float(c[1]), float(c[4]), float(c[5])))
            except (IndexError, ValueError, TypeError):
                continue
        cursor = chunk_end
        if request_count % 5 == 0:
            time.sleep(0.5)

    seen = {}
    for b in all_bars:
        seen[b.ts_open] = b
    all_bars = [seen[k] for k in sorted(seen)]
    print(f"  Fetched {len(all_bars)} 1h bars ({request_count} requests).")
    _save_cache(cache_file, all_bars)
    return all_bars


def _save_cache(path, bars):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "timeframe", "ts_open", "open", "high", "low", "close", "volume"])
        for b in bars:
            w.writerow([b.symbol, b.timeframe, b.ts_open, b.open, b.high, b.low, b.close, b.volume])


def _load_cache(path, symbol):
    bars = []
    try:
        with open(path, "r") as f:
            for row in csv.DictReader(f):
                bars.append(Bar(row["symbol"], row["timeframe"], int(row["ts_open"]),
                                float(row["open"]), float(row["high"]),
                                float(row["low"]), float(row["close"]), float(row["volume"])))
    except Exception:
        return []
    return bars


def build_4h_bars(bars_1h: List[Bar], symbol: str) -> List[Bar]:
    buckets: dict = {}
    for b in bars_1h:
        boundary = (b.ts_open // 14400) * 14400
        if boundary not in buckets:
            buckets[boundary] = Bar(symbol, "4h", boundary, b.open, b.high, b.low, b.close, b.volume)
        else:
            agg = buckets[boundary]
            agg.high = max(agg.high, b.high)
            agg.low = min(agg.low, b.low)
            agg.close = b.close
            agg.volume += b.volume
    return [buckets[k] for k in sorted(buckets)]


# -----------------------------------------------------------------------
# ADX Calculation (Wilder's smoothing)
# -----------------------------------------------------------------------
def calc_adx(bars: List[Bar], period: int = 14) -> List[Optional[float]]:
    """Calculate ADX using Wilder's smoothing. Returns list aligned with bars."""
    n = len(bars)
    if n < period * 2 + 1:
        return [None] * n

    # Directional Movement
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    tr = [0.0] * n

    for i in range(1, n):
        hi_diff = bars[i].high - bars[i - 1].high
        lo_diff = bars[i - 1].low - bars[i].low
        plus_dm[i] = hi_diff if hi_diff > lo_diff and hi_diff > 0 else 0.0
        minus_dm[i] = lo_diff if lo_diff > hi_diff and lo_diff > 0 else 0.0
        tr[i] = max(
            bars[i].high - bars[i].low,
            abs(bars[i].high - bars[i - 1].close),
            abs(bars[i].low - bars[i - 1].close)
        )

    # Wilder's smoothing for +DM, -DM, TR
    smoothed_plus = sum(plus_dm[1:period + 1])
    smoothed_minus = sum(minus_dm[1:period + 1])
    smoothed_tr = sum(tr[1:period + 1])

    adx_values = [None] * n
    dx_list = []

    for i in range(period, n):
        if i > period:
            smoothed_plus = smoothed_plus - smoothed_plus / period + plus_dm[i]
            smoothed_minus = smoothed_minus - smoothed_minus / period + minus_dm[i]
            smoothed_tr = smoothed_tr - smoothed_tr / period + tr[i]

        if smoothed_tr == 0:
            dx_list.append(0.0)
            continue

        plus_di = 100 * smoothed_plus / smoothed_tr
        minus_di = 100 * smoothed_minus / smoothed_tr
        di_sum = plus_di + minus_di

        dx = 100 * abs(plus_di - minus_di) / di_sum if di_sum > 0 else 0.0
        dx_list.append(dx)

        if len(dx_list) == period:
            adx = sum(dx_list) / period
            adx_values[i] = adx
        elif len(dx_list) > period:
            prev_adx = adx_values[i - 1] if adx_values[i - 1] is not None else sum(dx_list[-period:]) / period
            adx = (prev_adx * (period - 1) + dx) / period
            adx_values[i] = adx

    return adx_values


# -----------------------------------------------------------------------
# Volume Profile Calculation
# -----------------------------------------------------------------------
def calc_volume_profile(bars: List[Bar], num_bins: int = 50) -> Tuple[float, float, float]:
    """Calculate POC, VAH, VAL from a set of bars.

    Returns (poc_price, vah_price, val_price).
    Uses a fixed-range volume histogram.
    """
    if not bars:
        return 0.0, 0.0, 0.0

    lo = min(b.low for b in bars)
    hi = max(b.high for b in bars)
    if hi == lo:
        return lo, hi, lo

    bin_size = (hi - lo) / num_bins
    bins = [0.0] * num_bins

    for b in bars:
        # Distribute volume across bins the bar spans
        bar_lo_bin = max(0, int((b.low - lo) / bin_size))
        bar_hi_bin = min(num_bins - 1, int((b.high - lo) / bin_size))
        n_bins_touched = bar_hi_bin - bar_lo_bin + 1
        vol_per_bin = b.volume / n_bins_touched if n_bins_touched > 0 else 0
        for bi in range(bar_lo_bin, bar_hi_bin + 1):
            bins[bi] += vol_per_bin

    # POC = bin with highest volume
    poc_bin = max(range(num_bins), key=lambda i: bins[i])
    poc_price = lo + (poc_bin + 0.5) * bin_size

    # Value Area = 70% of total volume, expanding from POC
    total_vol = sum(bins)
    va_target = total_vol * 0.70
    va_vol = bins[poc_bin]
    va_lo_bin = poc_bin
    va_hi_bin = poc_bin

    while va_vol < va_target and (va_lo_bin > 0 or va_hi_bin < num_bins - 1):
        expand_up = bins[va_hi_bin + 1] if va_hi_bin < num_bins - 1 else 0
        expand_down = bins[va_lo_bin - 1] if va_lo_bin > 0 else 0

        if expand_up >= expand_down and va_hi_bin < num_bins - 1:
            va_hi_bin += 1
            va_vol += bins[va_hi_bin]
        elif va_lo_bin > 0:
            va_lo_bin -= 1
            va_vol += bins[va_lo_bin]
        elif va_hi_bin < num_bins - 1:
            va_hi_bin += 1
            va_vol += bins[va_hi_bin]
        else:
            break

    val_price = lo + va_lo_bin * bin_size
    vah_price = lo + (va_hi_bin + 1) * bin_size

    return poc_price, vah_price, val_price


# -----------------------------------------------------------------------
# VPMR Backtest Engine
# -----------------------------------------------------------------------
class VMPREngine:
    def __init__(self, params: dict = None):
        self.p = {**DEFAULT_PARAMS, **(params or {})}
        self.equity = PORTFOLIO_VALUE
        self.peak_equity = PORTFOLIO_VALUE
        self.max_drawdown = 0.0

        # Position state
        self.in_position = False
        self.position_side = ""  # "long" or "short"
        self.entry_price = 0.0
        self.stop_price = 0.0
        self.tp_price = 0.0
        self.position_size = 0.0
        self.bars_in_position = 0

        # ── Funnel Counters ──
        self.total_bars = 0
        self.bars_with_profile = 0

        # Regime
        self.adx_not_ready = 0
        self.adx_trending = 0
        self.adx_ranging = 0

        # Proximity
        self.near_vah = 0
        self.near_val = 0
        self.not_near_boundary = 0

        # RSI
        self.rsi_not_ready = 0
        self.rsi_long_pass = 0
        self.rsi_short_pass = 0
        self.rsi_fail = 0

        # Entries
        self.long_signals = 0
        self.short_signals = 0
        self.entries = 0

        # Exits
        self.exit_reasons = Counter()
        self.trade_pnls = []
        self.trade_durations = []
        self.trade_log = []

        # Profile stats
        self.va_widths = []
        self.adx_values_at_eval = []

    def run(self, bars_1h: List[Bar], bars_4h: List[Bar]):
        vp_lookback = self.p["vp_lookback_bars"]
        adx_period = self.p["adx_period"]

        # Pre-compute ADX on 4h bars
        adx_4h = calc_adx(bars_4h, adx_period)

        # Pre-compute RSI on 1h closes
        closes_1h = [b.close for b in bars_1h]
        rsi_1h = Indicators.calc_rsi(closes_1h, self.p["rsi_period"])

        # Pre-compute ATR on 1h bars
        atr_1h = Indicators.calc_atr(bars_1h, 14)

        for i in range(vp_lookback, len(bars_1h)):
            self.total_bars += 1
            current = bars_1h[i]

            # ── Check exits first ──
            if self.in_position:
                self._check_exit(current, i)
                if self.in_position:
                    continue  # still in position, skip entry logic

            # ── Volume Profile (rolling 24h window) ──
            vp_bars = bars_1h[i - vp_lookback:i]
            poc, vah, val = calc_volume_profile(vp_bars)
            if poc == 0:
                continue
            self.bars_with_profile += 1

            va_width = (vah - val) / current.close * 100  # as % of price
            self.va_widths.append(va_width)

            # ── Regime filter: ADX on 4h ──
            current_4h_boundary = (current.ts_open // 14400) * 14400
            adx_idx = None
            for j in range(len(bars_4h) - 1, -1, -1):
                if bars_4h[j].ts_open <= current_4h_boundary:
                    adx_idx = j
                    break

            if adx_idx is None or adx_4h[adx_idx] is None:
                self.adx_not_ready += 1
                continue

            current_adx = adx_4h[adx_idx]
            self.adx_values_at_eval.append(current_adx)

            if current_adx >= self.p["adx_max"]:
                self.adx_trending += 1
                continue
            self.adx_ranging += 1

            # ── Proximity to VA boundaries ──
            prox = self.p["entry_proximity_pct"]
            near_val_flag = current.close <= val * (1 + prox)
            near_vah_flag = current.close >= vah * (1 - prox)

            if near_val_flag:
                self.near_val += 1
            elif near_vah_flag:
                self.near_vah += 1
            else:
                self.not_near_boundary += 1
                continue

            # ── RSI confirmation ──
            if rsi_1h[i] is None:
                self.rsi_not_ready += 1
                continue

            current_rsi = rsi_1h[i]
            atr = atr_1h[i] if atr_1h[i] is not None else 0

            # Long setup: near VAL + RSI oversold
            if near_val_flag and current_rsi < self.p["rsi_oversold"] and self.p["allow_longs"]:
                self.rsi_long_pass += 1
                self.long_signals += 1
                self._enter_long(current, atr, poc, val, i)
                continue

            # Short setup: near VAH + RSI overbought
            if near_vah_flag and current_rsi > self.p["rsi_overbought"] and self.p["allow_shorts"]:
                self.rsi_short_pass += 1
                self.short_signals += 1
                self._enter_short(current, atr, poc, vah, i)
                continue

            self.rsi_fail += 1

    def _enter_long(self, bar: Bar, atr: float, poc: float, val: float, bar_idx: int):
        entry = bar.close * (1 + self.p["slippage_bps"] / 10000)
        stop = entry - self.p["stop_atr_mult"] * atr if atr > 0 else entry * 0.98
        tp = poc  # revert to POC

        if stop >= entry or tp <= entry:
            return

        risk_per_unit = entry - stop
        dollars_at_risk = self.equity * self.p["risk_per_trade"]
        size = dollars_at_risk / risk_per_unit

        fee = entry * size * (self.p["fee_bps"] / 10000)
        self.equity -= fee

        self.in_position = True
        self.position_side = "long"
        self.entry_price = entry
        self.stop_price = stop
        self.tp_price = tp
        self.position_size = size
        self.bars_in_position = 0
        self.entries += 1
        self._entry_bar_idx = bar_idx

    def _enter_short(self, bar: Bar, atr: float, poc: float, vah: float, bar_idx: int):
        entry = bar.close * (1 - self.p["slippage_bps"] / 10000)
        stop = entry + self.p["stop_atr_mult"] * atr if atr > 0 else entry * 1.02
        tp = poc  # revert to POC

        if stop <= entry or tp >= entry:
            return

        risk_per_unit = stop - entry
        dollars_at_risk = self.equity * self.p["risk_per_trade"]
        size = dollars_at_risk / risk_per_unit

        fee = entry * size * (self.p["fee_bps"] / 10000)
        self.equity -= fee

        self.in_position = True
        self.position_side = "short"
        self.entry_price = entry
        self.stop_price = stop
        self.tp_price = tp
        self.position_size = size
        self.bars_in_position = 0
        self.entries += 1
        self._entry_bar_idx = bar_idx

    def _check_exit(self, bar: Bar, bar_idx: int):
        self.bars_in_position += 1
        exit_price = 0.0
        reason = ""

        if self.position_side == "long":
            if bar.low <= self.stop_price:
                exit_price = self.stop_price
                reason = "STOP_LOSS"
            elif bar.high >= self.tp_price:
                exit_price = self.tp_price
                reason = "TAKE_PROFIT"
            elif self.bars_in_position >= self.p["time_stop_bars"]:
                exit_price = bar.close
                reason = "TIME_STOP"
        else:  # short
            if bar.high >= self.stop_price:
                exit_price = self.stop_price
                reason = "STOP_LOSS"
            elif bar.low <= self.tp_price:
                exit_price = self.tp_price
                reason = "TAKE_PROFIT"
            elif self.bars_in_position >= self.p["time_stop_bars"]:
                exit_price = bar.close
                reason = "TIME_STOP"

        if exit_price > 0:
            fee = exit_price * self.position_size * (self.p["fee_bps"] / 10000)

            if self.position_side == "long":
                pnl = (exit_price - self.entry_price) * self.position_size - fee
                pnl_pct = (exit_price - self.entry_price) / self.entry_price * 100
            else:
                pnl = (self.entry_price - exit_price) * self.position_size - fee
                pnl_pct = (self.entry_price - exit_price) / self.entry_price * 100

            self.equity += pnl
            self.peak_equity = max(self.peak_equity, self.equity)
            dd = (self.peak_equity - self.equity) / self.peak_equity * 100
            self.max_drawdown = max(self.max_drawdown, dd)

            self.trade_pnls.append(pnl_pct)
            self.trade_durations.append(self.bars_in_position)
            self.exit_reasons[reason] += 1

            ts_str = datetime.fromtimestamp(bar.ts_open, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            self.trade_log.append({
                "exit_ts": ts_str,
                "side": self.position_side,
                "entry": self.entry_price,
                "exit": exit_price,
                "pnl_pct": pnl_pct,
                "reason": reason,
                "bars_held": self.bars_in_position,
            })

            self.in_position = False
            self.position_side = ""


# -----------------------------------------------------------------------
# Reporting
# -----------------------------------------------------------------------
def print_vpmr_report(e: VMPREngine, symbol: str, days: int, label: str = "VPMR BASELINE"):
    print()
    print("=" * 75)
    print(f"  VPMR BACKTEST — {symbol} — {days}d — [{label}]")
    print("=" * 75)

    print()
    print("-" * 75)
    print("  FUNNEL AUDIT")
    print("-" * 75)
    print(f"  Total bars evaluated:          {e.total_bars}")
    print(f"  Bars with valid profile:       {e.bars_with_profile}")
    print()

    print(f"  REGIME FILTER (ADX < {e.p['adx_max']}):")
    print(f"    ADX not ready:               {e.adx_not_ready}")
    print(f"    ADX >= {e.p['adx_max']} (trending):       {e.adx_trending}")
    print(f"    ADX < {e.p['adx_max']} (ranging, pass):    {e.adx_ranging}")
    if e.bars_with_profile > 0:
        print(f"    Pass rate:                   {e.adx_ranging / e.bars_with_profile * 100:.1f}%")
    print()

    print(f"  PROXIMITY TO VA BOUNDARY:")
    print(f"    Near VAL (potential long):    {e.near_val}")
    print(f"    Near VAH (potential short):   {e.near_vah}")
    print(f"    Not near boundary (skip):    {e.not_near_boundary}")
    if e.adx_ranging > 0:
        boundary_hits = e.near_val + e.near_vah
        print(f"    Boundary hit rate:           {boundary_hits / e.adx_ranging * 100:.1f}%")
    print()

    print(f"  RSI CONFIRMATION:")
    print(f"    RSI not ready:               {e.rsi_not_ready}")
    print(f"    Long: RSI < {e.p['rsi_oversold']} (pass):       {e.rsi_long_pass}")
    print(f"    Short: RSI > {e.p['rsi_overbought']} (pass):      {e.rsi_short_pass}")
    print(f"    RSI fail (wrong range):      {e.rsi_fail}")
    print()

    print(f"  ENTRIES:")
    print(f"    Long signals:                {e.long_signals}")
    print(f"    Short signals:               {e.short_signals}")
    print(f"    Total entries:               {e.entries}")
    print()

    print("-" * 75)
    print("  TRADE RESULTS")
    print("-" * 75)
    print(f"  Trades executed:               {e.entries}")
    print(f"  Final equity:                  ${e.equity:,.2f}")
    print(f"  Total return:                  {(e.equity / PORTFOLIO_VALUE - 1) * 100:+.2f}%")
    print(f"  Max drawdown:                  {e.max_drawdown:.2f}%")
    print()

    if e.exit_reasons:
        print(f"  Exit breakdown:")
        for reason, count in e.exit_reasons.most_common():
            print(f"    {reason}: {count}")
        print()

    if e.trade_pnls:
        n = len(e.trade_pnls)
        wins = [p for p in e.trade_pnls if p > 0]
        losses = [p for p in e.trade_pnls if p <= 0]
        avg_pnl = sum(e.trade_pnls) / n
        gross_profit = sum(p for p in e.trade_pnls if p > 0)
        gross_loss = abs(sum(p for p in e.trade_pnls if p <= 0))
        pf = gross_profit / gross_loss if gross_loss > 0 else float('inf') if gross_profit > 0 else 0

        print(f"  Win rate:                      {len(wins)}/{n} ({len(wins) / n * 100:.1f}%)")
        print(f"  Avg PnL%:                      {avg_pnl:+.4f}%")
        print(f"  Profit factor:                 {pf:.2f}")
        if wins:
            print(f"  Avg win:                       {sum(wins) / len(wins):+.4f}%")
        if losses:
            print(f"  Avg loss:                      {sum(losses) / len(losses):+.4f}%")
        if e.trade_durations:
            print(f"  Avg bars held:                 {sum(e.trade_durations) / len(e.trade_durations):.1f}")
            print(f"  Min/Max bars held:             {min(e.trade_durations)}/{max(e.trade_durations)}")
        print()

        # Expectancy
        wr = len(wins) / n
        avg_w = sum(wins) / len(wins) if wins else 0
        avg_l = abs(sum(losses) / len(losses)) if losses else 0
        expectancy = wr * avg_w - (1 - wr) * avg_l
        print(f"  Expectancy:                    {expectancy:+.4f}%")
        print(f"  Expectancy after fees:         {expectancy:+.4f}% (fees already deducted)")
    else:
        print(f"  No trades executed.")
    print()

    # VA width distribution
    if e.va_widths:
        vw = e.va_widths
        print(f"  VALUE AREA WIDTH (% of price): n={len(vw)}")
        print(f"    Min: {min(vw):.3f}%  Max: {max(vw):.3f}%  Median: {sorted(vw)[len(vw)//2]:.3f}%")
        print()

    # ADX distribution
    if e.adx_values_at_eval:
        av = e.adx_values_at_eval
        below_25 = sum(1 for a in av if a < 25)
        print(f"  ADX DISTRIBUTION at eval: n={len(av)}")
        print(f"    Min: {min(av):.1f}  Max: {max(av):.1f}  Median: {sorted(av)[len(av)//2]:.1f}")
        print(f"    Below 25: {below_25}/{len(av)} ({below_25/len(av)*100:.1f}%)")
        print()

    # Trade log
    if e.trade_log:
        print("-" * 75)
        print(f"  TRADE LOG ({len(e.trade_log)} trades):")
        print(f"  {'#':>3}  {'Exit Time':>19}  {'Side':>5}  {'Entry':>10}  {'Exit':>10}  {'PnL%':>8}  {'Reason':>12}  {'Bars':>4}")
        print("  " + "-" * 80)
        for i, t in enumerate(e.trade_log, 1):
            print(f"  {i:3d}  {t['exit_ts']:>19}  {t['side']:>5}  ${t['entry']:>9,.2f}  ${t['exit']:>9,.2f}  "
                  f"{t['pnl_pct']:>+7.3f}%  {t['reason']:>12}  {t['bars_held']:>4}")
    print()

    # Funnel summary
    print("=" * 75)
    print("  FUNNEL SUMMARY")
    print("=" * 75)
    stages = [
        ("1. Total eval bars", e.total_bars),
        ("2. Valid volume profile", e.bars_with_profile),
        ("3. ADX < 25 (ranging)", e.adx_ranging),
        ("4. Near VA boundary", e.near_val + e.near_vah),
        ("5. RSI confirmed", e.rsi_long_pass + e.rsi_short_pass),
        ("6. Entries", e.entries),
    ]
    for label_s, count in stages:
        pct = count / e.total_bars * 100 if e.total_bars > 0 else 0
        bar_len = int(count / max(e.total_bars, 1) * 40)
        bar_s = "#" * max(bar_len, 1) if count > 0 else "."
        print(f"  {label_s:<30}  {count:>6}  ({pct:>7.3f}%)  {bar_s}")
    print("=" * 75)


def run_experiment(bars_1h, bars_4h, symbol, days, label, params):
    engine = VMPREngine(params)
    engine.run(bars_1h, bars_4h)
    print_vpmr_report(engine, symbol, days, label)
    return engine


def print_comparison(results: Dict[str, VMPREngine]):
    print()
    print("=" * 110)
    print("  VPMR EXPERIMENT COMPARISON TABLE")
    print("=" * 110)

    headers = list(results.keys())
    header_str = f"  {'Metric':<30}" + "".join(f"  {h:>12}" for h in headers)
    print(header_str)
    print("  " + "-" * (30 + 14 * len(headers)))

    metrics = [
        ("Total bars", lambda e: e.total_bars),
        ("ADX ranging", lambda e: e.adx_ranging),
        ("Near boundary", lambda e: e.near_val + e.near_vah),
        ("RSI confirmed", lambda e: e.rsi_long_pass + e.rsi_short_pass),
        ("Entries", lambda e: e.entries),
        ("Long / Short", lambda e: f"{e.long_signals}/{e.short_signals}"),
        ("Final equity", lambda e: f"${e.equity:,.0f}"),
        ("Max DD", lambda e: f"{e.max_drawdown:.2f}%"),
        ("Win rate", lambda e: f"{len([p for p in e.trade_pnls if p > 0])}/{len(e.trade_pnls)}" if e.trade_pnls else "N/A"),
        ("Avg PnL%", lambda e: f"{sum(e.trade_pnls)/len(e.trade_pnls):+.3f}%" if e.trade_pnls else "N/A"),
        ("Profit factor", lambda e: f"{sum(p for p in e.trade_pnls if p > 0) / abs(sum(p for p in e.trade_pnls if p <= 0)):.2f}" if e.trade_pnls and sum(p for p in e.trade_pnls if p <= 0) < 0 else "N/A"),
        ("Avg bars held", lambda e: f"{sum(e.trade_durations)/len(e.trade_durations):.1f}" if e.trade_durations else "N/A"),
    ]

    for name, fn in metrics:
        vals = []
        for key in headers:
            v = fn(results[key])
            if isinstance(v, (int, float)):
                vals.append(f"{v:>12}")
            else:
                vals.append(f"{v:>12}")
        print(f"  {name:<30}" + "".join(f"  {v}" for v in vals))

    print("=" * 110)

    # Verdict
    print()
    print("=" * 75)
    print("  VERDICT")
    print("=" * 75)
    for label_v, engine in results.items():
        n = engine.entries
        if n == 0:
            print(f"  {label_v:<20}  trades=0   INSUFFICIENT")
            continue
        wr = f"{len([p for p in engine.trade_pnls if p > 0])}/{n}"
        avg = sum(engine.trade_pnls) / n
        meets_floor = "YES" if n >= 30 else "NO"
        edge = "POSITIVE" if avg > 0 else "NEGATIVE"
        print(f"  {label_v:<20}  trades={n:<4}  WR={wr:<8}  avgPnL={avg:+.4f}%  "
              f"edge={edge:<8}  meets_30_floor={meets_floor}")
    print("=" * 75)


def main():
    parser = argparse.ArgumentParser(description="VPMR Backtest")
    parser.add_argument("--days", type=int, default=365, help="Days of history")
    parser.add_argument("--symbol", type=str, default="BTC-USD", help="Product ID")
    parser.add_argument("--experiments", action="store_true",
                        help="Run diagnostic experiments")
    args = parser.parse_args()

    bars_1h = fetch_1h_candles_public(args.symbol, args.days)
    bars_4h = build_4h_bars(bars_1h, args.symbol)

    print(f"  Built {len(bars_4h)} 4h bars from {len(bars_1h)} 1h bars.")
    print(f"  Date range: {datetime.fromtimestamp(bars_1h[0].ts_open, tz=timezone.utc).strftime('%Y-%m-%d')} "
          f"to {datetime.fromtimestamp(bars_1h[-1].ts_open, tz=timezone.utc).strftime('%Y-%m-%d')}")

    results = {}

    # ── BASELINE ──
    results["BASELINE"] = run_experiment(
        bars_1h, bars_4h, args.symbol, args.days, "VPMR BASELINE", DEFAULT_PARAMS)

    if args.experiments:
        print("\n\n" + "=" * 75)
        print("  RUNNING VPMR DIAGNOSTIC EXPERIMENTS")
        print("=" * 75)

        # A: Wider RSI bands (less strict confirmation)
        results["RSI_35_65"] = run_experiment(
            bars_1h, bars_4h, args.symbol, args.days,
            "A: RSI [35,65]",
            {**DEFAULT_PARAMS, "rsi_oversold": 35, "rsi_overbought": 65})

        # B: Wider RSI bands (moderate)
        results["RSI_40_60"] = run_experiment(
            bars_1h, bars_4h, args.symbol, args.days,
            "B: RSI [40,60]",
            {**DEFAULT_PARAMS, "rsi_oversold": 40, "rsi_overbought": 60})

        # C: Higher ADX threshold (more bars allowed)
        results["ADX_30"] = run_experiment(
            bars_1h, bars_4h, args.symbol, args.days,
            "C: ADX < 30",
            {**DEFAULT_PARAMS, "adx_max": 30})

        # D: Wider proximity (easier boundary matching)
        results["PROX_0.3%"] = run_experiment(
            bars_1h, bars_4h, args.symbol, args.days,
            "D: PROXIMITY 0.3%",
            {**DEFAULT_PARAMS, "entry_proximity_pct": 0.003})

        # E: Longs only (removes short complications)
        results["LONGS_ONLY"] = run_experiment(
            bars_1h, bars_4h, args.symbol, args.days,
            "E: LONGS ONLY",
            {**DEFAULT_PARAMS, "allow_shorts": False})

        # F: Combined relaxation
        results["RELAXED"] = run_experiment(
            bars_1h, bars_4h, args.symbol, args.days,
            "F: RELAXED",
            {
                **DEFAULT_PARAMS,
                "rsi_oversold": 35,
                "rsi_overbought": 65,
                "adx_max": 30,
                "entry_proximity_pct": 0.003,
            })

        # G: Max addressable
        results["MAX_ADDR"] = run_experiment(
            bars_1h, bars_4h, args.symbol, args.days,
            "G: MAX ADDRESSABLE",
            {
                **DEFAULT_PARAMS,
                "rsi_oversold": 40,
                "rsi_overbought": 60,
                "adx_max": 35,
                "entry_proximity_pct": 0.005,
            })

        print_comparison(results)

    print("\nDone.")


if __name__ == "__main__":
    main()
