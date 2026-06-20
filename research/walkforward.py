"""
research/walkforward.py — Rolling walk-forward validation runner.

Design (pre-registered):
  - Train window: 180 days
  - Test window: 60 days
  - Step: 60 days
  - For each window:
    1. Run all configs on train set
    2. Select best by Sharpe (after friction)
    3. Evaluate selected config on test set (OOS)
  - Return per-window OOS results for all configs
"""
import csv
import os
import time
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple

from .types import Bar, BacktestResult
from .costs import FrictionModel
from .backtest import run_backtest
from .rules import run_rule


@dataclass
class WindowResult:
    """Result from one walk-forward window."""
    window_id: int
    train_start: int     # bar index
    train_end: int       # bar index
    test_start: int      # bar index
    test_end: int        # bar index
    # Best IS config
    best_config_name: str = ""
    best_config_params: dict = field(default_factory=dict)
    best_is_sharpe: float = 0.0
    # OOS result of the IS-selected config
    oos_result: Optional[BacktestResult] = None
    # All OOS results (for FDR analysis)
    all_oos_results: List[BacktestResult] = field(default_factory=list)


@dataclass
class WalkForwardResult:
    """Aggregate walk-forward output."""
    symbol: str
    timeframe: str
    windows: List[WindowResult]
    total_configs_tested: int = 0


def compute_windows(
    n_bars: int,
    train_bars: int,
    test_bars: int,
    step_bars: int,
) -> List[Tuple[int, int, int, int]]:
    """
    Compute (train_start, train_end, test_start, test_end) bar indices.

    All indices are inclusive start, exclusive end.
    """
    windows = []
    cursor = 0
    while cursor + train_bars + test_bars <= n_bars:
        t_start = cursor
        t_end = cursor + train_bars
        oos_start = t_end
        oos_end = t_end + test_bars
        windows.append((t_start, t_end, oos_start, oos_end))
        cursor += step_bars
    return windows


def run_walk_forward(
    bars: List[Bar],
    configs: List[Tuple[str, dict]],     # [(rule_name, params), ...]
    friction: FrictionModel,
    train_days: int = 180,
    test_days: int = 60,
    step_days: int = 60,
    bars_per_day: int = 24,              # 1h bars
    initial_equity: float = 10000.0,
    risk_per_trade: float = 0.01,
    stop_atr_mult: float = 2.0,
    tp_atr_mult: float = 3.0,
    time_stop_bars: int = 24,
    selection_metric: str = "sharpe",     # IS selection metric
    symbol: str = "",
    timeframe: str = "1h",
) -> WalkForwardResult:
    """
    Run walk-forward validation.

    For each window:
      1. Run all configs on train bars → rank by selection_metric
      2. Run all configs on test bars → store OOS results
      3. The IS-best config's OOS result is highlighted
    """
    train_bars_n = train_days * bars_per_day
    test_bars_n = test_days * bars_per_day
    step_bars_n = step_days * bars_per_day

    windows_spec = compute_windows(len(bars), train_bars_n, test_bars_n, step_bars_n)

    print(f"  Walk-forward: {len(windows_spec)} windows, "
          f"{train_days}d train / {test_days}d test / {step_days}d step")
    print(f"  Configs: {len(configs)}")

    all_windows: List[WindowResult] = []

    for w_idx, (tr_s, tr_e, ts_s, ts_e) in enumerate(windows_spec):
        train_slice = bars[tr_s:tr_e]
        test_slice = bars[ts_s:ts_e]

        if len(train_slice) < 100 or len(test_slice) < 50:
            continue

        print(f"    Window {w_idx}: train[{tr_s}:{tr_e}] test[{ts_s}:{ts_e}] "
              f"({len(train_slice)}/{len(test_slice)} bars)")

        # ── IS: run all configs on train ──
        is_results: List[Tuple[str, dict, BacktestResult]] = []
        for rule_name, params in configs:
            signals = run_rule(train_slice, rule_name, params)
            result = run_backtest(
                train_slice, signals, friction, initial_equity,
                risk_per_trade, stop_atr_mult, tp_atr_mult, time_stop_bars,
                symbol, timeframe, rule_name, params,
            )
            is_results.append((rule_name, params, result))

        # Select best IS config by metric
        def _score(r: BacktestResult) -> float:
            if r.trade_count < 5:
                return -999.0
            if selection_metric == "sharpe":
                return r.sharpe
            elif selection_metric == "expectancy":
                return r.expectancy_pct
            elif selection_metric == "pf":
                return r.profit_factor
            return r.sharpe

        is_results.sort(key=lambda x: _score(x[2]), reverse=True)
        best_name, best_params, best_is = is_results[0] if is_results else ("", {}, None)

        # ── OOS: run all configs on test ──
        oos_results: List[BacktestResult] = []
        best_oos = None
        for rule_name, params in configs:
            signals = run_rule(test_slice, rule_name, params)
            result = run_backtest(
                test_slice, signals, friction, initial_equity,
                risk_per_trade, stop_atr_mult, tp_atr_mult, time_stop_bars,
                symbol, timeframe, rule_name, params,
            )
            oos_results.append(result)

            if rule_name == best_name and params == best_params:
                best_oos = result

        wr = WindowResult(
            window_id=w_idx,
            train_start=tr_s,
            train_end=tr_e,
            test_start=ts_s,
            test_end=ts_e,
            best_config_name=best_name,
            best_config_params=best_params,
            best_is_sharpe=best_is.sharpe if best_is else 0,
            oos_result=best_oos,
            all_oos_results=oos_results,
        )
        all_windows.append(wr)

    return WalkForwardResult(
        symbol=symbol,
        timeframe=timeframe,
        windows=all_windows,
        total_configs_tested=len(configs),
    )


def summarize_walk_forward(wf: WalkForwardResult) -> dict:
    """Produce a summary dict from walk-forward results."""
    oos_results = [w.oos_result for w in wf.windows if w.oos_result is not None]
    if not oos_results:
        return {"windows": 0, "oos_trades": 0}

    oos_trades = sum(r.trade_count for r in oos_results)
    oos_pf = [r.profit_factor for r in oos_results if r.trade_count >= 5]
    oos_expect = [r.expectancy_pct for r in oos_results if r.trade_count >= 5]
    oos_dd = [r.max_drawdown_pct for r in oos_results if r.trade_count >= 5]

    return {
        "symbol": wf.symbol,
        "windows": len(wf.windows),
        "configs_tested": wf.total_configs_tested,
        "oos_trades_total": oos_trades,
        "oos_windows_with_trades": len(oos_pf),
        "oos_pf_median": sorted(oos_pf)[len(oos_pf)//2] if oos_pf else 0,
        "oos_expect_median": sorted(oos_expect)[len(oos_expect)//2] if oos_expect else 0,
        "oos_dd_median": sorted(oos_dd)[len(oos_dd)//2] if oos_dd else 0,
        "oos_pf_positive": sum(1 for p in oos_pf if p > 1.0),
        "oos_expect_positive": sum(1 for e in oos_expect if e > 0),
    }
