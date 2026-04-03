"""
CB-RTB — paper-mode runtime entrypoint.

Usage:
    python main.py

This is the only supported runtime entrypoint.
src/ is not imported. bot/main.py has been deleted.
"""
import asyncio
import logging
import time

import bot.config as config
from bot.db import db
from bot.coinbase_adapter import CoinbaseAdapter
from bot.adapters import PaperAdapter
from bot.market_data import MarketDataProcessor
from bot.aggregator import BarAggregator
from bot.state_machine import StateMachine
from bot.execution import ExecutionService
from bot.safeguards import Safeguards
from bot.journal import Journal
from bot.models import Signal, Order
from bot.events import log_event

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("MAIN")


# ---------------------------------------------------------------------------
# Signal consumer helpers
# ---------------------------------------------------------------------------

def _process_new_signals(exec_service: ExecutionService, safeguards: Safeguards) -> None:
    """Consume NEW signals. Guards block entry creation when tripped."""
    if not safeguards.can_trade():
        return

    new_signals = Journal.get_new_signals()
    for s_data in new_signals:
        signal = Signal(**s_data)

        # Log the signal at the moment we first observe it
        log_event(
            "SIGNAL_EMITTED",
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            execution_price=signal.execution_price,
        )

        order = exec_service.process_signal(signal)
        if order is None:
            continue

        status_map = {
            "PENDING": "ORDER_PENDING",
            "REJECTED_POSITION_OPEN": "REJECTED_POSITION_OPEN",
            "REJECTED_INVALID_DATA": "REJECTED_INVALID_DATA",
            "REJECTED_INVALID_SIZE": "REJECTED_INVALID_SIZE",
        }
        new_status = status_map.get(order.status, "PROCESSED")
        Journal.update_signal_status(signal.signal_id, new_status)

        if order.status == "PENDING":
            log_event(
                "ORDER_PENDING",
                order_id=order.order_id,
                signal_id=signal.signal_id,
                symbol=order.symbol,
                size=order.size,
                price=order.price,
            )


def _check_fills_and_positions(exec_service: ExecutionService, safeguards: Safeguards, symbol: str) -> None:
    """After reconcile, verify stop invariant on any open position."""
    pos = Journal.get_open_position(symbol)
    if pos:
        safeguards.check_stop_invariant(symbol)
        if not safeguards.trading_enabled:
            log_event(
                "STOP_REQUIRED",
                symbol=symbol,
                stop_price=pos.get("stop_price"),
                stop_active=pos.get("stop_active"),
            )


def _collect_reconcile_events(before_orders: dict, symbol: str) -> None:
    """
    Log ORDER_SUBMITTED, ORDER_FILLED, ORDER_FAILED_EXCHANGE, ORDER_TIMEOUT,
    and POSITION_OPENED events by comparing order/position state before and
    after reconcile.
    """
    # Re-read all orders for the symbol that were previously PENDING
    for order_id, prev_status in before_orders.items():
        rows = db.fetch_all("SELECT * FROM orders WHERE order_id=?", (order_id,))
        if not rows:
            continue
        row = rows[0]
        cur_status = row["status"]

        if prev_status == "PENDING" and cur_status == "PENDING" and row.get("exchange_order_id"):
            # Just got submitted this tick
            log_event(
                "ORDER_SUBMITTED",
                order_id=order_id,
                exchange_order_id=row["exchange_order_id"],
            )
        elif prev_status in ("PENDING", "PARTIAL") and cur_status == "FILLED":
            log_event(
                "ORDER_FILLED",
                order_id=order_id,
                signal_id=row["signal_id"],
                fill_price=row["price"],
                fill_size=row["executed_size"],
            )
            # Check if a position was opened
            pos = Journal.get_open_position(symbol)
            if pos:
                log_event(
                    "POSITION_OPENED",
                    symbol=pos["symbol"],
                    avg_entry=pos["avg_entry"],
                    size=pos["current_size"],
                    stop_price=pos["stop_price"],
                )
        elif prev_status == "PENDING" and cur_status == "FAILED":
            remote_status = row.get("fail_reason", "UNKNOWN")
            log_event("ORDER_FAILED_EXCHANGE", order_id=order_id, remote_status=remote_status)
        elif prev_status == "PENDING" and cur_status == "EXPIRED":
            created_at = row.get("created_at", 0)
            age = int(time.time()) - created_at if created_at else 0
            log_event("ORDER_TIMEOUT", order_id=order_id, age_seconds=age)


# ---------------------------------------------------------------------------
# Session summary
# ---------------------------------------------------------------------------

def _print_session_summary(mode: str, start_ts: float) -> None:
    duration_sec = int(time.time() - start_ts)
    hours, rem = divmod(duration_sec, 3600)
    minutes = rem // 60

    def _count(event_type: str) -> int:
        rows = db.fetch_all(
            "SELECT COUNT(*) as c FROM event_log WHERE event_type=? AND ts >= ?",
            (event_type, int(start_ts)),
        )
        return rows[0]["c"] if rows else 0

    print("\n=== Session Summary ===")
    print(f"Runtime mode:          {mode}")
    print(f"Duration:              {hours}h {minutes}m")
    print(f"Signals generated:     {_count('SIGNAL_EMITTED')}")
    print(f"Orders placed:         {_count('ORDER_PENDING')}")
    print(f"Orders filled:         {_count('ORDER_FILLED')}")
    print(f"Orders failed:         {_count('ORDER_FAILED_EXCHANGE')}")
    print(f"Orders timed out:      {_count('ORDER_TIMEOUT')}")
    print(f"Positions opened:      {_count('POSITION_OPENED')}")
    print(f"Trading disabled:      {_count('TRADING_DISABLED')}")
    print("=======================\n")


# ---------------------------------------------------------------------------
# Async tasks
# ---------------------------------------------------------------------------

async def market_data_task(
    md_processor: MarketDataProcessor,
) -> None:
    logger.info("Market data task starting.")
    await md_processor.run()


async def signal_consumer_task(
    exec_service: ExecutionService,
    adapter: PaperAdapter,
    safeguards: Safeguards,
    symbol: str,
    reconcile_interval: int,
    max_pending_age: int,
) -> None:
    logger.info("Signal consumer task starting.")
    while True:
        try:
            # Snapshot pending orders before reconcile to detect transitions
            pending_rows = db.fetch_all(
                "SELECT order_id, status FROM orders WHERE status IN ('PENDING','PARTIAL')"
            )
            before_orders = {r["order_id"]: r["status"] for r in pending_rows}

            _process_new_signals(exec_service, safeguards)
            exec_service.reconcile_pending_orders(timeout=max_pending_age, adapter=adapter)
            _collect_reconcile_events(before_orders, symbol)
            _check_fills_and_positions(exec_service, safeguards, symbol)
        except Exception as exc:
            logger.error("Signal consumer error: %s", exc)
        await asyncio.sleep(reconcile_interval)


async def safeguard_task(
    safeguards: Safeguards,
    reconcile_interval: int,
) -> None:
    logger.info("Safeguard monitor task starting.")
    while True:
        try:
            was_enabled = safeguards.trading_enabled
            safeguards.can_trade()  # evaluates all guards, may disable
            if was_enabled and not safeguards.trading_enabled:
                log_event(
                    "TRADING_DISABLED",
                    reason="safeguard_tripped",
                    guard_name=str(list(safeguards._tripped)),
                )
        except Exception as exc:
            logger.error("Safeguard task error: %s", exc)
        await asyncio.sleep(reconcile_interval)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run() -> None:
    start_ts = time.time()

    # 1. Validate config — exits on any invalid condition
    config.validate()
    mode = config.runtime_mode()
    sym = config.symbol()
    pv = config.portfolio_value()
    reconcile_interval = config.reconcile_interval_sec()
    max_pending_age = config.max_pending_order_age_sec()

    logger.info("CB-RTB starting in %s mode | symbol=%s | portfolio=%.2f", mode, sym, pv)

    # 2. Set DB path (paper uses paper_journal.db — never contaminates production data)
    paper_db = config.paper_db_path()
    db.db_path = paper_db
    db._init_db()
    logger.info("Paper DB: %s", paper_db)

    # 3 & 4. Init adapters
    coinbase_adapter = CoinbaseAdapter()
    paper_adapter = PaperAdapter()

    # 5. Bar aggregator (warms from DB)
    aggregator = BarAggregator(sym)

    # 6. State machine (loads persisted state)
    state_machine = StateMachine()

    # 7. Execution service
    exec_service = ExecutionService(portfolio_value=pv)

    # 8. Safeguards
    safeguards = Safeguards(
        trading_enabled=config.trading_enabled(),
        ws_stale_timeout_sec=config.ws_stale_timeout_sec(),
        max_daily_loss_fraction=config.max_daily_loss(),
        portfolio_value=pv,
    )

    # 9. Market data processor + wire on_bar_close callback
    def on_bar_close(bar):
        Journal.upsert_bar(bar)
        aggregator.add(bar)
        if bar.timeframe == "1h" and aggregator.ready():
            state_machine.process_bars(aggregator.get_bars_1h(), aggregator.get_bars_4h())

    md_processor = MarketDataProcessor(coinbase_adapter, on_bar_close_callback=on_bar_close)
    safeguards.set_md_processor(md_processor)

    # 10. Connect WebSocket
    coinbase_adapter.ws_connect([sym])
    logger.info("WebSocket connecting for %s ...", sym)

    # Launch tasks
    tasks = [
        asyncio.create_task(market_data_task(md_processor)),
        asyncio.create_task(
            signal_consumer_task(
                exec_service, paper_adapter, safeguards, sym,
                reconcile_interval, max_pending_age,
            )
        ),
        asyncio.create_task(safeguard_task(safeguards, reconcile_interval)),
    ]

    try:
        await asyncio.gather(*tasks)
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        coinbase_adapter.ws_disconnect()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        _print_session_summary(mode, start_ts)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
