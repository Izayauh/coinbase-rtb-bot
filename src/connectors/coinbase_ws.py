import asyncio
import json
import logging
import websockets

logger = logging.getLogger(__name__)

class CoinbaseWebsocket:
    WS_URL = "wss://advanced-trade-ws.coinbase.com"

    def __init__(self, symbols, on_message_callback=None):
        self.symbols = symbols
        self.on_message_callback = on_message_callback
        self.ws = None
        self._running = False

    async def connect(self):
        self._running = True
        while self._running:
            try:
                async with websockets.connect(self.WS_URL) as ws:
                    self.ws = ws
                    logger.info("Connected to Coinbase WebSocket.")
                    await self._subscribe()
                    await self._listen()
            except websockets.ConnectionClosed:
                logger.warning("WebSocket disconnected. Reconnecting in 5s...")
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"WebSocket error: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)

    async def _subscribe(self):
        # We subscribe to market_trades and heartbeats
        sub_msg = {
            "type": "subscribe",
            "product_ids": self.symbols,
            "channel": "market_trades"
        }
        await self.ws.send(json.dumps(sub_msg))
        
        heartbeat_msg = {
            "type": "subscribe",
            "product_ids": self.symbols,
            "channel": "heartbeats"
        }
        await self.ws.send(json.dumps(heartbeat_msg))
        logger.info(f"Subscribed to market_trades and heartbeats for {self.symbols}")

    async def _listen(self):
        async for message in self.ws:
            if not self._running:
                break
            
            try:
                data = json.loads(message)
                if self.on_message_callback:
                    await self.on_message_callback(data)
            except Exception as e:
                logger.error(f"Error processing WS message: {e}")

    def stop(self):
        self._running = False
        if self.ws:
            asyncio.create_task(self.ws.close())
