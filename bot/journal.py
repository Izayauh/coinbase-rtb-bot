import logging
import json
import time
from typing import List
from db import db
from models import Bar, Signal, Order

logger = logging.getLogger(__name__)

class Journal:
    @staticmethod
    def upsert_bar(bar: Bar):
        query = """
            INSERT INTO bars (symbol, timeframe, ts_open, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, timeframe, ts_open)
            DO UPDATE SET 
                high=excluded.high,
                low=excluded.low,
                close=excluded.close,
                volume=excluded.volume
        """
        # Exact overwrite correctly aligns final volume boundaries against replay overlapping inputs dynamically avoiding double-counting functionally.
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

    @staticmethod
    def get_new_signals() -> List[dict]:
        query = "SELECT * FROM signals WHERE status='NEW'"
        return [dict(r) for r in db.fetch_all(query)]

    @staticmethod
    def update_signal_status(signal_id: str, status: str):
        query = "UPDATE signals SET status=? WHERE signal_id=?"
        db.execute(query, (status, signal_id))

    @staticmethod
    def get_open_position(symbol: str) -> dict:
        query = "SELECT * FROM positions WHERE symbol=? AND state IN ('OPEN', 'PENDING')"
        rows = db.fetch_all(query, (symbol,))
        return dict(rows[0]) if rows else {}

    @staticmethod
    def has_active_exposure(symbol: str) -> bool:
        pos_query = "SELECT COUNT(*) as c FROM positions WHERE symbol=? AND state IN ('OPEN', 'PENDING')"
        pos_count = db.fetch_all(pos_query, (symbol,))[0]['c']
        ord_query = "SELECT COUNT(*) as c FROM orders WHERE symbol=? AND status IN ('PENDING', 'PARTIAL')"
        ord_count = db.fetch_all(ord_query, (symbol,))[0]['c']
        return pos_count > 0 or ord_count > 0

    @staticmethod
    def insert_order(order_data: dict):
        query = """
            INSERT INTO orders (
                order_id, signal_id, symbol, side, price, size, executed_size, status, created_at,
                exchange_order_id, submitted_at, updated_at, fail_reason
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(order_id) DO UPDATE SET 
                status=excluded.status, 
                executed_size=excluded.executed_size,
                exchange_order_id=excluded.exchange_order_id,
                submitted_at=excluded.submitted_at,
                updated_at=excluded.updated_at,
                fail_reason=excluded.fail_reason
        """
        db.execute(query, (
            order_data['order_id'], order_data['signal_id'], order_data['symbol'],
            order_data['side'], order_data['price'], order_data['size'],
            order_data.get('executed_size', 0.0), order_data['status'], order_data['created_at'],
            order_data.get('exchange_order_id'), order_data.get('submitted_at'), 
            order_data.get('updated_at'), order_data.get('fail_reason')
        ))

    @staticmethod
    def get_pending_orders() -> List[dict]:
        query = "SELECT * FROM orders WHERE status='PENDING'"
        return [dict(r) for r in db.fetch_all(query)]

    @staticmethod
    def get_order_for_signal(signal_id: str) -> dict:
        query = "SELECT * FROM orders WHERE signal_id=?"
        rows = db.fetch_all(query, (signal_id,))
        return dict(rows[0]) if rows else {}

    @staticmethod
    def update_order_status(order_id: str, status: str):
        query = "UPDATE orders SET status=? WHERE order_id=?"
        db.execute(query, (status, order_id))

    @staticmethod
    def update_order_execution(order_id: str, new_executed_size: float, new_status: str):
        query = "UPDATE orders SET executed_size=?, status=? WHERE order_id=?"
        db.execute(query, (new_executed_size, new_status, order_id))

    @staticmethod
    def insert_execution(execution_data: dict):
        query = """
            INSERT INTO executions (execution_id, order_id, price, size, fee, ts)
            VALUES (?, ?, ?, ?, ?, ?)
        """
        db.execute(query, (
            execution_data['execution_id'], execution_data['order_id'],
            execution_data['price'], execution_data['size'],
            execution_data['fee'], execution_data['ts']
        ))

    @staticmethod
    def upsert_position(position_data: dict):
        query = """
            INSERT INTO positions (symbol, entry_ts, avg_entry, current_size, realized_pnl, unrealized_pnl, stop_price, state, stop_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                entry_ts=excluded.entry_ts,
                avg_entry=excluded.avg_entry,
                current_size=excluded.current_size,
                realized_pnl=excluded.realized_pnl,
                unrealized_pnl=excluded.unrealized_pnl,
                stop_price=excluded.stop_price,
                state=excluded.state,
                stop_active=excluded.stop_active
        """
        db.execute(query, (
            position_data['symbol'], position_data['entry_ts'],
            position_data['avg_entry'], position_data['current_size'],
            position_data['realized_pnl'], position_data['unrealized_pnl'],
            position_data['stop_price'], position_data['state'],
            1 if position_data.get('stop_active') else 0
        ))
