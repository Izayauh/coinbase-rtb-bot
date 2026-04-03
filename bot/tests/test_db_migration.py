import os
import sqlite3
import pytest
from bot.db import Database

@pytest.fixture
def old_db_path(tmp_path):
    db_file = tmp_path / "old_schema.db"
    # Create the old schema without execution_price and executed_size
    schema = """
    CREATE TABLE IF NOT EXISTS signals (
        signal_id TEXT PRIMARY KEY,
        symbol TEXT,
        signal_type TEXT,
        regime_snapshot TEXT,
        breakout_level REAL,
        retest_level REAL,
        atr REAL,
        rsi REAL,
        status TEXT
    );

    CREATE TABLE IF NOT EXISTS orders (
        order_id TEXT PRIMARY KEY,
        signal_id TEXT UNIQUE,
        symbol TEXT,
        side TEXT,
        price REAL,
        size REAL,
        status TEXT,
        created_at INTEGER,
        FOREIGN KEY(signal_id) REFERENCES signals(signal_id)
    );
    """
    with sqlite3.connect(db_file) as conn:
        conn.executescript(schema)
    
    return str(db_file)

def test_migration_adds_missing_columns(old_db_path):
    # Initialize Database, which will run the migration
    db = Database(db_path=old_db_path)
    
    # Check that columns were added
    with sqlite3.connect(old_db_path) as conn:
        cursor = conn.cursor()
        
        # Check signals table
        cursor.execute("PRAGMA table_info(signals)")
        signals_cols = [row[1] for row in cursor.fetchall()]
        assert "execution_price" in signals_cols
        
        # Check orders table
        cursor.execute("PRAGMA table_info(orders)")
        orders_cols = [row[1] for row in cursor.fetchall()]
        assert "executed_size" in orders_cols

def test_migration_is_idempotent(old_db_path):
    # Initialize Database twice to ensure repeated runs are safe
    db1 = Database(db_path=old_db_path)
    db2 = Database(db_path=old_db_path)
    
    # Insert some data using the new schema
    db2.execute(
        "INSERT INTO signals (signal_id, symbol, execution_price) VALUES (?, ?, ?)",
        ("sig1", "BTC-USD", 50000.0)
    )
    
    db2.execute(
        "INSERT INTO orders (order_id, signal_id, executed_size) VALUES (?, ?, ?)",
        ("ord1", "sig1", 0.5)
    )
    
    # Confirm inserts using new columns succeed
    with sqlite3.connect(old_db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("SELECT * FROM signals WHERE signal_id='sig1'")
        signal = cursor.fetchone()
        assert signal["execution_price"] == 50000.0
        
        cursor.execute("SELECT * FROM orders WHERE order_id='ord1'")
        order = cursor.fetchone()
        assert order["executed_size"] == 0.5
