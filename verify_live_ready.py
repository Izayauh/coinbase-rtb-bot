#!/usr/bin/env python
"""
verify_live_ready.py — Live order path readiness check.

Answers: "Can this bot place a live buy order right now?"

Checks all gates required to place a real order without starting the bot
or placing any orders. Run this before launching main.py in live mode.

Exit codes:
  0  YES — all gates green, bot can place a live buy order
  1  NO  — one or more blockers listed

Usage:
    python verify_live_ready.py
    # or from start-live.ps1 (recommended before launching the bot)
"""
import os
import sys

# Allow running from project root without package install
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot.readiness import check_readiness


def main() -> int:
    print()
    print("Live Order Path Readiness Check")
    print("=" * 50)
    print()

    ready, blockers, live_balances = check_readiness()

    if live_balances:
        print("  --- Live Coinbase Account (fetched now) ---")
        base = ""
        try:
            import bot.config as _cfg
            sym = _cfg.symbol()
            base = sym.split("-")[0] if "-" in sym else sym
        except Exception:
            pass
        priority = {"USD", "USDC"}
        if base:
            priority.add(base)
        shown = set()
        for currency in sorted(priority):
            if currency in live_balances:
                print(f"  {currency:<8}: {live_balances[currency]:>20,.8f}")
                shown.add(currency)
        for currency, value in sorted(live_balances.items()):
            if currency not in shown and value > 0:
                print(f"  {currency:<8}: {value:>20,.8f}")
        print()

    if ready:
        print("LIVE ORDER PATH ARMED: YES")
        print("  All gates green — bot can place a live buy order.")
        print("  To start:  .\\start-live.ps1")
    else:
        print("LIVE ORDER PATH ARMED: NO")
        print(f"  {len(blockers)} blocker(s) must be resolved:\n")
        for i, b in enumerate(blockers, 1):
            print(f"  [{i}] {b}")

    print()
    return 0 if ready else 1


if __name__ == "__main__":
    sys.exit(main())
