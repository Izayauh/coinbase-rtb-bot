"""Fail-closed live strategy authorization.

Research evidence and live order authority are deliberately separate.  A
strategy may submit live BUY orders only when a local, expiring authorization
artifact matches the configured strategy, product, and risk caps.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1


def _canonical_payload(payload: dict[str, Any]) -> bytes:
    unsigned = dict(payload)
    unsigned.pop("authorization_hash", None)
    return json.dumps(
        unsigned,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def compute_authorization_hash(payload: dict[str, Any]) -> str:
    """Return the SHA-256 digest for an authorization payload."""
    return hashlib.sha256(_canonical_payload(payload)).hexdigest()


def validate_authorization(
    path: str | Path,
    *,
    strategy_id: str,
    strategy_version: str,
    product_id: str,
    max_order_size_usd: float,
    max_position_size_usd: float,
    now: int | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Validate one live strategy authorization artifact.

    Returns ``(valid, reason, payload)``.  Missing or malformed evidence always
    fails closed.
    """
    auth_path = Path(path)
    if not auth_path.is_absolute():
        auth_path = Path.cwd() / auth_path
    if not auth_path.is_file():
        return False, f"strategy authorization file missing: {auth_path}", {}

    try:
        payload = json.loads(auth_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, f"strategy authorization is unreadable: {exc}", {}
    if not isinstance(payload, dict):
        return False, "strategy authorization must be a JSON object", {}

    required = {
        "schema_version",
        "status",
        "strategy_id",
        "strategy_version",
        "product_id",
        "evidence_status",
        "evidence_file",
        "evidence_sha256",
        "authorized_by",
        "authorized_at",
        "expires_at",
        "max_order_size_usd",
        "max_position_size_usd",
        "authorization_hash",
    }
    missing = sorted(required - set(payload))
    if missing:
        return False, f"strategy authorization missing fields: {', '.join(missing)}", payload

    expected_hash = compute_authorization_hash(payload)
    if payload.get("authorization_hash") != expected_hash:
        return False, "strategy authorization hash mismatch", payload
    if payload.get("schema_version") != SCHEMA_VERSION:
        return False, "unsupported strategy authorization schema", payload
    if payload.get("status") != "AUTHORIZED":
        return False, "strategy authorization status is not AUTHORIZED", payload
    if payload.get("evidence_status") != "EVIDENCE_PASSED":
        return False, "strategy evidence status is not EVIDENCE_PASSED", payload
    if payload.get("strategy_id") != strategy_id:
        return False, "strategy authorization id does not match config", payload
    if str(payload.get("strategy_version")) != str(strategy_version):
        return False, "strategy authorization version does not match config", payload
    if payload.get("product_id") != product_id:
        return False, "strategy authorization product does not match config", payload

    evidence_hash = str(payload.get("evidence_sha256", ""))
    if len(evidence_hash) != 64 or any(c not in "0123456789abcdef" for c in evidence_hash.lower()):
        return False, "strategy evidence SHA-256 is invalid", payload
    evidence_path = Path(str(payload.get("evidence_file", "")))
    if not evidence_path.is_absolute():
        evidence_path = auth_path.parent / evidence_path
    if not evidence_path.is_file():
        return False, f"strategy evidence file missing: {evidence_path}", payload
    evidence_bytes = evidence_path.read_bytes()
    if hashlib.sha256(evidence_bytes).hexdigest() != evidence_hash.lower():
        return False, "strategy evidence file hash mismatch", payload
    try:
        evidence = json.loads(evidence_bytes.decode("utf-8"))
    except Exception as exc:
        return False, f"strategy evidence file is unreadable: {exc}", payload
    evidence_status = evidence.get("evidence_status") or evidence.get("promotion")
    if evidence_status != "EVIDENCE_PASSED":
        return False, "linked strategy evidence did not pass", payload
    for field, expected in (
        ("strategy_id", strategy_id),
        ("strategy_version", str(strategy_version)),
        ("product_id", product_id),
    ):
        if str(evidence.get(field, "")) != str(expected):
            return False, f"linked strategy evidence {field} does not match", payload
    if not str(payload.get("authorized_by", "")).strip():
        return False, "strategy authorization has no accountable authorizer", payload

    current = int(time.time()) if now is None else int(now)
    try:
        authorized_at = int(payload["authorized_at"])
        expires_at = int(payload["expires_at"])
        auth_order_cap = float(payload["max_order_size_usd"])
        auth_position_cap = float(payload["max_position_size_usd"])
    except (TypeError, ValueError):
        return False, "strategy authorization has invalid numeric fields", payload

    if authorized_at > current:
        return False, "strategy authorization is not active yet", payload
    if expires_at <= current:
        return False, "strategy authorization has expired", payload
    if auth_order_cap <= 0 or auth_order_cap > float(max_order_size_usd):
        return False, "strategy authorization order cap exceeds configured cap", payload
    if auth_position_cap <= 0 or auth_position_cap > float(max_position_size_usd):
        return False, "strategy authorization position cap exceeds configured cap", payload

    return True, "authorized", payload


def validate_configured_authorization() -> tuple[bool, str, dict[str, Any]]:
    """Validate the authorization selected by ``config.yaml``."""
    from . import config

    if not config.require_strategy_authorization():
        return True, "strategy authorization not required", {}
    return validate_authorization(
        config.strategy_authorization_file(),
        strategy_id=config.strategy_id(),
        strategy_version=config.strategy_version(),
        product_id=config.symbol(),
        max_order_size_usd=config.max_order_size_usd(),
        max_position_size_usd=config.max_position_size_usd(),
    )
