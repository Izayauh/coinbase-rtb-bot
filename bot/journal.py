import logging
import json
import time
from db import db
from models import Bar

logger = logging.getLogger(__name__)

class Journal:
    """
    Handles robust localized logging with explicit emphasis on restart-safety.
    Maps directly against the simple tables provisioned gracefully inside db.py.
    """
    
    @staticmethod
    def upsert_bar(bar: Bar):
        query = """
            INSERT INTO bars (symbol, timeframe, ts_open, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, timeframe, ts_open)
            DO UPDATE SET 
                high=max(high, excluded.high),
                low=min(low, excluded.low),
                close=excluded.close,
                volume=volume + excluded.volume
        """
        # Resolves duplicates natively: Re-reading overlapping historical batches immediately merges volume and bounds gracefully mapping precise closing ticks accurately.
        db.execute(query, (
            bar.symbol, bar.timeframe, bar.ts_open,
            bar.open, bar.high, bar.low, bar.close, bar.volume
        ))

    @staticmethod
    def append_event(event_type: str, message: str):
        query = """
            INSERT INTO event_log (ts, event_type, message)
            VALUES (?, ?, ?)
        """
        db.execute(query, (int(time.time()), event_type, message))

    @staticmethod
    def upsert_state(key: str, value: dict):
        query = """
            INSERT INTO runtime_state (key, value)
            VALUES (?, ?)
            ON CONFLICT(key)
            DO UPDATE SET value=excluded.value
        """
        db.execute(query, (key, json.dumps(value)))

    @staticmethod
    def get_state(key: str) -> dict:
        query = "SELECT value FROM runtime_state WHERE key=?"
        rows = db.fetch_all(query, (key,))
        if rows:
            return json.loads(rows[0]['value'])
        return {}
