from research_pipeline.cli.derive import derive
from research_pipeline.config import load_config

SEC = 1_000_000


def test_derive_is_idempotent(store, run_id):
    rid, _ = store.append_raw(run_id, "coinbase_ws", "seed", {"derive": 1}, 0, 0)
    for i in range(12):
        t = i * 60 * SEC
        mid = 100 + i * 0.2
        store.insert_quote("BTC-USD", mid - 0.05, 2.0, mid + 0.05, 1.0, t, t, rid)
        store.insert_trade("BTC-USD", f"d{i}", mid, 0.1, "SELL", t, t, rid)

    cfg = load_config()
    first = derive(store, cfg, step_seconds=60, max_points=100)
    second = derive(store, cfg, step_seconds=60, max_points=100)
    assert first["feature_rows_inserted"] > 0
    assert first["label_rows_inserted"] > 0
    assert first["order_math_records_computed"] == first["decision_points"]
    assert second["feature_rows_inserted"] == 0
    assert second["label_rows_inserted"] == 0
    assert second["order_math_rows_inserted"] == 0


def test_derive_emits_terminal_book_features_from_order_math(store, run_id):
    raw_id, _ = store.append_raw(
        run_id, "coinbase_ws", "l2_data", {"snapshot": "derive"}, 0, 0
    )
    store.insert_l2_updates([
        ("BTC-USD", 1, 1, "snapshot", "bid", 100.0, 3.0, 0, 0, raw_id),
        ("BTC-USD", 1, 1, "snapshot", "bid", 99.9, 2.0, 0, 0, raw_id),
        ("BTC-USD", 1, 1, "snapshot", "offer", 100.1, 1.0, 0, 0, raw_id),
        ("BTC-USD", 1, 1, "snapshot", "offer", 100.2, 2.0, 0, 0, raw_id),
    ])
    for minute in range(2):
        time_us = minute * 60 * SEC
        store.insert_quote(
            "BTC-USD", 100.0, 3.0, 100.1, 1.0,
            time_us, time_us, raw_id,
        )

    result = derive(store, load_config(), step_seconds=60, max_points=10)

    assert {row["name"] for row in result["book_features"]} == {
        "depth_imbalance_10bps",
        "depth_imbalance_25bps",
        "multilevel_imbalance",
    }
