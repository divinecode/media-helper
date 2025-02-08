import logging
import sys
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

async def post_init(application: Application) -> None:
    """Initialize bot after application setup but before polling starts."""
    try:
        bot = application.bot_data.get('bot_instance')
        if bot:
            await bot.initialize(application)
            logger.info("Bot initialized successfully")
    except Exception as e:
        logger.error(f"Error during post-initialization: {e}", exc_info=True)
        sys.exit(1)

def main() -> None:
    """Start the bot."""
    try:
        config = Config.from_env()
        bot = VideoDownloadBot(config)
      
        application = (
            Application.builder()
            .token(config.bot_token)
            .read_timeout(config.read_timeout)
            .write_timeout(config.write_timeout)
            .connection_pool_size(config.connection_pool_size)
            .pool_timeout(config.pool_timeout)
            .post_init(post_init)
            .build()
        )

        # Store bot instance for post_init
        application.bot_data['bot_instance'] = bot
        
        application.add_handler(MessageHandler(filters.ALL, bot.handle_message))
        logger.info("Starting bot")
        
        # Run the bot until stopped
        application.run_polling(drop_pending_updates=True)
        
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()