import re
import asyncio
import logging
from typing import List
from telegram import Update
from telegram.ext import (
    MessageHandler,
    ContextTypes,
    filters
)
from config import Config

logger = logging.getLogger(__name__)

class VideoDownloadBot:
    def __init__(self, config: Config):
        self.config = config
        self.downloaders = []  # Will be initialized later
        
    async def initialize(self):
        # Create temp directory if it doesn't exist
        self.config.temp_dir.mkdir(exist_ok=True)
        # Initialize downloaders after temp dir is created
        from downloaders.tiktok import TikTokDownloader
        from downloaders.youtube import YouTubeShortsDownloader
        from downloaders.coub import CoubDownloader
        
        self.downloaders = [
            TikTokDownloader(self.config),
            YouTubeShortsDownloader(self.config),
            CoubDownloader(self.config)
        ]
        
    def _bot_was_mentioned(self, update: Update) -> bool:
        message = update.effective_message
        if not message or not message.entities:
            return False
            
        return any(
            entity.type == "mention" and 
            message.parse_entity(entity).lower() == f"@{self.config.bot_username}".lower()
            for entity in message.entities
        )
        
    def _extract_urls(self, text: str) -> List[str]:
        return re.findall(r'(https?://\S+)', text)

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            try:
                if not self._bot_was_mentioned(update):
                    return
                    
                message = update.effective_message
                if not message or not message.text:
                    await message.reply_text("Дурашка, поддерживаются только текстовые сообщения, в которых есть ссылки.")
                    return
                    
                urls = self._extract_urls(message.text)
                if not urls:
                    await message.reply_text("Анлак, в твоем сообщении нет ссылок.")
                    return
                    
                url = urls[0]  # Process first URL only
                
                # Send acknowledgment message
                status_message = await message.reply_text("Воу, воу... работяги добывают видео, подождите, пожалуйста.")
                
                # Find appropriate downloader
                downloader = next((d for d in self.downloaders if d.can_handle(url)), None)
                if not downloader:
                    await status_message.edit_text(
                        "Анлак, я не умею скачивать видео с этого ресурса!"
                    )
                    return
                    
                try:
                    video_data = await asyncio.wait_for(
                        downloader.download(url),
                        timeout=self.config.download_timeout
                    )
                except asyncio.TimeoutError:
                    await status_message.edit_text(
                        "Анлак, скачивание видео заняло слишком много времени."
                    )
                    return
                except Exception as e:
                    logger.error(f"Error downloading video: {e}")
                    await status_message.edit_text("Анлак, не получилось скачать видео.")
                    return
                
                if not video_data:
                    await status_message.edit_text("Анлак, не получилось скачать видео.")
                    return
                    
                try:
                    await status_message.edit_text("Опааа, работяги завершили работы. Грузим видео в сообщение...")
                    # First send the video
                    await message.reply_video(
                        video=video_data,
                        read_timeout=self.config.read_timeout,
                        write_timeout=self.config.write_timeout,
                        connect_timeout=self.config.connect_timeout
                    )
                    # Only delete status message after successful video send
                    await status_message.delete()
                except Exception as e:
                    logger.error(f"Error sending video: {e}")
                    await status_message.edit_text("Анлак, видео скачалось, но не получилось его отправить. Попробуй снова.")
                
            except asyncio.TimeoutError:
                logger.error("Operation timed out")
                await update.effective_message.reply_text(
                    "Анлак, действие заняло слишком много времени. Попробуй позже."
                )
            except Exception as e:
                logger.error(f"Error handling message: {e}")
                try:
                    await update.effective_message.reply_text(
                        "Анлак, произошла ошибка. Попробуй позже."
                    )
                except Exception as send_error:
                    logger.error(f"Failed to send error message: {send_error}")