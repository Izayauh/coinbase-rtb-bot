import json

import authorize_strategy
from bot import config


def _patch_candidate_config(monkeypatch, output):
    monkeypatch.setattr(
        config,
        "strategy_id",
        lambda: "btc_derivatives_stress_exhaustion",
    )
    monkeypatch.setattr(config, "strategy_version", lambda: "1.0.0")
    monkeypatch.setattr(config, "symbol", lambda: "BTC-USD")
    monkeypatch.setattr(config, "max_order_size_usd", lambda: 15.0)
    monkeypatch.setattr(config, "max_position_size_usd", lambda: 30.0)
    monkeypatch.setattr(
        config,
        "strategy_authorization_file",
        lambda: str(output),
    )


def _evidence(path, complete):
    path.write_text(json.dumps({
        "evidence_status": "EVIDENCE_PASSED",
        "strategy_id": "btc_derivatives_stress_exhaustion",
        "strategy_version": "1.0.0",
        "product_id": "BTC-USD",
        "exit_contract": {
            "exit_contract_id": "derivatives_stress_exit_v1",
            "path_dependent_evidence_complete": complete,
        },
    }))


def test_authorization_blocks_incomplete_path_exit_evidence(
    tmp_path,
    monkeypatch,
):
    output = tmp_path / "authorization.json"
    evidence = tmp_path / "evidence.json"
    _patch_candidate_config(monkeypatch, output)
    _evidence(evidence, False)

    result = authorize_strategy.main([
        "--evidence",
        str(evidence),
        "--authorized-by",
        "operator",
    ])

    assert result == 1
    assert not output.exists()


def test_authorization_accepts_complete_matching_candidate_evidence(
    tmp_path,
    monkeypatch,
):
    output = tmp_path / "authorization.json"
    evidence = tmp_path / "evidence.json"
    _patch_candidate_config(monkeypatch, output)
    _evidence(evidence, True)

    result = authorize_strategy.main([
        "--evidence",
        str(evidence),
        "--authorized-by",
        "operator",
    ])

    assert result == 0
    payload = json.loads(output.read_text())
    assert payload["strategy_id"] == "btc_derivatives_stress_exhaustion"
    assert payload["authorization_hash"]
