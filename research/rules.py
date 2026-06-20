"""
research/rules.py — Rule families for the research harness.

Original pilot families:
  1. Momentum: EMA crossover + RSI band confirmation
  2. Mean Reversion: Bollinger Band touch + RSI extreme

Option A spot-only exploration families:
  3. Failed Breakdown Rebound: downside range break that quickly reclaims
  4. Volatility Shock Reversion: outsized selloff with capitulation/reclaim
  5. Range Expansion Continuation: breakout close with expanding range/volume

Rules are stateless functions: bars + params → List[Signal].
No position tracking, no exit logic (that's the backtest engine's job).
"""
import math
from typing import List, Optional

from .types import Bar, Signal


# -----------------------------------------------------------------------
# Indicator helpers (self-contained, no bot/ dependency)
# -----------------------------------------------------------------------
def _ema(values: List[float], period: int) -> List[Optional[float]]:
    """Exponential moving average. Returns None for warmup bars."""
    result: List[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return result

    # Seed with SMA
    sma = sum(values[:period]) / period
    result[period - 1] = sma
    mult = 2.0 / (period + 1)

    for i in range(period, len(values)):
        prev = result[i - 1]
        if prev is None:
            result[i] = values[i]
        else:
            result[i] = (values[i] - prev) * mult + prev

    return result


def _sma(values: List[float], period: int) -> List[Optional[float]]:
    """Simple moving average."""
    result: List[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return result

    window_sum = sum(values[:period])
    result[period - 1] = window_sum / period

    for i in range(period, len(values)):
        window_sum += values[i] - values[i - period]
        result[i] = window_sum / period

    return result


def _rsi(values: List[float], period: int = 14) -> List[Optional[float]]:
    """RSI using Wilder's smoothing."""
    result: List[Optional[float]] = [None] * len(values)
    if len(values) < period + 1:
        return result

    gains = []
    losses = []
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0:
        result[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        result[period] = 100 - 100 / (1 + rs)

    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]
        gain = max(change, 0)
        loss = max(-change, 0)

        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

        if avg_loss == 0:
            result[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            result[i] = 100 - 100 / (1 + rs)

    return result


def _stddev(values: List[float], period: int) -> List[Optional[float]]:
    """Population standard deviation over rolling window."""
    result: List[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return result

    for i in range(period - 1, len(values)):
        window = values[i - period + 1:i + 1]
        mean = sum(window) / period
        variance = sum((x - mean) ** 2 for x in window) / period
        result[i] = math.sqrt(variance)

    return result


def _rolling_max(values: List[float], period: int) -> List[Optional[float]]:
    """Rolling maximum over the last `period` values inclusive."""
    result: List[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return result

    for i in range(period - 1, len(values)):
        result[i] = max(values[i - period + 1:i + 1])
    return result


def _rolling_min(values: List[float], period: int) -> List[Optional[float]]:
    """Rolling minimum over the last `period` values inclusive."""
    result: List[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return result

    for i in range(period - 1, len(values)):
        result[i] = min(values[i - period + 1:i + 1])
    return result


# -----------------------------------------------------------------------
# Rule Family 1: Momentum (EMA Crossover)
# -----------------------------------------------------------------------
def momentum_ema_cross(
    bars: List[Bar],
    fast_period: int = 8,
    slow_period: int = 34,
    rsi_long_min: float = 50,
    rsi_long_max: float = 65,
    rsi_short_min: float = 35,
    rsi_short_max: float = 50,
    rsi_period: int = 14,
) -> List[Signal]:
    """
    EMA crossover with RSI band confirmation.

    Long: fast crosses above slow + RSI in [rsi_long_min, rsi_long_max].
    Short: fast crosses below slow + RSI in [rsi_short_min, rsi_short_max].
    """
    closes = [b.close for b in bars]
    ema_f = _ema(closes, fast_period)
    ema_s = _ema(closes, slow_period)
    rsi = _rsi(closes, rsi_period)

    signals: List[Signal] = []
    start = max(slow_period + 1, rsi_period + 1)

    for i in range(start, len(bars)):
        if any(v is None for v in [ema_f[i], ema_s[i], ema_f[i-1], ema_s[i-1], rsi[i]]):
            continue

        # Bullish cross
        if ema_f[i - 1] <= ema_s[i - 1] and ema_f[i] > ema_s[i]:
            if rsi_long_min <= rsi[i] <= rsi_long_max:
                signals.append(Signal(
                    bar_index=i,
                    direction="long",
                    rule_name="momentum_ema_cross",
                    params={
                        "fast": fast_period, "slow": slow_period,
                        "rsi_lo": rsi_long_min, "rsi_hi": rsi_long_max,
                    },
                ))

        # Bearish cross
        elif ema_f[i - 1] >= ema_s[i - 1] and ema_f[i] < ema_s[i]:
            if rsi_short_min <= rsi[i] <= rsi_short_max:
                signals.append(Signal(
                    bar_index=i,
                    direction="short",
                    rule_name="momentum_ema_cross",
                    params={
                        "fast": fast_period, "slow": slow_period,
                        "rsi_lo": rsi_short_min, "rsi_hi": rsi_short_max,
                    },
                ))

    return signals


# -----------------------------------------------------------------------
# Rule Family 2: Mean Reversion (Bollinger Band)
# -----------------------------------------------------------------------
def mean_reversion_bbands(
    bars: List[Bar],
    bb_period: int = 20,
    bb_sigma: float = 2.0,
    rsi_oversold: float = 30,
    rsi_overbought: float = 70,
    rsi_period: int = 14,
) -> List[Signal]:
    """
    Bollinger Band mean reversion with RSI extreme confirmation.

    Long: close < lower band AND RSI < rsi_oversold.
    Short: close > upper band AND RSI > rsi_overbought.
    """
    closes = [b.close for b in bars]
    sma = _sma(closes, bb_period)
    std = _stddev(closes, bb_period)
    rsi = _rsi(closes, rsi_period)

    signals: List[Signal] = []
    start = max(bb_period, rsi_period + 1)

    for i in range(start, len(bars)):
        if any(v is None for v in [sma[i], std[i], rsi[i]]):
            continue
        if std[i] == 0:
            continue

        upper = sma[i] + bb_sigma * std[i]
        lower = sma[i] - bb_sigma * std[i]

        # Long: close below lower band + RSI oversold
        if closes[i] < lower and rsi[i] < rsi_oversold:
            signals.append(Signal(
                bar_index=i,
                direction="long",
                rule_name="mean_reversion_bbands",
                params={
                    "bb_period": bb_period, "bb_sigma": bb_sigma,
                    "rsi_thresh": rsi_oversold,
                },
            ))

        # Short: close above upper band + RSI overbought
        elif closes[i] > upper and rsi[i] > rsi_overbought:
            signals.append(Signal(
                bar_index=i,
                direction="short",
                rule_name="mean_reversion_bbands",
                params={
                    "bb_period": bb_period, "bb_sigma": bb_sigma,
                    "rsi_thresh": rsi_overbought,
                },
            ))

    return signals


# -----------------------------------------------------------------------
# Rule Family 3: Failed Breakdown Rebound (spot-only long)
# -----------------------------------------------------------------------
def failed_breakdown_rebound(
    bars: List[Bar],
    lookback: int = 24,
    atr_period: int = 14,
    min_breach_atr: float = 0.15,
    min_close_reclaim: float = 0.25,
    max_close_distance_atr: float = 0.75,
) -> List[Signal]:
    """
    Long-only spring / failed-breakdown setup.

    Trigger when the current bar:
      - undercuts the lowest low of the prior lookback window,
      - closes back above that prior support,
      - and finishes far enough off the low to imply rejection.
    """
    closes = [b.close for b in bars]
    lows = [b.low for b in bars]
    highs = [b.high for b in bars]
    atr_like = _ema([h - l for h, l in zip(highs, lows)], atr_period)

    signals: List[Signal] = []
    start = max(lookback + 1, atr_period + 1)

    for i in range(start, len(bars)):
        prior_support = min(lows[i - lookback:i])
        bar_range = highs[i] - lows[i]
        atr_val = atr_like[i]
        if atr_val is None or atr_val <= 0 or bar_range <= 0:
            continue

        breach = prior_support - lows[i]
        if breach < min_breach_atr * atr_val:
            continue
        if closes[i] <= prior_support:
            continue

        reclaim_ratio = (closes[i] - lows[i]) / bar_range
        close_distance_atr = (closes[i] - prior_support) / atr_val
        if reclaim_ratio < min_close_reclaim:
            continue
        if close_distance_atr > max_close_distance_atr:
            continue

        signals.append(Signal(
            bar_index=i,
            direction="long",
            rule_name="failed_breakdown_rebound",
            params={
                "lookback": lookback,
                "min_breach_atr": min_breach_atr,
                "min_close_reclaim": min_close_reclaim,
                "max_close_distance_atr": max_close_distance_atr,
            },
        ))

    return signals


# -----------------------------------------------------------------------
# Rule Family 4: Volatility Shock Reversion (spot-only long)
# -----------------------------------------------------------------------
def volatility_shock_reversion(
    bars: List[Bar],
    atr_period: int = 14,
    shock_atr_mult: float = 1.5,
    min_close_off_low: float = 0.35,
    volume_mult: float = 1.2,
    volume_period: int = 20,
) -> List[Signal]:
    """
    Long-only capitulation/reversal setup.

    Trigger when a bar has an unusually large true range, elevated volume,
    and a close that recovers materially from the intrabar washout.
    """
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    closes = [b.close for b in bars]
    volumes = [b.volume for b in bars]
    ranges = [h - l for h, l in zip(highs, lows)]
    atr_like = _ema(ranges, atr_period)
    vol_sma = _sma(volumes, volume_period)

    signals: List[Signal] = []
    start = max(atr_period + 1, volume_period + 1)

    for i in range(start, len(bars)):
        atr_val = atr_like[i]
        avg_vol = vol_sma[i]
        bar_range = ranges[i]
        if atr_val is None or avg_vol is None or atr_val <= 0 or avg_vol <= 0 or bar_range <= 0:
            continue

        drop_from_prev_close = bars[i - 1].close - closes[i]
        close_off_low = (closes[i] - lows[i]) / bar_range

        if bar_range < shock_atr_mult * atr_val:
            continue
        if drop_from_prev_close <= 0:
            continue
        if close_off_low < min_close_off_low:
            continue
        if volumes[i] < volume_mult * avg_vol:
            continue

        signals.append(Signal(
            bar_index=i,
            direction="long",
            rule_name="volatility_shock_reversion",
            params={
                "shock_atr_mult": shock_atr_mult,
                "min_close_off_low": min_close_off_low,
                "volume_mult": volume_mult,
            },
        ))

    return signals


# -----------------------------------------------------------------------
# Rule Family 5: Range Expansion Continuation (spot-only long)
# -----------------------------------------------------------------------
def range_expansion_continuation(
    bars: List[Bar],
    lookback: int = 24,
    atr_period: int = 14,
    breakout_buffer_atr: float = 0.1,
    range_atr_mult: float = 1.2,
    close_in_bar_min: float = 0.7,
    volume_mult: float = 1.0,
    volume_period: int = 20,
) -> List[Signal]:
    """
    Long-only breakout continuation setup.

    Trigger when price closes above the prior lookback high with an expanded
    range, a strong close location, and at least average/above-average volume.
    """
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    closes = [b.close for b in bars]
    volumes = [b.volume for b in bars]
    ranges = [h - l for h, l in zip(highs, lows)]
    atr_like = _ema(ranges, atr_period)
    vol_sma = _sma(volumes, volume_period)

    signals: List[Signal] = []
    start = max(lookback + 1, atr_period + 1, volume_period + 1)

    for i in range(start, len(bars)):
        atr_val = atr_like[i]
        avg_vol = vol_sma[i]
        bar_range = ranges[i]
        if atr_val is None or avg_vol is None or atr_val <= 0 or avg_vol <= 0 or bar_range <= 0:
            continue

        prior_high = max(highs[i - lookback:i])
        close_location = (closes[i] - lows[i]) / bar_range

        if closes[i] <= prior_high + breakout_buffer_atr * atr_val:
            continue
        if bar_range < range_atr_mult * atr_val:
            continue
        if close_location < close_in_bar_min:
            continue
        if volumes[i] < volume_mult * avg_vol:
            continue

        signals.append(Signal(
            bar_index=i,
            direction="long",
            rule_name="range_expansion_continuation",
            params={
                "lookback": lookback,
                "breakout_buffer_atr": breakout_buffer_atr,
                "range_atr_mult": range_atr_mult,
                "close_in_bar_min": close_in_bar_min,
                "volume_mult": volume_mult,
            },
        ))

    return signals


# -----------------------------------------------------------------------
# Rule Registry + Parameter Grids
# -----------------------------------------------------------------------
MOMENTUM_GRID = {
    "fast_period": [5, 8, 13],
    "slow_period": [21, 34, 55],
    "rsi_long_min": [45, 50, 55],
    "rsi_long_max": [60, 65, 70],
    # rsi_short bands mirror: [35,30,25] to [50,45,40]
}

MEAN_REVERSION_GRID = {
    "bb_period": [14, 20],
    "bb_sigma": [2.0, 2.5],
    "rsi_oversold": [25, 30, 35],
    # rsi_overbought mirrors: [75, 70, 65]
}

FAILED_BREAKDOWN_GRID = {
    "lookback": [24, 48],
    "min_breach_atr": [0.10, 0.25],
    "min_close_reclaim": [0.25, 0.45],
    "max_close_distance_atr": [0.60, 0.90],
}

VOL_SHOCK_GRID = {
    "shock_atr_mult": [1.5, 2.0],
    "min_close_off_low": [0.30, 0.45],
    "volume_mult": [1.0, 1.4],
}

RANGE_EXPANSION_GRID = {
    "lookback": [24, 48],
    "breakout_buffer_atr": [0.0, 0.15],
    "range_atr_mult": [1.0, 1.4],
    "close_in_bar_min": [0.65, 0.80],
}


def generate_momentum_configs() -> List[dict]:
    """Generate all momentum parameter combinations (81 configs)."""
    configs = []
    for fast in MOMENTUM_GRID["fast_period"]:
        for slow in MOMENTUM_GRID["slow_period"]:
            if fast >= slow:
                continue  # fast must be < slow
            for rsi_lo in MOMENTUM_GRID["rsi_long_min"]:
                for rsi_hi in MOMENTUM_GRID["rsi_long_max"]:
                    if rsi_lo >= rsi_hi:
                        continue
                    configs.append({
                        "fast_period": fast,
                        "slow_period": slow,
                        "rsi_long_min": rsi_lo,
                        "rsi_long_max": rsi_hi,
                        "rsi_short_min": 100 - rsi_hi,  # mirror
                        "rsi_short_max": 100 - rsi_lo,  # mirror
                    })
    return configs


def generate_mean_reversion_configs() -> List[dict]:
    """Generate all mean reversion parameter combinations (12 configs)."""
    configs = []
    for period in MEAN_REVERSION_GRID["bb_period"]:
        for sigma in MEAN_REVERSION_GRID["bb_sigma"]:
            for rsi_os in MEAN_REVERSION_GRID["rsi_oversold"]:
                rsi_ob = 100 - rsi_os  # mirror
                configs.append({
                    "bb_period": period,
                    "bb_sigma": sigma,
                    "rsi_oversold": rsi_os,
                    "rsi_overbought": rsi_ob,
                })
    return configs


def generate_failed_breakdown_configs() -> List[dict]:
    configs = []
    for lookback in FAILED_BREAKDOWN_GRID["lookback"]:
        for min_breach_atr in FAILED_BREAKDOWN_GRID["min_breach_atr"]:
            for min_close_reclaim in FAILED_BREAKDOWN_GRID["min_close_reclaim"]:
                for max_close_distance_atr in FAILED_BREAKDOWN_GRID["max_close_distance_atr"]:
                    configs.append({
                        "lookback": lookback,
                        "min_breach_atr": min_breach_atr,
                        "min_close_reclaim": min_close_reclaim,
                        "max_close_distance_atr": max_close_distance_atr,
                    })
    return configs


def generate_volatility_shock_configs() -> List[dict]:
    configs = []
    for shock_atr_mult in VOL_SHOCK_GRID["shock_atr_mult"]:
        for min_close_off_low in VOL_SHOCK_GRID["min_close_off_low"]:
            for volume_mult in VOL_SHOCK_GRID["volume_mult"]:
                configs.append({
                    "shock_atr_mult": shock_atr_mult,
                    "min_close_off_low": min_close_off_low,
                    "volume_mult": volume_mult,
                })
    return configs


def generate_range_expansion_configs() -> List[dict]:
    configs = []
    for lookback in RANGE_EXPANSION_GRID["lookback"]:
        for breakout_buffer_atr in RANGE_EXPANSION_GRID["breakout_buffer_atr"]:
            for range_atr_mult in RANGE_EXPANSION_GRID["range_atr_mult"]:
                for close_in_bar_min in RANGE_EXPANSION_GRID["close_in_bar_min"]:
                    configs.append({
                        "lookback": lookback,
                        "breakout_buffer_atr": breakout_buffer_atr,
                        "range_atr_mult": range_atr_mult,
                        "close_in_bar_min": close_in_bar_min,
                    })
    return configs


def run_rule(bars: List[Bar], rule_name: str, params: dict) -> List[Signal]:
    """Dispatch to the correct rule function."""
    if rule_name == "momentum_ema_cross":
        return momentum_ema_cross(bars, **params)
    elif rule_name == "mean_reversion_bbands":
        return mean_reversion_bbands(bars, **params)
    elif rule_name == "failed_breakdown_rebound":
        return failed_breakdown_rebound(bars, **params)
    elif rule_name == "volatility_shock_reversion":
        return volatility_shock_reversion(bars, **params)
    elif rule_name == "range_expansion_continuation":
        return range_expansion_continuation(bars, **params)
    else:
        raise ValueError(f"Unknown rule: {rule_name}")
