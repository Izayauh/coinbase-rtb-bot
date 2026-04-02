import logging
from typing import Dict, Any
from src.connectors.coinbase_ws import CoinbaseWebsocket

logger = logging.getLogger(__name__)

class MarketDataService:
    """
    Responsibilities:
    - connects to market-data WebSocket
    - subscribes to candles, trades, ticker/status, heartbeats
    - normalizes and stores: trades, 1m/1h/4h bars
    - publishes fresh bar-close events
    """
    def __init__(self, config):
        self.symbols = config.symbols
        self.ws_client = CoinbaseWebsocket(self.symbols, self.on_market_data)
        self.last_heartbeat = {}
        
    async def start(self):
        logger.info("Starting Market Data Service...")
        await self.ws_client.connect()

    def stop(self):
        self.ws_client.stop()
        logger.info("Market Data Service stopped.")

    async def on_market_data(self, data: Dict[str, Any]):
        channel = data.get("channel")
        if channel == "heartbeats":
            for event in data.get("events", []):
                for hb in event.get("heartbeats", []):
                    prod = hb.get("product_id")
                    self.last_heartbeat[prod] = hb.get("time")
        elif channel == "market_trades":
            self.process_trades(data)

    def process_trades(self, data: Dict[str, Any]):
        # Currently just logs trades. Phase 2 involves the BarBuilder.
        pass
