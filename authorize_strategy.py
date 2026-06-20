#!/usr/bin/env python
"""Create an expiring live authorization from passed strategy evidence.

This tool never places an order and never removes the entry halt.  It refuses
to create authority unless the evidence file is machine-readable, passed, and
matches the currently configured strategy/product.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

from bot import config
from bot.strategy_authorization import compute_authorization_hash
from bot.strategy_registry import get_implementation


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--authorized-by", required=True)
    parser.add_argument("--expires-hours", type=float, default=24.0)
    parser.add_argument(
        "--output", default=config.strategy_authorization_file()
    )
    args = parser.parse_args(argv)

    evidence_path = Path(args.evidence).resolve()
    evidence_bytes = evidence_path.read_bytes()
    evidence = json.loads(evidence_bytes.decode("utf-8"))
    status = evidence.get("evidence_status") or evidence.get("promotion")
    expected = {
        "strategy_id": config.strategy_id(),
        "strategy_version": config.strategy_version(),
        "product_id": config.symbol(),
    }
    errors = []
    implementation = get_implementation(
        config.strategy_id(),
        config.strategy_version(),
    )
    if implementation is None or not implementation.implementation_ready:
        errors.append(
            implementation.reason
            if implementation is not None
            else "configured strategy has no registered live implementation"
        )
    if status != "EVIDENCE_PASSED":
        errors.append("evidence status is not EVIDENCE_PASSED")
    for field, value in expected.items():
        if str(evidence.get(field, "")) != str(value):
            errors.append(f"evidence {field} does not match config")
    if implementation and implementation.exit_contract_id:
        exit_evidence = evidence.get("exit_contract") or {}
        if exit_evidence.get("exit_contract_id") != implementation.exit_contract_id:
            errors.append("evidence exit contract does not match implementation")
        if not exit_evidence.get("path_dependent_evidence_complete"):
            errors.append("path-dependent exit evidence is incomplete")
    if args.expires_hours <= 0 or args.expires_hours > 168:
        errors.append("expires-hours must be > 0 and <= 168")
    if errors:
        for error in errors:
            print(f"BLOCKED: {error}")
        return 1

    now = int(time.time())
    output = Path(args.output).resolve()
    try:
        evidence_ref = str(evidence_path.relative_to(output.parent))
    except ValueError:
        evidence_ref = str(evidence_path)
    payload = {
        "schema_version": 1,
        "status": "AUTHORIZED",
        **expected,
        "evidence_status": "EVIDENCE_PASSED",
        "evidence_file": evidence_ref,
        "evidence_sha256": hashlib.sha256(evidence_bytes).hexdigest(),
        "authorized_by": args.authorized_by.strip(),
        "authorized_at": now,
        "expires_at": now + int(args.expires_hours * 3600),
        "max_order_size_usd": config.max_order_size_usd(),
        "max_position_size_usd": config.max_position_size_usd(),
    }
    if not payload["authorized_by"]:
        print("BLOCKED: authorized-by must not be empty")
        return 1
    payload["authorization_hash"] = compute_authorization_hash(payload)
    output.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(f"Authorization created: {output}")
    print("Entry halt was NOT removed. Run acceptance before any activation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
