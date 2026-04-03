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

async def signal_consumer_task(exec_service):
    from journal import Journal
    from models import Signal
    logger.info("Signal consumer task starting...")
    while True:
        try:
            new_signals_data = Journal.get_new_signals()
            for s_data in new_signals_data:
                signal = Signal(**s_data)
                order = exec_service.process_signal(signal)
                
                if order:
                    status_mappings = {
                        "PENDING": "ORDER_PENDING",
                        "REJECTED_POSITION_OPEN": "REJECTED_POSITION_OPEN",
                        "REJECTED_INVALID_DATA": "REJECTED_INVALID_DATA",
                        "REJECTED_INVALID_SIZE": "REJECTED_INVALID_SIZE"
                    }
                    new_status = status_mappings.get(order.status, "PROCESSED")
                    Journal.update_signal_status(signal.signal_id, new_status)
            
            # Reconcile loop
            exec_service.reconcile_pending_orders()
        except Exception as e:
            logger.error(f"Error in signal consumer: {e}")
        
        await asyncio.sleep(1)

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
    
    from execution import ExecutionService
    exec_service = ExecutionService()

    md_task = asyncio.create_task(md_processor.run())
    user_task = asyncio.create_task(user_order_task(adapter))
    signal_task = asyncio.create_task(signal_consumer_task(exec_service))

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
        signal_task.cancel()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
