import os
import json
import logging
import asyncio
from typing import List, Dict, Any
from coinbase.rest import RESTClient
from coinbase.websocket import WSClient

logger = logging.getLogger(__name__)

class CoinbaseAdapter:
    def __init__(self, api_key: str = None, api_secret: str = None):
        """
        Builds the unified REST and WS adapter over the official coinbase-advanced-py SDK.
        Ensures thread-safe boundaries between SDK threads and the main asyncio loop.
        """
        self.api_key = api_key or os.getenv("COINBASE_API_KEY")
        self.api_secret = api_secret or os.getenv("COINBASE_API_SECRET")
        self._enabled = bool(self.api_key and self.api_secret)
        
        if self._enabled:
            self.rest = RESTClient(api_key=self.api_key, api_secret=self.api_secret)
            logger.info("Coinbase REST Client initialized.")
        else:
            self.rest = None
            logger.warning("Coinbase API credentials missing. Running adapter in disconnected mode.")

        self.ws_client = None
        self.market_queue = asyncio.Queue()
        self.user_queue = asyncio.Queue()
        
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

    def set_loop(self, loop):
        self._loop = loop

    # --- REST Endpoints Wrapped in Async ---
    
    async def get_balances(self):
        if not self._enabled: return None
        return await asyncio.to_thread(self.rest.get_accounts)

    async def get_open_orders(self, product_id: str = None):
        if not self._enabled: return []
        kwargs = {"order_status": ["OPEN", "PENDING"]}
        if product_id: kwargs["product_id"] = product_id
        return await asyncio.to_thread(self.rest.list_orders, **kwargs)

    async def place_order(self, client_order_id: str, product_id: str, side: str, order_configuration: dict):
        if not self._enabled: return None
        return await asyncio.to_thread(
            self.rest.create_order,
            client_order_id=client_order_id,
            product_id=product_id,
            side=side,
            order_configuration=order_configuration
        )

    async def cancel_orders(self, order_ids: List[str]):
        if not self._enabled: return None
        return await asyncio.to_thread(self.rest.cancel_orders, order_ids=order_ids)

    async def get_fills(self, order_id: str = None, product_id: str = None):
        if not self._enabled: return []
        kwargs = {}
        if order_id: kwargs["order_id"] = order_id
        if product_id: kwargs["product_id"] = product_id
        return await asyncio.to_thread(self.rest.list_fills, **kwargs)

    # --- WebSocket Abstraction ---

    def _on_ws_message(self, msg):
        """
        Callback fired by the coinbase-advanced-py SDK thread.
        It must safely dispatch into the asyncio queue tied to the main event loop.
        """
        if not self._loop:
            return
            
        try:
            data = json.loads(msg)
            channel = data.get("channel")
            if channel in ["market_trades", "heartbeats"]:
                asyncio.run_coroutine_threadsafe(self.market_queue.put(data), self._loop)
            elif channel == "user":
                asyncio.run_coroutine_threadsafe(self.user_queue.put(data), self._loop)
        except Exception as e:
            logger.error(f"WS payload parsing error: {e}")

    def ws_connect(self, product_ids: List[str]):
        """Starts the Coinbase WS client. Handles both public data and authenticated user orders natively."""
        if not self._enabled:
            logger.warning("Skipping WS Connect - No API keys configured")
            return
            
        self.ws_client = WSClient(
            api_key=self.api_key,
            api_secret=self.api_secret,
            on_message=self._on_ws_message
        )
        # SDK manages its own background thread connection
        self.ws_client.open()
        
        # Subscribe to channels required for v0
        self.ws_client.subscribe(product_ids=product_ids, channels=["market_trades", "heartbeats", "user"])
        logger.info(f"WS Subscribed to {product_ids} (market_trades, heartbeats, user).")

    def ws_disconnect(self):
        if self.ws_client:
            self.ws_client.close()
            logger.info("WS Disconnected")
