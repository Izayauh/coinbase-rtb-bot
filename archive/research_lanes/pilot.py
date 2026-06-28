"""
research/pilot.py — Pilot experiment runner for Lane A.

Pre-registered design:
  - 5 assets × 2 rule families × 93 configs = 465 configs
  - Walk-forward: 180d train / 60d test / 60d step
  - 3 friction sensitivity runs: 0.5x, 1.0x, 1.5x
  - Stage-1 BH-FDR screen at alpha=0.10
  - Output: per-config OOS metrics CSV

Usage:
    python -m research.pilot
    python -m research.pilot --download-only
    python -m research.pilot --pilot-only
"""
import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from research.types import Bar, BacktestResult
from research.costs import FrictionModel, ALTCOIN_SPREADS
from research.data import ensure_data, print_manifest, DATASETS_DIR
from research.universe import get_pilot_universe, freeze_universe
from research.rules import (
    generate_momentum_configs, generate_mean_reversion_configs, run_rule,
)
from research.backtest import run_backtest
from research.walkforward import run_walk_forward, summarize_walk_forward
from research.multiple_testing import screen_oos_results


RESULTS_DIR = os.path.join(DATASETS_DIR, "..", "results")


def download_pilot_data(days: int = 730, n_assets: int = 5):
    """Download 730d of 1h data for the pilot universe."""
    print("\n=== DOWNLOADING PILOT DATA ===\n")

    # Freeze universe if needed
    universe = get_pilot_universe(n_assets)
    if not universe:
        print("  Freezing universe first...")
        freeze_universe(max_count=30)
        universe = get_pilot_universe(n_assets)

    print(f"  Pilot universe ({len(universe)} assets): {universe}\n")

    for symbol in universe:
        ensure_data(symbol, "1h", days)

    print("\n=== MANIFEST ===")
    print_manifest()
    return universe


def build_configs():
    """Build all pre-registered configs."""
    configs = []

    # Momentum family
    for params in generate_momentum_configs():
        configs.append(("momentum_ema_cross", params))

    # Mean reversion family
    for params in generate_mean_reversion_configs():
        configs.append(("mean_reversion_bbands", params))

    return configs


def run_pilot(
    universe: list,
    configs: list,
    days: int = 730,
    sensitivities: list = None,
):
    """Run the full pilot experiment."""
    if sensitivities is None:
        sensitivities = [0.5, 1.0, 1.5]

    os.makedirs(RESULTS_DIR, exist_ok=True)

    print(f"\n{'='*80}")
    print(f"  LANE A PILOT EXPERIMENT")
    print(f"  Universe: {universe}")
    print(f"  Configs: {len(configs)}")
    print(f"  Friction sensitivities: {sensitivities}")
    print(f"{'='*80}\n")

    all_summaries = []

    for sensitivity in sensitivities:
        friction = FrictionModel(
            sensitivity=sensitivity,
            spread_overrides=ALTCOIN_SPREADS,
        )

        print(f"\n--- Friction {sensitivity}x: {friction.summary()} ---\n")

        for symbol in universe:
            bars = ensure_data(symbol, "1h", days)
            if not bars or len(bars) < 4320:  # need at least 180d of data
                print(f"  SKIP {symbol}: insufficient data ({len(bars) if bars else 0} bars)")
                continue

            print(f"\n  Running walk-forward for {symbol} ({len(bars)} bars, "
                  f"friction={sensitivity}x) ...")

            wf_result = run_walk_forward(
                bars=bars,
                configs=configs,
                friction=friction,
                train_days=180,
                test_days=60,
                step_days=60,
                symbol=symbol,
                timeframe="1h",
            )

            summary = summarize_walk_forward(wf_result)
            summary["sensitivity"] = sensitivity
            all_summaries.append(summary)

            print(f"    {symbol} [{sensitivity}x]: "
                  f"windows={summary['windows']} "
                  f"oos_trades={summary['oos_trades_total']} "
                  f"pf_med={summary['oos_pf_median']:.2f} "
                  f"expect_med={summary['oos_expect_median']:.4f} "
                  f"dd_med={summary['oos_dd_median']:.2f}")

            # Run Stage-1 FDR screen on all OOS results
            all_oos = []
            for window in wf_result.windows:
                all_oos.extend(window.all_oos_results)

            if all_oos:
                screened = screen_oos_results(all_oos, min_trades=10, alpha=0.10)
                sig_count = sum(1 for _, _, is_sig, _ in screened if is_sig)
                print(f"    FDR screen: {len(screened)} eligible, {sig_count} significant "
                      f"(alpha=0.10)")

    # Save summaries
    _save_results(all_summaries, configs)
    _print_final_report(all_summaries)

    return all_summaries


def _save_results(summaries: list, configs: list):
    """Save results to JSON."""
    path = os.path.join(RESULTS_DIR, "pilot_results.json")
    with open(path, "w") as f:
        json.dump({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "total_configs": len(configs),
            "summaries": summaries,
        }, f, indent=2, default=str)
    print(f"\n  Results saved to {path}")


def _print_final_report(summaries: list):
    """Print the final pilot report."""
    print(f"\n{'='*80}")
    print(f"  PILOT RESULTS SUMMARY")
    print(f"{'='*80}")

    print(f"\n  {'Symbol':<12} {'Sens':>5} {'Win':>4} {'OOS Trades':>10} "
          f"{'PF Med':>7} {'Expect':>8} {'DD Med':>7} {'PF>1':>5} {'E>0':>5}")
    print("  " + "-" * 72)

    for s in summaries:
        sym = s.get("symbol", "?")
        print(f"  {sym:<12} {s['sensitivity']:>4.1f}x {s['windows']:>4} "
              f"{s['oos_trades_total']:>10} {s['oos_pf_median']:>6.2f} "
              f"{s['oos_expect_median']:>+7.4f} {s['oos_dd_median']:>6.2f} "
              f"{s.get('oos_pf_positive',0):>5} {s.get('oos_expect_positive',0):>5}")

    # Verdict
    base_summaries = [s for s in summaries if s.get("sensitivity") == 1.0]
    any_edge = any(s.get("oos_expect_median", 0) > 0 for s in base_summaries)
    multi_asset = len(set(s["symbol"] for s in base_summaries
                         if s.get("oos_expect_median", 0) > 0)) >= 2

    print(f"\n{'='*80}")
    if not any_edge:
        print("  VERDICT: LANE A FAILS — no positive OOS expectancy at 1.0x friction")
        print("  Kill trigger K2: zero raw edge")
    elif not multi_asset:
        print("  VERDICT: LANE A FAILS — edge confined to single asset")
        print("  Kill trigger K4: single-asset confinement")
    else:
        # Check friction robustness
        high_summaries = [s for s in summaries if s.get("sensitivity") == 1.5]
        robust = any(s.get("oos_expect_median", 0) > 0 for s in high_summaries)
        if robust:
            print("  VERDICT: LANE A PROMISING — survivors exist at 1.5x friction")
            print("  Next: Stage-2 validation (CSCV/PBO) + portfolio-form check")
        else:
            print("  VERDICT: LANE A MARGINAL — edge exists at 1.0x but dies at 1.5x")
            print("  Kill trigger K3: cost-killed")
    print(f"{'='*80}")


def main():
    parser = argparse.ArgumentParser(description="Lane A Pilot Experiment")
    parser.add_argument("--download-only", action="store_true",
                        help="Only download data, don't run experiment")
    parser.add_argument("--pilot-only", action="store_true",
                        help="Skip download, run experiment on cached data")
    parser.add_argument("--assets", type=int, default=5,
                        help="Number of pilot assets")
    parser.add_argument("--days", type=int, default=730,
                        help="Days of history to download")
    args = parser.parse_args()

    configs = build_configs()
    print(f"  Pre-registered configs: {len(configs)}")
    print(f"    Momentum: {len(generate_momentum_configs())}")
    print(f"    Mean reversion: {len(generate_mean_reversion_configs())}")

    if args.download_only:
        download_pilot_data(args.days, args.assets)
        return

    if not args.pilot_only:
        universe = download_pilot_data(args.days, args.assets)
    else:
        universe = get_pilot_universe(args.assets)

    if not universe:
        print("  ERROR: No universe available. Run with --download-only first.")
        return

    run_pilot(universe, configs, args.days)


if __name__ == "__main__":
    main()
