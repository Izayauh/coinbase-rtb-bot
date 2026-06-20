import json
import time

from bot.acceptance_receipt import (
    compute_acceptance_hash,
    validate_acceptance_receipt,
)


def _receipt(tmp_path, **overrides):
    payload = {
        "schema_version": 2,
        "generated_at": int(time.time()),
        "strategy_id": "btc_derivatives_stress_exhaustion",
        "strategy_version": "1.0.0",
        "ready_for_tiny_live": True,
        "decision": "READY",
        "order_submitted": False,
        "gates": [],
    }
    payload.update(overrides)
    payload["acceptance_hash"] = compute_acceptance_hash(payload)
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(payload))
    return path, payload


def _validate(path, now=None):
    return validate_acceptance_receipt(
        path,
        strategy_id="btc_derivatives_stress_exhaustion",
        strategy_version="1.0.0",
        max_age_seconds=300,
        now=now,
    )


def test_fresh_ready_acceptance_receipt_passes(tmp_path):
    path, _ = _receipt(tmp_path)
    valid, reason, _ = _validate(path)
    assert valid is True
    assert reason == "acceptance receipt valid"


def test_blocked_or_tampered_acceptance_receipt_fails(tmp_path):
    path, payload = _receipt(tmp_path)
    payload["decision"] = "BLOCKED"
    path.write_text(json.dumps(payload))
    valid, reason, _ = _validate(path)
    assert valid is False
    assert "hash mismatch" in reason


def test_stale_acceptance_receipt_fails(tmp_path):
    generated = int(time.time()) - 301
    path, _ = _receipt(tmp_path, generated_at=generated)
    valid, reason, _ = _validate(path, now=generated + 301)
    assert valid is False
    assert "stale" in reason
