import asyncio
import logging
import sys
from pathlib import Path
from config import Config
from bot import VideoDownloadBot
from telethon.client import TelegramClient

# Enable logging
logging.basicConfig(
    format="[%(levelname) 5s/%(asctime)s] %(name)s: %(message)s",
    level=logging.INFO
)

# Set higher logging level for httpx
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

async def main() -> None:
    """Start the bot."""

    # Create state directory
    config = Config.from_env()
    config.state_dir.mkdir(parents=True, exist_ok=True)
    bot = VideoDownloadBot(config)

    try:

        logger.info("Starting bot...")
        await bot.start()
            
        # Run until disconnected
        await bot.client.run_until_disconnected()
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")
    except Exception as e:
        logger.error(f"Critical error: {e}", exc_info=True)
        return 1
    finally:
        await bot.stop()
        
    return 0

if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main())
        sys.exit(exit_code)
    except KeyboardInterrupt:
        sys.exit(1)