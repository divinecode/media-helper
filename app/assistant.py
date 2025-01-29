from datetime import datetime
import logging
from typing import List, Dict, Optional
import importlib
import asyncio
from dataclasses import dataclass
from telegram import Update, Message, User, Chat, PhotoSize
from telegram.ext import ContextTypes
from telegram.constants import ChatAction
from g4f.client import AsyncClient
from g4f.providers.retry_provider import RetryProvider
from config import Config

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
    images: List[bytes] = None

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
        self.g4f_client = self._initialize_client()
        self.message_locks: Dict[int, asyncio.Lock] = {}
        self.bot_id: Optional[int] = None
        logger.info("ChatAssistant initialized successfully")

    def _initialize_client(self) -> AsyncClient:
        """Initialize G4F client with providers."""
        logger.debug("Initializing ChatAssistant with config: %s", self._get_config_debug_info())
        provider_classes = self._load_providers()
        
        return AsyncClient(
            provider=RetryProvider(
                providers=provider_classes,
                shuffle=self.config.chat.shuffle_providers
            )
        )

    def _get_config_debug_info(self) -> dict:
        """Get configuration debug information."""
        return {
            'model': self.config.chat.model,
            'timeout': self.config.chat.timeout,
            'max_history': self.config.chat.max_history,
            'providers': self.config.chat.providers
        }

    def _load_providers(self) -> List:
        """Load and initialize providers."""
        provider_classes = []
        for provider_name in self.config.chat.providers:
            try:
                provider_module = importlib.import_module("g4f.Provider")
                provider_class = getattr(provider_module, provider_name)
                provider_classes.append(provider_class)
                logger.debug("Successfully loaded provider: %s", provider_name)
            except (ImportError, AttributeError) as e:
                logger.warning("Failed to load provider %s: %s", provider_name, e)

        if not provider_classes:
            from g4f.Provider import ChatGptt, Blackbox
            provider_classes = [ChatGptt, Blackbox]
            logger.info("Using default providers: ChatGptt, Blackbox")

        return provider_classes

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle chat message and generate response."""
        message = update.effective_message
        conversation_context = await self.get_conversation_context(message)
        images = self._get_message_images(message)

        if not self._is_valid_message(message):
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

    def _get_message_images(self, message: Message) -> List[str]:
        """Extract images from message and convert to base64."""
        images = []
        
        # Check photos in message
        if message.photo:
            photo: PhotoSize = max(message.photo, key=lambda p: p.file_size)
            images.append(photo.file_id)
            
        # Check document attachments
        if message.document and message.document.mime_type.startswith('image/'):
            images.append(message.document.file_id)
            
        return images

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

    async def _build_chat_messages(self, images: List[bytes], conversation_context: List[MessageContext], message: Message) -> List[dict]:
        """Build complete message context for AI."""
        logger.debug("Building chat messages context")
        messages = []
        
        if conversation_context:
            logger.debug(f"Adding {len(conversation_context)} context messages")
            messages.extend([msg.to_dict() for msg in conversation_context])
        
        messages.append(MessageContext(
            role="system",
            content=self.config.chat.system_prompt,
            name="System",
            tag=None,
            msg_id=-1,
            time=datetime.now().timestamp()
        ).to_dict())

        current_context = await self._create_user_message(message, images)
        logger.debug(f"Created user message context: {current_context.to_dict()}")
        messages.append(current_context.to_dict())
        
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
                    await message.chat.send_action(ChatAction.TYPING)
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

    async def _send_ai_response(self, messages: List[dict], message: Message, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Process GPT response and send it to the user."""
        user_id = message.from_user.id
        try:
            logger.debug(f"Requesting AI completion for user {user_id}")
            for idx, msg in enumerate(messages):
                logger.debug(f"Message {idx}: {msg}")
            
            response = await self.g4f_client.chat.completions.create(
                model=self.config.chat.model,
                messages=messages,
                timeout=self.config.chat.timeout
            )

            if response and response.choices:
                assistant_response = response.choices[0].message.content
                await message.reply_text(
                    assistant_response,
                    reply_to_message_id=message.message_id
                )
                logger.debug("Sent response to user")
            else:
                raise ValueError("No valid response received")

        except Exception as e:
            logger.error(f"AI response error for user {user_id}: {str(e)}", exc_info=True)
            await message.reply_text(
                "Извини, что-то пошло не так с обработкой запроса. Попробуй позже.",
                reply_to_message_id=message.message_id
            )

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