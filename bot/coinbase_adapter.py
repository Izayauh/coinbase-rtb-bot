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


def _extract_order_id(response) -> str:
    """
    Pull the exchange order ID out of a Coinbase create_order response.
    Handles the three formats the SDK may return depending on version:
      1. Object with .success / .success_response.order_id
      2. Plain dict with 'success_response.order_id' or 'order_id'
      3. Object with a direct .order_id attribute
    Returns an empty string if the order was rejected or the ID cannot be found.
    """
    # Format 1: SDK response object
    if hasattr(response, "success"):
        if not response.success:
            err = getattr(response, "error_response", "unknown error")
            logger.error("Exchange rejected order: %s", err)
            return ""
        sr = getattr(response, "success_response", None)
        if sr is not None:
            return str(getattr(sr, "order_id", "") or "")

    # Format 2: dict
    if isinstance(response, dict):
        if not response.get("success", True):
            logger.error("Exchange rejected order: %s", response.get("error_response"))
            return ""
        sr = response.get("success_response", {})
        return str(sr.get("order_id", "") or response.get("order_id", ""))

    # Format 3: object with direct order_id attribute
    return str(getattr(response, "order_id", "") or "")


class CoinbaseAdapter:
    WS_URL = "wss://advanced-trade-ws.coinbase.com"

    def __init__(self, api_key: str = None, api_secret: str = None):
        self.api_key = api_key or os.getenv("COINBASE_API_KEY")
        self.api_secret = api_secret or os.getenv("COINBASE_API_SECRET")
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
        self._reconnect_count = 0
        self._on_reconnect = None  # optional callback: fn(count) called on each reconnect

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

    def sync_get_fills(self, order_id: str = None) -> list:
        """Synchronous wrapper for real exchange reconciliation inside execution loops."""
        if not self._enabled: return []
        try:
            kwargs = {}
            if order_id: kwargs["order_id"] = order_id
            res = self.rest.list_fills(**kwargs)
            # The Coinbase API generally returns a dict with a 'fills' key, or an iterator depending on wrapper.
            # Easiest is to traverse the iterator or list structure safely
            if hasattr(res, 'fills'): 
                return res.fills
            elif isinstance(res, dict) and 'fills' in res:
                return res.get('fills', [])
            elif isinstance(res, list):
                return res
            elif hasattr(res, 'to_dict'):
                d = res.to_dict()
                return d.get('fills', [])
            return []
        except Exception as e:
            logger.error(f"Failed to fetch fills for {order_id}: {e}")
            return []

    def sync_get_order(self, order_id: str) -> dict:
        """Synchronous wrapper to get precise remote order status."""
        if not self._enabled: return {}
        try:
            res = self.rest.get_order(order_id)
            if hasattr(res, 'order'): 
                return res.order
            elif hasattr(res, 'to_dict'):
                d = res.to_dict()
                return d.get('order', {})
            elif isinstance(res, dict) and 'order' in res:
                return res.get('order', {})
            return res if isinstance(res, dict) else {}
        except Exception as e:
            logger.error(f"Failed to fetch order status {order_id}: {e}")
            return {}

    def submit_order_intent(self, order) -> dict:
        """
        Submit an order intent to the exchange.

        Paper / disconnected mode (_enabled=False):
            Returns synthetic metadata — no real order sent.

        Live mode (_enabled=True):
            Submits a real limit-IOC order via the Coinbase Advanced Trade REST API.
            Kill switch is verified immediately before the API call as a final
            defense-in-depth check (can_trade() is the primary gate upstream).
        """
        if not self._enabled:
            # Paper / disconnected — synthetic response, no real money at risk.
            return {
                "exchange_order_id": f"cb_{order.order_id}",
                "submitted_at": int(time.time()),
                "status": "OPEN",
            }

        # --- Live path ---
        # Defense-in-depth kill switch check immediately before API call.
        import os
        from . import config as _cfg
        ks_file = _cfg.kill_switch_file()
        if os.path.exists(ks_file):
            raise RuntimeError(
                f"Kill switch / entry halt '{ks_file}' is active — refusing to submit live order "
                f"{order.order_id}. Remove the file to resume."
            )

        if _cfg.require_strategy_authorization():
            from .strategy_registry import get_implementation

            implementation = get_implementation(
                _cfg.strategy_id(),
                _cfg.strategy_version(),
            )
            if implementation is None or not implementation.implementation_ready:
                reason = (
                    implementation.reason
                    if implementation is not None
                    else "no registered live implementation"
                )
                raise RuntimeError(
                    f"Live BUY blocked by strategy implementation gate: {reason}"
                )
            from pathlib import Path
            from .acceptance_receipt import validate_acceptance_receipt

            accepted, reason, _ = validate_acceptance_receipt(
                Path(_cfg.acceptance_receipt_file()),
                strategy_id=_cfg.strategy_id(),
                strategy_version=_cfg.strategy_version(),
                max_age_seconds=_cfg.acceptance_receipt_max_age_seconds(),
            )
            if not accepted:
                raise RuntimeError(
                    f"Live BUY blocked by final acceptance receipt: {reason}"
                )
            from .strategy_authorization import validate_configured_authorization

            authorized, reason, _ = validate_configured_authorization()
            if not authorized:
                raise RuntimeError(
                    f"Live BUY blocked by strategy authorization gate: {reason}"
                )

        response = self.rest.create_order(
            client_order_id=order.order_id,
            product_id=order.symbol,
            side=order.side,
            order_configuration={
                "limit_limit_ioc": {
                    "base_size": str(round(order.size, 8)),
                    "limit_price": str(round(order.price, 2)),
                }
            },
        )

        # Parse exchange order ID — handle multiple SDK response formats.
        exch_id = _extract_order_id(response)
        if not exch_id:
            raise ValueError(
                f"Exchange returned no order_id for {order.order_id}. "
                f"Response: {response}"
            )

        logger.info(
            "Live order submitted: local=%s  exchange=%s  size=%s  limit=%s",
            order.order_id, exch_id, order.size, order.price,
        )
        return {
            "exchange_order_id": exch_id,
            "submitted_at": int(time.time()),
            "status": "OPEN",
        }

    # --- Thin Advanced WebSocket Loop ---

    def _build_jwt(self) -> str:
        """
        Build a WebSocket JWT from the configured credentials.
        Raises ValueError with a clear message if the key cannot be parsed.
        The most common cause is a malformed PEM (newlines stripped to literal \\n).
        """
        try:
            return jwt_generator.build_ws_jwt(self.api_key, self.api_secret)
        except Exception as e:
            raise ValueError(
                f"Failed to build JWT from COINBASE_API_SECRET — key may be malformed. "
                f"Ensure the PEM has real newlines, not literal \\n. "
                f"Original error: {e}"
            ) from e

    async def _ws_payload(self, channel: str, product_ids: List[str], jwt_token: str = ""):
        msg = {
            "type": "subscribe",
            "product_ids": product_ids,
            "channel": channel
        }
        if jwt_token:
            msg["jwt"] = jwt_token
        return json.dumps(msg)

    def set_reconnect_callback(self, fn) -> None:
        """Set a callback invoked on every WebSocket (re)connect. Signature: fn(count: int)."""
        self._on_reconnect = fn

    async def ws_loop(self, product_ids: List[str]):
        """Maintain the public market-data socket.

        Market trades and heartbeats are public Coinbase channels. Keeping
        them separate from the authenticated user channel removes the old
        forced two-minute JWT reconnect cycle. Order state is reconciled over
        REST elsewhere in the runtime, so no private socket is required here.
        """
        self._ws_running = True
        retry_delay = 1

        while self._ws_running:
            try:
                async with websockets.connect(
                    self.WS_URL, close_timeout=3, ping_interval=20, ping_timeout=20
                ) as ws:
                    self._reconnect_count += 1
                    if self._reconnect_count == 1:
                        logger.info("Direct Advanced WS connected.")
                    else:
                        logger.info("Direct Advanced WS reconnected (count=%d).", self._reconnect_count)
                    if self._on_reconnect:
                        try:
                            self._on_reconnect(self._reconnect_count)
                        except Exception:
                            pass

                    # Public channels need no JWT. Coinbase recommends
                    # heartbeats alongside market data to keep the connection
                    # open during quiet periods.
                    await ws.send(await self._ws_payload("market_trades", product_ids))
                    await ws.send(await self._ws_payload("heartbeats", product_ids))
                    retry_delay = 1

                    async for msg in ws:
                        if not self._ws_running:
                            break

                        data = json.loads(msg)
                        channel = data.get("channel")

                        if channel in ["market_trades", "heartbeats"]:
                            await self.market_queue.put(data)

            except Exception as e:
                if not self._ws_running:
                    break
                logger.error(
                    "WS Exception: %s; reconnecting in %ss",
                    e or type(e).__name__,
                    retry_delay,
                )
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 30)

    def ws_connect(self, product_ids: List[str]):
        # market_trades and heartbeats are public channels — no credentials
        # required. Orders and fills are reconciled via REST.
        self.ws_task = asyncio.create_task(self.ws_loop(product_ids))

    def ws_disconnect(self):
        self._ws_running = False
        if self.ws_task:
            self.ws_task.cancel()
