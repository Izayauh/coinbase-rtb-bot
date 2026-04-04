"""
Operator safety guards.

Safeguards.can_trade() returns False if ANY guard is tripped.
Guards that trip set trading_enabled=False and persist it so the state
survives restarts.

Guards:
  1. trading_enabled  — config flag, persisted in runtime_state
  2. stale_stream     — time since last trade on MarketDataProcessor
  3. stop_required    — stop invariant after fill (structural safety net)
  4. daily_loss       — wired structurally; not economically meaningful
                        until exit logic + mark-to-market exist
  5. max_pending_age  — passed as timeout to reconcile_pending_orders()
                        (not enforced here; just surfaced for config access)
"""
import logging
import os
import time
from typing import Optional

from .journal import Journal

logger = logging.getLogger(__name__)

# Key for persistence in runtime_state table
_STATE_KEY = "safeguards"


class Safeguards:
    def __init__(
        self,
        trading_enabled: bool = True,
        ws_stale_timeout_sec: int = 15,
        max_daily_loss_fraction: float = 0.015,
        portfolio_value: float = 10000.0,
        kill_switch_file: str = "KILL_SWITCH",
        max_order_size_usd: float = 500.0,
        max_position_size_usd: float = 1000.0,
    ):
        self.ws_stale_timeout_sec = ws_stale_timeout_sec
        self.max_daily_loss_fraction = max_daily_loss_fraction
        self.portfolio_value = portfolio_value
        self.kill_switch_file = kill_switch_file
        self.max_order_size_usd = max_order_size_usd
        self.max_position_size_usd = max_position_size_usd

        # Load persisted state; config value wins if not previously overridden
        persisted = Journal.get_state(_STATE_KEY)
        if persisted and "trading_enabled" in persisted:
            self._trading_enabled: bool = bool(persisted["trading_enabled"])
        else:
            self._trading_enabled = trading_enabled

        # Restore tripped-guard metadata so recovery logic and non-recoverable
        # guards (stop_required) behave correctly across restarts.
        if persisted and "tripped" in persisted:
            self._tripped: set = set(persisted["tripped"])
        else:
            self._tripped: set = set()

        if not self._trading_enabled:
            logger.warning(
                "Safeguards: trading_enabled=False loaded from persisted state. "
                "Tripped guards: %s", self._tripped
            )

        # Reference to MarketDataProcessor (set after init via set_md_processor)
        self._md_processor = None

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------

    def set_md_processor(self, md_processor) -> None:
        """Provide the MarketDataProcessor instance so we can read last_trade_ts."""
        self._md_processor = md_processor

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def can_trade(self) -> bool:
        """Return True only if all guards pass.

        _check_stale_stream() is always called (even when trading is already
        disabled) so its recovery path can re-enable trading when the stream
        comes back.
        """
        stale = self._check_stale_stream()
        if self._check_kill_switch():
            return False
        if not self._trading_enabled:
            return False
        if stale:
            return False
        if self._check_daily_loss():
            return False
        return True

    def check_stop_invariant(self, symbol: str) -> bool:
        """
        Check that an open position has stop_active=True and stop_price > 0.
        Call after every fill. Returns True if invariant holds, False if violated.
        """
        pos = Journal.get_open_position(symbol)
        if not pos:
            return True
        ok = bool(pos.get("stop_active")) and float(pos.get("stop_price", 0)) > 0
        if not ok:
            self._disable(
                "stop_required",
                f"Stop invariant violated for {symbol}: "
                f"stop_active={pos.get('stop_active')} stop_price={pos.get('stop_price')}",
            )
        return ok

    def check_order_size(self, size: float, price: float) -> bool:
        """
        Return True if the order's USD notional is within the cap.
        Return False and log if it exceeds max_order_size_usd.
        Does NOT disable trading — caller decides whether to reject or halt.
        """
        notional = size * price
        if notional > self.max_order_size_usd:
            logger.warning(
                "Order size cap exceeded: %.2f USD > max %.2f USD (size=%.5f @ %.2f)",
                notional, self.max_order_size_usd, size, price,
            )
            return False
        return True

    def check_position_size(self, new_total_size: float, price: float) -> bool:
        """
        Return True if the resulting position's USD notional is within the cap.
        Return False and disable trading if it exceeds max_position_size_usd.
        Called after a fill is applied to the position.
        """
        notional = new_total_size * price
        if notional > self.max_position_size_usd:
            self._disable(
                "position_size_exceeded",
                f"Position size cap exceeded: {notional:.2f} USD > max "
                f"{self.max_position_size_usd:.2f} USD",
            )
            return False
        return True

    def disable(self, reason: str) -> None:
        """External call to disable trading (e.g. from signal consumer after guard check)."""
        self._disable(reason, reason)

    # ------------------------------------------------------------------
    # Internal guard evaluators
    # ------------------------------------------------------------------

    def _check_stale_stream(self) -> bool:
        """Returns True (tripped) if market stream has gone silent."""
        if self._md_processor is None:
            return False
        last_ts = getattr(self._md_processor, "last_trade_ts", 0.0)
        if last_ts == 0.0:
            # No trade received yet — stream hasn't started; don't disable yet
            return False
        age = time.time() - last_ts
        if age > self.ws_stale_timeout_sec:
            self._disable(
                "stale_stream",
                f"Market stream stale for {age:.0f}s (threshold {self.ws_stale_timeout_sec}s)",
            )
            return True
        # Recoverable: if stream resumes and no other guard is tripped, re-enable
        if "stale_stream" in self._tripped and self._trading_enabled is False:
            # Only re-enable if stale_stream was the sole cause
            if self._tripped == {"stale_stream"}:
                logger.info("Safeguards: stream recovered, re-enabling trading.")
                self._trading_enabled = True
                self._tripped.discard("stale_stream")
                self._persist()
        return False

    def _check_kill_switch(self) -> bool:
        """Returns True (blocked) if the kill switch file exists on disk."""
        if os.path.exists(self.kill_switch_file):
            self._disable(
                "kill_switch",
                f"Kill switch file '{self.kill_switch_file}' is present. "
                "Remove the file and restart to resume trading.",
            )
            return True
        return False

    def _check_daily_loss(self) -> bool:
        """
        Structural daily-loss guard. Not economically meaningful yet because
        there is no exit logic or mark-to-market. Returns False in this phase.
        """
        return False

    def _disable(self, guard_name: str, message: str) -> None:
        if guard_name not in self._tripped:
            logger.warning("Safeguards: trading DISABLED — %s — %s", guard_name, message)
            self._tripped.add(guard_name)
        self._trading_enabled = False
        self._persist()

    def _persist(self) -> None:
        Journal.upsert_state(
            _STATE_KEY,
            {"trading_enabled": self._trading_enabled, "tripped": list(self._tripped)},
        )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def trading_enabled(self) -> bool:
        return self._trading_enabled
