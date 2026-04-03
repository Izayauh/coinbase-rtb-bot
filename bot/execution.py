import time
import logging
import uuid
from typing import Optional
from models import Signal, Order, Position, Execution
from journal import Journal
from risk import RiskManager

logger = logging.getLogger(__name__)

class ExecutionService:
    def __init__(self, portfolio_value: float = 10000.0):
        self.portfolio_value = portfolio_value

    def process_signal(self, signal: Signal) -> Optional[Order]:
        """
        Processes an emitted signal, enforcing constraints before generating an order intent.
        """
        # 1. Execution idempotency: never create multiple live entry attempts for the same signal_id
        existing_order_data = Journal.get_order_for_signal(signal.signal_id)
        if existing_order_data:
            logger.info(f"Signal {signal.signal_id} has already been processed (Idempotency Guard).")
            return Order(**existing_order_data)

        # 2. One-position model
        open_position = Journal.get_open_position(signal.symbol)
        if open_position:
            logger.info(f"Position already active for {signal.symbol}. Rejecting signal {signal.signal_id}.")
            return self._record_rejected_order(signal, "REJECTED_POSITION_OPEN")

        # 3. Entry constraints and Risk bounds
        entry_price = signal.breakout_level
        # The true initial stop should be structurally distinct from entry. We use signal.retest_level and ATR.
        stop_loss = signal.retest_level - signal.atr

        if entry_price <= 0 or stop_loss <= 0 or signal.atr <= 0:
            logger.error(f"Signal {signal.signal_id} contains missing or unstructured data correctly. Rejecting.")
            return self._record_rejected_order(signal, "REJECTED_INVALID_DATA")

        size = RiskManager.calculate_size(self.portfolio_value, entry_price, stop_loss)
        if size <= 0:
            logger.warning(f"Calculated size for {signal.signal_id} is <= 0. Rejecting.")
            return self._record_rejected_order(signal, "REJECTED_INVALID_SIZE")

        # Calculate max slippage bound for IOC
        limit_price = RiskManager.get_ioc_limit(entry_price)

        # Generate pending order intent
        order = Order(
            order_id=f"ord_{signal.signal_id}",
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            side="BUY",
            price=limit_price,
            size=size,
            status="PENDING",
            created_at=int(time.time())
        )
        Journal.insert_order(order.__dict__)
        
        return order

    def _record_rejected_order(self, signal: Signal, status: str) -> Order:
        order = Order(
            order_id=f"ord_{signal.signal_id}_rej",
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            side="BUY",
            price=signal.breakout_level,
            size=0.0,
            status=status,
            created_at=int(time.time())
        )
        Journal.insert_order(order.__dict__)
        return order

    def handle_fill(self, order: Order, signal: Signal, fill_price: float, fill_size: float, fee: float = 0.0):
        """
        Handles an execution fill and transitions order & positions safely.
        """
        if order.status not in ("PENDING", "PARTIAL"):
            logger.error(f"Cannot process fill for order {order.order_id} in state {order.status}")
            return
            
        execution = Execution(
            execution_id=f"exec_{uuid.uuid4().hex[:8]}",
            order_id=order.order_id,
            price=fill_price,
            size=fill_size,
            fee=fee,
            ts=int(time.time())
        )
        Journal.insert_execution(execution.__dict__)
        
        # update order status
        new_status = "FILLED" if fill_size >= order.size else "PARTIAL" # Simplified for v0
        order.status = new_status
        Journal.update_order_status(order.order_id, new_status)
        
        # compute and persist initial stop level using the signal context
        # do not rely on strategy state alone
        stop_loss = signal.retest_level - signal.atr
        
        # Upsert position
        open_pos_data = Journal.get_open_position(order.symbol)
        if not open_pos_data:
            position = Position(
                symbol=order.symbol,
                entry_ts=int(time.time()),
                avg_entry=fill_price,
                current_size=fill_size,
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                stop_price=stop_loss,
                state="OPEN"
            )
        else:
            # Add to existing position
            pos = Position(**open_pos_data)
            new_size = pos.current_size + fill_size
            new_avg = ((pos.avg_entry * pos.current_size) + (fill_price * fill_size)) / new_size
            position = Position(
                symbol=pos.symbol,
                entry_ts=pos.entry_ts,
                avg_entry=new_avg,
                current_size=new_size,
                realized_pnl=pos.realized_pnl,
                unrealized_pnl=pos.unrealized_pnl,
                stop_price=stop_loss, 
                state="OPEN"
            )
            
        Journal.upsert_position(position.__dict__)

    def mark_order_failed(self, order: Order):
        order.status = "FAILED"
        Journal.update_order_status(order.order_id, "FAILED")
