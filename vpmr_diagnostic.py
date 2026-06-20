#!/usr/bin/env python
"""
vpmr_diagnostic.py — VPMR one-shot diagnostic cycle.

Tests exactly 4 VPMR variants × 2 time horizons (365d, 730d):
  1. BASELINE         (current VPMR params)
  2. CONFIRM          (require reversal bar after boundary touch)
  3. TIGHT_STOP       (1.0× ATR stop instead of 1.5×)
  4. CONFIRM+TIGHT    (both changes combined)

Decision threshold:
  - PF >= 1.2 AND max DD < 25% AND positive expectancy AND stable on 730d
  - Otherwise: ARCHIVE VPMR

Usage:
    python vpmr_diagnostic.py
"""
import csv
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from typing import List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot.models import Bar
from bot.strategy import Indicators

# ── Reuse helpers from vpmr_backtest ──
from vpmr_backtest import (
    fetch_1h_candles_public, build_4h_bars, calc_adx, calc_volume_profile,
    PORTFOLIO_VALUE
)

# -----------------------------------------------------------------------
# Parameters
# -----------------------------------------------------------------------
BASE_PARAMS = {
    "vp_lookback_bars": 24,
    "value_area_pct": 0.70,
    "adx_period": 14,
    "adx_max": 25,
    "rsi_period": 14,
    "rsi_oversold": 30,
    "rsi_overbought": 70,
    "entry_proximity_pct": 0.001,
    "stop_atr_mult": 1.5,
    "time_stop_bars": 12,
    "risk_per_trade": 0.015,
    "slippage_bps": 3,
    "fee_bps": 4,
    "allow_longs": True,
    "allow_shorts": True,
    # Diagnostic additions
    "require_confirmation": False,  # NEW: require reversal bar
}

# -----------------------------------------------------------------------
# Engine with confirmation bar support
# -----------------------------------------------------------------------
class DiagnosticEngine:
    def __init__(self, params: dict):
        self.p = {**BASE_PARAMS, **params}
        self.equity = PORTFOLIO_VALUE
        self.peak_equity = PORTFOLIO_VALUE
        self.max_drawdown = 0.0

        # Position
        self.in_position = False
        self.position_side = ""
        self.entry_price = 0.0
        self.stop_price = 0.0
        self.tp_price = 0.0
        self.position_size = 0.0
        self.bars_in_position = 0

        # Pending confirmation
        self.pending_side = ""       # "long" or "short" waiting for confirmation
        self.pending_val = 0.0
        self.pending_vah = 0.0
        self.pending_poc = 0.0
        self.pending_atr = 0.0
        self.pending_bar_idx = 0

        # Counters
        self.total_bars = 0
        self.setups_found = 0
        self.confirmation_pass = 0
        self.confirmation_fail = 0
        self.entries = 0
        self.long_entries = 0
        self.short_entries = 0
        self.exit_reasons = Counter()
        self.trade_pnls = []
        self.trade_durations = []
        self.equity_curve = []

    def run(self, bars_1h: List[Bar], bars_4h: List[Bar]):
        vp_lookback = self.p["vp_lookback_bars"]
        adx_4h = calc_adx(bars_4h, self.p["adx_period"])
        closes_1h = [b.close for b in bars_1h]
        rsi_1h = Indicators.calc_rsi(closes_1h, self.p["rsi_period"])
        atr_1h = Indicators.calc_atr(bars_1h, 14)

        for i in range(vp_lookback, len(bars_1h)):
            self.total_bars += 1
            current = bars_1h[i]

            # Track equity curve
            self.equity_curve.append(self.equity)

            # ── Exits first ──
            if self.in_position:
                self._check_exit(current)
                if self.in_position:
                    # Check for pending confirmation while in position — skip
                    self.pending_side = ""
                    continue

            # ── Check pending confirmation ──
            if self.pending_side and self.p["require_confirmation"]:
                confirmed = self._check_confirmation(current, bars_1h, i, atr_1h)
                if confirmed:
                    self.confirmation_pass += 1
                else:
                    self.confirmation_fail += 1
                    self.pending_side = ""
                continue  # Whether confirmed or not, consumed this bar

            # ── Volume Profile ──
            vp_bars = bars_1h[i - vp_lookback:i]
            poc, vah, val = calc_volume_profile(vp_bars)
            if poc == 0:
                continue

            # ── ADX regime ──
            current_4h_boundary = (current.ts_open // 14400) * 14400
            adx_idx = None
            for j in range(len(bars_4h) - 1, -1, -1):
                if bars_4h[j].ts_open <= current_4h_boundary:
                    adx_idx = j
                    break
            if adx_idx is None or adx_4h[adx_idx] is None:
                continue
            if adx_4h[adx_idx] >= self.p["adx_max"]:
                continue

            # ── Proximity ──
            prox = self.p["entry_proximity_pct"]
            near_val = current.close <= val * (1 + prox)
            near_vah = current.close >= vah * (1 - prox)

            if not near_val and not near_vah:
                continue

            # ── RSI ──
            if rsi_1h[i] is None:
                continue

            atr = atr_1h[i] if atr_1h[i] is not None else 0

            # Long setup
            if near_val and rsi_1h[i] < self.p["rsi_oversold"] and self.p["allow_longs"]:
                self.setups_found += 1
                if self.p["require_confirmation"]:
                    # Set pending — check next bar for reversal
                    self.pending_side = "long"
                    self.pending_val = val
                    self.pending_vah = vah
                    self.pending_poc = poc
                    self.pending_atr = atr
                    self.pending_bar_idx = i
                else:
                    self._enter_long(current, atr, poc, val)
                continue

            # Short setup
            if near_vah and rsi_1h[i] > self.p["rsi_overbought"] and self.p["allow_shorts"]:
                self.setups_found += 1
                if self.p["require_confirmation"]:
                    self.pending_side = "short"
                    self.pending_val = val
                    self.pending_vah = vah
                    self.pending_poc = poc
                    self.pending_atr = atr
                    self.pending_bar_idx = i
                else:
                    self._enter_short(current, atr, poc, vah)
                continue

    def _check_confirmation(self, bar: Bar, bars_1h: List[Bar], i: int, atr_1h) -> bool:
        """Check if the bar confirms a reversal back inside the Value Area."""
        atr = atr_1h[i] if atr_1h[i] is not None else self.pending_atr

        if self.pending_side == "long":
            # Confirmation: this bar closes ABOVE the VAL (moved back inside VA)
            if bar.close > self.pending_val:
                self._enter_long(bar, atr, self.pending_poc, self.pending_val)
                self.pending_side = ""
                return True
            self.pending_side = ""
            return False

        elif self.pending_side == "short":
            # Confirmation: this bar closes BELOW the VAH (moved back inside VA)
            if bar.close < self.pending_vah:
                self._enter_short(bar, atr, self.pending_poc, self.pending_vah)
                self.pending_side = ""
                return True
            self.pending_side = ""
            return False

        self.pending_side = ""
        return False

    def _enter_long(self, bar: Bar, atr: float, poc: float, val: float):
        entry = bar.close * (1 + self.p["slippage_bps"] / 10000)
        stop = entry - self.p["stop_atr_mult"] * atr if atr > 0 else entry * 0.98
        tp = poc

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
        self.long_entries += 1

    def _enter_short(self, bar: Bar, atr: float, poc: float, vah: float):
        entry = bar.close * (1 - self.p["slippage_bps"] / 10000)
        stop = entry + self.p["stop_atr_mult"] * atr if atr > 0 else entry * 1.02
        tp = poc

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
        self.short_entries += 1

    def _check_exit(self, bar: Bar):
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
        else:
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

            self.in_position = False
            self.position_side = ""


# -----------------------------------------------------------------------
# Compact reporting
# -----------------------------------------------------------------------
def summarize(engine: DiagnosticEngine, label: str) -> dict:
    """Extract key metrics into a dict."""
    e = engine
    n = len(e.trade_pnls)
    if n == 0:
        return {
            "label": label, "trades": 0, "longs": 0, "shorts": 0,
            "win_rate": "N/A", "avg_pnl": 0, "pf": 0, "max_dd": 0,
            "avg_hold": 0, "expectancy": 0, "equity": e.equity,
            "setups": e.setups_found, "conf_pass": e.confirmation_pass,
            "conf_fail": e.confirmation_fail,
            "exits": dict(e.exit_reasons),
        }

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
        "label": label,
        "trades": n,
        "longs": e.long_entries,
        "shorts": e.short_entries,
        "win_rate": f"{wr_n}/{n} ({wr_n/n*100:.1f}%)",
        "avg_pnl": avg_pnl,
        "pf": pf,
        "max_dd": e.max_drawdown,
        "avg_hold": sum(e.trade_durations) / n if e.trade_durations else 0,
        "expectancy": expectancy,
        "equity": e.equity,
        "setups": e.setups_found,
        "conf_pass": e.confirmation_pass,
        "conf_fail": e.confirmation_fail,
        "exits": dict(e.exit_reasons),
    }


def print_grid(results_365: list, results_730: list):
    """Print a compact grid of all variants × both horizons."""
    print()
    print("=" * 120)
    print("  VPMR DIAGNOSTIC CYCLE — COMPARISON GRID")
    print("=" * 120)

    # Header
    print(f"  {'Variant':<20} | {'Trades':>6} {'WR':>12} {'AvgPnL%':>9} {'PF':>6} {'MaxDD':>8} "
          f"{'AvgHold':>7} {'Expect%':>9} {'Equity':>10} | {'Trades':>6} {'WR':>12} {'AvgPnL%':>9} {'PF':>6} {'MaxDD':>8} "
          f"{'Equity':>10}")
    print(f"  {'':20} | {'──── 365-DAY ────':^65} | {'──── 730-DAY ────':^57}")
    print("  " + "-" * 116)

    for r365, r730 in zip(results_365, results_730):
        label = r365["label"]
        print(f"  {label:<20} | "
              f"{r365['trades']:>6} {r365['win_rate']:>12} {r365['avg_pnl']:>+8.4f}% {r365['pf']:>5.2f} {r365['max_dd']:>7.2f}% "
              f"{r365['avg_hold']:>6.1f}  {r365['expectancy']:>+8.4f}% ${r365['equity']:>9,.0f} | "
              f"{r730['trades']:>6} {r730['win_rate']:>12} {r730['avg_pnl']:>+8.4f}% {r730['pf']:>5.2f} {r730['max_dd']:>7.2f}% "
              f"${r730['equity']:>9,.0f}")

    print("=" * 120)

    # Exit breakdown
    print()
    print("  EXIT BREAKDOWN (365d):")
    for r in results_365:
        exits_str = "  ".join(f"{k}={v}" for k, v in sorted(r["exits"].items()))
        print(f"    {r['label']:<20}: {exits_str}")

    # Confirmation stats
    print()
    print("  CONFIRMATION STATS (365d):")
    for r in results_365:
        if r["conf_pass"] + r["conf_fail"] > 0:
            total = r["conf_pass"] + r["conf_fail"]
            print(f"    {r['label']:<20}: setups={r['setups']}  confirm_pass={r['conf_pass']}  "
                  f"confirm_fail={r['conf_fail']}  pass_rate={r['conf_pass']/total*100:.1f}%")
        else:
            print(f"    {r['label']:<20}: no confirmation required (direct entry)")


def print_verdict(results_365: list, results_730: list):
    """Print the hard verdict based on predefined criteria."""
    print()
    print("=" * 80)
    print("  DIAGNOSTIC VERDICT")
    print("=" * 80)

    any_pass = False
    for r365, r730 in zip(results_365, results_730):
        label = r365["label"]
        pf_ok = r365["pf"] >= 1.2
        dd_ok = r365["max_dd"] < 25
        edge_ok = r365["avg_pnl"] > 0.03  # "non-trivial margin"
        stable = r730["pf"] >= 1.0 and r730["avg_pnl"] > 0  # doesn't collapse
        trades_ok = r365["trades"] >= 30

        checks = {
            "PF >= 1.2": pf_ok,
            "DD < 25%": dd_ok,
            "AvgPnL > 0.03%": edge_ok,
            "730d stable": stable,
            "Trades >= 30": trades_ok,
        }

        all_pass = all(checks.values())
        if all_pass:
            any_pass = True

        status = "PASS ✓" if all_pass else "FAIL"
        detail = "  ".join(f"{k}={'Y' if v else 'N'}" for k, v in checks.items())
        print(f"  {label:<20}  {status:<8}  {detail}")

    print()
    print("-" * 80)
    if any_pass:
        print("  DECISION: CONTINUE — at least one variant meets all criteria.")
        print("  NEXT: Proceed to walk-forward validation on the passing variant(s).")
    else:
        print("  DECISION: ARCHIVE VPMR")
        print()
        print("  No variant meets ALL of: PF >= 1.2, DD < 25%, AvgPnL > 0.03%, 730d stability.")
        print("  The strategy has adequate frequency but NO robust edge.")
        print("  Do not iterate further. Move to Dynamic EMA Crossover (Rank 2) from Research Pack.")
    print("=" * 80)


def main():
    symbol = "BTC-USD"

    # ── Define the 4 variants ──
    variants = {
        "BASELINE": {},
        "CONFIRM": {"require_confirmation": True},
        "TIGHT_STOP": {"stop_atr_mult": 1.0},
        "CONFIRM+TIGHT": {"require_confirmation": True, "stop_atr_mult": 1.0},
    }

    # ── Run on both horizons ──
    results_365 = []
    results_730 = []

    for days in [365, 730]:
        print(f"\n{'='*80}")
        print(f"  FETCHING {days}-DAY DATA")
        print(f"{'='*80}")

        bars_1h = fetch_1h_candles_public(symbol, days)
        bars_4h = build_4h_bars(bars_1h, symbol)
        print(f"  {len(bars_1h)} 1h bars → {len(bars_4h)} 4h bars")
        print(f"  Range: {datetime.fromtimestamp(bars_1h[0].ts_open, tz=timezone.utc).strftime('%Y-%m-%d')} "
              f"to {datetime.fromtimestamp(bars_1h[-1].ts_open, tz=timezone.utc).strftime('%Y-%m-%d')}")

        for label, overrides in variants.items():
            params = {**BASE_PARAMS, **overrides}
            engine = DiagnosticEngine(params)
            engine.run(bars_1h, bars_4h)
            s = summarize(engine, label)

            print(f"\n  [{days}d] {label}: trades={s['trades']}  WR={s['win_rate']}  "
                  f"avgPnL={s['avg_pnl']:+.4f}%  PF={s['pf']:.2f}  DD={s['max_dd']:.2f}%  "
                  f"equity=${s['equity']:,.0f}")

            if days == 365:
                results_365.append(s)
            else:
                results_730.append(s)

    # ── Print grid and verdict ──
    print_grid(results_365, results_730)
    print_verdict(results_365, results_730)
    print("\nDone.")


if __name__ == "__main__":
    main()
