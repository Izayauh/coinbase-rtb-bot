import json
import hashlib
import time

from bot.strategy_authorization import (
    compute_authorization_hash,
    validate_authorization,
)


def _artifact(tmp_path, **overrides):
    now = int(time.time())
    evidence = {
        "evidence_status": "EVIDENCE_PASSED",
        "strategy_id": "candidate_v1",
        "strategy_version": "1.2.3",
        "product_id": "BTC-USD",
    }
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(
        json.dumps(evidence, sort_keys=True), encoding="utf-8"
    )
    payload = {
        "schema_version": 1,
        "status": "AUTHORIZED",
        "strategy_id": "candidate_v1",
        "strategy_version": "1.2.3",
        "product_id": "BTC-USD",
        "evidence_status": "EVIDENCE_PASSED",
        "evidence_file": evidence_path.name,
        "evidence_sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
        "authorized_by": "operator",
        "authorized_at": now - 10,
        "expires_at": now + 3600,
        "max_order_size_usd": 10.0,
        "max_position_size_usd": 20.0,
    }
    payload.update(overrides)
    payload["authorization_hash"] = compute_authorization_hash(payload)
    path = tmp_path / "authorization.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path, payload


def _validate(path):
    return validate_authorization(
        path,
        strategy_id="candidate_v1",
        strategy_version="1.2.3",
        product_id="BTC-USD",
        max_order_size_usd=15.0,
        max_position_size_usd=30.0,
    )


def test_valid_strategy_authorization_passes(tmp_path):
    path, _ = _artifact(tmp_path)
    valid, reason, _ = _validate(path)
    assert valid is True
    assert reason == "authorized"


def test_tampered_strategy_authorization_fails(tmp_path):
    path, payload = _artifact(tmp_path)
    payload["max_order_size_usd"] = 14.0
    path.write_text(json.dumps(payload), encoding="utf-8")
    valid, reason, _ = _validate(path)
    assert valid is False
    assert "hash mismatch" in reason


def test_expired_strategy_authorization_fails(tmp_path):
    path, _ = _artifact(tmp_path, expires_at=int(time.time()) - 1)
    valid, reason, _ = _validate(path)
    assert valid is False
    assert "expired" in reason


def test_authorization_cannot_expand_configured_caps(tmp_path):
    path, _ = _artifact(tmp_path, max_order_size_usd=16.0)
    valid, reason, _ = _validate(path)
    assert valid is False
    assert "order cap" in reason
