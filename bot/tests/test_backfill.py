"""
Historical backfill tests.

Tests:
  1. aggregate_4h groups 1h bars into correct 4h boundaries
  2. aggregate_4h excludes current (incomplete) 4h bar
  3. _parse_candles handles SDK object responses
  4. _parse_candles handles dict responses
  5. _parse_candles skips unparseable entries
  6. backfill_bars persists correct bar counts (mocked REST)
  7. backfill_bars raises when adapter has no credentials
  8. BarAggregator.ready() returns True after backfill
  9. Strategy readiness diagnostic prints NO with bar deficit
"""
import time
import types
import pytest

from bot.models import Bar
from bot.backfill import aggregate_4h, _parse_candles, backfill_bars


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_1h_bar(ts_open: int, price: float = 100.0, volume: float = 10.0) -> Bar:
    return Bar(
        symbol="BTC-USD", timeframe="1h", ts_open=ts_open,
        open=price, high=price + 5, low=price - 2, close=price + 1,
        volume=volume,
    )


# ---------------------------------------------------------------------------
# Test 1: aggregate_4h groups correctly
# ---------------------------------------------------------------------------

def test_aggregate_4h_groups_correctly():
    """8 consecutive 1h bars at epoch 0..7 should produce 2 complete 4h bars."""
    bars_1h = [_make_1h_bar(i * 3600, price=100 + i) for i in range(8)]

    # now=86400 → current boundary = 86400, which won't exclude our test bars
    result = aggregate_4h(bars_1h, "BTC-USD", now=86400)

    assert len(result) == 2

    # First 4h bar: boundary 0, contains hours 0-3
    b0 = result[0]
    assert b0.ts_open == 0
    assert b0.timeframe == "4h"
    assert b0.open == 100.0       # from ts=0 bar
    assert b0.close == 104.0      # from ts=10800 bar (i=3, close=103+1=104)
    assert b0.high == 108.0       # max(105,106,107,108)
    assert b0.low == 98.0         # min(98,99,100,101)
    assert b0.volume == 40.0      # 4 * 10

    # Second 4h bar: boundary 14400, contains hours 4-7
    b1 = result[1]
    assert b1.ts_open == 14400
    assert b1.open == 104.0       # i=4
    assert b1.close == 108.0      # i=7, close=107+1=108


# ---------------------------------------------------------------------------
# Test 2: aggregate_4h excludes current boundary
# ---------------------------------------------------------------------------

def test_aggregate_4h_excludes_current_boundary():
    """The 4h bar matching the current time boundary is excluded (incomplete)."""
    # Place bars in the current 4h window
    now = int(time.time())
    current_boundary = (now // 14400) * 14400
    bars_1h = [_make_1h_bar(current_boundary + i * 3600) for i in range(3)]

    result = aggregate_4h(bars_1h, "BTC-USD", now=now)

    # The only bucket is the current boundary, which should be excluded
    assert len(result) == 0


def test_aggregate_4h_keeps_completed_boundaries():
    """4h bars before the current boundary are kept."""
    now = int(time.time())
    current_boundary = (now // 14400) * 14400
    old_boundary = current_boundary - 14400  # previous 4h period

    bars_1h = [_make_1h_bar(old_boundary + i * 3600) for i in range(4)]

    result = aggregate_4h(bars_1h, "BTC-USD", now=now)
    assert len(result) == 1
    assert result[0].ts_open == old_boundary


# ---------------------------------------------------------------------------
# Test 3 & 4: _parse_candles
# ---------------------------------------------------------------------------

def test_parse_candles_sdk_objects():
    """Parse SDK response with .candles list of objects."""
    candle1 = types.SimpleNamespace(
        start="1700000000", open="34000", high="34500",
        low="33800", close="34200", volume="100.5",
    )
    candle2 = types.SimpleNamespace(
        start="1700003600", open="34200", high="34600",
        low="34100", close="34400", volume="80.3",
    )
    response = types.SimpleNamespace(candles=[candle1, candle2])

    bars = _parse_candles(response, "BTC-USD")

    assert len(bars) == 2
    assert bars[0].ts_open == 1700000000
    assert bars[0].open == 34000.0
    assert bars[0].close == 34200.0
    assert bars[1].ts_open == 1700003600


def test_parse_candles_dict_format():
    """Parse dict-style candle response."""
    response = {
        "candles": [
            {"start": "1700000000", "open": "34000", "high": "34500",
             "low": "33800", "close": "34200", "volume": "100.5"},
        ]
    }

    bars = _parse_candles(response, "BTC-USD")

    assert len(bars) == 1
    assert bars[0].symbol == "BTC-USD"
    assert bars[0].timeframe == "1h"
    assert bars[0].volume == 100.5


# ---------------------------------------------------------------------------
# Test 5: _parse_candles skips garbage
# ---------------------------------------------------------------------------

def test_parse_candles_skips_bad_entries():
    """Unparseable candle entries are skipped without crashing."""
    response = {
        "candles": [
            {"start": "1700000000", "open": "34000", "high": "34500",
             "low": "33800", "close": "34200", "volume": "100.5"},
            {"start": "bad", "open": "x"},  # garbage
            "not_a_dict",                    # garbage
        ]
    }

    bars = _parse_candles(response, "BTC-USD")
    assert len(bars) == 1


# ---------------------------------------------------------------------------
# Test 6: backfill_bars persists to DB
# ---------------------------------------------------------------------------

def test_backfill_bars_persists_correct_counts(test_db):
    """With mocked REST, backfill inserts 1h and 4h bars into the DB."""
    from bot.db import db

    now = int(time.time())
    # Create mock candles covering 12 hours (3 complete 4h bars)
    mock_candles = []
    for i in range(12):
        ts = now - (13 - i) * 3600  # 13h ago to 1h ago
        # Align to 1h boundary
        ts = (ts // 3600) * 3600
        mock_candles.append(types.SimpleNamespace(
            start=str(ts), open="50000", high="50500",
            low="49800", close="50200", volume="10.0",
        ))

    class MockREST:
        def get_candles(self, **kw):
            return types.SimpleNamespace(candles=mock_candles)

    adapter = types.SimpleNamespace(
        _enabled=True,
        rest=MockREST(),
    )

    result = backfill_bars(adapter, "BTC-USD", target_1h=12)

    assert result["bars_1h"] > 0
    assert result["bars_4h"] > 0

    # Verify DB has bars
    rows_1h = db.fetch_all(
        "SELECT COUNT(*) as c FROM bars WHERE timeframe='1h'"
    )
    assert rows_1h[0]["c"] > 0

    rows_4h = db.fetch_all(
        "SELECT COUNT(*) as c FROM bars WHERE timeframe='4h'"
    )
    assert rows_4h[0]["c"] > 0


# ---------------------------------------------------------------------------
# Test 7: backfill_bars raises when no creds
# ---------------------------------------------------------------------------

def test_backfill_raises_without_credentials():
    """backfill_bars raises RuntimeError when adapter._enabled is False."""
    adapter = types.SimpleNamespace(_enabled=False)

    with pytest.raises(RuntimeError, match="no credentials"):
        backfill_bars(adapter, "BTC-USD")


# ---------------------------------------------------------------------------
# Test 8: Aggregator ready after backfill
# ---------------------------------------------------------------------------

def test_aggregator_ready_after_sufficient_backfill(test_db):
    """BarAggregator.ready() returns True when DB has enough backfilled bars."""
    from bot.db import db
    from bot.aggregator import BarAggregator

    now = int(time.time())

    # Insert 830 1h bars
    for i in range(830):
        ts = now - (831 - i) * 3600
        ts = (ts // 3600) * 3600
        db.execute(
            "INSERT OR REPLACE INTO bars "
            "(symbol, timeframe, ts_open, open, high, low, close, volume) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("BTC-USD", "1h", ts, 50000, 50500, 49800, 50200, 10.0),
        )

    # Insert 210 4h bars
    for i in range(210):
        ts = now - (211 - i) * 14400
        ts = (ts // 14400) * 14400
        db.execute(
            "INSERT OR REPLACE INTO bars "
            "(symbol, timeframe, ts_open, open, high, low, close, volume) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("BTC-USD", "4h", ts, 50000, 50500, 49800, 50200, 10.0),
        )

    agg = BarAggregator("BTC-USD")

    assert agg.ready() is True
    assert len(agg.get_bars_1h()) >= 25
    assert len(agg.get_bars_4h()) >= 205


# ---------------------------------------------------------------------------
# Test 9: Aggregator not ready without backfill
# ---------------------------------------------------------------------------

def test_aggregator_not_ready_without_bars(test_db):
    """BarAggregator.ready() returns False when DB is empty."""
    from bot.aggregator import BarAggregator

    agg = BarAggregator("BTC-USD")

    assert agg.ready() is False
    assert len(agg.get_bars_1h()) == 0
    assert len(agg.get_bars_4h()) == 0
