import pytest
import os
import json

from bot.db import Database
import bot.db as global_db_module
import bot.journal as journal
from bot.models import Bar

@pytest.fixture
def temp_db():
    import uuid
    test_path = f"test_journal_{uuid.uuid4().hex}.db"
    if os.path.exists(test_path):
        try:
            os.remove(test_path)
        except OSError:
            pass

    global_db_module.db.db_path = test_path
    global_db_module.db._init_db()

    yield global_db_module.db

    # Cleanup properly cleanly defensively
    if os.path.exists(test_path):
        try:
            os.remove(test_path)
        except OSError:
            pass

def test_bar_upsert_duplicates(temp_db):
    b1 = Bar("BTC-USD", "1h", 10000, 100.0, 105.0, 95.0, 102.0, 10.0)
    journal.Journal.upsert_bar(b1)

    b2 = Bar("BTC-USD", "1h", 10000, 102.0, 110.0, 90.0, 108.0, 5.0)
    journal.Journal.upsert_bar(b2)

    rows = temp_db.fetch_all("SELECT * FROM bars")
    assert len(rows) == 1

    row = rows[0]
    assert row["high"] == 110.0
    assert row["low"] == 90.0
    assert row["close"] == 108.0
    # Phase 4 fix: overwrite semantics — second upsert's volume wins
    assert row["volume"] == 5.0

def test_runtime_state_survival(temp_db):
    state = {"stage": "BREAKOUT", "atr": 10.5}
    journal.Journal.upsert_state("algo_state", state)

    read_state = journal.Journal.get_state("algo_state")
    assert read_state["stage"] == "BREAKOUT"
    assert read_state["atr"] == 10.5

    state["stage"] = "RETEST"
    journal.Journal.upsert_state("algo_state", state)

    read_state_2 = journal.Journal.get_state("algo_state")
    assert read_state_2["stage"] == "RETEST"

def test_event_log_append(temp_db):
    journal.Journal.append_event("SYSTEM", "Booting")
    journal.Journal.append_event("SYSTEM", "Shutdown")

    rows = temp_db.fetch_all("SELECT * FROM event_log ORDER BY id ASC")
    assert len(rows) == 2
    assert rows[0]["message"] == "Booting"
    assert rows[1]["message"] == "Shutdown"
