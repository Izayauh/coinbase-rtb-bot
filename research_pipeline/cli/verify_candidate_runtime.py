"""Deterministic production verification for candidate and replay semantics."""
from __future__ import annotations

import json
import math
from tempfile import TemporaryDirectory
from pathlib import Path

from ..advisory import build_exit_contract
from ..candidates.derivatives_stress import (
    MIN_FUNDING_HISTORY,
    MIN_MINUTE_HISTORY,
    build_candidate_decisions,
)
from ..config import CostModel
from ..labeling.labeler import LabelEngine, QuoteSeries
from ..collectors import ingest_frame
from ..features import OnlineOrderMathSampler, compute_order_math_series
from ..storage import ResearchStore
from .evaluate_derivatives_stress import _net_return


MINUTE_US = 60_000_000


def _row(index: int, *, shock: bool = False, recovery: bool = False) -> dict:
    mid = 100.0 + index * 0.001
    open_interest = 1000.0 + index * 0.01
    funding = -0.000001 + (index % 24) * 0.00000001
    mark = mid * 1.0001
    if shock:
        mid *= 0.97
        open_interest *= 0.97
        funding = -0.001
        mark = mid * 0.98
    return {
        "event_time_us": index * MINUTE_US,
        "mid": mid,
        "open_interest": open_interest,
        "funding_event_time_us": (index // 60) * 60 * MINUTE_US,
        "funding_rate": funding,
        "mark_price": mark,
        "depth_imbalance_10bps": 0.3 if recovery else -0.2,
        "ofi_60s": 10.0 if recovery else -1.0,
        "microprice_delta_bps": 0.2 if recovery else -0.1,
        "bid_replenishment_ratio": 1.4 if recovery else 0.9,
        "ask_replenishment_ratio": 0.8 if recovery else 1.1,
    }


def verify_runtime() -> dict:
    total = max(
        MIN_MINUTE_HISTORY + 20,
        MIN_FUNDING_HISTORY * 60 + 20,
    )
    rows = [_row(index) for index in range(total)]
    for index, row in enumerate(rows):
        row["mid"] += ((index % 17) - 8) * 0.01
        row["open_interest"] += ((index % 19) - 9) * 0.2
        row["mark_price"] = (
            row["mid"] * (1 + ((index % 13) - 6) * 0.0001)
        )
        row["funding_rate"] = ((index // 60) % 11 - 5) * 0.00001
    for index in range(total - 5, total):
        rows[index].update(_row(index, shock=True, recovery=True))
        rows[index]["funding_event_time_us"] = index * MINUTE_US

    candidate = build_candidate_decisions(rows)
    strict_count = len(candidate["decisions"]["combined_strict_v1"])
    if strict_count != 1:
        raise RuntimeError(
            f"candidate episode verification failed: strict_count={strict_count}"
        )

    cm = CostModel()
    decision_time = 1_000_000
    horizon_us = 3_600_000_000
    entry = {
        "recv_time_us": decision_time,
        "best_bid": 99.9,
        "best_ask": 100.1,
    }
    exit_quote = {
        "recv_time_us": decision_time + horizon_us,
        "best_bid": 101.9,
        "best_ask": 102.1,
    }
    label = LabelEngine(
        cm,
        max_quote_staleness_us=2_000_000,
    ).label_one(
        QuoteSeries([entry, exit_quote]),
        "BTC-USD",
        decision_time,
        "1h",
        horizon_us,
        sensitivity=2.0,
    )
    cross_shard = _net_return(
        entry["best_bid"],
        entry["best_ask"],
        exit_quote["best_bid"],
        exit_quote["best_ask"],
        cm,
        2.0,
    )
    if label is None or not math.isclose(
        cross_shard,
        label["net_return"],
        abs_tol=1e-15,
    ):
        raise RuntimeError("cross-shard return math does not match replay labeler")

    exit_contract = build_exit_contract(
        candidate["decisions"]["combined_strict_v1"][0]
    )
    if not (
        exit_contract["stop_price"]
        < exit_contract["entry_reference"]
        < exit_contract["target_price"]
        and exit_contract["time_stop_seconds"] == 14_400
    ):
        raise RuntimeError("candidate exit contract is invalid")

    with TemporaryDirectory() as temp_dir:
        store = ResearchStore(str(Path(temp_dir) / "parity.db"))
        run_id = store.start_run("runtime_verifier", {})
        sampler = OnlineOrderMathSampler(
            store,
            "BTC-USD",
            max_stale_us=5_000_000,
        )
        frames = [
            ({
                "channel": "l2_data",
                "sequence_num": 1,
                "events": [{
                    "type": "snapshot",
                    "product_id": "BTC-USD",
                    "updates": [
                        {"side": "bid", "price_level": "99.9", "new_quantity": "2"},
                        {"side": "offer", "price_level": "100.1", "new_quantity": "1"},
                    ],
                }],
            }, 30_000_000),
            ({
                "channel": "l2_data",
                "sequence_num": 2,
                "events": [{
                    "type": "update",
                    "product_id": "BTC-USD",
                    "updates": [
                        {"side": "bid", "price_level": "99.9", "new_quantity": "3"},
                        {"side": "offer", "price_level": "100.1", "new_quantity": "0.5"},
                    ],
                }],
            }, 90_000_000),
        ]
        for frame, recv_time_us in frames:
            ingest_frame(
                store,
                run_id,
                "coinbase_ws",
                frame,
                recv_time_us,
                1,
            )
            sampler.observe_frame(frame, recv_time_us, 1)
        online = dict(store.conn.execute(
            "SELECT * FROM order_math WHERE event_time_us=60000000"
        ).fetchone())
        replay = compute_order_math_series(
            store,
            "BTC-USD",
            [60_000_000],
            5_000_000,
            persist=False,
        )[0]
        store.end_run(run_id, "OK")
        store.close()
    for key, value in replay.items():
        if online[key] != value:
            raise RuntimeError(f"online/replay order math mismatch at {key}")

    return {
        "status": "VERIFIED",
        "candidate_episode_check": True,
        "replay_cost_parity_check": True,
        "online_order_math_parity_check": True,
        "exit_contract_check": True,
        "strict_episode_count": strict_count,
        "live_authority_granted": False,
    }


def main(argv=None) -> int:
    del argv
    print(json.dumps(verify_runtime(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
