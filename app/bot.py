import os
import re
import asyncio
import logging
from typing import Dict, List, Optional, Set
from pathlib import Path
from dataclasses import dataclass
from temp_manager import TempManager
from media_types import MediaType, MediaItem
from video_processor import VideoProcessor
from telegram.ext import ContextTypes
from telegram import Update, InputMediaPhoto, InputMediaVideo

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

class VideoDownloadBot:
    def __init__(self, config: 'Config'):
        self.config = config
        self.temp_manager = TempManager(config.temp_dir)
        self.video_processor = VideoProcessor(config, self.temp_manager)
        self.downloaders = []
        self.MAX_TELEGRAM_SIZE_MB = 45
        self.MAX_COMPRESS_SIZE_MB = 100
        
        # Add concurrency control
        self.download_semaphore = asyncio.Semaphore(config.max_concurrent_downloads)
        
        # Track active users and their downloads
        self.active_downloads: Dict[int, Set[asyncio.Task]] = {}
        self.user_semaphores: Dict[int, asyncio.Semaphore] = {}
        self.max_downloads_per_user = config.max_downloads_per_user
        
    def get_user_semaphore(self, user_id: int) -> asyncio.Semaphore:
        """Get or create a semaphore for a specific user."""
        if user_id not in self.user_semaphores:
            self.user_semaphores[user_id] = asyncio.Semaphore(self.max_downloads_per_user)
        return self.user_semaphores[user_id]
        
    async def track_user_download(self, user_id: int, task: asyncio.Task):
        """Track a user's download task."""
        if user_id not in self.active_downloads:
            self.active_downloads[user_id] = set()
        self.active_downloads[user_id].add(task)
        try:
            await task
        finally:
            self.active_downloads[user_id].remove(task)
            if not self.active_downloads[user_id]:
                del self.active_downloads[user_id]
                
    async def cancel_user_downloads(self, user_id: int):
        """Cancel all active downloads for a user."""
        if user_id in self.active_downloads:
            tasks = self.active_downloads[user_id]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            
    async def initialize(self):
        """Initialize bot and create required directories."""
        self.config.temp_dir.mkdir(exist_ok=True)
        logger.debug("Initializing downloaders")
        await self._initialize_downloaders()

    async def _initialize_downloaders(self):
        """Initialize all supported downloaders."""
        from downloaders.tiktok import TikTokDownloader
        from downloaders.youtube import YouTubeShortsDownloader
        from downloaders.coub import CoubDownloader
        from downloaders.instagram import InstagramDownloader

        self.downloaders = [
            TikTokDownloader(self.config),
            YouTubeShortsDownloader(self.config),
            CoubDownloader(self.config),
            InstagramDownloader(self.config)
        ]

        logger.debug(f"Initialized {len(self.downloaders)} downloaders")

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle incoming message."""
        if not update.effective_user or not self._bot_was_mentioned(update):
            return
            
        user_id = update.effective_user.id
        message = update.effective_message
        
        if not message or not message.text:
            await message.reply_text(
                "Дурашка, поддерживаются только текстовые сообщения, в которых есть ссылки.",
                reply_to_message_id=message.message_id
            )
            return
            
        urls = self._extract_urls(message.text)
        if not urls:
            await message.reply_text(
                "Анлак, в твоём сообщении нет ссылок.",
                reply_to_message_id=message.message_id
            )
            return
            
        # Get user's semaphore
        user_sem = self.get_user_semaphore(user_id)
        
        # Create and track the download task
        download_task = asyncio.create_task(
            self._process_download(user_id, urls[0], message)
        )
        await self.track_user_download(user_id, download_task)
        
    async def _process_download(self, user_id: int, url: str, message: 'telegram.Message'):
        """Process a single download request."""
        try:
            user_sem = self.get_user_semaphore(user_id)
            async with user_sem:  # Limit per-user concurrent downloads
                async with self.download_semaphore:  # Limit total concurrent downloads
                    return await self._handle_download(user_id, url, message)
        except asyncio.CancelledError:
            logger.info(f"Download cancelled for user {user_id}")
            raise
        except Exception as e:
            logger.error(f"Error processing download: {e}", exc_info=True)
            await message.reply_text(
                "Анлак, произошла ошибка. Попробуй позже.",
                reply_to_message_id=message.message_id
            )
            
    async def _handle_download(self, user_id: int, url: str, message: 'telegram.Message'):
        """Handle the actual download process."""
        status_message = await message.reply_text(
            "Воу-воу... Работяги добывают контент, подождите, пожалуйста.",
            reply_to_message_id=message.message_id
        )
        
        try:
            downloader = next((d for d in self.downloaders if d.can_handle(url)), None)
            if not downloader:
                await status_message.edit_text(
                    "Анлак, я не умею скачивать контент с этого ресурса!"
                )
                return
                
            download_result = await asyncio.wait_for(
                downloader.download(url),
                timeout=self.config.download_timeout
            )
            
            if not download_result:
                await status_message.edit_text("Анлак, не получилось скачать контент.")
                return
                
            await status_message.edit_text("Опаааа! Работяги завершили работу. Обрабатываем контент...")
            
            # Process the media
            media_items = []
            if isinstance(download_result, bytes):
                media_items = [MediaItem.from_bytes(download_result, MediaType.VIDEO)]
            else:
                media_items = [
                    MediaItem.from_bytes(item.data, item.media_type, item.caption)
                    for item in download_result
                ]

            if len(media_items) == 1:
                # Single media item
                item = media_items[0]
                processed_data = await self._process_media(item, status_message, user_id)
                if not processed_data:
                    return

                if item.media_type == MediaType.VIDEO:
                    await message.reply_video(
                        video=processed_data,
                        caption=item.caption,
                        reply_to_message_id=message.message_id
                    )
                else:
                    await message.reply_photo(
                        photo=processed_data,
                        caption=item.caption,
                        reply_to_message_id=message.message_id
                    )
            else:
                # Multiple media items
                success = await self._send_media_group(message, media_items, status_message, user_id)
                if not success:
                    return
            
            await status_message.delete()
            
        except asyncio.TimeoutError:
            logger.error("Operation timed out")
            await status_message.edit_text(
                "Анлак, действие заняло слишком много времени. Попробуй позже."
            )
        except Exception as e:
            logger.error(f"Error handling download: {e}", exc_info=True)
            await status_message.edit_text(
                "Анлак, произошла ошибка. Попробуй позже."
            )

    async def _process_media(
        self,
        media_item: 'MediaItem',
        status_message: 'telegram.Message',
        user_id: int
    ) -> Optional[bytes]:
        """Process a single media item, compressing if necessary."""
        if media_item.media_type != MediaType.VIDEO:
            return media_item.data

        size_mb = media_item.size_mb
        logger.debug(f"Processing video for user {user_id}, initial size: {size_mb:.2f}MB")

        if size_mb > self.MAX_TELEGRAM_SIZE_MB:
            if size_mb > self.MAX_COMPRESS_SIZE_MB:
                await status_message.edit_text(
                    f"Анлак, видео слишком большое ({size_mb:.1f}MB) для обработки. "
                    "Выбери видео поменьше."
                )
                return None

            await status_message.edit_text(f"Сжимаем видео размером {size_mb:.1f}MB...")
            compressed_data = await self.video_processor.compress_video(
                media_item.data,
                self.MAX_TELEGRAM_SIZE_MB,
                user_id
            )
            
            if not compressed_data:
                await status_message.edit_text(
                    "Анлак, не удалось сжать видео. Выбери видео поменьше."
                )
                return None
            
            compressed_size = len(compressed_data) / (1024 * 1024)
            logger.debug(f"Compression result for user {user_id}: {size_mb:.2f}MB -> {compressed_size:.2f}MB")
            
            if compressed_size > self.MAX_TELEGRAM_SIZE_MB:
                await status_message.edit_text(
                    f"Анлак, даже после сжатия видео слишком большое ({compressed_size:.1f}MB). "
                    "Выбери видео поменьше."
                )
                return None
                
            await status_message.edit_text("Сжатие завершено, готовим к отправке...")
            return compressed_data

        return media_item.data

    async def _send_media_group(
        self,
        message: 'telegram.Message',
        media_items: List['MediaItem'],
        status_message: 'telegram.Message',
        user_id: int
    ) -> bool:
        """Send multiple media items as a group."""
        try:
            logger.debug(f"Preparing to send {len(media_items)} items as media group for user {user_id}")
            media_group = []
            first_caption = True

            for item in media_items:
                processed_data = await self._process_media(item, status_message, user_id)
                if not processed_data:
                    return False

                if item.media_type == MediaType.VIDEO:
                    media_group.append(
                        InputMediaVideo(
                            media=processed_data,
                            caption=item.caption if first_caption else None
                        )
                    )
                else:
                    media_group.append(
                        InputMediaPhoto(
                            media=processed_data,
                            caption=item.caption if first_caption else None
                        )
                    )
                first_caption = False

            await message.reply_media_group(
                media=media_group,
                reply_to_message_id=message.message_id
            )
            return True

        except Exception as e:
            logger.error(f"Error sending media group for user {user_id}: {e}", exc_info=True)
            return False

    def _bot_was_mentioned(self, update: Update) -> bool:
        """Check if the bot was mentioned in the message."""
        message = update.effective_message
        if not message or not message.entities:
            return False
            
        return any(
            entity.type == "mention" and 
            message.parse_entity(entity).lower() == f"@{self.config.bot_username}".lower()
            for entity in message.entities
        )
        
    def _extract_urls(self, text: str) -> List[str]:
        """Extract URLs from message text."""
        return re.findall(r'(https?://\S+)', text)

    async def cleanup(self):
        """Clean up resources when shutting down."""
        # Cancel all active downloads
        for user_id in list(self.active_downloads.keys()):
            await self.cancel_user_downloads(user_id)
            
        # Clean up all temporary directories
        self.temp_manager.cleanup_all_temp_dirs()
        
        # Shutdown thread pool in video processor
        self.video_processor.thread_pool.shutdown(wait=True)