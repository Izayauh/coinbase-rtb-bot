import pytest, uuid, os
import bot.db as global_db
from bot.state_machine import StateMachine
from bot.journal import Journal

def test_1():
    test_path = f"test_{uuid.uuid4().hex}.db"
    global_db.db.db_path = test_path
    global_db.db._init_db()
    sm = StateMachine()
    print("Test 1 state:", sm.state)
    Journal.upsert_state("algo_state", {"state": "WAITING_RETEST"})
    print("Test 1 post-upsert state from db:", Journal.get_state("algo_state"))

def test_2():
    test_path = f"test_{uuid.uuid4().hex}.db"
    global_db.db.db_path = test_path
    global_db.db._init_db()
    sm = StateMachine()
    print("Test 2 state:", sm.state)

test_1()
test_2()
