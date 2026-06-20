import time

import candidate_advisory_bridge as bridge
from bot import config
from research_pipeline.advisory import _hash, build_exit_contract


def _payload(now_us):
    signal = {
        "event_time_us": now_us - 10_000_000,
        "mid": 100.0,
        "spot_return_15m": -0.02,
        "spot_return_z": -2.0,
        "oi_return_15m": -0.03,
        "oi_return_z": -2.0,
        "funding_rate": -0.001,
        "funding_z": -2.0,
        "basis": -0.01,
        "basis_z": -2.0,
        "derivative_count": 3,
        "book_count": 4,
        "derivative_flags": {
            "oi_flush": True,
            "funding_tail": True,
            "negative_basis_tail": True,
        },
        "book_flags": {
            "bid_replenishment": True,
            "positive_ofi": True,
            "positive_microprice": True,
            "improving_depth": True,
        },
    }
    payload = {
        "schema_version": 1,
        "strategy_id": "btc_derivatives_stress_exhaustion",
        "strategy_version": "1.0.0",
        "product_id": "BTC-USD",
        "status": "SIGNAL",
        "variant": "combined_strict_v1",
        "decision_time_us": signal["event_time_us"],
        "generated_at_us": now_us - 1_000_000,
        "expires_at_us": now_us + 60_000_000,
        "signal": signal,
        "exit_contract": build_exit_contract(signal),
        "rows_seen": 1500,
        "eligible_rows": 20,
        "research_observation_only": True,
        "live_authority_granted": False,
    }
    payload["advisory_hash"] = _hash(payload)
    return payload


def _configure(monkeypatch, test_db, tmp_path, payload):
    monkeypatch.setattr(
        config,
        "strategy_id",
        lambda: "btc_derivatives_stress_exhaustion",
    )
    monkeypatch.setattr(config, "strategy_version", lambda: "1.0.0")
    monkeypatch.setattr(config, "live_db_path", lambda: test_db.db_path)
    monkeypatch.setattr(
        config,
        "kill_switch_file",
        lambda: str(tmp_path / "NO_ENTRY_HALT"),
    )
    monkeypatch.setattr(config, "research_advisory_max_age_seconds", lambda: 180)
    monkeypatch.setattr(
        bridge,
        "validate_configured_authorization",
        lambda: (True, "authorized", {}),
    )
    monkeypatch.setattr(
        bridge,
        "validate_acceptance_receipt",
        lambda *args, **kwargs: (True, "acceptance receipt valid", {}),
    )
    monkeypatch.setattr(
        bridge,
        "_download",
        lambda: (payload, {"sha256": "remote", "generation": 1, "size": 1}),
    )


def test_bridge_stages_fresh_authorized_signal_without_order(
    test_db,
    monkeypatch,
    tmp_path,
):
    now_us = int(time.time() * 1_000_000)
    payload = _payload(now_us)
    _configure(monkeypatch, test_db, tmp_path, payload)

    first = bridge.run_once(now_us=now_us)
    second = bridge.run_once(now_us=now_us)

    assert first["status"] == "STAGED"
    assert first["order_submitted"] is False
    assert second["status"] == "DUPLICATE"
    signals = test_db.fetch_all("SELECT * FROM signals")
    assert len(signals) == 1
    assert signals[0]["strategy_id"] == "btc_derivatives_stress_exhaustion"
    assert signals[0]["stop_price"] == payload["exit_contract"]["stop_price"]
    assert signals[0]["target_price"] == payload["exit_contract"]["target_price"]
    assert test_db.fetch_all("SELECT * FROM orders") == []


def test_bridge_does_not_stage_expired_signal(test_db, monkeypatch, tmp_path):
    now_us = int(time.time() * 1_000_000)
    payload = _payload(now_us)
    payload["expires_at_us"] = now_us - 1
    payload["advisory_hash"] = _hash(payload)
    _configure(monkeypatch, test_db, tmp_path, payload)

    result = bridge.run_once(now_us=now_us)

    assert result == {"status": "SKIPPED", "reason": "advisory expired"}
    assert test_db.fetch_all("SELECT * FROM signals") == []


def test_bridge_respects_entry_halt_before_cloud_read(
    monkeypatch,
    tmp_path,
):
    halt = tmp_path / "HALT"
    halt.write_text("halt")
    monkeypatch.setattr(
        config,
        "strategy_id",
        lambda: "btc_derivatives_stress_exhaustion",
    )
    monkeypatch.setattr(config, "kill_switch_file", lambda: str(halt))
    monkeypatch.setattr(
        bridge,
        "_download",
        lambda: (_ for _ in ()).throw(AssertionError("must not download")),
    )

    result = bridge.run_once()

    assert result == {"status": "SKIPPED", "reason": "entry halt is active"}


def test_bridge_requires_fresh_ready_acceptance_before_cloud_read(
    test_db,
    monkeypatch,
    tmp_path,
):
    now_us = int(time.time() * 1_000_000)
    payload = _payload(now_us)
    _configure(monkeypatch, test_db, tmp_path, payload)
    monkeypatch.setattr(
        bridge,
        "validate_acceptance_receipt",
        lambda *args, **kwargs: (False, "acceptance receipt is not READY", {}),
    )
    monkeypatch.setattr(
        bridge,
        "_download",
        lambda: (_ for _ in ()).throw(AssertionError("must not download")),
    )

    result = bridge.run_once(now_us=now_us)

    assert result == {
        "status": "SKIPPED",
        "reason": "acceptance receipt is not READY",
    }
