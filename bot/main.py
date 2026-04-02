import asyncio
import logging
from coinbase_adapter import CoinbaseAdapter
from market_data import MarketDataProcessor
from models import Bar

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("MAIN")

async def user_order_task(adapter: CoinbaseAdapter):
    logger.info("User order task starting...")
    while True:
        data = await adapter.user_queue.get()
        logger.info(f"User Order Data: {data}")
        adapter.user_queue.task_done()

async def on_bar_close(bar: Bar):
    logger.info(f"Bar closed: {bar.timeframe} = {bar.symbol} Open: {bar.open} Close: {bar.close} Vol: {bar.volume}")
    # Hook for Step 4 Strategy state machine!

async def main():
    logger.info("Booting CB-RTB v0 ...")
    
    # Requires COINBASE_API_KEY and COINBASE_API_SECRET in ENV
    adapter = CoinbaseAdapter()
    
    try:
        adapter.set_loop(asyncio.get_running_loop())
    except Exception as e:
        logger.error(f"Failed to tie asyncio loop: {e}")
        return
        
    md_processor = MarketDataProcessor(adapter, on_bar_close_callback=on_bar_close)

    md_task = asyncio.create_task(md_processor.run())
    user_task = asyncio.create_task(user_order_task(adapter))

    try:
        adapter.ws_connect(["BTC-USD"])
        while True:
            await asyncio.sleep(1)
            
    except KeyboardInterrupt:
        logger.info("Shutdown requested.")
    finally:
        adapter.ws_disconnect()
        md_task.cancel()
        user_task.cancel()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
