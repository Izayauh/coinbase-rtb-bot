"""Microstructure feature tests: value, freshness, missing-data behavior, registry."""
from research_pipeline.features import (
    quoted_spread_bps, compute_quoted_spread_series, register_specs_as_variants, REGISTRY,
    top_of_book_imbalance, book_feature_values, trade_window_features,
    realized_volatility, compute_quote_feature_series,
    compute_order_math_series, microprice, multi_level_ofi,
)
from research_pipeline.book import OrderBook

SEC = 1_000_000


def test_quoted_spread_value_ok():
    q = {"recv_time_us": 100, "best_bid": 99.95, "best_ask": 100.05}
    val, flag, fresh = quoted_spread_bps(q, decision_time_us=100, max_stale_us=SEC)
    assert flag == "ok" and fresh == 0
    assert abs(val - (0.10 / 100.0 * 10_000)) < 1e-9  # ~10 bps


def test_quoted_spread_missing_quote():
    val, flag, fresh = quoted_spread_bps(None, 100, SEC)
    assert val is None and flag == "missing"


def test_quoted_spread_stale():
    q = {"recv_time_us": 0, "best_bid": 99.95, "best_ask": 100.05}
    val, flag, fresh = quoted_spread_bps(q, decision_time_us=2 * SEC, max_stale_us=SEC)
    assert val is None and flag == "stale" and fresh == 2 * SEC


def test_quoted_spread_crossed():
    q = {"recv_time_us": 100, "best_bid": 100.10, "best_ask": 100.00}
    val, flag, _ = quoted_spread_bps(q, 100, SEC)
    assert val is None and flag == "crossed"


def test_quoted_spread_missing_side_not_imputed():
    q = {"recv_time_us": 100, "best_bid": None, "best_ask": 100.05}
    val, flag, _ = quoted_spread_bps(q, 100, SEC)
    assert val is None and flag == "missing"


def test_top_of_book_imbalance():
    q = {
        "recv_time_us": 100,
        "best_bid_qty": 3.0,
        "best_ask_qty": 1.0,
    }
    value, flag, freshness = top_of_book_imbalance(q, 100, SEC)
    assert value == 0.5 and flag == "ok" and freshness == 0


def test_book_depth_features():
    book = OrderBook("BTC-USD")
    book.apply_snapshot(
        [(100.0, 3.0), (99.95, 2.0)],
        [(100.1, 1.0), (100.15, 1.0)],
        connection_epoch=1,
        event_time_us=100,
    )
    values = book_feature_values(book, 100, SEC)
    assert values["depth_imbalance_10bps"][0] > 0
    assert values["depth_imbalance_25bps"][1] == "ok"
    assert values["multilevel_imbalance"][0] > 0


def test_trade_features_respect_maker_side_semantics():
    rows = [
        {"event_time_us": 10, "side": "SELL", "size": 3.0},  # aggressive buy
        {"event_time_us": 20, "side": "BUY", "size": 1.0},   # aggressive sell
    ]
    values = trade_window_features(rows, 0, 30, 100)
    assert values["signed_trade_flow"] == (2.0, "ok")
    assert values["trade_intensity"][0] > 0


def test_realized_volatility_needs_and_uses_mids():
    rows = [
        {"recv_time_us": i, "best_bid": 99.9 + i, "best_ask": 100.1 + i}
        for i in range(5)
    ]
    value, flag = realized_volatility(rows, 0, 4)
    assert flag == "ok" and value is not None and value > 0


def test_compute_series_persists_and_flags(store, run_id):
    rid, _ = store.append_raw(run_id, "coinbase_ws", "ticker", {"x": 1}, 0, 0)
    store.insert_quote("BTC-USD", 99.95, 1.0, 100.05, 1.0, 100, 100, rid)
    # decision at 100 (fresh) and at 100+5s (stale -> None)
    rows = compute_quoted_spread_series(store, "BTC-USD", [100, 100 + 5 * SEC],
                                        max_stale_us=2 * SEC)
    assert rows[0]["value"] is not None and rows[0]["flags"] == "ok"
    assert rows[1]["value"] is None and rows[1]["flags"] == "stale"
    assert store.count("features") == 2  # null rows are still recorded (with flags)


def test_compute_all_quote_features_persists(store, run_id):
    rid, _ = store.append_raw(run_id, "coinbase_ws", "seed", {"all": 1}, 0, 0)
    for i in range(8):
        t = i * SEC
        store.insert_quote("BTC-USD", 99.9 + i * 0.1, 3.0, 100.1 + i * 0.1, 1.0,
                           t, t, rid)
        store.insert_trade("BTC-USD", f"t{i}", 100.0, 0.1, "SELL", t, t, rid)
    rows = compute_quote_feature_series(
        store, "BTC-USD", [7 * SEC], max_stale_us=2 * SEC,
        trade_window_us=5 * SEC, volatility_window_us=7 * SEC,
        max_trade_gap_us=2 * SEC,
    )
    assert {r["name"] for r in rows} == {
        "quoted_spread_bps",
        "top_of_book_imbalance",
        "signed_trade_flow",
        "trade_intensity",
        "realized_volatility",
    }
    assert all(r["value"] is not None for r in rows)
    assert store.count("features") == 5


def test_register_specs_as_variants(store):
    n = register_specs_as_variants(store)
    assert n == len(REGISTRY)
    assert store.count_variants("microstructure") == len(REGISTRY)
    assert sum(1 for s in REGISTRY.values() if s.implemented) == 9


def test_microprice_moves_toward_ask_when_bid_queue_is_larger():
    value = microprice(100.0, 100.2, 9.0, 1.0)
    assert value is not None and value > 100.1


def test_multi_level_ofi_detects_bid_add_and_ask_depletion():
    previous_bids = [(100.0, 1.0), (99.9, 1.0)]
    previous_asks = [(100.1, 2.0), (100.2, 2.0)]
    current_bids = [(100.0, 3.0), (99.9, 1.0)]
    current_asks = [(100.1, 1.0), (100.2, 2.0)]
    assert multi_level_ofi(
        previous_bids, previous_asks, current_bids, current_asks, 2
    ) > 0


def test_compute_order_math_series_persists_time_series(store, run_id):
    raw1, _ = store.append_raw(
        run_id, "coinbase_ws", "l2_data", {"snapshot": 1}, 0, 0
    )
    store.insert_l2_updates([
        ("BTC-USD", 1, 1, "snapshot", "bid", 100.0, 3.0, 0, 0, raw1),
        ("BTC-USD", 1, 1, "snapshot", "bid", 99.9, 2.0, 0, 0, raw1),
        ("BTC-USD", 1, 1, "snapshot", "offer", 100.1, 1.0, 0, 0, raw1),
        ("BTC-USD", 1, 1, "snapshot", "offer", 100.2, 2.0, 0, 0, raw1),
    ])
    raw2, _ = store.append_raw(
        run_id, "coinbase_ws", "l2_data", {"update": 1}, 30 * SEC, 30 * SEC
    )
    store.insert_l2_updates([
        ("BTC-USD", 1, 2, "update", "bid", 100.0, 5.0,
         30 * SEC, 30 * SEC, raw2),
        ("BTC-USD", 1, 2, "update", "offer", 100.1, 0.5,
         30 * SEC, 30 * SEC, raw2),
    ])
    rows = compute_order_math_series(
        store,
        "BTC-USD",
        [0, 60 * SEC],
        max_stale_us=120 * SEC,
    )
    assert len(rows) == 2
    assert rows[-1]["flags"] == "ok"
    assert rows[-1]["queue_imbalance"] > 0
    assert rows[-1]["ofi_60s"] > 0
    assert rows[-1]["bid_add_rate_60s"] > 0
    assert store.count("order_math") == 2
