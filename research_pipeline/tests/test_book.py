"""Order-book reconstruction + health tests."""
from research_pipeline.book import OrderBook, replay_l2_rows


def _seed():
    b = OrderBook("BTC-USD")
    b.apply_snapshot(bids=[(100.0, 1.0), (99.95, 2.0)],
                     asks=[(100.10, 1.5), (100.20, 3.0)],
                     connection_epoch=1, event_time_us=1_000)
    return b


def test_snapshot_best_levels_and_validity():
    b = _seed()
    assert b.valid and b.invalid_reason == "OK"
    assert b.best_bid() == 100.0 and b.best_ask() == 100.10
    assert abs(b.mid() - 100.05) < 1e-9
    assert b.spread_bps() is not None and b.spread_bps() > 0


def test_update_absolute_quantity_and_removal():
    b = _seed()
    b.apply_update("bid", 100.05, 5.0, connection_epoch=1, event_time_us=2_000)  # add level
    assert b.best_bid() == 100.05 and b.bids[100.05] == 5.0
    b.apply_update("bid", 100.05, 0.0, connection_epoch=1, event_time_us=3_000)  # remove
    assert 100.05 not in b.bids and b.best_bid() == 100.0


def test_crossed_book_is_invalid():
    b = _seed()
    b.apply_update("offer", 99.0, 1.0, connection_epoch=1, event_time_us=2_000)  # ask below bid
    h = b.health()
    assert not h.valid and h.reason == "CROSSED"


def test_gap_invalidates_until_snapshot():
    b = _seed()
    b.invalidate("GAP")
    # updates while gapped are ignored; book stays invalid
    b.apply_update("bid", 100.0, 9.0, connection_epoch=1, event_time_us=2_000)
    assert not b.valid and b.invalid_reason == "GAP"
    # a fresh snapshot rebuilds it
    b.apply_snapshot([(100.0, 1.0)], [(100.1, 1.0)], connection_epoch=1, event_time_us=4_000)
    assert b.valid


def test_reconnect_epoch_invalidates():
    b = _seed()
    b.apply_update("bid", 100.0, 2.0, connection_epoch=2, event_time_us=2_000)
    assert not b.valid and b.invalid_reason == "RECONNECT"


def test_depth_within_bps():
    b = _seed()
    # best bid 100.0; 10 bps floor = 99.9 -> includes 100.0 (1.0) and 99.95 (2.0)
    assert abs(b.depth_within_bps("bid", 10) - 3.0) < 1e-9
    # best ask 100.10; 10 bps cap = 100.2001 -> includes 100.10 (1.5) and 100.20 (3.0)
    assert abs(b.depth_within_bps("ask", 10) - 4.5) < 1e-9


def test_stale_health():
    b = _seed()
    h = b.health(now_us=1_000 + 10_000_000, max_stale_us=5_000_000)
    assert not h.valid and h.reason == "STALE"


def test_replay_preserves_multiple_snapshot_boundaries():
    rows = [
        {"raw_id": 1, "update_kind": "snapshot", "side": "bid",
         "price_level": 100.0, "new_quantity": 1.0, "connection_epoch": 1,
         "event_time_us": 1},
        {"raw_id": 1, "update_kind": "snapshot", "side": "offer",
         "price_level": 100.1, "new_quantity": 1.0, "connection_epoch": 1,
         "event_time_us": 1},
        {"raw_id": 2, "update_kind": "update", "side": "bid",
         "price_level": 100.0, "new_quantity": 2.0, "connection_epoch": 1,
         "event_time_us": 2},
        {"raw_id": 3, "update_kind": "snapshot", "side": "bid",
         "price_level": 200.0, "new_quantity": 3.0, "connection_epoch": 2,
         "event_time_us": 3},
        {"raw_id": 3, "update_kind": "snapshot", "side": "offer",
         "price_level": 200.2, "new_quantity": 4.0, "connection_epoch": 2,
         "event_time_us": 3},
    ]
    book, summary = replay_l2_rows("BTC-USD", rows)
    assert summary == {"snapshots_applied": 2, "updates_applied": 1}
    assert book.valid and book.best_bid() == 200.0 and book.best_ask() == 200.2
