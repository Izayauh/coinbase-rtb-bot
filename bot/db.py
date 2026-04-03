import sqlite3
import logging
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

class Database:
    def __init__(self, db_path="journal.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        schema = """
        CREATE TABLE IF NOT EXISTS bars (
            symbol TEXT,
            timeframe TEXT,
            ts_open INTEGER,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume REAL,
            PRIMARY KEY (symbol, timeframe, ts_open)
        );

        CREATE TABLE IF NOT EXISTS event_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER,
            event_type TEXT,
            message TEXT
        );

        CREATE TABLE IF NOT EXISTS runtime_state (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        
        CREATE TABLE IF NOT EXISTS signals (
            signal_id TEXT PRIMARY KEY,
            symbol TEXT,
            signal_type TEXT,
            regime_snapshot TEXT,
            breakout_level REAL,
            retest_level REAL,
            atr REAL,
            rsi REAL,
            status TEXT,
            execution_price REAL
        );

        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY,
            signal_id TEXT UNIQUE,
            symbol TEXT,
            side TEXT,
            price REAL,
            size REAL,
            executed_size REAL,
            status TEXT,
            created_at INTEGER,
            FOREIGN KEY(signal_id) REFERENCES signals(signal_id)
        );

        CREATE TABLE IF NOT EXISTS executions (
            execution_id TEXT PRIMARY KEY,
            order_id TEXT,
            price REAL,
            size REAL,
            fee REAL,
            ts INTEGER,
            FOREIGN KEY(order_id) REFERENCES orders(order_id)
        );

        CREATE TABLE IF NOT EXISTS positions (
            symbol TEXT PRIMARY KEY,
            entry_ts INTEGER,
            avg_entry REAL,
            current_size REAL,
            realized_pnl REAL,
            unrealized_pnl REAL,
            stop_price REAL,
            state TEXT
        );
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.executescript(schema)
                logger.info(f"Database initialized cleanly at {self.db_path}")
        except Exception as e:
            logger.error(f"Failed to initialize SQLite database: {e}")
            raise

    def execute(self, query: str, params: tuple = ()):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            conn.commit()
            return cursor

    def fetch_all(self, query: str, params: tuple = ()) -> List[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]

# Default singleton instance bound explicitly tightly to v0 architecture
db = Database()
