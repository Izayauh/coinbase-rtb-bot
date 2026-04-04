#!/usr/bin/env python
"""
backtest.py — Offline strategy replay over historical Coinbase candles.

Drives the existing StateMachine + Indicators over fetched bars.
No DB, no adapters, no execution plumbing. Pure signal generation +
simulated fills with configurable fee/slippage assumptions.

Exit strategy (not implemented in the live bot yet):
  - initial stop   = retest_level - ATR
  - time stop      = 12 bars after entry (close at bar close)
  - trailing stop  = entry + 1.8 * ATR once price exceeds entry + 1.0 * ATR

Usage:
    python backtest.py                    # default: BTC-USD, 180 days
    python backtest.py --days 365
    python backtest.py --days 90 --symbol ETH-USD
"""
import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot.models import Bar
from bot.strategy import Indicators, is_bullish_regime

logging.basicConfig(level=logging.WARNING, format="%(message)s")
logger = logging.getLogger("BACKTEST")

# -----------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------
FEE_BPS = 10            # round-trip fee assumption (taker + maker)
SLIPPAGE_BPS = 5         # entry slippage
PORTFOLIO_VALUE = 10000.0
RISK_PER_TRADE = 0.002   # 0.20%
TIME_STOP_BARS = 12
TRAIL_TRIGGER_ATR = 1.0
TRAIL_DISTANCE_ATR = 1.8

# Strategy params (must match config.yaml / state_machine.py)
BREAKOUT_LOOKBACK = 20
BREAKOUT_VOLUME_MULT = 1.25
BREAKOUT_RSI_MIN = 56
BREAKOUT_RSI_MAX = 74
RETEST_WINDOW_BARS = 5
CONTINUATION_CHASE_ATR_MAX = 0.8


# -----------------------------------------------------------------------
# Data fetching
# -----------------------------------------------------------------------
def fetch_1h_candles(symbol: str, days: int) -> List[Bar]:
    """Fetch historical 1h candles from Coinbase REST API."""
    from coinbase.rest import RESTClient

    api_key = os.environ.get("COINBASE_API_KEY", "")
    api_secret = os.environ.get("COINBASE_API_SECRET", "")
    if not api_key or not api_secret:
        print("ERROR: COINBASE_API_KEY and COINBASE_API_SECRET must be set.")
        sys.exit(1)

    client = RESTClient(api_key=api_key, api_secret=api_secret)
    now = int(time.time())
    start = now - days * 86400

    all_bars: List[Bar] = []
    cursor = start
    chunk_size = 300 * 3600  # 300 candles * 1h

    print(f"Fetching {days} days of 1h candles for {symbol} ...")
    while cursor < now:
        chunk_end = min(cursor + chunk_size, now)
        resp = client.get_candles(
            product_id=symbol,
            start=str(cursor),
            end=str(chunk_end),
            granularity="ONE_HOUR",
        )
        candles = []
        if hasattr(resp, "candles") and resp.candles:
            candles = list(resp.candles)
        elif isinstance(resp, dict):
            candles = resp.get("candles", [])

        for c in candles:
            try:
                ts = int(c.start) if hasattr(c, "start") else int(c["start"])
                o = float(c.open) if hasattr(c, "open") else float(c["open"])
                h = float(c.high) if hasattr(c, "high") else float(c["high"])
                lo = float(c.low) if hasattr(c, "low") else float(c["low"])
                cl = float(c.close) if hasattr(c, "close") else float(c["close"])
                v = float(c.volume) if hasattr(c, "volume") else float(c["volume"])
                all_bars.append(Bar(symbol, "1h", ts, o, h, lo, cl, v))
            except Exception:
                continue
        cursor = chunk_end

    # deduplicate + sort
    seen = {}
    for b in all_bars:
        seen[b.ts_open] = b
    all_bars = [seen[k] for k in sorted(seen)]
    print(f"  Fetched {len(all_bars)} 1h bars.")
    return all_bars


def build_4h_bars(bars_1h: List[Bar], symbol: str) -> List[Bar]:
    """Aggregate 1h bars into 4h bars."""
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
# Trade records
# -----------------------------------------------------------------------
@dataclass
class Trade:
    entry_ts: int
    entry_price: float
    stop_price: float
    size: float
    atr_at_entry: float
    exit_ts: int = 0
    exit_price: float = 0.0
    exit_reason: str = ""
    pnl_usd: float = 0.0
    pnl_pct: float = 0.0
    bars_held: int = 0


# -----------------------------------------------------------------------
# Strategy replay engine
# -----------------------------------------------------------------------
class BacktestEngine:
    """
    Replays the exact strategy logic from state_machine.py + strategy.py
    over historical bars, simulating fills and exits.
    """
    IDLE = "IDLE"
    WAITING_RETEST = "WAITING_RETEST"
    RETEST_CONFIRMED = "RETEST_CONFIRMED"
    IN_POSITION = "IN_POSITION"

    def __init__(self, portfolio_value: float = PORTFOLIO_VALUE):
        self.portfolio_value = portfolio_value
        self.equity = portfolio_value
        self.state = self.IDLE

        # setup tracking
        self.breakout_bar: Optional[Bar] = None
        self.breakout_level = 0.0
        self.bars_since_breakout = 0
        self.retest_bar: Optional[Bar] = None

        # position tracking
        self.position: Optional[Trade] = None
        self.bars_in_position = 0
        self.trailing_active = False
        self.trail_stop = 0.0

        # results
        self.trades: List[Trade] = []
        self.equity_curve: List[float] = []

    def run(self, bars_1h: List[Bar], bars_4h: List[Bar]):
        """Replay strategy over bar-by-bar history."""
        if len(bars_1h) < 25 or len(bars_4h) < 205:
            print(f"ERROR: Need >= 25 1h bars and >= 205 4h bars. "
                  f"Have {len(bars_1h)} 1h, {len(bars_4h)} 4h.")
            return

        # Build a map of 4h bars by ts boundary for lookback
        bar_4h_by_boundary = {}
        for b in bars_4h:
            bar_4h_by_boundary[b.ts_open] = b

        # Walk 1h bars one at a time, building rolling context
        for i in range(25, len(bars_1h)):
            current_1h = bars_1h[i]
            context_1h = bars_1h[max(0, i - 29):i + 1]  # last 30 1h bars

            # Find matching 4h context up to this point
            current_4h_boundary = (current_1h.ts_open // 14400) * 14400
            context_4h = [b for b in bars_4h if b.ts_open <= current_4h_boundary]
            if len(context_4h) < 205:
                continue
            context_4h = context_4h[-210:]

            # Check open position first
            if self.position:
                self._check_exit(current_1h, context_1h)

            # Strategy evaluation (only when not in a position)
            if not self.position:
                if self.state == self.IDLE:
                    self._eval_breakout(context_1h, context_4h)
                elif self.state == self.WAITING_RETEST:
                    self._eval_retest(context_1h, context_4h)
                elif self.state == self.RETEST_CONFIRMED:
                    self._eval_continuation(context_1h, context_4h, current_1h)

            self.equity_curve.append(self.equity)

    def _eval_breakout(self, bars_1h: List[Bar], bars_4h: List[Bar]):
        if not is_bullish_regime(bars_4h):
            return

        latest = bars_1h[-1]
        past_20 = bars_1h[-21:-1]
        if len(past_20) < 20:
            return

        highest_20 = max(b.high for b in past_20)
        avg_vol_20 = sum(b.volume for b in past_20) / 20

        rsi = Indicators.calc_rsi([b.close for b in bars_1h])
        if rsi[-1] is None:
            return

        if latest.close <= highest_20:
            return
        if latest.volume <= BREAKOUT_VOLUME_MULT * avg_vol_20:
            return

        candle_range = latest.high - latest.low
        if candle_range == 0:
            return
        close_pct = (latest.close - latest.low) / candle_range
        if close_pct < 0.70:
            return
        if not (BREAKOUT_RSI_MIN <= rsi[-1] <= BREAKOUT_RSI_MAX):
            return

        self.state = self.WAITING_RETEST
        self.breakout_bar = latest
        self.breakout_level = highest_20
        self.bars_since_breakout = 0

    def _eval_retest(self, bars_1h: List[Bar], bars_4h: List[Bar]):
        if not is_bullish_regime(bars_4h):
            self._reset()
            return

        self.bars_since_breakout += 1
        if self.bars_since_breakout > RETEST_WINDOW_BARS:
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

        if touches and closes_above and closes_above_mid:
            self.state = self.RETEST_CONFIRMED
            self.retest_bar = latest

    def _eval_continuation(self, bars_1h: List[Bar], bars_4h: List[Bar], current: Bar):
        if not is_bullish_regime(bars_4h):
            self._reset()
            return

        if current.ts_open == self.retest_bar.ts_open:
            return

        if current.close > self.retest_bar.high:
            atr = Indicators.calc_atr(bars_1h, 14)[-1]
            if atr is None:
                self._reset()
                return

            if (current.close - self.breakout_level) > (CONTINUATION_CHASE_ATR_MAX * atr):
                self._reset()
                return

            # ENTRY
            entry_price = current.close * (1 + SLIPPAGE_BPS / 10000)
            stop_price = self.retest_bar.low - atr
            if stop_price >= entry_price:
                self._reset()
                return

            risk_per_unit = entry_price - stop_price
            dollars_at_risk = self.equity * RISK_PER_TRADE
            size = dollars_at_risk / risk_per_unit

            fee = entry_price * size * (FEE_BPS / 10000 / 2)  # half of round-trip

            self.position = Trade(
                entry_ts=current.ts_open,
                entry_price=entry_price,
                stop_price=stop_price,
                size=size,
                atr_at_entry=atr,
            )
            self.equity -= fee
            self.bars_in_position = 0
            self.trailing_active = False
            self.state = self.IN_POSITION

    def _check_exit(self, bar: Bar, bars_1h: List[Bar]):
        assert self.position is not None
        self.bars_in_position += 1
        pos = self.position

        exit_price = 0.0
        reason = ""

        # 1. Stop loss hit (bar low touches stop)
        if bar.low <= pos.stop_price:
            exit_price = pos.stop_price
            reason = "STOP_LOSS"

        # 2. Trailing stop
        elif self.trailing_active and bar.low <= self.trail_stop:
            exit_price = self.trail_stop
            reason = "TRAILING_STOP"

        # 3. Time stop
        elif self.bars_in_position >= TIME_STOP_BARS:
            exit_price = bar.close
            reason = "TIME_STOP"

        # Activate trailing stop if price exceeds trigger
        if not self.trailing_active:
            trigger = pos.entry_price + TRAIL_TRIGGER_ATR * pos.atr_at_entry
            if bar.high >= trigger:
                self.trailing_active = True
                self.trail_stop = pos.entry_price + (TRAIL_TRIGGER_ATR - TRAIL_DISTANCE_ATR) * pos.atr_at_entry
                self.trail_stop = max(self.trail_stop, pos.stop_price)

        # Update trailing stop
        if self.trailing_active and not reason:
            new_trail = bar.high - TRAIL_DISTANCE_ATR * pos.atr_at_entry
            if new_trail > self.trail_stop:
                self.trail_stop = new_trail

        if exit_price > 0:
            fee = exit_price * pos.size * (FEE_BPS / 10000 / 2)
            pnl = (exit_price - pos.entry_price) * pos.size - fee
            pnl_pct = pnl / self.equity * 100

            pos.exit_ts = bar.ts_open
            pos.exit_price = exit_price
            pos.exit_reason = reason
            pos.pnl_usd = pnl
            pos.pnl_pct = pnl_pct
            pos.bars_held = self.bars_in_position

            self.equity += pnl
            self.trades.append(pos)
            self.position = None
            self._reset()

    def _reset(self):
        self.state = self.IDLE
        self.breakout_bar = None
        self.breakout_level = 0.0
        self.bars_since_breakout = 0
        self.retest_bar = None


# -----------------------------------------------------------------------
# Reporting
# -----------------------------------------------------------------------
def print_report(engine: BacktestEngine, symbol: str, days: int):
    trades = engine.trades
    n = len(trades)
    print()
    print("=" * 60)
    print(f"  BACKTEST RESULTS — {symbol} — {days} days")
    print("=" * 60)
    print(f"  Fee assumption:      {FEE_BPS} bps round-trip")
    print(f"  Slippage assumption: {SLIPPAGE_BPS} bps entry")
    print(f"  Starting equity:     ${engine.portfolio_value:,.2f}")
    print(f"  Final equity:        ${engine.equity:,.2f}")
    print(f"  Total return:        {(engine.equity/engine.portfolio_value - 1)*100:+.2f}%")
    print()

    if n == 0:
        print("  TRADES: 0")
        print()
        print("  The strategy produced ZERO signals over the entire period.")
        print("  There is no evidence of any edge.")
        print("=" * 60)
        return

    wins = [t for t in trades if t.pnl_usd > 0]
    losses = [t for t in trades if t.pnl_usd <= 0]
    win_rate = len(wins) / n * 100

    avg_win = sum(t.pnl_usd for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t.pnl_usd for t in losses) / len(losses) if losses else 0
    avg_loss_abs = abs(avg_loss) if avg_loss != 0 else 1

    gross_profit = sum(t.pnl_usd for t in wins)
    gross_loss = abs(sum(t.pnl_usd for t in losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    expectancy = sum(t.pnl_usd for t in trades) / n
    expectancy_r = expectancy / avg_loss_abs if avg_loss_abs > 0 else 0

    # Max drawdown from equity curve
    peak = engine.portfolio_value
    max_dd = 0
    for eq in engine.equity_curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak
        if dd > max_dd:
            max_dd = dd

    # Exit reason breakdown
    exit_reasons = {}
    for t in trades:
        exit_reasons[t.exit_reason] = exit_reasons.get(t.exit_reason, 0) + 1

    avg_bars_held = sum(t.bars_held for t in trades) / n

    print(f"  Trades:              {n}")
    print(f"  Wins:                {len(wins)}")
    print(f"  Losses:              {len(losses)}")
    print(f"  Win rate:            {win_rate:.1f}%")
    print()
    print(f"  Avg win:             ${avg_win:+.2f}")
    print(f"  Avg loss:            ${avg_loss:+.2f}")
    print(f"  Avg win/avg loss:    {avg_win/avg_loss_abs:.2f}x")
    print(f"  Expectancy:          ${expectancy:+.2f}/trade")
    print(f"  Expectancy (R):      {expectancy_r:+.2f}R")
    print(f"  Profit factor:       {profit_factor:.2f}")
    print()
    print(f"  Max drawdown:        {max_dd*100:.2f}%")
    print(f"  Avg bars held:       {avg_bars_held:.1f}")
    print()
    print(f"  Exit reasons:")
    for reason, count in sorted(exit_reasons.items()):
        print(f"    {reason:<20}: {count}")
    print()

    # Minimum viability thresholds
    print("  --- Viability Assessment ---")
    issues = []
    if n < 30:
        issues.append(f"  FAIL: {n} trades — need >= 30 for statistical significance")
    if win_rate < 35:
        issues.append(f"  FAIL: {win_rate:.1f}% win rate — below 35% minimum")
    if expectancy <= 0:
        issues.append(f"  FAIL: ${expectancy:+.2f} expectancy — strategy loses money")
    if profit_factor < 1.0:
        issues.append(f"  FAIL: {profit_factor:.2f} profit factor — below breakeven")
    if max_dd > 0.15:
        issues.append(f"  WARN: {max_dd*100:.1f}% max drawdown — exceeds 15% threshold")
    if avg_win / avg_loss_abs < 1.5 and win_rate < 55:
        issues.append(f"  FAIL: Win/loss ratio {avg_win/avg_loss_abs:.2f} with {win_rate:.0f}% win rate — math doesn't work")

    if not issues:
        print("  PASS: Meets minimum viability thresholds.")
        print("  (This is necessary but NOT sufficient for live deployment.)")
    else:
        for issue in issues:
            print(issue)

    print("=" * 60)

    # Print individual trades
    print()
    print("  Individual Trades:")
    print(f"  {'#':>3}  {'Entry Date':>19}  {'Entry':>10}  {'Exit':>10}  {'PnL':>10}  {'Bars':>5}  {'Reason'}")
    print("  " + "-" * 85)
    for i, t in enumerate(trades, 1):
        entry_dt = datetime.fromtimestamp(t.entry_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        print(f"  {i:3d}  {entry_dt:>19}  ${t.entry_price:>9,.2f}  ${t.exit_price:>9,.2f}  "
              f"${t.pnl_usd:>+9.2f}  {t.bars_held:5d}  {t.exit_reason}")
    print()


# -----------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Backtest the breakout-retest strategy")
    parser.add_argument("--days", type=int, default=180, help="Days of history (default: 180)")
    parser.add_argument("--symbol", type=str, default="BTC-USD", help="Product ID (default: BTC-USD)")
    args = parser.parse_args()

    bars_1h = fetch_1h_candles(args.symbol, args.days)
    bars_4h = build_4h_bars(bars_1h, args.symbol)

    print(f"  Built {len(bars_4h)} 4h bars from {len(bars_1h)} 1h bars.")
    print(f"  Evaluation window: {len(bars_1h) - 25} bars after warmup.")

    engine = BacktestEngine()
    engine.run(bars_1h, bars_4h)

    print_report(engine, args.symbol, args.days)


if __name__ == "__main__":
    main()
