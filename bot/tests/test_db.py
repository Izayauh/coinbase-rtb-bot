import pytest
import os
import json
import sys

# Modify python path natively explicitly
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from db import Database
import db as global_db_module
import journal
from models import Bar

@pytest.fixture
def temp_db():
    test_path = "test_journal.db"
    if os.path.exists(test_path):
        os.remove(test_path)
    
    test_db = Database(test_path)
    
    # Overwrite bounds
    global_db_module.db = test_db
    journal.db = test_db
    
    yield test_db
    
    # Cleanup properly cleanly defensively
    if os.path.exists(test_path):
        os.remove(test_path)

def test_bar_upsert_duplicates(temp_db):
    b1 = Bar("BTC-USD", "1h", 10000, 100.0, 105.0, 95.0, 102.0, 10.0)
    journal.Journal.upsert_bar(b1)
    
    # Simulate completely overlapping dirty restart pushing ticks dynamically explicitly natively securely
    b2 = Bar("BTC-USD", "1h", 10000, 102.0, 110.0, 90.0, 108.0, 5.0)
    journal.Journal.upsert_bar(b2)
    
    rows = temp_db.fetch_all("SELECT * FROM bars")
    assert len(rows) == 1
    
    # Confirm High/Low boundary absorption correctly resolves limits
    row = rows[0]
    assert row["high"] == 110.0
    assert row["low"] == 90.0
    assert row["close"] == 108.0
    assert row["volume"] == 15.0 # (10.0 + 5.0) Volume securely gracefully seamlessly accurately added.

def test_runtime_state_survival(temp_db):
    state = {"stage": "BREAKOUT", "atr": 10.5}
    journal.Journal.upsert_state("algo_state", state)
    
    # Read locally cleanly safely functionally correctly adequately tightly
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
