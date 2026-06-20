#!/usr/bin/env python
"""Import one verified research advisory into the live signal journal.

This bridge never submits an order. The long-running runtime still applies the
entry halt, authorization, expiry, strategy-match, risk, and exchange gates.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time

from google.cloud import storage

from bot import config
from bot.db import db
from bot.journal import Journal
from bot.models import Signal
from bot.strategy_authorization import validate_configured_authorization
from bot.acceptance_receipt import validate_acceptance_receipt
from research_pipeline.advisory import validate_advisory


ROOT = Path(__file__).resolve().parent


def _download() -> tuple[dict, dict]:
    client = storage.Client(project=config.research_advisory_project())
    blob = client.bucket(config.research_advisory_bucket()).blob(
        config.research_advisory_object()
    )
    blob.reload()
    body = blob.download_as_bytes()
    digest = hashlib.sha256(body).hexdigest()
    metadata = dict(blob.metadata or {})
    if metadata.get("sha256") != digest:
        raise RuntimeError("cloud advisory object SHA-256 mismatch")
    return json.loads(body), {
        "generation": blob.generation,
        "sha256": digest,
        "size": len(body),
    }


def _bind_live_db() -> Path:
    path = Path(config.live_db_path())
    if not path.is_absolute():
        path = ROOT / path
    if not path.is_file():
        raise FileNotFoundError(f"live journal missing: {path}")
    db.db_path = str(path)
    required = {
        "strategy_id",
        "strategy_version",
        "expires_at_us",
        "stop_price",
        "target_price",
        "time_stop_seconds",
        "source_hash",
    }
    columns = {
        row["name"]
        for row in db.fetch_all("PRAGMA table_info(signals)")
    }
    missing = sorted(required - columns)
    if missing:
        raise RuntimeError(
            f"live journal signal schema is missing: {', '.join(missing)}"
        )
    return path


def run_once(*, now_us: int | None = None) -> dict:
    now = int(time.time() * 1_000_000) if now_us is None else int(now_us)
    if config.strategy_id() != "btc_derivatives_stress_exhaustion":
        return {
            "status": "SKIPPED",
            "reason": "configured strategy is not derivatives-stress",
        }
    if (ROOT / config.kill_switch_file()).exists():
        return {"status": "SKIPPED", "reason": "entry halt is active"}
    authorized, reason, _ = validate_configured_authorization()
    if not authorized:
        return {"status": "SKIPPED", "reason": reason}
    accepted, reason, _ = validate_acceptance_receipt(
        ROOT / config.acceptance_receipt_file(),
        strategy_id=config.strategy_id(),
        strategy_version=config.strategy_version(),
        max_age_seconds=config.acceptance_receipt_max_age_seconds(),
    )
    if not accepted:
        return {"status": "SKIPPED", "reason": reason}

    payload, remote = _download()
    valid, reason = validate_advisory(payload)
    if not valid:
        raise RuntimeError(reason)
    if payload["status"] != "SIGNAL":
        return {
            "status": "NO_SIGNAL",
            "decision_time_us": payload["decision_time_us"],
            "remote": remote,
        }
    if now > int(payload["expires_at_us"]):
        return {"status": "SKIPPED", "reason": "advisory expired"}
    age_seconds = (now - int(payload["generated_at_us"])) / 1_000_000
    if age_seconds > config.research_advisory_max_age_seconds():
        return {"status": "SKIPPED", "reason": "advisory is stale"}

    _bind_live_db()
    exit_contract = payload["exit_contract"]
    entry = float(exit_contract["entry_reference"])
    stop = float(exit_contract["stop_price"])
    signal_id = (
        f"adv_{payload['decision_time_us']}_"
        f"{payload['advisory_hash'][:12]}"
    )
    signal = Signal(
        signal_id=signal_id,
        symbol=payload["product_id"],
        signal_type="LONG",
        regime_snapshot=json.dumps(
            payload["signal"], sort_keys=True, separators=(",", ":")
        ),
        breakout_level=entry,
        retest_level=entry,
        atr=entry - stop,
        rsi=0.0,
        status="NEW",
        execution_price=entry,
        strategy_id=payload["strategy_id"],
        strategy_version=payload["strategy_version"],
        decision_time_us=int(payload["decision_time_us"]),
        expires_at_us=int(payload["expires_at_us"]),
        stop_price=stop,
        target_price=float(exit_contract["target_price"]),
        time_stop_seconds=int(exit_contract["time_stop_seconds"]),
        source_hash=payload["advisory_hash"],
    )
    inserted = Journal.insert_signal(signal)
    return {
        "status": "STAGED" if inserted else "DUPLICATE",
        "signal_id": signal_id,
        "order_submitted": False,
        "remote": remote,
    }


def main() -> int:
    result = run_once()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
