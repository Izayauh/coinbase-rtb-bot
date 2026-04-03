import logging
from typing import Dict, List
from .models import Bar

logger = logging.getLogger(__name__)

class BarBuilder:
    """
    Builds 1m, 1h, and 4h bars dynamically from streaming trades.
    Only manages streaming aggregations.
    """
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.current_bars: Dict[str, Bar] = {}
        self.completed_bars: Dict[str, List[Bar]] = {"1m": [], "1h": [], "4h": []}
        
    def _get_boundary(self, ts_sec: float, tf_str: str) -> int:
        """Returns the open timestamp boundary for a given timeframe."""
        if tf_str == "1m":
            return int(ts_sec // 60) * 60
        elif tf_str == "1h":
            return int(ts_sec // 3600) * 3600
        elif tf_str == "4h":
            return int(ts_sec // 14400) * 14400
        return int(ts_sec)

    def process_trade(self, price: float, size: float, ts_sec: float) -> List[Bar]:
        """
        Ingests a trade. Returns a list of newly completed bars at this tick (if any crossed a boundary).
        """
        newly_completed = []
        for tf in ["4h", "1h", "1m"]:
            boundary = self._get_boundary(ts_sec, tf)
            current = self.current_bars.get(tf)
            
            if current and current.ts_open < boundary:
                # True Time crossed the boundary -> old bar cleanly completed
                newly_completed.append(current)
                self.completed_bars[tf].append(current)
                self.current_bars[tf] = None
                current = None
            
            if not current:
                # Seed new bar
                self.current_bars[tf] = Bar(
                    symbol=self.symbol,
                    timeframe=tf,
                    ts_open=boundary,
                    open=price,
                    high=price,
                    low=price,
                    close=price,
                    volume=size
                )
            else:
                # Update rolling state
                current.high = max(current.high, price)
                current.low = min(current.low, price)
                current.close = price
                current.volume += size

        return newly_completed
