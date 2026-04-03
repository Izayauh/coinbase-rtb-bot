"""
BarAggregator — bridges BarBuilder's single-bar callback to StateMachine's
rolling-list interface.

Single-symbol. Does not manage multiple symbols.
"""
import logging
from collections import deque
from typing import List

from .models import Bar
from .db import db

logger = logging.getLogger(__name__)

_1H_MAXLEN = 30
_4H_MAXLEN = 210


class BarAggregator:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self._bars_1h: deque = deque(maxlen=_1H_MAXLEN)
        self._bars_4h: deque = deque(maxlen=_4H_MAXLEN)
        self._warm_from_db()

    def _warm_from_db(self) -> None:
        """Load recent bars from DB so aggregator is ready without waiting for live bars."""
        for tf, store, limit in (
            ("1h", self._bars_1h, _1H_MAXLEN),
            ("4h", self._bars_4h, _4H_MAXLEN),
        ):
            rows = db.fetch_all(
                "SELECT * FROM bars WHERE symbol=? AND timeframe=? "
                "ORDER BY ts_open ASC",
                (self.symbol, tf),
            )
            # Keep only the last `limit` rows (deque maxlen also enforces this)
            for row in rows[-limit:]:
                self._append(tf, Bar(**row))
        logger.info(
            "BarAggregator warmed: %d 1h bars, %d 4h bars for %s",
            len(self._bars_1h),
            len(self._bars_4h),
            self.symbol,
        )

    def _append(self, timeframe: str, bar: Bar) -> None:
        if timeframe == "1h":
            self._bars_1h.append(bar)
        elif timeframe == "4h":
            self._bars_4h.append(bar)

    def add(self, bar: Bar) -> None:
        """Add a newly completed bar. Only bars matching this aggregator's symbol are stored."""
        if bar.symbol != self.symbol:
            return
        self._append(bar.timeframe, bar)

    def get_bars_1h(self) -> List[Bar]:
        return list(self._bars_1h)

    def get_bars_4h(self) -> List[Bar]:
        return list(self._bars_4h)

    def ready(self) -> bool:
        """True when enough bars exist for StateMachine.process_bars() to run."""
        return len(self._bars_1h) >= 25 and len(self._bars_4h) >= 205
