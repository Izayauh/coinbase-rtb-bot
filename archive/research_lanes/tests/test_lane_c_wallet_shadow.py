import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from research.lane_c_wallet_shadow import (
    build_consensus_events,
    consensus_to_signals,
    load_wallet_events,
)
from research.types import Bar


def _sample_csv_path() -> str:
    return os.path.join(
        os.path.dirname(__file__), "..", "examples", "lane_c_wallet_events_sample.csv"
    )


def test_wallet_csv_filters_and_maps_coinbase_assets():
    events, stats = load_wallet_events(_sample_csv_path())

    assert len(events) == 4
    assert stats["rows_seen"] == 8
    assert stats["drop_action"] == 1
    assert stats["drop_entity_flag"] == 1
    assert stats["drop_unmapped_symbol"] == 1
    assert stats["drop_notional"] == 1
    assert {e.coinbase_symbol for e in events} == {"ETH-USD", "SOL-USD"}


def test_consensus_builds_distinct_wallet_delayed_events():
    events, _ = load_wallet_events(_sample_csv_path())
    consensus = build_consensus_events(
        events,
        delay_hours=4,
        consensus_window_hours=24,
        min_wallets=2,
        min_consensus_score=2.0,
    )

    assert len(consensus) == 2
    assert [c.symbol for c in consensus] == ["ETH-USD", "SOL-USD"]
    assert all(c.wallet_count == 2 for c in consensus)


def test_consensus_maps_onto_bar_signals_without_duplicates():
    events, _ = load_wallet_events(_sample_csv_path())
    consensus = build_consensus_events(events, delay_hours=4)

    bars = [
        Bar(symbol="ETH-USD", timeframe="1h", ts=1740830400 + i * 3600, open=100, high=101, low=99, close=100, volume=10)
        for i in range(48)
    ]
    eth_consensus = [c for c in consensus if c.symbol == "ETH-USD"]
    signals = consensus_to_signals(bars, eth_consensus)

    assert len(signals) == 1
    assert signals[0].direction == "long"
    assert signals[0].rule_name == "lane_c_wallet_shadow"
