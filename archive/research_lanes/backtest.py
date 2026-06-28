"""
research/backtest.py — No-lookahead bar-based backtest engine.

Core rule:
  - Signal on bar[i] → entry at bar[i+1] open (no lookahead).
  - All friction applied via FrictionModel.
  - Tracks MAE/MFE per trade.
  - Returns BacktestResult with full trade list + equity curve.
"""
import math
from typing import List, Optional

from .types import Bar, Signal, Trade, BacktestResult
from .costs import FrictionModel


def calc_atr(bars: List[Bar], period: int = 14) -> List[Optional[float]]:
    """ATR calculation. Returns list aligned with bars. None for warmup."""
    n = len(bars)
    result: List[Optional[float]] = [None] * n
    if n < 2:
        return result

    trs = [0.0] * n
    for i in range(1, n):
        trs[i] = max(
            bars[i].high - bars[i].low,
            abs(bars[i].high - bars[i - 1].close),
            abs(bars[i].low - bars[i - 1].close),
        )

    if n <= period:
        return result

    # Initial ATR = simple average
    atr_val = sum(trs[1:period + 1]) / period
    result[period] = atr_val

    for i in range(period + 1, n):
        atr_val = (atr_val * (period - 1) + trs[i]) / period
        result[i] = atr_val

    return result


def run_backtest(
    bars: List[Bar],
    signals: List[Signal],
    friction: FrictionModel,
    initial_equity: float = 10000.0,
    risk_per_trade: float = 0.01,
    stop_atr_mult: float = 2.0,
    tp_atr_mult: float = 3.0,
    time_stop_bars: int = 24,
    symbol: str = "",
    timeframe: str = "",
    rule_name: str = "",
    params: dict = None,
) -> BacktestResult:
    """
    Run a single backtest.

    Signal on bar[i] → entry at bar[i+1] open.
    Stop = entry ± stop_atr_mult * ATR.
    TP = entry ± tp_atr_mult * ATR.
    Time stop after time_stop_bars.
    """
    if params is None:
        params = {}

    atr = calc_atr(bars)
    equity = initial_equity
    peak_equity = initial_equity
    max_dd = 0.0
    equity_curve = [equity]
    trades: List[Trade] = []

    # Sort signals by bar index
    sorted_signals = sorted(signals, key=lambda s: s.bar_index)

    # Track position state
    in_position = False
    pos_direction = ""
    pos_entry_price = 0.0
    pos_stop = 0.0
    pos_tp = 0.0
    pos_size = 0.0
    pos_entry_bar = 0
    pos_bars_held = 0
    pos_entry_cost = 0.0
    pos_mae = 0.0  # worst unrealized loss (negative)
    pos_mfe = 0.0  # best unrealized gain (positive)

    signal_idx = 0  # pointer into sorted_signals

    for i in range(1, len(bars)):
        bar = bars[i]
        prev_bar = bars[i - 1]

        # ── Check for entry (signal was on bar[i-1], enter at bar[i] open) ──
        while signal_idx < len(sorted_signals):
            sig = sorted_signals[signal_idx]
            if sig.bar_index < i - 1:
                signal_idx += 1  # expired signal
                continue
            if sig.bar_index == i - 1 and not in_position:
                # Enter at this bar's open
                entry_atr = atr[i - 1]
                if entry_atr is None or entry_atr <= 0:
                    signal_idx += 1
                    break

                raw_entry = bar.open
                direction = sig.direction

                fill_price = friction.apply_entry_cost(raw_entry, direction, symbol)
                entry_cost_dollars = friction.entry_cost_dollars(
                    raw_entry, 1.0, symbol  # per-unit cost; scale by size below
                )

                # Stop and TP
                if direction == "long":
                    stop = fill_price - stop_atr_mult * entry_atr
                    tp = fill_price + tp_atr_mult * entry_atr
                    if stop >= fill_price or tp <= fill_price:
                        signal_idx += 1
                        break
                else:
                    stop = fill_price + stop_atr_mult * entry_atr
                    tp = fill_price - tp_atr_mult * entry_atr
                    if stop <= fill_price or tp >= fill_price:
                        signal_idx += 1
                        break

                # Position sizing
                risk_per_unit = abs(fill_price - stop)
                if risk_per_unit <= 0:
                    signal_idx += 1
                    break
                dollars_at_risk = equity * risk_per_trade
                size = dollars_at_risk / risk_per_unit

                # Deduct entry cost
                total_entry_cost = friction.entry_cost_dollars(raw_entry, size, symbol)
                equity -= total_entry_cost

                in_position = True
                pos_direction = direction
                pos_entry_price = fill_price
                pos_stop = stop
                pos_tp = tp
                pos_size = size
                pos_entry_bar = i
                pos_bars_held = 0
                pos_entry_cost = total_entry_cost
                pos_mae = 0.0
                pos_mfe = 0.0

                signal_idx += 1
                break
            else:
                break  # signal is for a future bar

        # ── Check exit if in position ──
        if in_position:
            pos_bars_held += 1

            # Update MAE/MFE
            if pos_direction == "long":
                unrealized_pct = (bar.close - pos_entry_price) / pos_entry_price
                worst_pct = (bar.low - pos_entry_price) / pos_entry_price
                best_pct = (bar.high - pos_entry_price) / pos_entry_price
            else:
                unrealized_pct = (pos_entry_price - bar.close) / pos_entry_price
                worst_pct = (pos_entry_price - bar.high) / pos_entry_price
                best_pct = (pos_entry_price - bar.low) / pos_entry_price

            pos_mae = min(pos_mae, worst_pct)
            pos_mfe = max(pos_mfe, best_pct)

            # Check exit conditions
            exit_price = 0.0
            exit_reason = ""

            if pos_direction == "long":
                if bar.low <= pos_stop:
                    exit_price = pos_stop
                    exit_reason = "STOP_LOSS"
                elif bar.high >= pos_tp:
                    exit_price = pos_tp
                    exit_reason = "TAKE_PROFIT"
                elif pos_bars_held >= time_stop_bars:
                    exit_price = bar.close
                    exit_reason = "TIME_STOP"
            else:
                if bar.high >= pos_stop:
                    exit_price = pos_stop
                    exit_reason = "STOP_LOSS"
                elif bar.low <= pos_tp:
                    exit_price = pos_tp
                    exit_reason = "TAKE_PROFIT"
                elif pos_bars_held >= time_stop_bars:
                    exit_price = bar.close
                    exit_reason = "TIME_STOP"

            if exit_price > 0:
                # Apply exit friction
                fill_exit = friction.apply_exit_cost(exit_price, pos_direction, symbol)
                exit_cost_dollars = friction.exit_cost_dollars(exit_price, pos_size, symbol)

                # P&L: entry friction is baked into pos_entry_price (fill_price at entry).
                # pos_entry_cost was already deducted from equity at entry time.
                # So here we just compute raw P&L from fill prices and deduct exit cost.
                if pos_direction == "long":
                    raw_pnl = (fill_exit - pos_entry_price) * pos_size
                else:
                    raw_pnl = (pos_entry_price - fill_exit) * pos_size

                net_pnl = raw_pnl - exit_cost_dollars
                equity += net_pnl

                # pnl_pct: total return on notional including all costs
                notional = pos_entry_price * pos_size
                total_costs = pos_entry_cost + exit_cost_dollars
                pnl_pct_final = (raw_pnl - total_costs) / notional if notional > 0 else 0

                peak_equity = max(peak_equity, equity)
                dd = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0
                max_dd = max(max_dd, dd)

                trades.append(Trade(
                    entry_bar=pos_entry_bar,
                    exit_bar=i,
                    direction=pos_direction,
                    entry_price=pos_entry_price,
                    exit_price=fill_exit,
                    stop_price=pos_stop,
                    size=pos_size,
                    pnl_dollar=raw_pnl - total_costs,
                    pnl_pct=pnl_pct_final,
                    entry_cost=pos_entry_cost,
                    exit_cost=exit_cost_dollars,
                    bars_held=pos_bars_held,
                    exit_reason=exit_reason,
                    mae_pct=pos_mae * 100,
                    mfe_pct=pos_mfe * 100,
                ))

                in_position = False

        equity_curve.append(equity)

    result = BacktestResult(
        symbol=symbol,
        timeframe=timeframe,
        rule_name=rule_name,
        params=params,
        trades=trades,
        equity_curve=equity_curve,
    )
    result.compute_metrics(initial_equity)

    return result
