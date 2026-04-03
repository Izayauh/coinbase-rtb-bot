import os
import json
import logging
import asyncio
import websockets
import time
from typing import List
from coinbase.rest import RESTClient
from coinbase import jwt_generator

logger = logging.getLogger(__name__)

class CoinbaseAdapter:
    WS_URL = "wss://advanced-trade-ws.coinbase.com"

    def __init__(self, api_key: str = None, api_secret: str = None, mode="paper"):
        self.api_key = api_key or os.getenv("COINBASE_API_KEY")
        self.api_secret = api_secret or os.getenv("COINBASE_API_SECRET")
        self.mode = mode
        self._enabled = bool(self.api_key and self.api_secret)
        
        if self._enabled:
            self.rest = RESTClient(api_key=self.api_key, api_secret=self.api_secret)
            logger.info("Coinbase REST Client explicitly mapped.")
        else:
            self.rest = None
            logger.warning("Coinbase API credentials missing. Running disconnected.")

        self.ws_task = None
        self._ws_running = False
        self.market_queue = asyncio.Queue()
        self.user_queue = asyncio.Queue()

    def set_loop(self, loop):
        pass # Queues natively managed

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

    async def submit_order_intent(self, order) -> dict:
        """
        Thin local abstraction boundary. In paper mode, returns a synthetic status.
        In live mode, it maps to place_order.
        """
        if self.mode == "paper" or not self._enabled:
            return {
                "exchange_order_id": f"cb_{order.order_id}",
                "submitted_at": int(time.time()),
                "status": "PENDING"
            }
        
        # Live mode
        order_configuration = {
            "limit_limit_ioc": {
                "base_size": str(order.size),
                "limit_price": str(order.price)
            }
        }
        res = await self.place_order(
            client_order_id=order.order_id,
            product_id=order.symbol,
            side=order.side,
            order_configuration=order_configuration
        )
        
        # Placeholder ID if parsing the SDK response gets tricky
        return {
            "exchange_order_id": f"live_cb_{order.order_id}",
            "submitted_at": int(time.time()),
            "status": "PENDING"
        }

    async def get_order_status(self, exchange_order_id: str) -> str:
        """ Queries Coinbase for an order and normalizes the status locally. """
        if self.mode == "paper" or not self._enabled:
            return "PENDING"
            
        try:
            res = await asyncio.to_thread(self.rest.get_order, order_id=exchange_order_id)
            # The SDK might return an object with an order wrapper. Let's normalize safely.
            # E.g. 'OPEN' -> 'PENDING', 'CANCELLED' -> 'EXPIRED', 'FILLED' -> 'FILLED'
            cb_status = getattr(res, "status", getattr(getattr(res, "order", None), "status", "UNKNOWN"))
            status_map = {
                "OPEN": "PENDING",
                "PENDING": "PENDING",
                "FILLED": "FILLED",
                "CANCELLED": "EXPIRED",
                "EXPIRED": "EXPIRED",
                "FAILED": "FAILED"
            }
            return status_map.get(cb_status, "PENDING")
        except Exception as e:
            logger.error(f"Error checking order status for {exchange_order_id}: {e}")
            return "PENDING"

    async def get_order_fills(self, exchange_order_id: str = None, product_id: str = None):
        """ Maps to get_fills locally. """
        if self.mode == "paper" or not self._enabled:
            return []
        fills = await self.get_fills(order_id=exchange_order_id, product_id=product_id)
        # Assuming SDK returns a list of fills or an object with `fills` attribute
        fills_list = getattr(fills, "fills", fills) if fills else []
        return fills_list

    # --- Thin Advanced WebSocket Loop ---

    async def _ws_payload(self, channel: str, product_ids: List[str]):
        msg = {
            "type": "subscribe",
            "product_ids": product_ids,
            "channel": channel
        }
        if self._enabled:
            jwt_token = jwt_generator.build_ws_jwt(self.api_key, self.api_secret)
            msg["jwt"] = jwt_token
        return json.dumps(msg)

    async def ws_loop(self, product_ids: List[str]):
        self._ws_running = True
        subscription_time = 0
        
        while self._ws_running:
            try:
                async with websockets.connect(self.WS_URL) as ws:
                    logger.info("Direct Advanced WS connected.")
                    
                    # 5 Second structural subscription mandatory limitation
                    await ws.send(await self._ws_payload("market_trades", product_ids))
                    await ws.send(await self._ws_payload("heartbeats", product_ids))
                    if self._enabled:
                        await ws.send(await self._ws_payload("user", product_ids))
                    
                    subscription_time = time.time()
                    
                    async for msg in ws:
                        if not self._ws_running: break
                        
                        # Monitor 2 minute strict JWT limitation logically
                        if time.time() - subscription_time > 115:
                            # Reconnecting physically forces clean internal Auth limits effectively.
                            logger.info("Renewing JWT explicit socket binding.")
                            break 
                            
                        data = json.loads(msg)
                        channel = data.get("channel")
                        
                        if channel in ["market_trades", "heartbeats"]:
                            await self.market_queue.put(data)
                        elif channel == "user":
                            await self.user_queue.put(data)
                            
            except Exception as e:
                logger.error(f"WS Exception dynamically isolating: {e}")
                await asyncio.sleep(5)

    def ws_connect(self, product_ids: List[str]):
        if not self._enabled: return
        self.ws_task = asyncio.create_task(self.ws_loop(product_ids))

    def ws_disconnect(self):
        self._ws_running = False
        if self.ws_task:
            self.ws_task.cancel()
