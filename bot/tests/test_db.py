import pytest

import bot.journal as journal
from bot.models import Bar


def test_bar_upsert_duplicates(test_db):
    b1 = journal.Journal.upsert_bar(Bar("BTC-USD", "1h", 10000, 100.0, 105.0, 95.0, 102.0, 10.0))
    b2 = journal.Journal.upsert_bar(Bar("BTC-USD", "1h", 10000, 102.0, 110.0, 90.0, 108.0, 5.0))

    rows = test_db.fetch_all("SELECT * FROM bars")
    assert len(rows) == 1

    row = rows[0]
    assert row["high"] == 110.0
    assert row["low"] == 90.0
    assert row["close"] == 108.0
    # Overwrite semantics: second upsert's volume wins, no accumulation
    assert row["volume"] == 5.0


def test_runtime_state_survival(test_db):
    state = {"stage": "BREAKOUT", "atr": 10.5}
    journal.Journal.upsert_state("algo_state", state)

    read_state = journal.Journal.get_state("algo_state")
    assert read_state["stage"] == "BREAKOUT"
    assert read_state["atr"] == 10.5

    state["stage"] = "RETEST"
    journal.Journal.upsert_state("algo_state", state)

    read_state_2 = journal.Journal.get_state("algo_state")
    assert read_state_2["stage"] == "RETEST"


def test_event_log_append(test_db):
    journal.Journal.append_event("SYSTEM", "Booting")
    journal.Journal.append_event("SYSTEM", "Shutdown")

    rows = test_db.fetch_all("SELECT * FROM event_log ORDER BY id ASC")
    assert len(rows) == 2
    assert rows[0]["message"] == "Booting"
    assert rows[1]["message"] == "Shutdown"
