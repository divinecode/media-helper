#import os
import re
import asyncio
import logging
from typing import Dict, List, Optional, Set
from temp_manager import TempManager
from media_types import DownloadResult, MediaType, MediaItem
from video_processor import VideoProcessor
from config import Config
from telegram.ext import ContextTypes, Application
from telegram import Update, InputMediaPhoto, InputMediaVideo, Message, PhotoSize, Bot, MessageEntity
from telegram.constants import ChatAction
from assistant import ChatAssistant

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

class VideoDownloadBot:
    MEDIA_BOT_TAG = "media_bot_message"  # Tag for identifying bot-generated media messages
    bot_id: Optional[int] = None

    def __init__(self, config: Config):
        """Initialize bot with configuration."""
        self.config = config

        self.temp_manager = TempManager(config.temp_dir)
        self.video_processor = VideoProcessor(config, self.temp_manager)
        self.downloaders = []
        
        # Use compression config values instead of hardcoded ones
        self.MAX_TELEGRAM_SIZE_MB = config.compression.max_telegram_size_mb
        self.MAX_COMPRESS_SIZE_MB = config.compression.max_compress_size_mb
        
        # Add concurrency control
        self.download_semaphore = asyncio.Semaphore(config.max_concurrent_downloads)
        
        # Track active users and their downloads
        self.active_downloads: Dict[int, Set[asyncio.Task]] = {}
        self.user_semaphores: Dict[int, asyncio.Semaphore] = {}
        self.max_downloads_per_user = config.max_downloads_per_user
        
        # Add a task manager for message processing
        self.message_tasks: Dict[int, Set[asyncio.Task]] = {}

        # Replace G4F client initialization with ChatAssistant
        self.assistant = ChatAssistant(config)

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
            
    async def initialize(self, application: Application):
        """Initialize bot and create required directories."""
        bot: Bot = application.bot

        self.bot_id = (await bot.get_me()).id
        self.assistant.set_bot_id(self.bot_id)
        
        # Initialize assistant
        await self.assistant.initialize()
        
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
        """Handle incoming message with non-blocking concurrent processing."""
        if not update.effective_user \
            or update.effective_user.id == self.bot_id \
            or update.effective_user.is_bot \
            or not update.effective_message \
            or update.edited_message:
            return

        message = update.effective_message

        # Check if this is a private chat or bot was mentioned
        is_private_chat = message.chat.type == "private"
        was_mentioned = self._bot_was_mentioned(update)
        is_reply = message.reply_to_message is not None and message.reply_to_message.from_user.id == self.bot_id
        
        if not (is_private_chat or was_mentioned or is_reply):
            return

        # Extract message text and images
        text = message.text or message.caption or ""
        
        # Handle URLs first
        urls = self._extract_urls(text)
        if urls:
            await self._handle_url_download(urls[0], message)
            return

        await self.assistant.handle_message(update, context)

    async def _handle_url_download(self, url: str, message: Message):
        """Handle URL download in a separate task."""
        user_id = message.from_user.id
        process_task = asyncio.create_task(
            self._process_download(user_id, url, message)
        )
        
        if user_id not in self.message_tasks:
            self.message_tasks[user_id] = set()
        self.message_tasks[user_id].add(process_task)
        
        process_task.add_done_callback(
            lambda t: self._cleanup_task(user_id, t)
        )

    async def _process_download(self, user_id: int, url: str, message: Message):
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
            
    async def _handle_download(self, user_id: int, url: str, message: Message):
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
            
            # Convert to MediaItems
            media_items = [
                MediaItem.from_bytes(
                    item.data if isinstance(item, DownloadResult) else item,
                    item.media_type if isinstance(item, DownloadResult) else MediaType.VIDEO,
                    item.caption if isinstance(item, DownloadResult) else None
                ) for item in (download_result if isinstance(download_result, list) else [download_result])
            ]

            # Process and send all media items
            await self._send_media_items(message, media_items, status_message, user_id)
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

    async def _send_media_items(
        self,
        message: Message,
        media_items: List[MediaItem],
        status_message: Message,
        user_id: int
    ) -> None:
        """Process and send all media items."""
        if not media_items:
            return

        photos_and_videos = []
        audio_items = []

        # Process all items
        for item in media_items:
            processed_data = await self._process_media(item, status_message, user_id)
            if not processed_data:
                continue

            if item.media_type == MediaType.AUDIO:
                audio_items.append((processed_data, item.caption))
            elif item.media_type in (MediaType.VIDEO, MediaType.PHOTO):
                media_cls = InputMediaVideo if item.media_type == MediaType.VIDEO else InputMediaPhoto
                photos_and_videos.append(media_cls(
                    media=processed_data,
                    caption=item.caption,
                    has_spoiler=True,  # Add spoiler to prevent autoplay
                    parse_mode=None,  # Disable markdown parsing
                    caption_entities=[
                        MessageEntity(
                            type="custom_emoji",  # Using custom_emoji as a hack to store metadata
                            offset=0,
                            length=0,
                            custom_emoji_id=self.MEDIA_BOT_TAG
                        )
                    ] if item.caption else None
                ))

        # Send photos and videos as media group
        if photos_and_videos:
            await message.reply_media_group(
                media=photos_and_videos,
                reply_to_message_id=message.message_id
            )

        # Send audio files separately (Telegram doesn't support audio in media groups)
        for audio_data, caption in audio_items:
            await message.reply_audio(
                audio=audio_data,
                caption=caption,
                title=caption or "Audio track",
                reply_to_message_id=message.message_id,
                caption_entities=[
                    MessageEntity(
                        type="custom_emoji",
                        offset=0,
                        length=0,
                        custom_emoji_id=self.MEDIA_BOT_TAG
                    )
                ] if caption else None
            )

    async def _process_media(
        self,
        media_item: MediaItem,
        status_message: Message,
        user_id: int
    ) -> Optional[bytes]:
        """Process a single media item, applying compression based on configuration."""
        # Handle non-video content directly
        if media_item.media_type in (MediaType.PHOTO, MediaType.AUDIO):
            return media_item.data

        size_mb = media_item.size_mb
        logger.debug(f"Processing video for user {user_id}, initial size: {size_mb:.2f}MB")

        # Check size limits first
        if size_mb > self.MAX_COMPRESS_SIZE_MB:
            await status_message.edit_text(
                f"Анлак, видео слишком большое ({size_mb:.1f}MB) для обработки. "
                "Выбери видео поменьше."
            )
            return None

        # Check if we need compression
        needs_compression = (
            size_mb > self.config.compression.default_compress_threshold_mb or 
            size_mb > self.MAX_TELEGRAM_SIZE_MB
        )

        if needs_compression:
            compression_msg = "Применяем сжатие видео..."
            if size_mb > self.MAX_TELEGRAM_SIZE_MB:
                compression_msg = f"Сжимаем большое видео размером {size_mb:.1f}MB..."
            await status_message.edit_text(compression_msg)

            # Apply compression with force_compress for videos above threshold
            force_compress = size_mb > self.config.compression.default_compress_threshold_mb
            compressed_data = await self.video_processor.compress_video(
                media_item.data,
                self.MAX_TELEGRAM_SIZE_MB,
                user_id,
                force_compress=force_compress
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
                
            if compressed_size < size_mb:
                await status_message.edit_text("Сжатие завершено, готовим к отправке...")
                return compressed_data
            else:
                logger.debug("Compression didn't reduce file size, using original")
                return media_item.data

        return media_item.data

    def _bot_was_mentioned(self, update: Update) -> bool:
        """Check if the bot was mentioned in the message."""
        message = update.effective_message
        if not message:
            return False
        
        bot_mention = f"@{update.get_bot().username}".lower()
        if not message.entities and message.caption:
            return message.caption.lower().find(bot_mention) != -1
            
        if message.entities:
            return any(
                entity.type == "mention" and 
                message.parse_entity(entity).lower() == bot_mention
                for entity in message.entities
            )
        
        return False
        
    def _extract_urls(self, text: str) -> List[str]:
        """Extract URLs from message text."""
        return re.findall(r'(https?://\S+)', text)

    def _cleanup_task(self, user_id: int, task: asyncio.Task) -> None:
        """Remove completed task from tracking."""
        if user_id in self.message_tasks:
            self.message_tasks[user_id].discard(task)
            if not self.message_tasks[user_id]:
                del self.message_tasks[user_id]
                
    async def cancel_user_downloads(self, user_id: int) -> None:
        """Cancel all active downloads for a user."""
        if user_id in self.message_tasks:
            tasks = self.message_tasks[user_id]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            del self.message_tasks[user_id]

    async def cleanup(self):
        """Clean up resources when shutting down."""
        # Cancel all active tasks
        all_tasks = []
        for user_id in list(self.message_tasks.keys()):
            all_tasks.extend(self.message_tasks[user_id])
            
        for task in all_tasks:
            task.cancel()
            
        if all_tasks:
            await asyncio.gather(*all_tasks, return_exceptions=True)
            
        # Clean up all temporary directories
        self.temp_manager.cleanup_all_temp_dirs()
        
        # Shutdown thread pool in video processor
        self.video_processor.thread_pool.shutdown(wait=True)