import json
import math
from datetime import datetime, timezone

from research_pipeline.cli.evaluate_derivatives_stress import (
    _net_return,
    _operational_gates,
)
from research_pipeline.config import CostModel
from research_pipeline.labeling.labeler import LabelEngine, QuoteSeries
from research_pipeline.cli.verify_candidate_runtime import verify_runtime


def test_cross_shard_return_math_matches_replay_labeler():
    cm = CostModel()
    t = 1_000_000
    horizon = 3_600_000_000
    entry = {
        "recv_time_us": t,
        "best_bid": 99.9,
        "best_ask": 100.1,
    }
    exit_quote = {
        "recv_time_us": t + horizon,
        "best_bid": 101.9,
        "best_ask": 102.1,
    }
    label = LabelEngine(cm, max_quote_staleness_us=2_000_000).label_one(
        QuoteSeries([entry, exit_quote]),
        "BTC-USD",
        t,
        "1h",
        horizon,
        sensitivity=2.0,
    )
    result = _net_return(
        entry["best_bid"],
        entry["best_ask"],
        exit_quote["best_bid"],
        exit_quote["best_ask"],
        cm,
        2.0,
    )
    assert label is not None
    assert math.isclose(result, label["net_return"], abs_tol=1e-15)


def test_current_verified_shard_can_supply_operational_gates_without_history():
    report = {
        "shard_id": "20260620T120000Z",
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "RUNNING",
        "stages": {
            "derive": {
                "stdout": json.dumps(
                    {"health": {"counts": {"gaps": 0}}}
                )
            },
            "upload": {"returncode": 0},
            "query_mirror": {
                "objects_verified": 3,
                "tables": ["order_math", "quotes", "context_events"],
            },
        },
    }
    gates, evidence = _operational_gates(
        bucket=None,
        prefix="coinbase/BTC-USD/shards",
        project="bitwise-trader",
        current_verification_passed=True,
        current_report=report,
    )
    assert gates == {
        "replay_parity": True,
        "freshness": True,
        "outage": True,
        "storage": True,
    }
    assert evidence["current_shard_included"] is True


def test_production_runtime_verifier_covers_episode_and_replay_parity():
    result = verify_runtime()
    assert result["status"] == "VERIFIED"
    assert result["candidate_episode_check"] is True
    assert result["replay_cost_parity_check"] is True
    assert result["online_order_math_parity_check"] is True
    assert result["exit_contract_check"] is True
    assert result["live_authority_granted"] is False
