#!/usr/bin/env python
"""
ema_crossover_backtest.py — Dynamic EMA Crossover strategy backtest & funnel audit.

Research Pack Rank 2 strategy:
  - 8/34 EMA crossover on 1h bars
  - RSI [50-65] for longs, [35-50] for shorts
  - ADX >= 25 regime filter (trending markets only)
  - Initial stop: min(swing low, 2× ATR from entry)
  - Trailing stop: after 1.5× ATR profit, trail at 1× ATR
  - 4-hour cooldown after stop-out in same direction
  - Position sizing: 1.0% of equity

Why higher frequency than breakout-retest:
  - EMA cross only requires 2 EMAs to cross + RSI in band (2 conditions)
  - Breakout-retest required 7+ sequential conditions
  - Crossovers happen dozens of times per year on 1h BTC-USD
  - Operates in trending periods (ADX >= 25) — complementary to VPMR's ranging filter

Usage:
    python ema_crossover_backtest.py
    python ema_crossover_backtest.py --diagnostic
"""
import argparse
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from typing import List, Optional, Dict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot.models import Bar
from bot.strategy import Indicators
from vpmr_backtest import (
    fetch_1h_candles_public, build_4h_bars, calc_adx, PORTFOLIO_VALUE
)

# -----------------------------------------------------------------------
# Strategy Parameters
# -----------------------------------------------------------------------
DEFAULT_PARAMS = {
    # EMA
    "ema_fast": 8,
    "ema_slow": 34,

    # Regime
    "adx_period": 14,
    "adx_min": 25,             # ADX must be >= 25 (trending)

    # RSI confirmation
    "rsi_period": 14,
    "rsi_long_min": 50,        # long RSI band: [50, 65]
    "rsi_long_max": 65,
    "rsi_short_min": 35,       # short RSI band: [35, 50]
    "rsi_short_max": 50,

    # Stop & exit
    "stop_atr_mult": 2.0,      # initial stop: 2× ATR (or swing low if tighter)
    "swing_lookback": 10,      # bars to look back for swing low/high
    "trail_trigger_atr": 1.5,  # activate trailing after 1.5× ATR profit
    "trail_distance_atr": 1.0, # trail at 1× ATR distance

    # Time stop (bars)
    "time_stop_bars": 24,      # 24 hours max hold for trend trades

    # Cooldown
    "cooldown_bars": 4,        # 4-bar cooldown after stop-out (same direction)

    # Risk
    "risk_per_trade": 0.010,   # 1.0% of equity
    "slippage_bps": 5,         # taker (market order after 3-bar limit miss)
    "fee_bps": 10,             # taker fee assumption (conservative)

    # Direction
    "allow_longs": True,
    "allow_shorts": True,
}


# -----------------------------------------------------------------------
# Engine
# -----------------------------------------------------------------------
class EMACrossEngine:
    def __init__(self, params: dict = None):
        self.p = {**DEFAULT_PARAMS, **(params or {})}
        self.equity = PORTFOLIO_VALUE
        self.peak_equity = PORTFOLIO_VALUE
        self.max_drawdown = 0.0

        # Position
        self.in_position = False
        self.position_side = ""
        self.entry_price = 0.0
        self.stop_price = 0.0
        self.position_size = 0.0
        self.bars_in_position = 0
        self.trailing_active = False
        self.trail_stop = 0.0
        self.position_atr = 0.0

        # Cooldown
        self.cooldown_long = 0
        self.cooldown_short = 0

        # Previous EMA state for cross detection
        self.prev_fast_above_slow = None

        # ── Funnel Counters ──
        self.total_bars = 0
        self.warmup_skip = 0

        # Cross detection
        self.bullish_crosses = 0
        self.bearish_crosses = 0

        # Regime
        self.adx_not_ready = 0
        self.adx_fail = 0
        self.adx_pass = 0

        # RSI
        self.rsi_not_ready = 0
        self.rsi_pass = 0
        self.rsi_fail = 0

        # Cooldown kills
        self.cooldown_kills = 0

        # Entries
        self.long_entries = 0
        self.short_entries = 0
        self.entries = 0
        self.entry_skipped_bad_stop = 0

        # Exits
        self.exit_reasons = Counter()
        self.trade_pnls = []
        self.trade_durations = []
        self.equity_curve = []

    def run(self, bars_1h: List[Bar], bars_4h: List[Bar]):
        ema_fast_period = self.p["ema_fast"]
        ema_slow_period = self.p["ema_slow"]

        # Pre-compute indicators
        closes_1h = [b.close for b in bars_1h]
        ema_fast = Indicators.calc_ema(closes_1h, ema_fast_period)
        ema_slow = Indicators.calc_ema(closes_1h, ema_slow_period)
        rsi_1h = Indicators.calc_rsi(closes_1h, self.p["rsi_period"])
        atr_1h = Indicators.calc_atr(bars_1h, 14)
        adx_4h = calc_adx(bars_4h, self.p["adx_period"])

        start_idx = max(ema_slow_period + 1, 15)  # need EMA warmup

        for i in range(start_idx, len(bars_1h)):
            self.total_bars += 1
            current = bars_1h[i]

            # Track equity
            self.equity_curve.append(self.equity)

            # Tick cooldowns
            if self.cooldown_long > 0:
                self.cooldown_long -= 1
            if self.cooldown_short > 0:
                self.cooldown_short -= 1

            # ── Exit check ──
            if self.in_position:
                self._check_exit(current, atr_1h[i])
                if self.in_position:
                    continue

            # ── EMA values ──
            if ema_fast[i] is None or ema_slow[i] is None:
                self.warmup_skip += 1
                continue
            if ema_fast[i - 1] is None or ema_slow[i - 1] is None:
                self.warmup_skip += 1
                continue

            fast_now = ema_fast[i]
            slow_now = ema_slow[i]
            fast_prev = ema_fast[i - 1]
            slow_prev = ema_slow[i - 1]

            # Detect cross
            bullish_cross = fast_prev <= slow_prev and fast_now > slow_now
            bearish_cross = fast_prev >= slow_prev and fast_now < slow_now

            if not bullish_cross and not bearish_cross:
                continue  # no cross this bar

            if bullish_cross:
                self.bullish_crosses += 1
            if bearish_cross:
                self.bearish_crosses += 1

            # ── ADX regime filter ──
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
            if current_adx < self.p["adx_min"]:
                self.adx_fail += 1
                continue
            self.adx_pass += 1

            # ── RSI confirmation ──
            if rsi_1h[i] is None:
                self.rsi_not_ready += 1
                continue

            current_rsi = rsi_1h[i]
            atr = atr_1h[i] if atr_1h[i] is not None else 0

            if bullish_cross and self.p["allow_longs"]:
                if self.p["rsi_long_min"] <= current_rsi <= self.p["rsi_long_max"]:
                    self.rsi_pass += 1
                    # Cooldown check
                    if self.cooldown_long > 0:
                        self.cooldown_kills += 1
                        continue
                    self._enter(current, bars_1h, i, atr, "long")
                else:
                    self.rsi_fail += 1
                continue

            if bearish_cross and self.p["allow_shorts"]:
                if self.p["rsi_short_min"] <= current_rsi <= self.p["rsi_short_max"]:
                    self.rsi_pass += 1
                    if self.cooldown_short > 0:
                        self.cooldown_kills += 1
                        continue
                    self._enter(current, bars_1h, i, atr, "short")
                else:
                    self.rsi_fail += 1

    def _enter(self, bar: Bar, bars_1h: List[Bar], idx: int, atr: float, side: str):
        entry = bar.close * (1 + self.p["slippage_bps"] / 10000) if side == "long" \
            else bar.close * (1 - self.p["slippage_bps"] / 10000)

        # Stop: min(swing extreme, 2× ATR) — whichever is tighter
        lookback = self.p["swing_lookback"]
        recent = bars_1h[max(0, idx - lookback):idx]

        if side == "long":
            swing_stop = min(b.low for b in recent) if recent else entry * 0.98
            atr_stop = entry - self.p["stop_atr_mult"] * atr if atr > 0 else entry * 0.98
            stop = max(swing_stop, atr_stop)  # tighter = higher for longs
            if stop >= entry:
                self.entry_skipped_bad_stop += 1
                return
        else:
            swing_stop = max(b.high for b in recent) if recent else entry * 1.02
            atr_stop = entry + self.p["stop_atr_mult"] * atr if atr > 0 else entry * 1.02
            stop = min(swing_stop, atr_stop)  # tighter = lower for shorts
            if stop <= entry:
                self.entry_skipped_bad_stop += 1
                return

        risk_per_unit = abs(entry - stop)
        if risk_per_unit == 0:
            return
        dollars_at_risk = self.equity * self.p["risk_per_trade"]
        size = dollars_at_risk / risk_per_unit

        fee = entry * size * (self.p["fee_bps"] / 10000)
        self.equity -= fee

        self.in_position = True
        self.position_side = side
        self.entry_price = entry
        self.stop_price = stop
        self.position_size = size
        self.position_atr = atr
        self.bars_in_position = 0
        self.trailing_active = False
        self.trail_stop = 0.0
        self.entries += 1
        if side == "long":
            self.long_entries += 1
        else:
            self.short_entries += 1

    def _check_exit(self, bar: Bar, atr: Optional[float]):
        self.bars_in_position += 1
        exit_price = 0.0
        reason = ""
        pos_atr = self.position_atr

        if self.position_side == "long":
            # Check stop
            effective_stop = self.trail_stop if self.trailing_active else self.stop_price
            if bar.low <= effective_stop:
                exit_price = effective_stop
                reason = "TRAILING_STOP" if self.trailing_active else "STOP_LOSS"
            elif self.bars_in_position >= self.p["time_stop_bars"]:
                exit_price = bar.close
                reason = "TIME_STOP"

            # Update trailing
            if not self.trailing_active and pos_atr > 0:
                trigger = self.entry_price + self.p["trail_trigger_atr"] * pos_atr
                if bar.high >= trigger:
                    self.trailing_active = True
                    self.trail_stop = self.entry_price  # move to breakeven
            if self.trailing_active and not reason:
                new_trail = bar.high - self.p["trail_distance_atr"] * pos_atr
                if new_trail > self.trail_stop:
                    self.trail_stop = new_trail

        else:  # short
            effective_stop = self.trail_stop if self.trailing_active else self.stop_price
            if bar.high >= effective_stop:
                exit_price = effective_stop
                reason = "TRAILING_STOP" if self.trailing_active else "STOP_LOSS"
            elif self.bars_in_position >= self.p["time_stop_bars"]:
                exit_price = bar.close
                reason = "TIME_STOP"

            if not self.trailing_active and pos_atr > 0:
                trigger = self.entry_price - self.p["trail_trigger_atr"] * pos_atr
                if bar.low <= trigger:
                    self.trailing_active = True
                    self.trail_stop = self.entry_price  # breakeven
            if self.trailing_active and not reason:
                new_trail = bar.low + self.p["trail_distance_atr"] * pos_atr
                if new_trail < self.trail_stop:
                    self.trail_stop = new_trail

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

            # Set cooldown
            if reason in ("STOP_LOSS", "TRAILING_STOP"):
                if self.position_side == "long":
                    self.cooldown_long = self.p["cooldown_bars"]
                else:
                    self.cooldown_short = self.p["cooldown_bars"]

            self.in_position = False
            self.position_side = ""


# -----------------------------------------------------------------------
# Reporting
# -----------------------------------------------------------------------
def summarize(engine: EMACrossEngine) -> dict:
    e = engine
    n = len(e.trade_pnls)
    if n == 0:
        return {"trades": 0, "longs": 0, "shorts": 0, "win_rate": "N/A",
                "avg_pnl": 0, "pf": 0, "max_dd": 0, "avg_hold": 0,
                "expectancy": 0, "equity": e.equity}

    wins = [p for p in e.trade_pnls if p > 0]
    losses = [p for p in e.trade_pnls if p <= 0]
    gross_p = sum(wins) if wins else 0
    gross_l = abs(sum(losses)) if losses else 0
    pf = gross_p / gross_l if gross_l > 0 else (float('inf') if gross_p > 0 else 0)
    avg_pnl = sum(e.trade_pnls) / n

    wr_n = len(wins)
    avg_w = sum(wins) / wr_n if wr_n else 0
    avg_l_val = abs(sum(losses) / len(losses)) if losses else 0
    expectancy = (wr_n / n) * avg_w - (1 - wr_n / n) * avg_l_val

    return {
        "trades": n, "longs": e.long_entries, "shorts": e.short_entries,
        "win_rate": f"{wr_n}/{n} ({wr_n/n*100:.1f}%)",
        "avg_pnl": avg_pnl, "pf": pf, "max_dd": e.max_drawdown,
        "avg_hold": sum(e.trade_durations) / n if e.trade_durations else 0,
        "expectancy": expectancy, "equity": e.equity,
    }


def print_report(e: EMACrossEngine, label: str, days: int):
    s = summarize(e)
    print()
    print("=" * 80)
    print(f"  EMA CROSSOVER — {label} — {days}d")
    print("=" * 80)

    # Funnel
    print()
    print("-" * 80)
    print("  FUNNEL AUDIT")
    print("-" * 80)
    total_crosses = e.bullish_crosses + e.bearish_crosses
    print(f"  Total eval bars:           {e.total_bars}")
    print(f"  Warmup skipped:            {e.warmup_skip}")
    print(f"  Bullish crosses:           {e.bullish_crosses}")
    print(f"  Bearish crosses:           {e.bearish_crosses}")
    print(f"  Total crosses:             {total_crosses}")
    print()
    print(f"  ADX >= {e.p['adx_min']}:")
    print(f"    Not ready:               {e.adx_not_ready}")
    print(f"    FAIL (not trending):     {e.adx_fail}")
    print(f"    PASS (trending):         {e.adx_pass}")
    if total_crosses > 0:
        print(f"    Pass rate:               {e.adx_pass/total_crosses*100:.1f}%")
    print()
    print(f"  RSI confirmation:")
    print(f"    Not ready:               {e.rsi_not_ready}")
    print(f"    PASS:                    {e.rsi_pass}")
    print(f"    FAIL:                    {e.rsi_fail}")
    if e.adx_pass > 0:
        print(f"    Pass rate:               {e.rsi_pass/e.adx_pass*100:.1f}%")
    print(f"  Cooldown kills:            {e.cooldown_kills}")
    print(f"  Bad stop geometry:         {e.entry_skipped_bad_stop}")
    print(f"  ENTRIES:                   {e.entries} (L={e.long_entries} S={e.short_entries})")
    print()

    # Results
    print("-" * 80)
    print("  TRADE RESULTS")
    print("-" * 80)
    print(f"  Trades:                    {s['trades']}")
    print(f"  Win rate:                  {s['win_rate']}")
    print(f"  Avg PnL%:                  {s['avg_pnl']:+.4f}%")
    print(f"  Profit factor:             {s['pf']:.2f}")
    print(f"  Max drawdown:              {s['max_dd']:.2f}%")
    print(f"  Avg bars held:             {s['avg_hold']:.1f}")
    print(f"  Expectancy:                {s['expectancy']:+.4f}%")
    print(f"  Final equity:              ${s['equity']:,.2f}")
    print(f"  Total return:              {(s['equity']/PORTFOLIO_VALUE - 1)*100:+.2f}%")
    if e.exit_reasons:
        print(f"  Exits: {dict(e.exit_reasons)}")
    print()

    # Funnel summary
    print("=" * 80)
    print("  FUNNEL SUMMARY")
    print("=" * 80)
    stages = [
        ("1. Total bars", e.total_bars),
        ("2. EMA crosses", total_crosses),
        ("3. ADX pass", e.adx_pass),
        ("4. RSI pass", e.rsi_pass),
        ("5. After cooldown", e.rsi_pass - e.cooldown_kills),
        ("6. Entries", e.entries),
    ]
    for lbl, count in stages:
        pct = count / e.total_bars * 100 if e.total_bars > 0 else 0
        bar_len = int(count / max(e.total_bars, 1) * 40)
        bar_s = "#" * max(bar_len, 1) if count > 0 else "."
        print(f"  {lbl:<25}  {count:>6}  ({pct:>7.3f}%)  {bar_s}")
    print("=" * 80)


def print_comparison_grid(variants: Dict[str, dict], horizons: List[int]):
    """Print compact grid: variants × horizons."""
    print()
    print("=" * 130)
    print("  EMA CROSSOVER — COMPARISON GRID")
    print("=" * 130)

    # Build header
    hz_labels = [f"── {d}d ──" for d in horizons]
    print(f"  {'Variant':<20}", end="")
    for h in horizons:
        print(f" | {'Trades':>6} {'WR':>11} {'AvgPnL%':>9} {'PF':>6} {'MaxDD':>8} {'AvgHld':>6} {'Equity':>10}", end="")
    print()
    print(f"  {'':20}", end="")
    for h in horizons:
        print(f" | {'── ' + str(h) + '-DAY ':─<60}", end="")
    print()
    print("  " + "-" * 126)

    for variant_label in list(next(iter(variants.values())).keys()):
        print(f"  {variant_label:<20}", end="")
        for days in horizons:
            s = variants[days][variant_label]
            print(f" | {s['trades']:>6} {s['win_rate']:>11} {s['avg_pnl']:>+8.4f}% {s['pf']:>5.2f} {s['max_dd']:>7.2f}% "
                  f"{s['avg_hold']:>5.1f}  ${s['equity']:>9,.0f}", end="")
        print()

    print("=" * 130)


def print_verdict(variants: Dict[str, dict], horizons: List[int]):
    """Hard pass/fail against criteria."""
    print()
    print("=" * 80)
    print("  VERDICT")
    print("=" * 80)

    any_pass = False
    for variant_label in list(next(iter(variants.values())).keys()):
        s365 = variants[365][variant_label]
        s730 = variants[730][variant_label]

        pf_ok = s365["pf"] >= 1.2
        dd_ok = s365["max_dd"] < 25
        edge_ok = s365["avg_pnl"] > 0.03
        stable = s730["pf"] >= 1.0 and s730["avg_pnl"] > 0
        trades_ok = s365["trades"] >= 30

        checks = {
            "PF>=1.2": pf_ok, "DD<25%": dd_ok, "PnL>0.03%": edge_ok,
            "730d_ok": stable, "N>=30": trades_ok,
        }
        all_pass = all(checks.values())
        if all_pass:
            any_pass = True

        detail = "  ".join(f"{k}={'Y' if v else 'N'}" for k, v in checks.items())
        status = "PASS" if all_pass else "FAIL"
        print(f"  {variant_label:<20}  {status:<6}  {detail}")

    print()
    print("-" * 80)
    if any_pass:
        print("  DECISION: CONTINUE — proceed to walk-forward validation")
    else:
        # Check if any variant is close enough for diagnostic
        close_enough = False
        for variant_label in list(next(iter(variants.values())).keys()):
            s365 = variants[365][variant_label]
            s730 = variants[730][variant_label]
            if (s365["trades"] >= 30 and s365["pf"] >= 1.0 and s365["avg_pnl"] > 0
                    and s730["avg_pnl"] > -0.05):
                close_enough = True
                break

        if close_enough:
            print("  DECISION: REVISE ONCE — baseline shows marginal promise,")
            print("            one diagnostic cycle with tighter parameters permitted")
        else:
            print("  DECISION: ARCHIVE — no variant shows meaningful edge")
            print("            Move to Statistical Volatility Reversion (Rank 3)")
    print("=" * 80)


def main():
    parser = argparse.ArgumentParser(description="EMA Crossover Backtest")
    parser.add_argument("--diagnostic", action="store_true",
                        help="Run diagnostic variants if baseline is marginal")
    args = parser.parse_args()

    symbol = "BTC-USD"
    horizons = [365, 730]

    # Define variants
    variant_defs = {
        "BASELINE": {},
    }

    if args.diagnostic:
        variant_defs.update({
            # Wider RSI bands
            "WIDE_RSI": {"rsi_long_min": 45, "rsi_long_max": 70,
                         "rsi_short_min": 30, "rsi_short_max": 55},
            # Tighter stop (1.5 ATR instead of 2.0)
            "TIGHT_STOP": {"stop_atr_mult": 1.5},
            # No cooldown
            "NO_COOL": {"cooldown_bars": 0},
            # Combined: wider RSI + no cooldown
            "WIDE+NOCOOL": {"rsi_long_min": 45, "rsi_long_max": 70,
                            "rsi_short_min": 30, "rsi_short_max": 55,
                            "cooldown_bars": 0},
            # Lower ADX threshold
            "ADX_20": {"adx_min": 20},
            # Longs only
            "LONGS_ONLY": {"allow_shorts": False},
        })

    all_summaries: Dict[int, dict] = {}

    for days in horizons:
        print(f"\n{'='*80}")
        print(f"  LOADING {days}-DAY DATA")
        print(f"{'='*80}")

        bars_1h = fetch_1h_candles_public(symbol, days)
        bars_4h = build_4h_bars(bars_1h, symbol)
        print(f"  {len(bars_1h)} 1h bars → {len(bars_4h)} 4h bars")

        day_summaries = {}
        for label, overrides in variant_defs.items():
            params = {**DEFAULT_PARAMS, **overrides}
            engine = EMACrossEngine(params)
            engine.run(bars_1h, bars_4h)
            print_report(engine, f"{label} [{days}d]", days)
            day_summaries[label] = summarize(engine)

        all_summaries[days] = day_summaries

    # ── Comparison grid ──
    print_comparison_grid(all_summaries, horizons)

    # ── Cross-strategy comparison ──
    print()
    print("=" * 80)
    print("  CROSS-STRATEGY COMPARISON (365d)")
    print("=" * 80)
    ema_s = all_summaries[365]["BASELINE"]
    print(f"  {'Strategy':<35} {'Trades':>7} {'WR':>10} {'AvgPnL%':>9} {'PF':>6} {'MaxDD':>8}")
    print("  " + "-" * 75)
    print(f"  {'Breakout-Retest (archived)':<35} {'1':>7} {'0/1':>10} {'-0.200%':>9} {'0.00':>6} {'N/A':>8}")
    print(f"  {'VPMR (archived)':<35} {'149':>7} {'59/149':>10} {'-0.039%':>9} {'0.92':>6} {'38.70%':>8}")
    print(f"  {'EMA Crossover BASELINE':<35} {ema_s['trades']:>7} {ema_s['win_rate']:>10} "
          f"{ema_s['avg_pnl']:>+8.3f}% {ema_s['pf']:>5.2f} {ema_s['max_dd']:>7.2f}%")
    if args.diagnostic and "WIDE_RSI" in all_summaries[365]:
        for lbl in variant_defs:
            if lbl == "BASELINE":
                continue
            s = all_summaries[365][lbl]
            print(f"  {'EMA ' + lbl:<35} {s['trades']:>7} {s['win_rate']:>10} "
                  f"{s['avg_pnl']:>+8.3f}% {s['pf']:>5.2f} {s['max_dd']:>7.2f}%")
    print("=" * 80)

    # ── Verdict ──
    print_verdict(all_summaries, horizons)

    print("\nDone.")


if __name__ == "__main__":
    main()
