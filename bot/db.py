import sqlite3
import logging
import time
from contextlib import closing
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

class Database:
    BUSY_TIMEOUT_MS = 30000
    LOCK_RETRIES = 6

    def __init__(self, db_path="journal.db"):
        self.db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            timeout=self.BUSY_TIMEOUT_MS / 1000,
        )
        conn.execute(f"PRAGMA busy_timeout={self.BUSY_TIMEOUT_MS}")
        return conn

    @staticmethod
    def _is_busy(exc: BaseException) -> bool:
        text = str(exc).lower()
        return "database is locked" in text or "database is busy" in text

    def _retry(self, operation):
        for attempt in range(self.LOCK_RETRIES):
            try:
                return operation()
            except sqlite3.OperationalError as exc:
                if not self._is_busy(exc) or attempt == self.LOCK_RETRIES - 1:
                    raise
                delay = min(0.1 * (2 ** attempt), 2.0)
                logger.warning(
                    "SQLite busy at %s; retrying in %.1fs (%d/%d)",
                    self.db_path,
                    delay,
                    attempt + 1,
                    self.LOCK_RETRIES,
                )
                time.sleep(delay)

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
            execution_price REAL,
            strategy_id TEXT,
            strategy_version TEXT,
            decision_time_us INTEGER,
            expires_at_us INTEGER,
            stop_price REAL,
            target_price REAL,
            time_stop_seconds INTEGER,
            source_hash TEXT
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
            exchange_order_id TEXT,
            submitted_at INTEGER,
            updated_at INTEGER,
            fail_reason TEXT,
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
            state TEXT,
            stop_active INTEGER,
            entry_order_id TEXT,
            strategy_id TEXT,
            strategy_version TEXT,
            entry_fee REAL DEFAULT 0,
            target_price REAL,
            time_stop_at INTEGER,
            source_signal_hash TEXT
        );

        CREATE TABLE IF NOT EXISTS equity_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            portfolio_value REAL,
            unrealized_pnl REAL,
            realized_pnl REAL,
            total_equity REAL,
            open_positions INTEGER
        );

        CREATE TABLE IF NOT EXISTS trade_outcomes (
            outcome_id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            strategy_version TEXT NOT NULL,
            entry_order_id TEXT NOT NULL,
            exit_order_id TEXT NOT NULL UNIQUE,
            entry_ts INTEGER NOT NULL,
            exit_ts INTEGER NOT NULL,
            quantity REAL NOT NULL,
            avg_entry REAL NOT NULL,
            avg_exit REAL NOT NULL,
            entry_fee REAL NOT NULL,
            exit_fee REAL NOT NULL,
            gross_pnl REAL NOT NULL,
            net_pnl REAL NOT NULL,
            return_bps REAL NOT NULL,
            holding_seconds INTEGER NOT NULL,
            exit_reason TEXT NOT NULL,
            position_closed INTEGER NOT NULL,
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS learning_reviews (
            strategy_id TEXT NOT NULL,
            strategy_version TEXT NOT NULL,
            sample_count INTEGER NOT NULL,
            win_rate REAL NOT NULL,
            expectancy_bps REAL NOT NULL,
            profit_factor REAL,
            total_net_pnl REAL NOT NULL,
            max_drawdown_usd REAL NOT NULL,
            status TEXT NOT NULL,
            generated_at INTEGER NOT NULL,
            PRIMARY KEY(strategy_id, strategy_version)
        );
        """
        def initialize():
            with closing(self._connect()) as conn:
                # WAL allows the long-running bot and the sell-only exit watcher
                # to read concurrently while serializing their brief writes.
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.executescript(schema)
                self._run_migrations(conn)
                conn.commit()
                logger.info(f"Database initialized cleanly at {self.db_path}")
        try:
            self._retry(initialize)
        except Exception as e:
            logger.error(f"Failed to initialize SQLite database: {e}")
            raise

    def _run_migrations(self, conn: sqlite3.Connection):
        """Lightweight SQLite migration routine to add missing columns"""
        expected_columns = {
            "signals": {
                "execution_price": "REAL",
                "strategy_id": "TEXT",
                "strategy_version": "TEXT",
                "decision_time_us": "INTEGER",
                "expires_at_us": "INTEGER",
                "stop_price": "REAL",
                "target_price": "REAL",
                "time_stop_seconds": "INTEGER",
                "source_hash": "TEXT",
            },
            "orders": {
                "executed_size": "REAL",
                "exchange_order_id": "TEXT",
                "submitted_at": "INTEGER",
                "updated_at": "INTEGER",
                "fail_reason": "TEXT"
            },
            "positions": {
                "stop_active": "INTEGER",
                "entry_order_id": "TEXT",
                "strategy_id": "TEXT",
                "strategy_version": "TEXT",
                "entry_fee": "REAL DEFAULT 0",
                "target_price": "REAL",
                "time_stop_at": "INTEGER",
                "source_signal_hash": "TEXT",
            }
        }
        
        cursor = conn.cursor()
        for table, columns in expected_columns.items():
            cursor.execute(f"PRAGMA table_info({table})")
            existing_columns = {row[1] for row in cursor.fetchall()}
            
            for col_name, col_type in columns.items():
                if col_name not in existing_columns:
                    logger.info(f"Migration: Adding column {col_name} to {table}")
                    try:
                        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")
                    except Exception as e:
                        logger.error(f"Migration failed adding {col_name} to {table}: {e}")
                        raise

    def execute(self, query: str, params: tuple = ()):
        def operation():
            with closing(self._connect()) as conn:
                cursor = conn.cursor()
                cursor.execute(query, params)
                conn.commit()
                return cursor
        return self._retry(operation)

    def fetch_all(self, query: str, params: tuple = ()) -> List[Dict[str, Any]]:
        def operation():
            with closing(self._connect()) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(query, params)
                return [dict(row) for row in cursor.fetchall()]
        return self._retry(operation)

# Default singleton instance bound explicitly tightly to v0 architecture
db = Database()
