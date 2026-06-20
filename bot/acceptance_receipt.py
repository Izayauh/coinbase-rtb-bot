"""Tamper-evident, short-lived final acceptance receipt."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time
from typing import Any, Mapping


def compute_acceptance_hash(payload: Mapping[str, Any]) -> str:
    unsigned = dict(payload)
    unsigned.pop("acceptance_hash", None)
    encoded = json.dumps(
        unsigned,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def validate_acceptance_receipt(
    path: str | Path,
    *,
    strategy_id: str,
    strategy_version: str,
    max_age_seconds: int = 300,
    now: int | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    receipt_path = Path(path)
    if not receipt_path.is_file():
        return False, f"acceptance receipt missing: {receipt_path}", {}
    try:
        payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, f"acceptance receipt unreadable: {exc}", {}
    if not isinstance(payload, dict):
        return False, "acceptance receipt must be a JSON object", {}
    if payload.get("acceptance_hash") != compute_acceptance_hash(payload):
        return False, "acceptance receipt hash mismatch", payload
    if payload.get("decision") != "READY" or not payload.get(
        "ready_for_tiny_live"
    ):
        return False, "acceptance receipt is not READY", payload
    if payload.get("order_submitted") is not False:
        return False, "acceptance receipt order invariant failed", payload
    if payload.get("strategy_id") != strategy_id:
        return False, "acceptance receipt strategy id mismatch", payload
    if str(payload.get("strategy_version")) != str(strategy_version):
        return False, "acceptance receipt strategy version mismatch", payload
    current = int(time.time()) if now is None else int(now)
    try:
        age = current - int(payload["generated_at"])
    except (KeyError, TypeError, ValueError):
        return False, "acceptance receipt generated_at is invalid", payload
    if age < 0:
        return False, "acceptance receipt is from the future", payload
    if age > int(max_age_seconds):
        return False, "acceptance receipt is stale", payload
    return True, "acceptance receipt valid", payload
