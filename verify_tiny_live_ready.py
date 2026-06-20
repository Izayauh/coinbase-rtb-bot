#!/usr/bin/env python
"""Run every required gate for the first tiny capped live trial."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from bot.acceptance import evaluate_tiny_live_acceptance, write_report
from bot import config


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="tiny_live_acceptance.json")
    parser.add_argument("--skip-tests", action="store_true")
    args = parser.parse_args(argv)

    report = evaluate_tiny_live_acceptance(run_tests=not args.skip_tests)
    write_report(report, args.output)
    receipt = Path(config.acceptance_receipt_file()).resolve()
    if receipt != Path(args.output).resolve():
        write_report(report, receipt)
    print(json.dumps(report, indent=2, sort_keys=True))
    print()
    print(f"TINY LIVE ACCEPTANCE: {report['decision']}")
    print(f"Report: {args.output}")
    print(f"Activation receipt: {receipt}")
    return 0 if report["ready_for_tiny_live"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
