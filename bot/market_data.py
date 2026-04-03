import asyncio
import logging
from datetime import datetime
from .coinbase_adapter import CoinbaseAdapter
from .bars import BarBuilder

logger = logging.getLogger(__name__)

class MarketDataProcessor:
    def __init__(self, adapter: CoinbaseAdapter, on_bar_close_callback=None):
        self.adapter = adapter
        self.bar_builders = {}
        self.on_bar_close = on_bar_close_callback

    def get_builder(self, product_id: str) -> BarBuilder:
        if product_id not in self.bar_builders:
            self.bar_builders[product_id] = BarBuilder(product_id)
        return self.bar_builders[product_id]

    async def run(self):
        logger.info("Market Data Processor loop started.")
        while True:
            data = await self.adapter.market_queue.get()
            
            try:
                channel = data.get("channel")
                
                if channel == "market_trades":
                    for event in data.get("events", []):
                        for trade in event.get("trades", []):
                            product_id = trade.get("product_id")
                            price = float(trade.get("price"))
                            size = float(trade.get("size"))
                            ts_str = trade.get("time") # ISO 8601
                            
                            # Parse into epoch safely
                            ts_sec = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
                            
                            builder = self.get_builder(product_id)
                            closed_bars = builder.process_trade(price, size, ts_sec)
                            
                            if closed_bars and self.on_bar_close:
                                for bar in closed_bars:
                                    if asyncio.iscoroutinefunction(self.on_bar_close):
                                        await self.on_bar_close(bar)
                                    else:
                                        self.on_bar_close(bar)
                                        
            except Exception as e:
                logger.error(f"Error processing market data event: {e}")
            finally:
                self.adapter.market_queue.task_done()
