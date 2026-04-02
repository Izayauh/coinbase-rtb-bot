import logging
from src.db.database import db

logger = logging.getLogger(__name__)

class JournalService:
    """
    Responsibilities:
    - persist every signal
    - persist every order event
    - track setup quality
    """
    
    @staticmethod
    def log_signal(signal_data: dict):
        query = """
            INSERT INTO signals (signal_id, symbol, signal_type, regime_snapshot, breakout_level, retest_level, atr, rsi, expected_rr, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(signal_id) DO UPDATE SET status=excluded.status
        """
        db.execute(query, (
            signal_data.get('signal_id'),
            signal_data.get('symbol'),
            signal_data.get('signal_type'),
            signal_data.get('regime_snapshot'),
            signal_data.get('breakout_level'),
            signal_data.get('retest_level'),
            signal_data.get('atr'),
            signal_data.get('rsi'),
            signal_data.get('expected_rr'),
            signal_data.get('status', 'NEW')
        ))
        logger.info(f"Journaled signal {signal_data.get('signal_id')} for {signal_data.get('symbol')}")

    @staticmethod
    def log_order(order_data: dict):
        query = """
            INSERT INTO orders (order_id_internal, exchange_order_id, client_order_id, symbol, side, order_type, tif, price, size, status, linked_signal_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(order_id_internal) DO UPDATE SET 
                exchange_order_id=excluded.exchange_order_id,
                status=excluded.status
        """
        db.execute(query, (
            order_data.get('order_id_internal'),
            order_data.get('exchange_order_id'),
            order_data.get('client_order_id'),
            order_data.get('symbol'),
            order_data.get('side'),
            order_data.get('order_type'),
            order_data.get('tif'),
            order_data.get('price'),
            order_data.get('size'),
            order_data.get('status'),
            order_data.get('linked_signal_id')
        ))
        logger.info(f"Journaled order {order_data.get('order_id_internal')} - Config: {order_data.get('side')} {order_data.get('size')} {order_data.get('symbol')}")

    @staticmethod
    def log_risk_event(ts: int, event_type: str, symbol: str, message: str, action_taken: str):
        query = """
            INSERT INTO risk_events (ts, event_type, symbol, message, action_taken)
            VALUES (?, ?, ?, ?, ?)
        """
        db.execute(query, (ts, event_type, symbol, message, action_taken))
        logger.warning(f"Risk Event [{event_type}] on {symbol}: {action_taken} - {message}")
