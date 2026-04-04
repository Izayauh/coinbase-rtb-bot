"""
CB-RTB — paper-mode runtime entrypoint.

Usage:
    python main.py

This is the only supported runtime entrypoint.
src/ is not imported. bot/main.py has been deleted.
"""
import asyncio
import logging
import sys
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
from bot.readiness import parse_coinbase_balances

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

        _REJECTION_STATUSES = {
            "REJECTED_POSITION_OPEN",
            "REJECTED_INVALID_DATA",
            "REJECTED_INVALID_SIZE",
            "REJECTED_SIZE_CAP",
        }
        status_map = {"PENDING": "ORDER_PENDING"} | {s: s for s in _REJECTION_STATUSES}
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
        elif order.status in _REJECTION_STATUSES:
            log_event(
                "ORDER_REJECTED",
                order_id=order.order_id,
                signal_id=signal.signal_id,
                symbol=order.symbol,
                reason=order.status,
            )


def _check_fills_and_positions(exec_service: ExecutionService, safeguards: Safeguards, symbol: str) -> None:
    """After reconcile, verify stop invariant on any open position."""
    pos = Journal.get_open_position(symbol)
    if pos:
        ok = safeguards.check_stop_invariant(symbol)
        if not ok:
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

    before_orders format: {order_id: {"status": str, "exchange_order_id": str|None}}
    Snapshot must be taken after _process_new_signals so new orders are visible.
    ORDER_SUBMITTED fires on the None->present transition of exchange_order_id.
    """
    for order_id, prev in before_orders.items():
        rows = db.fetch_all("SELECT * FROM orders WHERE order_id=?", (order_id,))
        if not rows:
            continue
        row = rows[0]
        prev_status = prev["status"]
        prev_exch_id = prev["exchange_order_id"]
        cur_status = row["status"]
        cur_exch_id = row.get("exchange_order_id")

        if (prev_status == "PENDING" and cur_status == "PENDING"
                and prev_exch_id is None and cur_exch_id):
            # exchange_order_id just appeared — order was submitted this tick
            log_event(
                "ORDER_SUBMITTED",
                order_id=order_id,
                exchange_order_id=cur_exch_id,
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
    print(f"Orders rejected:       {_count('ORDER_REJECTED')}")
    print(f"Orders filled:         {_count('ORDER_FILLED')}")
    print(f"Orders failed:         {_count('ORDER_FAILED_EXCHANGE')}")
    print(f"Orders timed out:      {_count('ORDER_TIMEOUT')}")
    print(f"Positions opened:      {_count('POSITION_OPENED')}")
    print(f"WS reconnects:         {_count('WS_RECONNECT')}")
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
            # Process new signals first so newly created orders are visible
            # in the snapshot taken below.
            _process_new_signals(exec_service, safeguards)

            # Snapshot after new orders exist but before reconcile submits them.
            # Captures exchange_order_id=None so the None->present transition
            # can be detected to emit ORDER_SUBMITTED exactly once per order.
            pending_rows = db.fetch_all(
                "SELECT order_id, status, exchange_order_id "
                "FROM orders WHERE status IN ('PENDING','PARTIAL')"
            )
            before_orders = {
                r["order_id"]: {
                    "status": r["status"],
                    "exchange_order_id": r["exchange_order_id"],
                }
                for r in pending_rows
            }

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


async def equity_snapshot_task(
    portfolio_value: float,
    snapshot_interval: int = 60,
) -> None:
    logger.info("Equity snapshot task starting.")
    while True:
        await asyncio.sleep(snapshot_interval)
        try:
            rows = db.fetch_all(
                "SELECT COALESCE(SUM(unrealized_pnl),0) AS up, "
                "COALESCE(SUM(realized_pnl),0) AS rp, "
                "COUNT(*) AS cnt FROM positions WHERE state='OPEN'"
            )
            unrealized = float(rows[0]["up"]) if rows else 0.0
            realized = float(rows[0]["rp"]) if rows else 0.0
            open_pos = int(rows[0]["cnt"]) if rows else 0
            total = portfolio_value + realized + unrealized
            from bot.journal import Journal as _J
            _J.insert_equity_snapshot(portfolio_value, unrealized, realized, total, open_pos)
            log_event(
                "EQUITY_SNAPSHOT",
                total_equity=round(total, 4),
                unrealized_pnl=round(unrealized, 4),
                realized_pnl=round(realized, 4),
                open_positions=open_pos,
            )
        except Exception as exc:
            logger.error("Equity snapshot error: %s", exc)


# ---------------------------------------------------------------------------
# Live mode startup helpers
# ---------------------------------------------------------------------------

def _abort_if_live_creds_missing(adapter: CoinbaseAdapter) -> None:
    """
    Fail fast if the adapter has no credentials.

    Called once at live startup before any operations. When _enabled=False the
    adapter silently returns synthetic fills — allowing the bot to run in live
    mode with fake orders. That is unacceptable; we abort immediately instead.
    """
    if not adapter._enabled:
        logger.error(
            "LIVE MODE ABORTED: COINBASE_API_KEY and/or COINBASE_API_SECRET "
            "are not set. Set both as persistent User environment variables "
            "(see start-live.ps1 for the correct workflow)."
        )
        sys.exit(1)


async def _fetch_live_balances_for_banner(adapter: CoinbaseAdapter) -> dict:
    """
    Fetch real Coinbase account balances to display in the startup banner.

    Exits the process on auth failure — we must not proceed to live trading
    if the credentials do not authenticate successfully against the exchange.

    Returns {currency: available_float} on success.
    """
    try:
        response = await adapter.get_balances()
        balances = parse_coinbase_balances(response)
        if balances:
            logger.info(
                "Live account fetch OK: %d currencies found.",
                len(balances),
            )
        else:
            logger.warning(
                "Live account fetch returned no accounts. "
                "Credentials may be valid but portfolio scope may be empty."
            )
        return balances
    except Exception as exc:
        logger.error(
            "LIVE MODE ABORTED: Coinbase account fetch failed — %s. "
            "Verify COINBASE_API_KEY and COINBASE_API_SECRET are correct.",
            exc,
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# Live mode banner
# ---------------------------------------------------------------------------

def _print_live_banner(
    sym: str,
    pv: float,
    live_notional: float,
    ks_file: str,
    ks_state: str,
    allowlist: list,
    live_balances: dict = None,
) -> None:
    w = 66
    border = "!" * w
    print(f"\n{border}")
    print("!" + " *** LIVE TRADING MODE — REAL MONEY AT RISK ***".center(w - 2) + "!")
    print("!" + "".center(w - 2) + "!")
    print("!" + f"  Symbol               : {sym}".ljust(w - 2) + "!")
    print("!" + f"  Portfolio (config)   : ${pv:,.2f}  <- configured, NOT live balance".ljust(w - 2) + "!")
    print("!" + f"  Test notional        : ${live_notional:.2f}/order (0 = risk sizing)".ljust(w - 2) + "!")
    print("!" + f"  Product allowlist    : {allowlist}".ljust(w - 2) + "!")
    print("!" + f"  Kill switch          : {ks_file}  [{ks_state}]".ljust(w - 2) + "!")
    print("!" + "".center(w - 2) + "!")
    if live_balances:
        print("!" + "  === Live Coinbase Account (fetched this startup) ===".ljust(w - 2) + "!")
        base_currency = sym.split("-")[0] if "-" in sym else sym
        priority = {"USD", "USDC", base_currency}
        shown = set()
        for currency in sorted(priority):
            if currency in live_balances:
                line = f"  {currency:<8}: {live_balances[currency]:>18,.8f}"
                print("!" + line.ljust(w - 2) + "!")
                shown.add(currency)
        for currency, value in sorted(live_balances.items()):
            if currency not in shown and value > 0:
                line = f"  {currency:<8}: {value:>18,.8f}"
                print("!" + line.ljust(w - 2) + "!")
    else:
        print("!" + "  WARNING: Could not fetch live Coinbase account balance.".ljust(w - 2) + "!")
    print("!" + "".center(w - 2) + "!")
    print("!" + "  Press Ctrl+C within 5 seconds to ABORT.".center(w - 2) + "!")
    print(f"{border}\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run() -> None:
    import os as _os
    start_ts = time.time()

    # 1. Validate config — exits on any invalid condition
    config.validate()
    mode = config.runtime_mode()
    sym = config.symbol()
    pv = config.portfolio_value()
    reconcile_interval = config.reconcile_interval_sec()
    max_pending_age = config.max_pending_order_age_sec()
    live_notional = config.live_test_order_notional_usd()
    ks_file = config.kill_switch_file()
    ks_state = "ACTIVE — trading BLOCKED" if _os.path.exists(ks_file) else "not present"

    logger.info("CB-RTB starting in %s mode | symbol=%s | portfolio=%.2f", mode, sym, pv)

    # 1b. Create the Coinbase adapter early — needed for live credential checks
    #     before any other component is initialised.
    coinbase_adapter = CoinbaseAdapter()

    # Startup summary — live mode screams; paper mode is quiet
    if mode == "live":
        # Hard fail if credentials are absent. Without them, CoinbaseAdapter
        # silently returns synthetic fills while in live mode — unacceptable.
        _abort_if_live_creds_missing(coinbase_adapter)

        # Prove credentials by fetching real account info. Exits on auth failure.
        # Displayed in banner to distinguish configured value from live balance.
        live_balances = await _fetch_live_balances_for_banner(coinbase_adapter)

        _print_live_banner(
            sym, pv, live_notional, ks_file, ks_state,
            config.product_allowlist(), live_balances,
        )
        # 5-second abort window — operator last chance before real orders are possible
        for i in range(5, 0, -1):
            print(f"  Starting in {i}s ...  (Ctrl+C to abort)", end="\r", flush=True)
            await asyncio.sleep(1)
        print()
    else:
        print("\n=== CB-RTB Startup Configuration ===")
        print(f"  Mode:                  {mode}")
        print(f"  Symbol:                {sym}")
        print(f"  Portfolio value:       ${pv:,.2f}")
        print(f"  Kill switch file:      {ks_file}  [{ks_state}]")
        print(f"  Product allowlist:     {config.product_allowlist()}")
        print(f"  Max order size (USD):  ${config.max_order_size_usd():,.2f}")
        print(f"  Max position (USD):    ${config.max_position_size_usd():,.2f}")
        print(f"  Max daily loss:        {config.max_daily_loss() * 100:.2f}%")
        print(f"  Reconcile interval:    {reconcile_interval}s")
        print(f"  Max pending order age: {max_pending_age}s")
        print(f"  WS stale timeout:      {config.ws_stale_timeout_sec()}s")
        print("=====================================\n")

    # 2. DB — paper and live use separate files; live data never mixes with paper data
    journal_db = config.live_db_path() if mode == "live" else config.paper_db_path()
    db.db_path = journal_db
    db._init_db()
    logger.info("%s DB: %s", mode.capitalize(), journal_db)

    # 3. Init remaining adapters
    #    Paper → PaperAdapter (synthetic fills, no real orders)
    #    Live  → CoinbaseAdapter directly (real REST calls in submit_order_intent)
    #    coinbase_adapter was already created above for the live startup checks.
    paper_adapter = PaperAdapter()
    order_adapter = coinbase_adapter if mode == "live" else paper_adapter

    # 4. Bar aggregator (warms from DB)
    aggregator = BarAggregator(sym)

    # 5. State machine (loads persisted state)
    state_machine = StateMachine()

    # 6. Safeguards (constructed before ExecutionService so it can be wired in)
    safeguards = Safeguards(
        trading_enabled=config.trading_enabled(),
        ws_stale_timeout_sec=config.ws_stale_timeout_sec(),
        max_daily_loss_fraction=config.max_daily_loss(),
        portfolio_value=pv,
        kill_switch_file=ks_file,
        max_order_size_usd=config.max_order_size_usd(),
        max_position_size_usd=config.max_position_size_usd(),
    )

    # 7. Execution service
    exec_service = ExecutionService(
        portfolio_value=pv,
        safeguards=safeguards,
        live_test_notional_usd=live_notional if mode == "live" else 0.0,
    )

    # 8. Market data processor + wire on_bar_close callback
    def on_bar_close(bar):
        Journal.upsert_bar(bar)
        aggregator.add(bar)
        if bar.timeframe == "1h" and aggregator.ready():
            state_machine.process_bars(aggregator.get_bars_1h(), aggregator.get_bars_4h())

    md_processor = MarketDataProcessor(coinbase_adapter, on_bar_close_callback=on_bar_close)
    safeguards.set_md_processor(md_processor)

    # 9. Connect WebSocket + wire reconnect event
    def _on_ws_reconnect(count: int) -> None:
        if count > 1:
            log_event("WS_RECONNECT", count=count, symbol=sym)

    coinbase_adapter.set_reconnect_callback(_on_ws_reconnect)
    coinbase_adapter.ws_connect([sym])
    logger.info("WebSocket connecting for %s ...", sym)

    log_event(
        "PROCESS_START",
        mode=mode,
        symbol=sym,
        portfolio_value=pv,
        live_notional=live_notional if mode == "live" else None,
    )

    # Launch tasks
    tasks = [
        asyncio.create_task(market_data_task(md_processor)),
        asyncio.create_task(
            signal_consumer_task(
                exec_service, order_adapter, safeguards, sym,
                reconcile_interval, max_pending_age,
            )
        ),
        asyncio.create_task(safeguard_task(safeguards, reconcile_interval)),
        asyncio.create_task(equity_snapshot_task(pv)),
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
        log_event("PROCESS_STOP", mode=mode, symbol=sym)
        _print_session_summary(mode, start_ts)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
