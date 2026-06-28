"""
research/option_a_spot.py — Spot-only Option A exploration runner.

Purpose:
  - Reuse the existing walk-forward / friction-aware harness.
  - Test materially different, event-driven long-only spot rule families.
  - Keep the original Lane A pilot intact while iterating on new hypotheses.

Families tested here:
  - failed_breakdown_rebound
  - volatility_shock_reversion
  - range_expansion_continuation

Usage:
  ./venv/Scripts/python.exe -m research.option_a_spot
  ./venv/Scripts/python.exe -m research.option_a_spot --families failed_breakdown_rebound range_expansion_continuation
  ./venv/Scripts/python.exe -m research.option_a_spot --sensitivities 1.0
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from research.costs import FrictionModel, ALTCOIN_SPREADS
from research.data import ensure_data, DATASETS_DIR
from research.rules import (
    generate_failed_breakdown_configs,
    generate_volatility_shock_configs,
    generate_range_expansion_configs,
)
from research.universe import get_pilot_universe
from research.walkforward import run_walk_forward, summarize_walk_forward


RESULTS_DIR = os.path.join(DATASETS_DIR, "..", "results")
RESULTS_PATH = os.path.join(RESULTS_DIR, "option_a_spot_results.json")

FAMILY_BUILDERS = {
    "failed_breakdown_rebound": generate_failed_breakdown_configs,
    "volatility_shock_reversion": generate_volatility_shock_configs,
    "range_expansion_continuation": generate_range_expansion_configs,
}


def build_family_configs(selected_families: list[str]) -> dict[str, list[tuple[str, dict]]]:
    family_configs: dict[str, list[tuple[str, dict]]] = {}
    for family in selected_families:
        builder = FAMILY_BUILDERS[family]
        family_configs[family] = [(family, params) for params in builder()]
    return family_configs


def run_experiments(universe: list[str], family_configs: dict[str, list[tuple[str, dict]]], days: int, sensitivities: list[float]) -> dict:
    os.makedirs(RESULTS_DIR, exist_ok=True)

    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "days": days,
        "universe": universe,
        "families": {name: len(configs) for name, configs in family_configs.items()},
        "results": [],
    }

    for sensitivity in sensitivities:
        friction = FrictionModel(sensitivity=sensitivity, spread_overrides=ALTCOIN_SPREADS)
        for symbol in universe:
            bars = ensure_data(symbol, "1h", days)
            if not bars or len(bars) < 4320:
                continue

            for family_name, configs in family_configs.items():
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
                summary["family"] = family_name
                summary["sensitivity"] = sensitivity
                summary["config_count"] = len(configs)
                payload["results"].append(summary)
                print(
                    f"{symbol} {family_name} {sensitivity:.1f}x "
                    f"trades={summary.get('oos_trades_total', 0)} "
                    f"pf={summary.get('oos_pf_median', 0):.2f} "
                    f"exp={summary.get('oos_expect_median', 0):+.4f}"
                )

    with open(RESULTS_PATH, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"\nSaved results to {RESULTS_PATH}")
    return payload


def main():
    parser = argparse.ArgumentParser(description="Spot-only Option A exploration")
    parser.add_argument("--assets", type=int, default=5)
    parser.add_argument("--days", type=int, default=730)
    parser.add_argument("--families", nargs="*", choices=sorted(FAMILY_BUILDERS), default=sorted(FAMILY_BUILDERS))
    parser.add_argument("--sensitivities", nargs="*", type=float, default=[0.5, 1.0, 1.5])
    args = parser.parse_args()

    universe = get_pilot_universe(args.assets)
    if not universe:
        raise SystemExit("No cached universe available.")

    family_configs = build_family_configs(args.families)
    run_experiments(universe, family_configs, args.days, args.sensitivities)


if __name__ == "__main__":
    main()
