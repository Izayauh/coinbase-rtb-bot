import logging
import uuid
import time
from typing import Optional
from models import Order, Position
from risk import RiskManager
from journal import Journal
from db import db

logger = logging.getLogger(__name__)

class ExecutionEngine:
    def __init__(self, portfolio_value: float = 10000.0):
        """
        Initializes execution constraints. In completely pure CB-RTB v0, portfolio value
        is injected/tracked externally.
        """
        self.portfolio_value = portfolio_value

    def process_pending_signals(self, latest_price: float):
        """
        Reads pending valid signals securely exactly once natively isolating 
        positions dynamically against execution collisions.
        """
        query = "SELECT * FROM signals WHERE status = 'NEW'"
        signals = db.fetch_all(query)
        
        for sig_row in signals:
            signal_id = sig_row["signal_id"]
            symbol = sig_row["symbol"]
            
            # 1. One-position model only
            open_pos = Journal.get_open_position(symbol)
            if open_pos:
                logger.warning(f"Rejecting signal {signal_id}: Position already explicitly ACTIVE for {symbol}")
                self._update_signal_status(signal_id, "REJECTED_ALREADY_OPEN")
                continue
                
            # 2. Strict Idempotency 
            existing_order = Journal.get_order_for_signal(signal_id)
            if existing_order:
                logger.warning(f"Rejecting signal {signal_id}: Order intent already distinctly exists in journal.")
                self._update_signal_status(signal_id, "REJECTED_DUPLICATE")
                continue
                
            self._attempt_entry(dict(sig_row), latest_price)

    def _attempt_entry(self, signal: dict, latest_price: float):
        signal_id = signal["signal_id"]
        symbol = signal["symbol"]
        
        # Initial protection computed against signal context
        # Buffer slightly below structural retest touch
        stop_loss = signal["retest_level"] - (signal["atr"] * 0.5)
        
        # Bounded entry limits protecting against market chase
        ioc_limit = RiskManager.get_ioc_limit(latest_price)
        
        # Computes natively against 0.20%
        size = RiskManager.calculate_size(self.portfolio_value, latest_price, stop_loss)
        if size <= 0:
            logger.error(f"Signal {signal_id} rejected due to risk boundaries validating negative/zero sizes securely.")
            self._update_signal_status(signal_id, "REJECTED_RISK")
            return
            
        order_id = str(uuid.uuid4())
        
        # Execution is fully isolated locally reflecting a secure fill constraint
        order = {
            "order_id": order_id,
            "signal_id": signal_id,
            "symbol": symbol,
            "side": "BUY",
            "price": ioc_limit,
            "size": size,
            "status": "FILLED",
            "created_at": int(time.time())
        }
        Journal.insert_order(order)
        self._update_signal_status(signal_id, "EXECUTED")
        
        self._create_position(symbol, latest_price, size, stop_loss)
        
    def _create_position(self, symbol: str, fill_price: float, size: float, stop_loss: float):
        query = """
            INSERT INTO positions (symbol, entry_ts, avg_entry, current_size, realized_pnl, unrealized_pnl, stop_price, state)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """
        db.execute(query, (
            symbol, int(time.time()), fill_price, size, 0.0, 0.0, stop_loss, "OPEN"
        ))

    def _update_signal_status(self, signal_id: str, status: str):
        db.execute("UPDATE signals SET status=? WHERE signal_id=?", (status, signal_id))
