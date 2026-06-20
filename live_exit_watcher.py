#!/usr/bin/env python
"""
live_exit_watcher.py - live Coinbase exits for DB-tracked crypto positions.

This companion only reduces exposure. It reads live_journal.db, looks for OPEN
positions created by the bot, and submits a Coinbase Advanced Trade SELL when
the recorded stop, configured R target, or time stop fires.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
import urllib.request
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from bot import config  # noqa: E402
from bot.coinbase_adapter import CoinbaseAdapter, _extract_order_id  # noqa: E402
from bot.db import db  # noqa: E402
from bot.events import log_event  # noqa: E402
from bot.readiness import parse_coinbase_balances  # noqa: E402

LOCK_PATH = ROOT / "live_exit_watcher.lock"


@contextmanager
def _lock():
    deadline = time.time() + 10
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK_PATH, "a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"0\n")
        handle.flush()
    handle.seek(0)
    locked = False
    while True:
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
            break
        except OSError:
            if time.time() > deadline:
                handle.close()
                raise TimeoutError("live_exit_watcher: lock busy")
            time.sleep(0.5)
    try:
        yield
    finally:
        if locked:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        try:
            handle.close()
        except OSError:
            pass


def _live_cfg(key: str, default):
    return (getattr(config, "_raw", {}).get("live") or {}).get(key, default)


def _enabled() -> bool:
    return bool(_live_cfg("auto_exit_enabled", False))


def _float(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _field(obj, *names, default=None):
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def _current_price(symbol: str) -> float:
    url = f"https://api.exchange.coinbase.com/products/{symbol}/ticker"
    req = urllib.request.Request(url, headers={"User-Agent": "Hermes-Crypto-Exit/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.load(resp)
    return float(data["price"])


def _open_positions() -> list[dict]:
    rows = db.fetch_all(
        "SELECT * FROM positions WHERE state='OPEN' AND current_size > 0"
    )
    return [dict(r) for r in rows]


def _reason(pos: dict, price: float, now: int) -> tuple[str | None, float | None]:
    entry = _float(pos.get("avg_entry"))
    stop = _float(pos.get("stop_price"))
    entry_ts = int(_float(pos.get("entry_ts")))
    stop_active = bool(pos.get("stop_active"))
    take_profit_r = _float(_live_cfg("take_profit_r", 1.5), 1.5)
    time_stop_bars = int(_float(_live_cfg("time_stop_bars", 12), 12))
    explicit_target = _float(pos.get("target_price"))
    explicit_time_stop = int(_float(pos.get("time_stop_at")))

    if stop_active and stop > 0 and price <= stop:
        return f"STOP_LOSS price={price:.2f} stop={stop:.2f}", price

    risk = entry - stop
    target = explicit_target
    target_reason = "TAKE_PROFIT"
    if target <= entry and entry > 0 and risk > 0 and take_profit_r > 0:
        target = entry + risk * take_profit_r
        target_reason = f"TAKE_PROFIT_{take_profit_r:g}R"
    if target > entry:
        if price >= target:
            return (
                f"{target_reason} price={price:.2f} target={target:.2f}",
                price,
            )

    if explicit_time_stop > 0 and now >= explicit_time_stop:
        return f"TIME_STOP price={price:.2f}", price
    if (
        not explicit_time_stop
        and entry_ts
        and time_stop_bars > 0
        and now - entry_ts >= time_stop_bars * 3600
    ):
        return f"TIME_STOP_{time_stop_bars}H price={price:.2f}", price

    return None, None


def _available_base(adapter: CoinbaseAdapter, symbol: str) -> float:
    base = symbol.split("-", 1)[0]
    try:
        balances = parse_coinbase_balances(adapter.rest.get_accounts())
        return _float(balances.get(base), 0.0)
    except Exception:
        return 0.0


def _insert_exit_signal(signal_id: str, symbol: str, reason: str, price: float) -> None:
    db.execute(
        """
        INSERT INTO signals (
            signal_id, symbol, signal_type, regime_snapshot, breakout_level,
            retest_level, atr, rsi, status, execution_price
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(signal_id) DO NOTHING
        """,
        (signal_id, symbol, "EXIT", reason, 0.0, 0.0, 0.0, 0.0, "EXIT_PENDING", price),
    )


def _insert_order(order_id: str, signal_id: str, symbol: str, qty: float, price: float) -> None:
    db.execute(
        """
        INSERT INTO orders (
            order_id, signal_id, symbol, side, price, size, executed_size, status,
            created_at, exchange_order_id, submitted_at, updated_at, fail_reason
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(order_id) DO NOTHING
        """,
        (order_id, signal_id, symbol, "SELL", price, qty, 0.0, "PENDING",
         int(time.time()), None, None, None, None),
    )


def _update_order(order_id: str, *, status: str, executed_size: float = 0.0,
                  exchange_order_id: str | None = None, fail_reason: str | None = None) -> None:
    db.execute(
        """
        UPDATE orders
        SET status=?, executed_size=?, exchange_order_id=COALESCE(?, exchange_order_id),
            updated_at=?, fail_reason=?
        WHERE order_id=?
        """,
        (status, executed_size, exchange_order_id, int(time.time()), fail_reason, order_id),
    )


def _record_exit_fill(
    order_id: str,
    pos: dict,
    fills: list,
    reason: str,
) -> tuple[float, float, float]:
    total_qty = 0.0
    total_notional = 0.0
    total_fee = 0.0
    for fill in fills:
        qty = _float(_field(fill, "size", default=0.0))
        price = _float(_field(fill, "price", default=0.0))
        fee = _float(_field(fill, "commission", "fee", default=0.0))
        trade_id = str(_field(fill, "trade_id", "tradeId", default=uuid.uuid4().hex))
        if qty <= 0 or price <= 0:
            continue
        exec_id = f"exit_{trade_id}"
        try:
            db.execute(
                "INSERT INTO executions (execution_id, order_id, price, size, fee, ts) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (exec_id, order_id, price, qty, fee, int(time.time())),
            )
        except Exception:
            # The exchange may return the same fill on repeated reconciliation.
            # A duplicate execution must not reduce the position twice.
            continue
        total_qty += qty
        total_notional += qty * price
        total_fee += fee

    if total_qty <= 0:
        return 0.0, 0.0, 0.0

    avg_exit = total_notional / total_qty
    entry = _float(pos.get("avg_entry"))
    current_size = _float(pos.get("current_size"))
    entry_fee_total = _float(pos.get("entry_fee"))
    fee_fraction = min(1.0, total_qty / current_size) if current_size > 0 else 0.0
    allocated_entry_fee = entry_fee_total * fee_fraction
    realized = (avg_exit - entry) * total_qty - allocated_entry_fee - total_fee
    remaining = max(0.0, current_size - total_qty)
    remaining_entry_fee = max(0.0, entry_fee_total - allocated_entry_fee)
    state = "CLOSED" if remaining <= 1e-8 else "OPEN"
    stop_active = 0 if state == "CLOSED" else int(bool(pos.get("stop_active")))

    db.execute(
        """
        UPDATE positions
        SET current_size=?, realized_pnl=COALESCE(realized_pnl, 0) + ?,
            unrealized_pnl=0.0, state=?, stop_active=?, entry_fee=?
        WHERE symbol=?
        """,
        (remaining, realized, state, stop_active, remaining_entry_fee, pos["symbol"]),
    )
    from bot.learning import record_trade_outcome

    record_trade_outcome(
        symbol=pos["symbol"],
        strategy_id=str(pos.get("strategy_id") or config.strategy_id()),
        strategy_version=str(
            pos.get("strategy_version") or config.strategy_version()
        ),
        entry_order_id=str(pos.get("entry_order_id") or "legacy_entry"),
        exit_order_id=order_id,
        entry_ts=int(_float(pos.get("entry_ts"))),
        exit_ts=int(time.time()),
        quantity=total_qty,
        avg_entry=entry,
        avg_exit=avg_exit,
        entry_fee=allocated_entry_fee,
        exit_fee=total_fee,
        exit_reason=reason,
        position_closed=state == "CLOSED",
    )
    return total_qty, avg_exit, realized


def _place_exit(adapter: CoinbaseAdapter, pos: dict, reason: str, observed_price: float) -> dict:
    symbol = pos["symbol"]
    avail = _available_base(adapter, symbol)
    qty = min(_float(pos.get("current_size")), avail)
    if qty <= 0:
        log_event("EXIT_SKIPPED_NO_BALANCE", symbol=symbol, reason=reason)
        return {"ok": False, "reason": "no_available_base_balance"}

    slippage_bps = _float(_live_cfg("exit_slippage_bps", 20), 20)
    limit_price = round(observed_price * (1 - slippage_bps / 10000.0), 2)
    signal_id = f"exit_sig_{symbol}_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    order_id = f"exit_ord_{symbol}_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    _insert_exit_signal(signal_id, symbol, reason, observed_price)
    _insert_order(order_id, signal_id, symbol, qty, limit_price)

    response = adapter.rest.create_order(
        client_order_id=order_id,
        product_id=symbol,
        side="SELL",
        order_configuration={
            "limit_limit_ioc": {
                "base_size": str(round(qty, 8)),
                "limit_price": str(limit_price),
            }
        },
    )
    exchange_order_id = _extract_order_id(response)
    if not exchange_order_id:
        _update_order(order_id, status="FAILED", fail_reason=f"no_exchange_order_id: {response}")
        return {"ok": False, "reason": "no_exchange_order_id"}

    _update_order(order_id, status="PENDING", exchange_order_id=exchange_order_id)
    log_event("EXIT_ORDER_SUBMITTED", symbol=symbol, order_id=order_id,
              exchange_order_id=exchange_order_id, reason=reason)

    fills = []
    remote_status = ""
    for _ in range(6):
        time.sleep(2)
        fills = adapter.sync_get_fills(order_id=exchange_order_id)
        if fills:
            break
        remote_order = adapter.sync_get_order(exchange_order_id)
        remote_status = str(_field(remote_order, "status", "order_status", default=""))
        if remote_status.upper() in {"CANCELLED", "FAILED", "EXPIRED", "FILLED"}:
            break

    filled_qty, avg_exit, realized = _record_exit_fill(
        order_id, pos, fills, reason
    )
    if filled_qty > 0:
        status = "FILLED" if filled_qty >= qty * 0.9999 else "PARTIAL"
        _update_order(order_id, status=status, executed_size=filled_qty,
                      exchange_order_id=exchange_order_id)
        db.execute("UPDATE signals SET status=? WHERE signal_id=?",
                   (f"EXIT_{status}", signal_id))
        log_event("EXIT_FILLED", symbol=symbol, order_id=order_id, qty=filled_qty,
                  avg_exit=avg_exit, realized_pnl=realized, reason=reason)
        return {"ok": True, "order_id": order_id, "qty": filled_qty,
                "avg_exit": avg_exit, "realized_pnl": realized, "reason": reason}

    fail_reason = remote_status or "no_fill"
    _update_order(order_id, status="FAILED", exchange_order_id=exchange_order_id,
                  fail_reason=fail_reason)
    db.execute("UPDATE signals SET status=? WHERE signal_id=?",
               ("EXIT_FAILED", signal_id))
    log_event("EXIT_FAILED", symbol=symbol, order_id=order_id, reason=reason,
              remote_status=fail_reason)
    return {"ok": False, "order_id": order_id, "reason": fail_reason}


def run_once() -> list[dict]:
    if config.runtime_mode() != "live" or not _enabled():
        return []

    live_db = Path(config.live_db_path())
    if not live_db.is_absolute():
        live_db = ROOT / live_db
    if not live_db.is_file():
        raise FileNotFoundError(
            f"Live exit watcher requires an existing journal: {live_db}"
        )

    # The long-running bot owns schema creation and migrations. Re-running DDL
    # from this every-minute watcher can contend with the live writer for an
    # exclusive SQLite lock and exhaust the cron timeout.
    db.db_path = str(live_db)
    positions = _open_positions()
    if not positions:
        return []

    adapter = CoinbaseAdapter()
    if not adapter._enabled:
        raise RuntimeError("Coinbase credentials missing; cannot run live exits")

    results = []
    now = int(time.time())
    for pos in positions:
        symbol = str(pos["symbol"])
        if symbol not in config.product_allowlist():
            continue
        price = _current_price(symbol)
        reason, observed = _reason(pos, price, now)
        if not reason:
            continue
        results.append(_place_exit(adapter, pos, reason, observed or price))
    return results


def main() -> int:
    with _lock():
        results = run_once()
    if results:
        print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
