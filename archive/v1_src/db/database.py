import sqlite3
import os
import logging

logger = logging.getLogger(__name__)

class Database:
    def __init__(self, db_path="journal.db", schema_path="src/db/schema.sql"):
        self.db_path = db_path
        self.schema_path = schema_path
        self._init_db()

    def _init_db(self):
        try:
            with sqlite3.connect(self.db_path) as conn:
                if os.path.exists(self.schema_path):
                    with open(self.schema_path, "r") as f:
                        schema = f.read()
                    conn.executescript(schema)
                    logger.info(f"Database initialized at {self.db_path}")
                else:
                    logger.warning(f"Schema file not found at {self.schema_path}. Proceeding without tables.")
        except Exception as e:
            logger.error(f"Failed to initialize database: {e}")
            raise

    def execute(self, query, params=()):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            conn.commit()
            return cursor

    def execute_many(self, query, params_list):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.executemany(query, params_list)
            conn.commit()
            return cursor

    def fetch_all(self, query, params=()):
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]

# Singleton instance
db = Database()
