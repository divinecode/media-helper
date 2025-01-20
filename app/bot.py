import re
import asyncio
import logging
from typing import List
from telegram.ext import ContextTypes
from telegram import Update
from config import Config

logger = logging.getLogger(__name__)

class VideoDownloadBot:
    def __init__(self, config: Config):
        self.config = config
        self.downloaders = []
        
    async def initialize(self):
        self.config.temp_dir.mkdir(exist_ok=True)

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
            original_message = message.message_id
            
            if not message or not message.text:
                await message.reply_text(
                    "Дурашка, поддерживаются только текстовые сообщения, в которых есть ссылки.",
                    reply_to_message_id=original_message
                )
                return
                
            urls = self._extract_urls(message.text)
            if not urls:
                await message.reply_text(
                    "Анлак, в твоём сообщении нет ссылок.",
                    reply_to_message_id=original_message
                )
                return
                
            url = urls[0]  # Process first URL only
            
            # Send acknowledgment message
            status_message = await message.reply_text(
                "Воу-воу... Работяги добывают видео, подождите, пожалуйста.",
                reply_to_message_id=original_message
            )
            
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
                await status_message.edit_text("Опаааа! Работяги завершили работу. Грузим видео в сообщение...")
                await message.reply_video(video=video_data, reply_to_message_id=original_message)
                await status_message.delete()
            except Exception as e:
                logger.error(f"Error sending video: {e}")
                await status_message.edit_text("Анлак, видео скачалось, но не получилось его отправить. Попробуй снова.")
                
        except asyncio.TimeoutError:
            logger.error("Operation timed out")
            await update.effective_message.reply_text(
                "Анлак, действие заняло слишком много времени. Попробуй позже.",
                reply_to_message_id=original_message
            )
        except Exception as e:
            logger.error(f"Error handling message: {e}")
            try:
                await update.effective_message.reply_text(
                    "Анлак, произошла ошибка. Попробуй позже.",
                    reply_to_message_id=original_message
                )
            except Exception as send_error:
                logger.error(f"Failed to send error message: {send_error}")