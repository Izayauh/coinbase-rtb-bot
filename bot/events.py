"""
Structured event logging.

log_event(event_type, **kwargs) writes a JSON payload to the event_log table
via Journal.append_event. All lifecycle events are logged from the signal
consumer task and safeguard task — not from core classes.

Event types:
  SIGNAL_EMITTED         — first time signal appears in get_new_signals()
  ORDER_PENDING          — process_signal() created a PENDING order
  ORDER_SUBMITTED        — adapter returned exchange_order_id
  ORDER_FILLED           — handle_fill() set status FILLED
  ORDER_FAILED_EXCHANGE  — remote order status is CANCELLED/FAILED/EXPIRED
  ORDER_TIMEOUT          — pending order expired by age
  POSITION_OPENED        — first fill created a new position
  STOP_REQUIRED          — stop invariant violated after fill
  TRADING_DISABLED       — a safeguard tripped
"""
import json
import logging

from .journal import Journal

logger = logging.getLogger(__name__)


def log_event(event_type: str, **kwargs) -> None:
    """Persist a structured event to event_log. Swallows errors so logging never crashes the bot."""
    try:
        Journal.append_event(event_type, json.dumps(kwargs))
    except Exception as exc:
        logger.error("Failed to log event %s: %s", event_type, exc)
