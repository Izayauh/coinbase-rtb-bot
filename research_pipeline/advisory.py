"""Real-time, research-only advisory publication for the frozen candidate."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
from pathlib import Path
import time
from typing import Any, Mapping, Sequence

from google.cloud import storage

from .candidates.derivatives_stress import (
    PRODUCT_ID,
    STRATEGY_ID,
    STRATEGY_VERSION,
    build_candidate_decisions,
)


HISTORY_US = 48 * 60 * 60 * 1_000_000
ADVISORY_TTL_US = 120 * 1_000_000
EXIT_CONTRACT_ID = "derivatives_stress_exit_v1"


def _float(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _hash(payload: Mapping[str, Any]) -> str:
    unsigned = dict(payload)
    unsigned.pop("advisory_hash", None)
    encoded = json.dumps(
        unsigned,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def build_exit_contract(signal: Mapping[str, Any]) -> dict:
    entry = float(signal["mid"])
    shock = abs(float(signal["spot_return_15m"]))
    stop_fraction = min(0.02, max(0.005, shock * 0.5))
    stop_price = entry * (1.0 - stop_fraction)
    target_price = entry + (entry - stop_price) * 1.5
    return {
        "exit_contract_id": EXIT_CONTRACT_ID,
        "entry_reference": entry,
        "stop_price": stop_price,
        "target_price": target_price,
        "time_stop_seconds": 4 * 60 * 60,
        "take_profit_r": 1.5,
        "stop_fraction": stop_fraction,
    }


def validate_advisory(payload: Mapping[str, Any]) -> tuple[bool, str]:
    if payload.get("advisory_hash") != _hash(payload):
        return False, "advisory hash mismatch"
    if payload.get("strategy_id") != STRATEGY_ID:
        return False, "advisory strategy id mismatch"
    if payload.get("strategy_version") != STRATEGY_VERSION:
        return False, "advisory strategy version mismatch"
    if payload.get("product_id") != PRODUCT_ID:
        return False, "advisory product mismatch"
    if payload.get("live_authority_granted") is not False:
        return False, "advisory must never grant live authority"
    if payload.get("status") == "SIGNAL":
        if payload.get("variant") not in {
            "combined_balanced_v1",
            "combined_strict_v1",
        }:
            return False, "signal advisory has non-promotable variant"
        signal = payload.get("signal")
        if not isinstance(signal, Mapping):
            return False, "signal advisory payload is missing"
        if int(signal.get("event_time_us", -1)) != int(
            payload.get("decision_time_us", -2)
        ):
            return False, "signal decision timestamp mismatch"
        expected_exit = build_exit_contract(signal)
        if payload.get("exit_contract") != expected_exit:
            return False, "signal exit contract mismatch"
    elif payload.get("status") != "NO_SIGNAL":
        return False, "unsupported advisory status"
    return True, "advisory valid"


def load_history(path: str | Path | None) -> list[dict]:
    if not path:
        return []
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = payload.get("rows", payload)
    if not isinstance(rows, list):
        raise ValueError("candidate history must contain a row list")
    return [dict(row) for row in rows]


def _latest_context(store, event_time_us: int) -> dict:
    oi_row = store.conn.execute(
        "SELECT availability_time_us, payload FROM context_events "
        "WHERE source_kind='funding_oi' "
        "AND native_id LIKE '%:open_interest:%' "
        "AND availability_time_us<=? AND availability_time_us>=? "
        "ORDER BY availability_time_us DESC LIMIT 1",
        (event_time_us, event_time_us - 300_000_000),
    ).fetchone()
    funding_row = store.conn.execute(
        "SELECT availability_time_us, event_time_us, payload "
        "FROM context_events WHERE source_kind='funding_oi' "
        "AND native_id LIKE '%:funding:%' "
        "AND availability_time_us<=? AND availability_time_us>=? "
        "ORDER BY availability_time_us DESC, event_time_us DESC LIMIT 1",
        (event_time_us, event_time_us - 7_200_000_000),
    ).fetchone()
    oi_payload = json.loads(oi_row["payload"]) if oi_row else {}
    funding_payload = json.loads(funding_row["payload"]) if funding_row else {}
    return {
        "open_interest": _float(oi_payload.get("open_interest")),
        "funding_event_time_us": (
            int(funding_row["event_time_us"]) if funding_row else None
        ),
        "funding_rate": _float(funding_payload.get("funding_rate")),
        "mark_price": _float(funding_payload.get("mark_price")),
    }


class CandidateAdvisoryPublisher:
    """Join online book rows with available derivatives context and publish."""

    def __init__(
        self,
        store,
        *,
        history_rows: Sequence[Mapping[str, Any]] = (),
        output_path: str | Path,
    ):
        self.store = store
        self.output_path = Path(output_path)
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.rows = {
            int(row["event_time_us"]): dict(row)
            for row in history_rows
        }

    def observe(self, order_math: Mapping[str, Any]) -> dict | None:
        if (
            order_math.get("product_id") != PRODUCT_ID
            or order_math.get("flags") != "ok"
        ):
            return None
        event_time_us = int(order_math["event_time_us"])
        row = {
            key: order_math.get(key)
            for key in (
                "event_time_us",
                "mid",
                "spread_bps",
                "queue_imbalance",
                "multilevel_depth_imbalance",
                "microprice_delta_bps",
                "depth_imbalance_10bps",
                "ofi_60s",
                "mlofi_5_60s",
                "bid_replenishment_ratio",
                "ask_replenishment_ratio",
            )
        }
        row.update(_latest_context(self.store, event_time_us))
        self.rows[event_time_us] = row
        cutoff = event_time_us - HISTORY_US
        self.rows = {
            timestamp: value
            for timestamp, value in self.rows.items()
            if timestamp >= cutoff
        }
        candidate = build_candidate_decisions(list(self.rows.values()))
        latest_signals = []
        for variant in ("combined_strict_v1", "combined_balanced_v1"):
            for signal in candidate["decisions"].get(variant, []):
                if int(signal["event_time_us"]) == event_time_us:
                    latest_signals.append((variant, signal))
        variant, signal = latest_signals[0] if latest_signals else (None, None)
        payload = {
            "schema_version": 1,
            "strategy_id": STRATEGY_ID,
            "strategy_version": STRATEGY_VERSION,
            "product_id": PRODUCT_ID,
            "status": "SIGNAL" if signal else "NO_SIGNAL",
            "variant": variant,
            "decision_time_us": event_time_us,
            "generated_at_us": int(time.time() * 1_000_000),
            "expires_at_us": event_time_us + ADVISORY_TTL_US,
            "signal": signal,
            "exit_contract": build_exit_contract(signal) if signal else None,
            "rows_seen": candidate["rows_seen"],
            "eligible_rows": candidate["eligible_rows"],
            "research_observation_only": True,
            "live_authority_granted": False,
        }
        payload["advisory_hash"] = _hash(payload)
        self.output_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return payload


def upload_advisory(
    path: str | Path,
    *,
    bucket: str,
    object_name: str,
    project: str,
    client=None,
) -> dict:
    source = Path(path)
    body = source.read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    client = client or storage.Client(project=project)
    blob = client.bucket(bucket).blob(object_name)
    blob.metadata = {"sha256": digest}
    blob.content_type = "application/json"
    blob.upload_from_string(body, content_type="application/json")
    blob.reload()
    if int(blob.size or -1) != len(body):
        raise RuntimeError(f"advisory size mismatch for {object_name}")
    if (blob.metadata or {}).get("sha256") != digest:
        raise RuntimeError(f"advisory SHA-256 mismatch for {object_name}")
    return {"key": object_name, "bytes": len(body), "sha256": digest}


class AdvisoryUploadPoller:
    def __init__(
        self,
        path: str | Path,
        *,
        bucket: str,
        object_name: str,
        project: str,
        poll_seconds: float = 15.0,
    ):
        self.path = Path(path)
        self.bucket = bucket
        self.object_name = object_name
        self.project = project
        self.poll_seconds = float(poll_seconds)
        self.last_hash: str | None = None

    async def run(self, *, max_seconds: float) -> dict:
        started = time.monotonic()
        totals = {"uploads": 0, "last_error": None}
        while True:
            if self.path.exists():
                digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
                if digest != self.last_hash:
                    try:
                        await asyncio.to_thread(
                            upload_advisory,
                            self.path,
                            bucket=self.bucket,
                            object_name=self.object_name,
                            project=self.project,
                        )
                        self.last_hash = digest
                        totals["uploads"] += 1
                        totals["last_error"] = None
                    except Exception as exc:
                        totals["last_error"] = f"{type(exc).__name__}: {exc}"
            remaining = max_seconds - (time.monotonic() - started)
            if remaining <= 0:
                break
            await asyncio.sleep(min(self.poll_seconds, remaining))
        return totals
