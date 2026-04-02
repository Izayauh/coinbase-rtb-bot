import asyncio
import logging
from src.core.config import config
from src.services.md_service import MarketDataService

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger("MAIN")

async def run_bot():
    logger.info("Starting CB-RTB v1...")
    
    # Init Market Data Service (Phase 1)
    md_service = MarketDataService(config)
    
    # Run the MD service
    try:
        await md_service.start()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        md_service.stop()

if __name__ == "__main__":
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        pass
