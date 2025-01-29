from datetime import datetime
import logging
from typing import List, Dict, Optional, Any
import importlib
import asyncio
import json
from dataclasses import dataclass, asdict
from telegram import Update, Message, User, Chat
from telegram.ext import ContextTypes
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
        self.message_locks: Dict[int, asyncio.Lock] = {}  # One message at a time per user
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

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE, images: List[bytes], conversation_context: List[dict]) -> None:
        """Handle chat message and generate response."""
        message = update.message
        if not self._is_valid_message(message):
            return

        try:
            await context.bot.send_chat_action(chat_id=message.chat_id, action='typing')
            await self._generate_and_send_response(message, context, images, conversation_context)
        except Exception as e:
            await self._handle_chat_error(message, context, e)

    def _is_valid_message(self, message: Message) -> bool:
        """Check if message should be processed."""
        return message.from_user and not message.via_bot

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

        chat_messages = await self._build_chat_messages(message_text, images, conversation_context, message)
        logger.debug(f"Built conversation context with {len(chat_messages)} messages")
        
        await self._send_ai_response(chat_messages, message, context)

    def _extract_message_text(self, message: Message) -> str:
        """Extract and clean message text."""
        return message.text or ""

    async def _build_chat_messages(self, message_text: str, images: List[str], conversation_context: List[MessageContext], message: Message) -> List[MessageContext]:
        """Build complete message context for AI."""
        logger.debug("Building chat messages context")
        messages: List[MessageContext] = []
        
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

        current_context = await self._create_user_message(message, message_text, images)
        logger.debug(f"Created user message context: {current_context}")
        messages.append(current_context)
        
        return messages

    async def _create_user_message(self, message: Message, custom_text: str, images: List[bytes] = []) -> MessageContext:
        """Create user message context with sanitized text."""
        logger.debug(f"Creating user message context for message_id={message.message_id}")

        my_id = self.bot_id == message.from_user.id
        sender: User = message.from_user

        text = custom_text if custom_text is not None else message.text or ""
        role = "assistant" if my_id else "user"

        name = "Assistant (you)" if my_id else f"{sender.first_name or ''} {sender.last_name or ''}".strip()

        context = MessageContext(
            role=role,
            content=text,
            name=name,
            tag=sender.username,
            msg_id=message.message_id,
            rpl_to=message.reply_to_message.message_id if message.reply_to_message else None,
            time=message.date.timestamp()
        )

        context.images = images
        return context

    async def _get_sender_lock(self, user_id: int) -> asyncio.Lock:
        """Get or create a lock for a specific sender."""
        if user_id not in self.message_locks:
            self.message_locks[user_id] = asyncio.Lock()
        return self.message_locks[user_id]

    async def _send_ai_response(self, messages: List[MessageContext], message: Message, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Process GPT response and send it to the user."""
        user_id = message.from_user.id
        
        lock = await self._get_sender_lock(user_id)
        async with lock:  # Ensure only one message at a time per user
            try:
                await context.bot.send_chat_action(chat_id=message.chat_id, action='typing')
                
                # Convert messages to G4F format
                g4f_messages = [msg.to_dict() for msg in messages]
                for idx, msg in enumerate(g4f_messages):
                    logger.debug(f"Message {idx}: {msg}")
                logger.debug(f"Requesting AI completion for user {user_id}")
                
                response = await self.g4f_client.chat.completions.create(
                    model=self.config.chat.model,
                    messages=g4f_messages,
                    timeout=self.config.chat.timeout
                )

                if response and response.choices:
                    assistant_response = response.choices[0].message.content
                    await message.reply_text(assistant_response)
                    logger.debug("Sent response to user")
                else:
                    raise ValueError("No valid response received")

            except Exception as e:
                logger.error(f"AI response error for user {user_id}: {str(e)}", exc_info=True)
                await message.reply_text("Извини, что-то пошло не так с обработкой запроса. Попробуй позже.")

    async def _handle_chat_error(self, message: Message, context: ContextTypes.DEFAULT_TYPE, error: Exception) -> None:
        """Handle errors during message processing."""
        logger.error("Error in chat handling: %s", str(error), exc_info=True)
        await message.reply_text("Извини, произошла ошибка при обработке сообщения.")

    async def _send_empty_message_response(self, message: Message, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Send help prompt for empty messages."""
        logger.debug("Empty message, sending help prompt")
        await context.bot.send_chat_action(chat_id=message.chat_id, action='typing')
        help_text = "Мммм... чем могу помочь?"
        await message.reply_text(help_text)

    def clear_history(self):
        """Clear chat history."""
        self.chat_history = []

    def set_bot_id(self, bot_id: int):
        """Set bot ID for message filtering."""
        self.bot_id = bot_id