"""Storage tests: migrations, append-only raw evidence, hashing, dedup, retention."""
import sqlite3

import pytest

from research_pipeline.storage import (
    ResearchStore, AppendOnlyError, canonical_json, payload_sha256,
)
from research_pipeline.storage.schema import SCHEMA_VERSION


def test_migrations_apply_and_set_user_version(store):
    v = store.conn.execute("PRAGMA user_version").fetchone()[0]
    assert v == SCHEMA_VERSION
    # All expected tables exist.
    names = {r[0] for r in store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"sources", "ingestion_runs", "raw_events", "trades", "l2_updates",
            "quotes", "gaps", "labels", "features", "variant_registry",
            "context_events", "order_math"} <= names


def test_migrations_idempotent(tmp_path):
    p = str(tmp_path / "r.db")
    s1 = ResearchStore(p); s1.close()
    s2 = ResearchStore(p)  # re-open: migrations must not double-apply
    assert s2.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    s2.close()


def test_payload_hash_is_stable_and_order_independent():
    a = {"b": 1, "a": [3, 2, 1]}
    b = {"a": [3, 2, 1], "b": 1}
    assert payload_sha256(a) == payload_sha256(b)
    assert canonical_json(a) == '{"a":[3,2,1],"b":1}'


def test_raw_append_only_blocks_update(store, run_id):
    rid, inserted = store.append_raw(run_id, "coinbase_ws", "market_trades",
                                     {"x": 1}, 1, 2)
    assert inserted
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute("UPDATE raw_events SET payload='tampered' WHERE id=?", (rid,))


def test_raw_append_only_blocks_delete(store, run_id):
    rid, _ = store.append_raw(run_id, "coinbase_ws", "ticker", {"y": 2}, 1, 2)
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute("DELETE FROM raw_events WHERE id=?", (rid,))


def test_raw_dedup_on_payload_hash(store, run_id):
    p = {"channel": "ticker", "n": 7}
    id1, ins1 = store.append_raw(run_id, "coinbase_ws", "ticker", p, 1, 2)
    id2, ins2 = store.append_raw(run_id, "coinbase_ws", "ticker", p, 1, 9)
    assert ins1 is True and ins2 is False
    assert id1 == id2
    assert store.count("raw_events") == 1


def test_trade_insert_idempotent(store, run_id):
    rid, _ = store.append_raw(run_id, "coinbase_ws", "market_trades", {"t": 1}, 1, 2)
    assert store.insert_trade("BTC-USD", "abc", 100.0, 0.1, "BUY", 1, 2, rid) is True
    assert store.insert_trade("BTC-USD", "abc", 100.0, 0.1, "BUY", 1, 2, rid) is False
    assert store.count("trades") == 1


def test_l2_batch_insert_is_atomic_and_complete(store, run_id):
    rid, _ = store.append_raw(run_id, "coinbase_ws", "l2_data", {"l2": 1}, 1, 2)
    rows = [
        ("BTC-USD", 1, 10, "snapshot", "bid", 100.0, 1.0, 1, 2, rid),
        ("BTC-USD", 1, 10, "snapshot", "offer", 100.1, 2.0, 1, 2, rid),
    ]
    assert store.insert_l2_updates(rows) == 2
    assert store.count("l2_updates") == 2


def test_retention_refuses_to_delete_unexported_raw(store, run_id):
    store.append_raw(run_id, "coinbase_ws", "ticker", {"z": 1}, 1, 2)
    with pytest.raises(AppendOnlyError):
        store.prune_derived(before_us=10**18, allow_raw_prune=True)
    # Raw evidence still present after the refusal.
    assert store.count("raw_events") == 1


def test_retention_prunes_derived_only(store, run_id):
    store.insert_feature("quoted_spread_bps", "v1", "BTC-USD", 100, 1.0, 0)
    store.insert_feature("quoted_spread_bps", "v1", "BTC-USD", 10**18, 1.0, 0)
    deleted = store.prune_derived(before_us=10**12, allow_raw_prune=False)
    assert deleted["features"] == 1
    assert deleted["raw_events"] == 0
    assert store.count("features") == 1


def test_derived_rows_are_idempotent(store):
    feature_args = (
        "quoted_spread_bps", "v1", "BTC-USD", 100, 1.0, 0, "ok", "same"
    )
    assert store.insert_feature(*feature_args) > 0
    assert store.insert_feature(*feature_args) == 0
    assert store.count("features") == 1

    label = {
        "product_id": "BTC-USD", "decision_time_us": 100, "horizon": "5m",
        "entry_side": "BUY", "entry_price": 100.0, "exit_side": "SELL",
        "exit_price": 101.0, "gross_return": 0.01, "fee_component": -0.001,
        "slippage_component": -0.001, "adverse_selection_component": -0.001,
        "spread_component": -0.001, "net_return": 0.006, "mfe": 0.02,
        "mae": -0.01, "valid": 1, "invalid_reason": None,
        "quote_source": "ticker", "sensitivity": 1.0,
        "cost_model_version": "cost_model_v1", "replay_version": "replay_v1",
    }
    assert store.insert_label(label) > 0
    assert store.insert_label(label) == 0
    assert store.count("labels") == 1


def test_context_records_preserve_vintages(store):
    base = {
        "source_id": "cpi_bls", "source_kind": "cpi_bls",
        "native_id": "CUUR0000SA0:2026:M05",
        "availability_time_us": 100, "event_time_us": 50,
        "url": "https://example.test", "parser_version": "v1",
        "payload": {"value": "320.0"},
    }
    assert store.insert_context({**base, "vintage": "2026-06-01"}) is True
    assert store.insert_context({**base, "vintage": "2026-06-01"}) is False
    assert store.insert_context({
        **base, "vintage": "2026-07-01", "payload": {"value": "320.1"},
    }) is True
    assert store.count("context_events") == 2


def test_order_math_rows_are_idempotent(store):
    record = {
        "feature_version": "order_math_v1",
        "product_id": "BTC-USD",
        "event_time_us": 100,
        "freshness_us": 0,
        "connection_epoch": 1,
        "best_bid": 99.0,
        "best_ask": 101.0,
        "mid": 100.0,
        "flags": "ok",
        "inputs_hash": "abc",
    }
    assert store.insert_order_math(record) > 0
    assert store.insert_order_math(record) == 0
    assert store.count("order_math") == 1


def test_refuses_journal_database_paths(tmp_path):
    for name in ("journal.db", "live_journal.db", "paper_journal.db"):
        with pytest.raises(AppendOnlyError):
            ResearchStore(str(tmp_path / name))
