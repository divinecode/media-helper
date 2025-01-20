import logging
from telegram import Update
from telegram.ext import Application, MessageHandler, filters
from config import Config
from bot import VideoDownloadBot

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# Set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

def main() -> None:
    """Start the bot."""
    config = Config.from_env()
    bot = VideoDownloadBot(config)
  
    application = (
        Application.builder()
        .token(config.bot_token)
        .read_timeout(config.read_timeout)
        .write_timeout(config.write_timeout)
        .connection_pool_size(config.connection_pool_size)
        .pool_timeout(config.pool_timeout)
        .build()
    )

    async def post_init(application: Application) -> None:
        await bot.initialize()
        logger.info("Bot initialized successfully")

    application.post_init = post_init
    application.add_handler(MessageHandler(filters.ALL, bot.handle_message))
 
    logger.info("Starting bot")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        pool_timeout=config.pool_timeout,
        read_timeout=config.read_timeout,
        write_timeout=config.write_timeout,
        connect_timeout=config.connect_timeout,
        drop_pending_updates=True
    )

if __name__ == "__main__":
    main()