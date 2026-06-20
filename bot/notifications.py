"""Pushover notifications for meaningful live crypto trading events."""
from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_NOTIFY_EVENTS = {
    "SIGNAL_EMITTED",
    "ORDER_PENDING",
    "ORDER_REJECTED",
    "ORDER_SUBMITTED",
    "ORDER_FILLED",
    "ORDER_FAILED_EXCHANGE",
    "ORDER_TIMEOUT",
    "POSITION_OPENED",
    "STOP_REQUIRED",
    "TRADING_DISABLED",
    "EXIT_ORDER_SUBMITTED",
    "EXIT_FILLED",
    "EXIT_FAILED",
    "EXIT_SKIPPED_NO_BALANCE",
    "RUNTIME_ERROR",
    "STRATEGY_DISABLED",
    "STRATEGY_RECOVERED",
}

ENV_PATHS = (
    Path(r"C:\Users\isaia\AppData\Local\hermes\.env"),
    Path(r"C:\Users\isaia\AppData\Local\hermes\hermes-agent\.env"),
)


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _load_env() -> None:
    """Load only missing env vars from Hermes env files."""
    for path in ENV_PATHS:
        try:
            if not path.is_file():
                continue
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if key and key not in os.environ:
                    os.environ[key] = value.strip().strip('"').strip("'")
        except Exception as exc:
            logger.debug("Could not load notification env from %s: %s", path, exc)


def _runtime_allows_notifications() -> bool:
    if _env_bool("CB_PUSHOVER_NOTIFY_PAPER", False):
        return True
    try:
        from . import config

        return config.runtime_mode() == "live"
    except Exception:
        return False


def _enabled_for_event(event_type: str) -> bool:
    if os.environ.get("PYTEST_CURRENT_TEST") and not _env_bool("CB_PUSHOVER_ENABLE_IN_TESTS", False):
        return False
    if not _env_bool("CB_PUSHOVER_ENABLED", True):
        return False
    if not _runtime_allows_notifications():
        return False
    configured = os.environ.get("CB_PUSHOVER_EVENTS", "").strip()
    if configured:
        allowed = {part.strip() for part in configured.split(",") if part.strip()}
    else:
        allowed = DEFAULT_NOTIFY_EVENTS
    return event_type in allowed


def _fmt_money(value: Any) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return str(value)


def _fmt_qty(value: Any) -> str:
    try:
        return f"{float(value):.8f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return str(value)


def _message_lines(event_type: str, payload: dict[str, Any]) -> tuple[str, list[str], int]:
    symbol = str(payload.get("symbol") or "BTC-USD")
    priority = 1

    if event_type == "SIGNAL_EMITTED":
        title = f"Coinbase {symbol} signal"
        lines = [
            f"Signal {payload.get('signal_type', 'LONG')} at {_fmt_money(payload.get('execution_price'))}",
            f"RSI {payload.get('rsi', 'n/a')} | ATR {payload.get('atr', 'n/a')}",
            f"Breakout {_fmt_money(payload.get('breakout_level'))} | retest {_fmt_money(payload.get('retest_level'))}",
        ]
    elif event_type == "ORDER_PENDING":
        title = f"Coinbase {symbol} order staged"
        lines = [
            f"BUY {_fmt_qty(payload.get('size'))} {symbol}",
            f"Limit {_fmt_money(payload.get('price'))}",
            f"Order {payload.get('order_id')}",
        ]
    elif event_type == "ORDER_SUBMITTED":
        title = f"Coinbase {symbol} order submitted"
        lines = [
            f"Order {payload.get('order_id')}",
            f"Exchange order {payload.get('exchange_order_id')}",
        ]
    elif event_type == "ORDER_FILLED":
        title = f"Coinbase {symbol} order filled"
        lines = [
            f"Fill {_fmt_qty(payload.get('fill_size'))} at {_fmt_money(payload.get('fill_price'))}",
            f"Order {payload.get('order_id')}",
        ]
    elif event_type == "POSITION_OPENED":
        title = f"Coinbase {symbol} position opened"
        lines = [
            f"Size {_fmt_qty(payload.get('size'))} at {_fmt_money(payload.get('avg_entry'))}",
            f"Stop {_fmt_money(payload.get('stop_price'))}",
        ]
    elif event_type in {"ORDER_REJECTED", "ORDER_FAILED_EXCHANGE", "ORDER_TIMEOUT"}:
        title = f"Coinbase {symbol} order problem"
        lines = [f"{event_type}: {json.dumps(payload, sort_keys=True)}"]
        priority = 1
    elif event_type == "TRADING_DISABLED":
        title = "Coinbase crypto trading disabled"
        lines = [
            f"Reason: {payload.get('reason', 'unknown')}",
            f"Guard: {payload.get('guard_name', 'unknown')}",
        ]
        priority = 1
    elif event_type == "STOP_REQUIRED":
        title = f"Coinbase {symbol} stop required"
        lines = [
            f"Stop price: {_fmt_money(payload.get('stop_price'))}",
            f"Stop active: {payload.get('stop_active')}",
        ]
        priority = 1
    elif event_type == "EXIT_ORDER_SUBMITTED":
        title = f"Coinbase {symbol} exit submitted"
        lines = [
            f"Reason: {payload.get('reason', 'unknown')}",
            f"Order {payload.get('order_id')}",
            f"Exchange order {payload.get('exchange_order_id')}",
        ]
    elif event_type == "EXIT_FILLED":
        title = f"Coinbase {symbol} exit filled"
        lines = [
            f"Sold {_fmt_qty(payload.get('qty'))} at {_fmt_money(payload.get('avg_exit'))}",
            f"Realized P&L {_fmt_money(payload.get('realized_pnl'))}",
            f"Reason: {payload.get('reason', 'unknown')}",
        ]
    elif event_type == "EXIT_FAILED":
        title = f"Coinbase {symbol} exit failed"
        lines = [
            f"Reason: {payload.get('reason', 'unknown')}",
            f"Remote status: {payload.get('remote_status', 'unknown')}",
            f"Order {payload.get('order_id')}",
        ]
        priority = 1
    elif event_type == "EXIT_SKIPPED_NO_BALANCE":
        title = f"Coinbase {symbol} exit skipped"
        lines = [f"Reason: {payload.get('reason', 'no available base balance')}"]
        priority = 1
    elif event_type == "RUNTIME_ERROR":
        title = "Coinbase crypto runtime error"
        lines = [
            f"Component: {payload.get('component', 'unknown')}",
            f"Error: {payload.get('error', 'unknown')}",
        ]
        priority = 1
    elif event_type == "STRATEGY_DISABLED":
        title = "Coinbase crypto strategy disabled"
        lines = [f"Reason: {payload.get('reason', 'history is not contiguous')}"]
        priority = 1
    elif event_type == "STRATEGY_RECOVERED":
        title = "Coinbase crypto strategy recovered"
        lines = [str(payload.get("reason", "contiguous history verified"))]
        priority = 0
    else:
        title = f"Coinbase crypto {event_type}"
        lines = [json.dumps(payload, sort_keys=True)]

    return title, lines, priority


def maybe_send_event_notification(event_type: str, payload: dict[str, Any]) -> bool:
    """Send a Pushover alert for selected live events. Returns True if sent."""
    _load_env()
    if not _enabled_for_event(event_type):
        return False

    token = os.environ.get("PUSHOVER_TOKEN", "").strip()
    user = os.environ.get("PUSHOVER_USER", "").strip()
    if not token or not user:
        logger.debug("Pushover not configured for crypto event %s", event_type)
        return False

    title, lines, priority = _message_lines(event_type, payload)
    data = urllib.parse.urlencode(
        {
            "token": token,
            "user": user,
            "title": title,
            "message": "\n".join(str(line) for line in lines if line is not None),
            "priority": str(priority),
            "sound": "cashregister",
        }
    ).encode()
    req = urllib.request.Request("https://api.pushover.net/1/messages.json", data=data)
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()
        return 200 <= getattr(resp, "status", 200) < 300


def send_operational_notification(
    title: str,
    message: str,
    *,
    priority: int = 1,
    sound: str = "siren",
) -> bool:
    """Send a direct operational alert outside the event journal."""
    _load_env()
    token = os.environ.get("PUSHOVER_TOKEN", "").strip()
    user = os.environ.get("PUSHOVER_USER", "").strip()
    if not token or not user:
        return False
    data = urllib.parse.urlencode(
        {
            "token": token,
            "user": user,
            "title": title,
            "message": message,
            "priority": str(priority),
            "sound": sound,
        }
    ).encode()
    req = urllib.request.Request("https://api.pushover.net/1/messages.json", data=data)
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()
        return 200 <= getattr(resp, "status", 200) < 300
