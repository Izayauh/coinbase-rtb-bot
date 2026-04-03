"""
PaperAdapter — wraps CoinbaseAdapter for paper-mode operation.

Inherits the real WebSocket market-data stream.
Overrides only the order/fill methods with deterministic synthetic responses.

Paper fill spec (non-negotiable):
- trade_id is deterministic: f"paper_fill_{order_id}"
- fill price  = order's stored limit price
- fill size   = order.size - order.executed_size (full remaining)
- commission  = 0.0
- Duplicate reconcile ticks return the same fill; handle_fill() rejects it
  because the execution_id already exists in the executions table.
"""
import logging
import time

from .coinbase_adapter import CoinbaseAdapter
from .db import db

logger = logging.getLogger(__name__)


class PaperAdapter(CoinbaseAdapter):
    """
    Thin paper-mode layer over CoinbaseAdapter.

    Use for paper mode. WebSocket market data works as normal (live trades
    from Coinbase feed bar-building). No real orders are sent to the exchange.
    """

    def submit_order_intent(self, order) -> dict:
        """Return deterministic synthetic exchange metadata. No real order sent."""
        return {
            "exchange_order_id": f"cb_{order.order_id}",
            "submitted_at": int(time.time()),
            "status": "OPEN",
        }

    def sync_get_fills(self, order_id: str) -> list:
        """
        Return one deterministic synthetic fill for the given exchange_order_id.

        The exchange_order_id format is 'cb_{order_id}', so we strip 'cb_' to
        get the local order_id and look up the current size/executed_size from DB.
        """
        # Derive local order_id from exchange_order_id
        local_order_id = order_id[3:] if order_id.startswith("cb_") else order_id

        rows = db.fetch_all(
            "SELECT price, size, executed_size FROM orders WHERE order_id=?",
            (local_order_id,),
        )
        if not rows:
            logger.warning("PaperAdapter: no order found for order_id=%s", local_order_id)
            return []

        row = rows[0]
        remaining = float(row["size"]) - float(row["executed_size"])
        if remaining <= 0:
            return []

        return [
            {
                "trade_id": f"paper_fill_{local_order_id}",
                "price": float(row["price"]),
                "size": remaining,
                "commission": 0.0,
            }
        ]

    def sync_get_order(self, exchange_order_id: str) -> dict:
        """Paper mode: any order with an exchange_order_id is treated as FILLED."""
        return {"status": "FILLED"}
