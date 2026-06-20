"""Collector normalization tests: timestamp parsing, trade/ticker/l2 ingest, idempotency."""
import asyncio

from research_pipeline.collectors import (
    CoinbaseCollector, CoinbaseIntxPoller, ingest_frame, iso_to_us,
)
from research_pipeline.context import ContextRecord
from research_pipeline.features import (
    OnlineOrderMathSampler,
    compute_order_math_series,
)


def test_iso_to_us_handles_z_and_nanoseconds():
    # nanosecond precision truncated to micros; 'Z' -> UTC
    us = iso_to_us("2026-06-19T04:00:00.123456789Z")
    assert us == iso_to_us("2026-06-19T04:00:00.123456Z")
    assert iso_to_us("2026-06-19T04:00:00Z") % 1_000_000 == 0
    assert iso_to_us(None) is None
    assert iso_to_us("garbage") is None


def _trades_frame(seq=5):
    return {
        "channel": "market_trades", "sequence_num": seq,
        "timestamp": "2026-06-19T04:00:00.500000000Z",
        "events": [{"type": "update", "trades": [
            {"trade_id": "t1", "product_id": "BTC-USD", "price": "62000.5",
             "size": "0.01", "side": "BUY", "time": "2026-06-19T04:00:00.100000000Z"},
            {"trade_id": "t2", "product_id": "BTC-USD", "price": "62001.0",
             "size": "0.02", "side": "SELL", "time": "2026-06-19T04:00:00.200000000Z"},
        ]}]}


def _ticker_frame(seq=6):
    return {
        "channel": "ticker", "sequence_num": seq,
        "timestamp": "2026-06-19T04:00:00.600000000Z",
        "events": [{"type": "snapshot", "tickers": [
            {"product_id": "BTC-USD", "price": "62000", "best_bid": "61999.99",
             "best_bid_quantity": "0.5", "best_ask": "62000.01", "best_ask_quantity": "0.4"}]}]}


def _l2_frame(seq=7):
    return {
        "channel": "l2_data", "sequence_num": seq,
        "timestamp": "2026-06-19T04:00:00.700000000Z",
        "events": [{"type": "snapshot", "product_id": "BTC-USD", "updates": [
            {"side": "bid", "event_time": "2026-06-19T04:00:00.650000000Z",
             "price_level": "61999.99", "new_quantity": "0.5"},
            {"side": "offer", "event_time": "2026-06-19T04:00:00.650000000Z",
             "price_level": "62000.01", "new_quantity": "0.4"}]}]}


def test_ingest_market_trades(store, run_id):
    s = ingest_frame(store, run_id, "coinbase_ws", _trades_frame(), 1_000, 1)
    assert s["raw_inserted"] == 1 and s["trades"] == 2
    assert store.count("trades") == 2
    row = store.conn.execute("SELECT * FROM trades WHERE trade_id='t1'").fetchone()
    assert row["price"] == 62000.5 and row["side"] == "BUY"
    assert row["event_time_us"] == iso_to_us("2026-06-19T04:00:00.100000000Z")


def test_ingest_ticker_quote(store, run_id):
    s = ingest_frame(store, run_id, "coinbase_ws", _ticker_frame(), 1_000, 1)
    assert s["quotes"] == 1
    q = store.conn.execute("SELECT * FROM quotes").fetchone()
    assert q["best_bid"] == 61999.99 and q["best_ask"] == 62000.01
    assert q["best_bid_qty"] == 0.5 and q["best_ask_qty"] == 0.4


def test_ingest_level2_as_l2_data(store, run_id):
    s = ingest_frame(store, run_id, "coinbase_ws", _l2_frame(), 1_000, 1)
    assert s["l2"] == 2
    rows = list(store.conn.execute("SELECT * FROM l2_updates ORDER BY id"))
    assert {r["side"] for r in rows} == {"bid", "offer"}
    assert rows[0]["update_kind"] == "snapshot" and rows[0]["new_quantity"] == 0.5


def test_ingest_is_idempotent(store, run_id):
    f = _trades_frame()
    s1 = ingest_frame(store, run_id, "coinbase_ws", f, 1_000, 1)
    s2 = ingest_frame(store, run_id, "coinbase_ws", f, 2_000, 1)  # same payload -> dedup
    assert s1["raw_inserted"] == 1 and s2["raw_inserted"] == 0
    assert s2["trades"] == 0                      # normalization gated on raw insert
    assert store.count("raw_events") == 1 and store.count("trades") == 2


def test_collector_defaults_allow_large_public_l2_snapshot(store):
    collector = CoinbaseCollector(store, ["BTC-USD"])
    assert "level2" in collector.channels
    assert collector.max_message_bytes >= 4 * 1024 * 1024


def test_intx_poller_persists_derivatives_context(store):
    class FakeAdapter:
        def fetch(self, _since_us, _until_us):
            return [ContextRecord(
                source_id="coinbase_intx_btc_perp",
                source_kind="funding_oi",
                native_id="BTC-PERP:open_interest:test",
                vintage="v1",
                availability_time_us=1,
                event_time_us=1,
                url="https://api.international.coinbase.com/test",
                parser_version="test_v1",
                payload={
                    "open_interest": "1.5",
                    "availability_basis": "retrieval_time_conservative",
                },
            )]

    result = asyncio.run(CoinbaseIntxPoller(
        store, poll_seconds=0, adapter=FakeAdapter()
    ).run(max_seconds=0))

    assert result["polls"] == 1
    assert result["inserted"] == 1
    assert store.count("context_events") == 1


def test_online_order_math_matches_closed_shard_replay(store, run_id):
    sampler = OnlineOrderMathSampler(
        store,
        "BTC-USD",
        max_stale_us=5_000_000,
    )
    snapshot = {
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
    }
    update_one = {
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
    }
    update_two = {
        "channel": "l2_data",
        "sequence_num": 3,
        "events": [{
            "type": "update",
            "product_id": "BTC-USD",
            "updates": [
                {"side": "bid", "price_level": "99.9", "new_quantity": "4"},
                {"side": "offer", "price_level": "100.1", "new_quantity": "0.25"},
            ],
        }],
    }
    for frame, recv_time in (
        (snapshot, 30_000_000),
        (update_one, 90_000_000),
        (update_two, 150_000_000),
    ):
        ingest_frame(store, run_id, "coinbase_ws", frame, recv_time, 1)
        sampler.observe_frame(frame, recv_time, 1)

    online = [
        dict(row)
        for row in store.conn.execute(
            "SELECT * FROM order_math ORDER BY event_time_us"
        )
    ]
    replay = compute_order_math_series(
        store,
        "BTC-USD",
        [60_000_000, 120_000_000],
        5_000_000,
        persist=False,
    )
    assert len(online) == len(replay) == 2
    for stored, expected in zip(online, replay):
        for key, value in expected.items():
            assert stored[key] == value
