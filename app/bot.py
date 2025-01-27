import re
import asyncio
import logging
from typing import Dict, List, Optional, Set
from temp_manager import TempManager
from media_types import DownloadResult, MediaType, MediaItem
from video_processor import VideoProcessor
from downloaders.base import VideoDownloader
from config import Config
from telethon import TelegramClient, events, connection
from telethon.events import NewMessage, CallbackQuery
from telethon.tl.custom import Message
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument
from assistant import ChatAssistant

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

class VideoDownloadBot:
    downloaders: List[VideoDownloader]

    def __init__(self, config: Config):
        """Initialize the bot with configuration."""
        self.config = config
        
        # Initialize Telethon client with proper proxy handling

        client_params = {
            'session': str('state/' + config.telegram.session),
            'api_id': config.telegram.api_id,
            'api_hash': config.telegram.api_hash,
            'timeout': config.telegram.timeout,
            'connection': connection.ConnectionTcpFull,
            'auto_reconnect': True,
            'retry_delay': 1
        }

        # Set MTProxy if configured
        if config.telegram.get_mtproxy_config():
            client_params.update({
                'connection': connection.ConnectionTcpMTProxyRandomizedIntermediate,
                'proxy': config.telegram.get_mtproxy_config()
            })
        # Set regular proxy if configured
        elif config.telegram.get_proxy_config():
            client_params['proxy'] = config.telegram.get_proxy_config()

        self.client = TelegramClient(**client_params)
        
        # Initialize components and event handlers
        self._setup_components()
        self._setup_handlers()

    def _setup_components(self) -> None:
        """Initialize all bot components."""
        self.temp_manager = TempManager(self.config.temp_dir)
        self.video_processor = VideoProcessor(self.config, self.temp_manager)
        self.assistant = ChatAssistant(self.config, self.client)
        self.downloaders = []
        
        # Use compression config values
        self.MAX_TELEGRAM_SIZE_MB = self.config.compression.max_telegram_size_mb
        self.MAX_COMPRESS_SIZE_MB = self.config.compression.max_compress_size_mb
        
        # Add concurrency control
        self.download_semaphore = asyncio.Semaphore(self.config.max_concurrent_downloads)
        self.active_downloads: Dict[int, Set[asyncio.Task]] = {}
        self.user_semaphores: Dict[int, asyncio.Semaphore] = {}
        self.message_tasks: Dict[int, Set[asyncio.Task]] = {}
        self.max_downloads_per_user = self.config.max_downloads_per_user

    def _setup_handlers(self) -> None:
        """Set up event handlers."""
        @self.client.on(events.NewMessage())
        async def handle_message(event: NewMessage.Event):
            if not event.message:
                return
                
            try:
                await self._handle_message(event.message)
            except Exception as e:
                logger.error(f"Error handling message: {e}", exc_info=True)

        @self.client.on(events.CallbackQuery())
        async def handle_callback(event: CallbackQuery.Event):
            try:
                await self._handle_callback(event)
            except Exception as e:
                logger.error(f"Error handling callback: {e}", exc_info=True)

    async def start(self) -> None:
        """Start the bot."""
        
        await self.client.start(
            phone=lambda : "bot" if self.config.telegram.use_bot else self.config.telegram.phone_number,
            password=lambda : self.config.telegram.password if not self.config.telegram.use_bot else None,
            bot_token=self.config.telegram.bot_token if self.config.telegram.use_bot else None
        )
        
        # Get bot info
        me = await self.client.get_me()
        if not me or not me.bot:
            raise RuntimeError("This user account is not a bot!")
            
        self.assistant.set_bot_id(me.id)
        logger.info(f"Bot authorized as @{me.username}")
        
        await self._initialize_downloaders()

    async def run(self):
        """Run the bot."""
        await self.start()
        logger.info("Bot started, waiting for messages")
        await self.client.run_until_disconnected()

    async def _handle_message(self, message: Message) -> None:
        """Handle new message events."""
        chat = await message.get_chat()
        sender = await message.get_sender()
        
        # Skip messages from bots
        if sender and sender.bot:
            return

        # Check if private chat or bot was mentioned
        is_private = message.is_private
        was_mentioned = message.mentioned
        
        if not (is_private or was_mentioned):
            return

        # Extract images and text
        images = await self._extract_images(message)
        text = message.text or message.raw_text or ""
        
        # Handle URLs first
        urls = self._extract_urls(text)
        if urls:
            await self._handle_url_download(message, urls[0])
            return

        # Handle chat message
        conversation_context = await self.assistant.get_conversation_context(message)
        await self.assistant.handle_message(message, images, conversation_context)

    async def _handle_callback(self, event: CallbackQuery.Event) -> None:
        """Handle callback query events."""
        # ...existing callback handling code...

    async def _extract_images(self, message: Message) -> List[str]:
        """Extract image files from message."""
        images = []
        if message.media:
            if isinstance(message.media, MessageMediaPhoto):
                images.append(await message.download_media(bytes))
            elif isinstance(message.media, MessageMediaDocument):
                if message.media.document.mime_type.startswith('image/'):
                    images.append(await message.download_media(bytes))
        return images

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

    async def _handle_url_download(self, message: Message, url: str):
        """Handle URL download in a separate task."""
        user_id = message.sender_id
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
            await message.reply("Анлак, произошла ошибка. Попробуй позже.")
            
    async def _handle_download(self, user_id: int, url: str, message: Message):
        """Handle the actual download process."""
        status_message: Message = await message.reply("Воу-воу... Работяги добывают контент, подождите, пожалуйста.")
        
        try:
            downloader = next((d for d in self.downloaders if d.can_handle(url)), None)
            if not downloader:
                await self.client.edit_message(
                    entity=status_message,
                    text="Анлак, я не умею скачивать контент с этого ресурса!"
                )
                return
                
            download_result = await asyncio.wait_for(
                downloader.download(url),
                timeout=self.config.download_timeout
            )
            
            if not download_result:
                await self.client.edit_message(
                    entity=status_message,
                    text="Анлак, не получилось скачать контент."
                )
                return
            
            await self.client.edit_message(
                entity=status_message,
                text="Опаааа! Работяги завершили работу. Обрабатываем контент..."
            )
            
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
            await self.client.edit_message(
                entity=status_message,
                text="Анлак, действие заняло слишком много времени. Попробуй позже."
            )
        except Exception as e:
            logger.error(f"Error handling download: {e}", exc_info=True)
            await self.client.edit_message(
                entity=message,
                text="Анлак, произошла ошибка. Попробуй позже."
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
                photos_and_videos.append({
                    'file': processed_data,
                    'caption': item.caption,
                    'force_document': item.media_type == MediaType.VIDEO,
                    'video': item.media_type == MediaType.VIDEO,
                    'supports_streaming': item.media_type == MediaType.VIDEO
                })

        # Send photos and videos as media group if multiple
        if photos_and_videos:
            if len(photos_and_videos) > 1:
                # For multiple files, use send_file with list
                await self.client.send_file(
                    entity=message.chat_id,
                    file=[item['file'] for item in photos_and_videos],
                    caption=[item['caption'] for item in photos_and_videos],
                    reply_to=message.id,
                    force_document=photos_and_videos[0]['force_document'],
                    supports_streaming=photos_and_videos[0]['supports_streaming']
                )
            else:
                # For single file, use simpler call
                item = photos_and_videos[0]
                await self.client.send_file(
                    entity=message.chat_id,
                    file=item['file'],
                    caption=item['caption'],
                    reply_to=message.id,
                    force_document=item['force_document'],
                    supports_streaming=item['supports_streaming']
                )

        # Send audio files separately
        for audio_data, caption in audio_items:
            await self.client.send_file(
                entity=message.chat_id,
                file=audio_data,
                caption=caption,
                voice_note=True,  # Mark as voice message
                reply_to=message.id
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
            await self.client.edit_message(
                entity=status_message,
                text=f"Анлак, видео слишком большое ({size_mb:.1f}MB) для обработки. Выбери видео поменьше."
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
            await self.client.edit_message(entity=status_message, text=compression_msg)

            # Apply compression with force_compress for videos above threshold
            force_compress = size_mb > self.config.compression.default_compress_threshold_mb
            compressed_data = await self.video_processor.compress_video(
                media_item.data,
                self.MAX_TELEGRAM_SIZE_MB,
                user_id,
                force_compress=force_compress
            )
            
            if not compressed_data:
                await self.client.edit_message(
                    entity=status_message,
                    text="Анлак, не удалось сжать видео. Выбери видео поменьше."
                )
                return None
            
            compressed_size = len(compressed_data) / (1024 * 1024)
            logger.debug(f"Compression result for user {user_id}: {size_mb:.2f}MB -> {compressed_size:.2f}MB")
            
            if compressed_size > self.MAX_TELEGRAM_SIZE_MB:
                await self.client.edit_message(
                    entity=status_message,
                    text=f"Анлак, даже после сжатия видео слишком большое ({compressed_size:.1f}MB). "
                    "Выбери видео поменьше."
                )
                return None
                
            if compressed_size < size_mb:
                await self.client.edit_message(entity=status_message, text="Сжатие завершено, готовим к отправке...")
                return compressed_data
            else:
                logger.debug("Compression didn't reduce file size, using original")
                return media_item.data

        return media_item.data

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

    async def stop(self) -> None:
        """Stop the bot properly."""
        try:
            await self.client.disconnect()
        except:
            pass
        
        # Cleanup resources
        self.assistant.clear_history()
        self.temp_manager.cleanup_all_temp_dirs()
        self.video_processor.thread_pool.shutdown(wait=True)

    def get_user_semaphore(self, user_id: int) -> asyncio.Semaphore:
        """Get or create a semaphore for a specific user."""
        if user_id not in self.user_semaphores:
            self.user_semaphores[user_id] = asyncio.Semaphore(self.max_downloads_per_user)
        return self.user_semaphores[user_id]