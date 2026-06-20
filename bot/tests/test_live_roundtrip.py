import time
import types

from bot.db import db
from bot.execution import ExecutionService
from bot.journal import Journal
from bot.models import Signal
from bot.safeguards import Safeguards


def _insert_signal(signal):
    db.execute(
        "INSERT INTO signals (signal_id, symbol, signal_type, regime_snapshot, "
        "breakout_level, retest_level, atr, rsi, status, execution_price) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            signal.signal_id,
            signal.symbol,
            signal.signal_type,
            signal.regime_snapshot,
            signal.breakout_level,
            signal.retest_level,
            signal.atr,
            signal.rsi,
            signal.status,
            signal.execution_price,
        ),
    )


class _BuyFillAdapter:
    def __init__(self):
        self.submitted = []

    def submit_order_intent(self, order):
        self.submitted.append(order)
        return {
            "exchange_order_id": "ex_buy_roundtrip",
            "submitted_at": int(time.time()),
            "status": "OPEN",
        }

    def sync_get_fills(self, order_id=None):
        assert order_id == "ex_buy_roundtrip"
        size = self.submitted[0].size
        return [
            {
                "price": "100.00",
                "size": str(size),
                "commission": "0.00",
                "trade_id": "buy_roundtrip_fill",
            }
        ]

    def sync_get_order(self, order_id):
        return {"status": "FILLED"}


def test_live_buy_fill_then_auto_sell_closes_position(test_db, monkeypatch):
    """
    Proves the full live-capable crypto round trip without touching Coinbase:
    BUY signal -> order submit -> fill opens DB position -> exit watcher sells
    -> sell fill closes the position.
    """
    signal = Signal(
        signal_id="sig_roundtrip_live_capable",
        symbol="BTC-USD",
        signal_type="LONG",
        regime_snapshot="roundtrip-test",
        breakout_level=99.0,
        retest_level=96.0,
        atr=1.0,
        rsi=60.0,
        status="NEW",
        execution_price=100.0,
    )
    _insert_signal(signal)

    safeguards = Safeguards(
        trading_enabled=True,
        ws_stale_timeout_sec=15,
        max_daily_loss_fraction=0.015,
        portfolio_value=1000.0,
        max_order_size_usd=15.0,
        max_position_size_usd=30.0,
    )
    exec_service = ExecutionService(
        portfolio_value=1000.0,
        safeguards=safeguards,
        live_test_notional_usd=10.0,
    )

    order = exec_service.process_signal(signal)
    assert order.status == "PENDING"

    buy_adapter = _BuyFillAdapter()
    exec_service.reconcile_pending_orders(timeout=60, adapter=buy_adapter)
    exec_service.reconcile_pending_orders(timeout=60, adapter=buy_adapter)

    opened = Journal.get_open_position("BTC-USD")
    assert opened["state"] == "OPEN"
    assert opened["avg_entry"] == 100.0
    assert opened["stop_price"] == 95.0
    assert opened["current_size"] == order.size

    import bot.config as cfg
    import live_exit_watcher as exit_watcher

    monkeypatch.setattr(
        cfg,
        "_raw",
        {
            "runtime": {
                "mode": "live",
                "live_db_path": test_db.db_path,
            },
            "symbols": ["BTC-USD"],
            "safety": {
                "product_allowlist": ["BTC-USD"],
                "kill_switch_file": str(test_db.db_path) + ".ENTRY_HALT",
            },
            "strategy": {
                "id": "roundtrip_test",
                "version": "1",
            },
            "live": {
                "auto_exit_enabled": True,
                "take_profit_r": 1.5,
                "exit_slippage_bps": 20,
                "time_stop_bars": 12,
            },
        },
    )
    monkeypatch.setattr(exit_watcher, "_current_price", lambda symbol: 108.0)
    monkeypatch.setattr(exit_watcher, "_available_base", lambda adapter, symbol: order.size)
    open(cfg.kill_switch_file(), "w").close()
    monkeypatch.setattr(
        exit_watcher.db,
        "_init_db",
        lambda: (_ for _ in ()).throw(
            AssertionError("exit watcher must not run live schema initialization")
        ),
    )

    sell_calls = []

    class _ExitAdapter:
        _enabled = True

        def __init__(self):
            self.rest = types.SimpleNamespace(create_order=self._create_order)

        def _create_order(self, **kwargs):
            sell_calls.append(kwargs)
            return types.SimpleNamespace(
                success=True,
                success_response=types.SimpleNamespace(order_id="ex_sell_roundtrip"),
            )

        def sync_get_fills(self, order_id=None):
            assert order_id == "ex_sell_roundtrip"
            return [
                {
                    "price": "108.00",
                    "size": str(order.size),
                    "commission": "0.01",
                    "trade_id": "sell_roundtrip_fill",
                }
            ]

        def sync_get_order(self, order_id):
            return {"status": "FILLED"}

    monkeypatch.setattr(exit_watcher, "CoinbaseAdapter", _ExitAdapter)

    results = exit_watcher.run_once()

    assert len(results) == 1
    assert results[0]["ok"] is True
    assert results[0]["reason"].startswith("TAKE_PROFIT_1.5R")
    assert sell_calls[0]["side"] == "SELL"
    assert sell_calls[0]["product_id"] == "BTC-USD"

    positions = db.fetch_all("SELECT * FROM positions WHERE symbol='BTC-USD'")
    assert positions[0]["state"] == "CLOSED"
    assert positions[0]["current_size"] == 0.0
    assert positions[0]["stop_active"] == 0
    assert positions[0]["realized_pnl"] > 0

    sell_orders = db.fetch_all("SELECT * FROM orders WHERE side='SELL'")
    assert len(sell_orders) == 1
    assert sell_orders[0]["status"] == "FILLED"

    events = db.fetch_all("SELECT event_type FROM event_log")
    assert {"EXIT_ORDER_SUBMITTED", "EXIT_FILLED"}.issubset(
        {row["event_type"] for row in events}
    )

    outcomes = db.fetch_all("SELECT * FROM trade_outcomes")
    assert len(outcomes) == 1
    assert outcomes[0]["strategy_id"] == "btc_breakout_retest_continuation"
    assert outcomes[0]["net_pnl"] > 0

    reviews = db.fetch_all("SELECT * FROM learning_reviews")
    assert len(reviews) == 1
    assert reviews[0]["sample_count"] == 1
    assert reviews[0]["status"] == "COLLECTING"


def test_exit_watcher_prefers_position_specific_target_and_time_stop():
    import live_exit_watcher as exit_watcher

    now = int(time.time())
    position = {
        "avg_entry": 100.0,
        "stop_price": 99.0,
        "stop_active": 1,
        "entry_ts": now - 60,
        "target_price": 101.5,
        "time_stop_at": now + 3600,
    }
    reason, _ = exit_watcher._reason(position, 101.6, now)
    assert reason.startswith("TAKE_PROFIT ")

    position["target_price"] = 110.0
    position["time_stop_at"] = now - 1
    reason, _ = exit_watcher._reason(position, 100.0, now)
    assert reason.startswith("TIME_STOP ")
