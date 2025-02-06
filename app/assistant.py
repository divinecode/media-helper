from datetime import datetime
import logging
from typing import List, Dict, Optional, Set
import importlib
import asyncio
import random
import threading
import time
from dataclasses import dataclass, field
from telegram import Update, Message, User, Chat, PhotoSize
from telegram.ext import ContextTypes
from telegram.constants import ChatAction
from g4f.client import AsyncClient, ChatCompletion
from g4f.providers.retry_provider import RetryProvider
from config import Config
import httpx

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

@dataclass
class MessageContext:
    """Message context data structure."""
    role: str
    content: str
    name: str
    tag: Optional[str]
    msg_id: int
    time: float
    rpl_to: Optional[int] = None
    images: List[bytes] = field(
        default_factory=lambda: []
    )

    def to_dict(self) -> dict:
        """Convert message context to dictionary format with formatted content."""
        return {
            'role': self.role,
            'content': f"#{self.msg_id}"
                + f" {datetime.fromtimestamp(self.time).strftime('%Y-%m-%d %H:%M:%S')} "
                + self.name + (f" (@{self.tag})" if self.tag else "")
                + (f" replied to #{self.rpl_to}" if self.rpl_to else "")
                + f": {self.content}",
        }

class ChatAssistant:
    def __init__(self, config: Config):
        """Initialize ChatAssistant with configuration."""
        self.config = config
        self.proxies: List[str] = []
        self.failed_proxies: Dict[str, int] = {}  # Track failure count for each proxy
        self.proxy_lock = threading.Lock()
        self.last_proxy_refresh = 0
        
        self.g4f_client = None
        self.g4f_vision_client = None
        self.message_locks: Dict[int, asyncio.Lock] = {}
        self.bot_id: Optional[int] = None

    async def initialize(self):
        """Asynchronously initialize the assistant."""
        await self.refresh_clients()
        logger.info("ChatAssistant initialized successfully")

    async def refresh_clients(self):
        """Initialize or refresh G4F clients with new proxies."""
        await self._refresh_proxies()
        proxies = None
        
        if self.config.chat.use_proxies:
            proxy = await self._get_working_proxy()
            if proxy:
                proxies = {"all": proxy}
                logger.debug(f"Using proxy: {proxy}")
            else:
                logger.warning("No working proxies available")
        else:
            logger.debug("Proxies disabled by configuration")
            
        self.g4f_client = self._initialize_client(self.config.chat.providers, proxies)
        self.g4f_vision_client = self._initialize_client(self.config.chat.vision_providers, proxies)

    def _initialize_client(self, providers: List[str], proxies: Optional[Dict[str, str]] = None) -> AsyncClient:
        """Initialize G4F client with providers and proxy."""
        provider_classes = self._load_providers(providers)
        
        # Configure client with SSL verification and custom transport
        transport = httpx.AsyncHTTPTransport(
            verify=self.config.chat.verify_ssl,
            retries=self.config.chat.provider_retries,
        )
        
        return AsyncClient(
            provider=RetryProvider(
                providers=provider_classes,
                shuffle=self.config.chat.shuffle_providers
            ),
            proxies=proxies,
            timeout=self.config.chat.request_timeout,
            transport=transport
        )

    async def _validate_proxy(self, proxy: str) -> bool:
        """Test a single proxy by making a simple request."""
        transport = httpx.AsyncHTTPTransport(
            verify=self.config.chat.verify_ssl,
            retries=self.config.chat.proxy_validation_retries
        )

        client = AsyncClient(
            provider=RetryProvider(
                providers=self._load_providers(self.config.chat.providers),
                shuffle=False  # Don't shuffle during validation
            ),
            proxies={"all": proxy},
            timeout=self.config.chat.proxy_validation_timeout,
            transport=transport
        )

        try:
            response = await client.chat.completions.create(
                model=self.config.chat.model,
                messages=[{"role": "user", "content": "Reply with number 1"}],
                timeout=self.config.chat.proxy_validation_timeout
            )
            
            if response and response.choices:
                answer = response.choices[0].message.content.strip()
                return answer is not None
                
        except Exception as e:
            logger.debug(f"Proxy validation failed for {proxy}: {str(e)}")
            return False

        return False

    async def _validate_proxies(self, proxies: List[str]) -> List[str]:
        """Validate a list of proxies in batches."""
        valid_proxies = []
        total = len(proxies)
        batch_size = self.config.chat.proxy_validation_batch_size

        for i in range(0, total, batch_size):
            batch = proxies[i:i + batch_size]
            tasks = [self._validate_proxy(proxy) for proxy in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            for proxy, is_valid in zip(batch, results):
                if isinstance(is_valid, bool) and is_valid:
                    valid_proxies.append(proxy)
                    
            logger.debug(f"Validated {i + len(batch)}/{total} proxies, {len(valid_proxies)} valid so far")
            
        return valid_proxies

    async def _refresh_proxies(self):
        """Refresh proxy list if needed."""
        current_time = time.time()
        if current_time - self.last_proxy_refresh > self.config.chat.proxy_refresh_interval:
            with self.proxy_lock:
                if self.config.chat.use_proxies:
                    from proxy_scraper import scrape_proxies
                    scraped_proxies = await scrape_proxies()
                    logger.info(f"Scraped {len(scraped_proxies)} proxies, validating...")
                    
                    self.proxies = await self._validate_proxies(scraped_proxies)
                    self.failed_proxies.clear()
                    self.last_proxy_refresh = current_time
                    
                    logger.info(f"Validation complete. {len(self.proxies)} valid proxies out of {len(scraped_proxies)}")
                else:
                    self.proxies = []
                    self.failed_proxies.clear()
                    logger.debug("Proxy usage is disabled")

    async def _get_working_proxy(self) -> Optional[str]:
        """Get a working proxy from the list."""
        with self.proxy_lock:
            # Filter out proxies that have failed too many times
            available_proxies = [
                p for p in self.proxies 
                if self.failed_proxies.get(p, 0) < self.config.chat.max_proxy_fails
            ]
            
            if not available_proxies:
                # If no proxies available, try refreshing the list
                await self._refresh_proxies()
                available_proxies = [
                    p for p in self.proxies 
                    if self.failed_proxies.get(p, 0) < self.config.chat.max_proxy_fails
                ]
                
                if not available_proxies:
                    return None
                    
            return random.choice(available_proxies)

    def _mark_proxy_failed(self, proxy: Optional[str]):
        """Mark a proxy as failed and track failure count."""
        if not proxy:
            return
            
        with self.proxy_lock:
            current_fails = self.failed_proxies.get(proxy, 0)
            self.failed_proxies[proxy] = current_fails + 1
            logger.debug(f"Marked proxy as failed: {proxy} (fails: {current_fails + 1})")

    def _load_providers(self, providers: List[str]) -> List:
        """Load and initialize providers."""
        provider_classes = []
        for provider_name in providers:
            try:
                provider_module = importlib.import_module("g4f.Provider")
                provider_class = getattr(provider_module, provider_name)
                provider_classes.append(provider_class)
                logger.debug("Successfully loaded provider: %s", provider_name)
            except (ImportError, AttributeError) as e:
                logger.warning("Failed to load provider %s: %s", provider_name, e)

        if not provider_classes:
            from g4f.Provider import Blackbox
            provider_classes = [Blackbox]
            logger.info("Using default providers: Blackbox")

        return provider_classes

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle chat message and generate response."""
        message = update.effective_message
        conversation_context = await self.get_conversation_context(message)

        images = await self._get_message_images(message)
        if len(images) < 1 and message.reply_to_message:
            images = await self._get_message_images(message.reply_to_message)

        if not self._is_valid_message(message) or (len(images) < 1 and len(message.text.strip()) < 1):
            return

        user_id = message.from_user.id
        lock = await self._get_sender_lock(user_id)
        
        async with lock:  # Ensure only one message at a time per user
            try:
                typing_task = asyncio.create_task(self._show_typing_indicator(message))
                try:
                    await self._generate_and_send_response(message, context, images, conversation_context)
                finally:
                    await self._stop_typing_indicator(typing_task)
            except Exception as e:
                await self._handle_chat_error(message, context, e)


    async def _get_message_images(self, message: Message, limit: int = 1) -> List[bytes]:
        """Extract images from message and convert to base64."""
        identifiers: List[str] = []

        # Check photos in message
        if message.photo:
            photo: PhotoSize = max(message.photo, key=lambda p: p.file_size)
            identifiers.append(photo.file_id)
            
        # Check document attachments
        if message.document and message.document.mime_type.startswith('image/'):
            identifiers.append(message.document.file_id)

        if len(identifiers) < 1:
            return []

        logger.debug(f"Total found images in message {len(identifiers)}, but will be capped to {limit}: {identifiers}")
        identifiers = identifiers[:limit]

        files = [(await message.get_bot().get_file(id)) for id in identifiers]
        downloaded = [bytes(await file.download_as_bytearray()) for file in files]
            
        return downloaded

    async def get_conversation_context(self, message: Message) -> List[MessageContext]:
        """Get conversation context from reply chain."""
        context_messages: List[MessageContext] = []
        current_message = message
        max_context = self.config.chat.max_history - 1  # Leave room for the current message

        while current_message.reply_to_message and len(context_messages) < max_context:
            reply_to = current_message.reply_to_message
            text = reply_to.text or reply_to.caption or ""
            context_messages.append(await self._create_user_message(reply_to, text))
            current_message = reply_to

        return context_messages

    def _is_valid_message(self, message: Message) -> bool:
        """Check if message should be processed."""
        return message.from_user and not message.via_bot

    async def _get_sender_lock(self, user_id: int) -> asyncio.Lock:
        """Get or create a lock for a specific sender."""
        if user_id not in self.message_locks:
            self.message_locks[user_id] = asyncio.Lock()
        return self.message_locks[user_id]

    async def _generate_and_send_response(self, message: Message, context: ContextTypes.DEFAULT_TYPE, images: List[bytes], conversation_context: List[MessageContext]) -> None:
        """Generate and send AI response."""
        user_id = message.from_user.id
        logger.debug(f"Processing message from user {user_id} with {len(images)} images and {len(conversation_context)} context messages")
        
        message_text = self._extract_message_text(message)
        logger.debug(f"Extracted message text ({len(message_text)} chars): {message_text[:100]}")
        
        if not message_text and not images:
            logger.debug("Empty message detected, sending help response")
            await self._send_empty_message_response(message, context)
            return

        chat_messages = await self._build_chat_messages(images, conversation_context, message)
        logger.debug(f"Built conversation context with {len(chat_messages)} messages")
        
        await self._send_ai_response(chat_messages, message, context)

    def _extract_message_text(self, message: Message) -> str:
        """Extract and clean message text."""
        text = (message.text or message.caption or "").strip()

        quote = message.quote
        if quote:
            text = f"> {quote.text}\n\n{text}"

        return text

    async def _build_chat_messages(self, images: List[bytes], conversation_context: List[MessageContext], message: Message) -> List[MessageContext]:
        """Build complete message context for AI."""
        logger.debug("Building chat messages context")
        messages = []
        
        if conversation_context:
            logger.debug(f"Adding {len(conversation_context)} context messages")
            messages.extend(conversation_context)
        
        messages.append(MessageContext(
            role="system",
            content=self.config.chat.system_prompt,
            name="System",
            tag=None,
            msg_id=-1,
            time=datetime.now().timestamp()
        ))

        current_context = await self._create_user_message(message, images)
        logger.debug(f"Created user message context: {current_context.to_dict()}")
        messages.append(current_context)
        
        return messages

    async def _create_user_message(self, message: Message, images: List[bytes] = None) -> MessageContext:
        """Create user message context."""
        logger.debug(f"Creating user message context for message_id={message.message_id}")
        
        my_id = self.bot_id == message.from_user.id
        sender: User = message.from_user

        context = MessageContext(
            role="assistant" if my_id else "user",
            content=self._extract_message_text(message),
            name="Assistant (you)" if my_id else f"{sender.first_name or ''} {sender.last_name or ''}".strip(),
            tag=sender.username,
            msg_id=message.message_id,
            rpl_to=message.reply_to_message.message_id if message.reply_to_message else None,
            time=message.date.timestamp()
        )

        if images:
            context.images = images
        
        return context

    async def _show_typing_indicator(self, message: Message):
        """Keep sending typing status until cancelled."""
        typing_interval = 4.5  # Slightly less than Telegram's 5-second typing status
        try:
            while True:
                try:
                    await message.chat.send_action(ChatAction.TYPING, message_thread_id=message.message_thread_id)
                    await asyncio.sleep(typing_interval)
                except Exception as e:
                    if "message was deleted" in str(e).lower() or "not enough rights" in str(e).lower():
                        break
                    logger.warning(f"Error sending typing status: {e}")
                    await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise

    async def _stop_typing_indicator(self, typing_task: asyncio.Task) -> None:
        """Cancel typing status task."""
        logger.debug("Cancelling typing status")
        typing_task.cancel()
        try:
            await typing_task
        except asyncio.CancelledError:
            pass

    async def _send_ai_response(self, messages: List[MessageContext], message: Message, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Process GPT response with proxy rotation on failure."""
        if len(messages) < 1:
            return

        user_id = message.from_user.id
        retry_count = 0
        last_error = None
        messages_mapped = [msg.to_dict() for msg in messages]
        images = messages[-1].images
        image = images[0] if images else None

        for idx, msg in enumerate(messages_mapped):
            logger.debug(f"Message {idx}: {msg}")
        logger.debug(f"Last message contains {len(images)} images")

        while retry_count < self.config.chat.retries:
            logger.debug(f"Requesting AI completion for user {user_id}. Retries: {retry_count}")
            try:
                client = self.g4f_vision_client if image else self.g4f_client
                current_proxy = client.get_proxy()
                
                response: Optional[ChatCompletion] = None
                try:
                    response = await client.chat.completions.create(
                        model=self.config.chat.vision_model if image else self.config.chat.model,
                        messages=messages_mapped,
                        timeout=self.config.chat.timeout,
                        image=image
                    )

                    if response and response.choices:
                        full: str = response.choices[0].message.content.strip()
                        if not full:
                            return  # Keep original empty response handling
                        
                        chunks = self.split_text(full)
                        for chunk in chunks:
                            await message.reply_text(
                                text=chunk,
                                reply_to_message_id=message.message_id
                            )
                        return  # Success, exit the method
                        
                except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
                    logger.warning(f"Connection error with provider: {e}")
                    self._mark_proxy_failed(current_proxy)
                    raise  # Re-raise to trigger retry
                    
            except Exception as e:
                last_error = e
                logger.error(f"AI response error with proxy {current_proxy}: {str(e)}", exc_info=True)
                self._mark_proxy_failed(current_proxy)
                
                retry_count += 1
                if retry_count < self.config.chat.retries:
                    await asyncio.sleep(self.config.chat.proxy_retry_delay)
                    await self.refresh_clients()  # Get new client with new proxy
                    continue
                break  # All retries failed

        # If we got here, all retries failed
        error_msg = "Извини, все провайдеры сейчас недоступны. Попробуй позже."
        if isinstance(last_error, (httpx.ConnectError, httpx.ConnectTimeout)):
            error_msg = "Извини, есть проблемы с подключением к серверу. Попробуй позже."
            
        await message.reply_text(
            error_msg,
            reply_to_message_id=message.message_id
        )

    def split_text(self, text: str, max_length: str = 4096) -> List[str]:
        chunks: List[str] = []
        while text:
            if len(text) <= max_length:
                chunks.append(text)
                break

            # Try to split at the last '\n' within limit
            split_idx = text.rfind('\n', 0, max_length)
            if split_idx == -1:
                # If no '\n' found, try to split at the last space
                split_idx = text.rfind(' ', 0, max_length)
                if split_idx == -1:
                    # If no space found, force split at max_length
                    split_idx = max_length

            chunks.append(text[:split_idx])
            text = text[split_idx:].lstrip()  # Remove leading newline or space in next chunk

        return chunks


    async def _handle_chat_error(self, message: Message, context: ContextTypes.DEFAULT_TYPE, error: Exception) -> None:
        """Handle errors during message processing."""
        logger.error("Error in chat handling: %s", str(error), exc_info=True)
        await message.reply_text(
            "Извини, произошла ошибка при обработке сообщения.",
            reply_to_message_id=message.message_id
        )

    async def _send_empty_message_response(self, message: Message, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Send help prompt for empty messages."""
        logger.debug("Empty message, sending help prompt")
        await message.reply_text(
            "Мммм... чем могу помочь?",
            reply_to_message_id=message.message_id
        )

    def set_bot_id(self, bot_id: int):
        """Set bot ID for message filtering."""
        self.bot_id = bot_id