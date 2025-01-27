import logging
from typing import List, Dict, Optional, Any
import importlib
import asyncio
import random
from dataclasses import dataclass, asdict
from telethon import TelegramClient
from telethon.tl.custom import Message
from telethon.tl.types import User, Channel, MessageReplyHeader
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
        """Convert to dictionary for G4F API, excluding None values and binary data."""
        # Only include role and content for G4F
        return {
            'role': self.role,
            'content': self.content
        }

class ChatAssistant:
    tg: TelegramClient
    bot_id: int

    def __init__(self, config: Config, tg: TelegramClient):
        """Initialize ChatAssistant with configuration."""
        self.config = config
        self.tg = tg
        self.g4f_client = self._initialize_client()
        self.message_locks: Dict[int, asyncio.Lock] = {}  # One message at a time per user
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

    async def handle_message(self, message: Message, images: List[bytes], conversation_context: List[dict]) -> None:
        """Handle chat message and generate response."""
        if not self._is_valid_message(message):
            return

        try:
            # Use message action instead of ChatAction
            async with message.client.action(message.chat_id, 'typing'):
                await self._generate_and_send_response(message, images, conversation_context)
        except Exception as e:
            await self._handle_chat_error(message, e)

    def _is_valid_message(self, message: Message) -> bool:
        """Check if message should be processed."""
        return message.sender and not message.via_bot

    async def _generate_and_send_response(self, message: Message, images: List[bytes], conversation_context: List[MessageContext]) -> None:
        """Generate and send AI response."""
        user_id = message.sender_id
        logger.debug(f"Processing message from user {user_id} with {len(images)} images and {len(conversation_context)} context messages")
        
        message_text = self._extract_message_text(message)
        logger.debug(f"Extracted message text ({len(message_text)} chars): {message_text[:100]}")
        
        if not message_text and not images:
            logger.debug("Empty message detected, sending help response")
            await self._send_empty_message_response(message)
            return

        chat_messages = await self._build_chat_messages(message_text, images, conversation_context, message)
        logger.debug(f"Built conversation context with {len(chat_messages)} messages")
        
        await self._send_ai_response(chat_messages, message)

    def _extract_message_text(self, message: Message) -> str:
        """Extract and clean message text."""
        return (message.text or message.raw_text or "").strip()

    async def _build_chat_messages(self, message_text: str, images: List[str], conversation_context: List[MessageContext], message: Message) -> List[MessageContext]:
        """Build complete message context for AI."""
        logger.debug("Building chat messages context")
        messages: List[MessageContext] = [
            MessageContext(
                role="system",
                content=self.config.chat.system_prompt,
                name="System",
                tag=None,
                msg_id=0,
                time=0
            )
        ]
        
        if conversation_context:
            logger.debug(f"Adding {len(conversation_context)} context messages")
            messages.extend(conversation_context)
        
        current_context = await self._create_user_message(message, message_text, images)
        logger.debug(f"Created user message context: {current_context}")
        messages.append(current_context)
        
        return messages

    async def _create_user_message(self, message: Message, custom_text: str, images: List[bytes] = []) -> MessageContext:
        """Create user message context."""
        logger.debug(f"Creating user message context for message_id={message.id}")

        my_id = self.bot_id == message.sender_id
        sender: User | Channel | None = message.sender

        text = custom_text if custom_text is not None else (message.text or message.raw_text or "")
        role = "assistant" if my_id else "user"

        if my_id:
            name = "Assistant (you)"
        elif isinstance(sender, User):
            name = f"{sender.first_name or ''} {sender.last_name or ''}".strip()
        elif isinstance(sender, Channel):
            name = sender.title
        else:
            name = "Unknown"

        context = MessageContext(
            role=role,
            content=text,
            name=name,
            tag=f"@{sender.username}" if sender.username else None,
            msg_id=message.id,
            rpl_to=message.reply_to.reply_to_msg_id if message.reply_to else None,
            time=message.date.timestamp()
        )

        context.images = images
        return context

    async def _get_sender_lock(self, user_id: int) -> asyncio.Lock:
        """Get or create a lock for a specific sender."""
        if user_id not in self.message_locks:
            self.message_locks[user_id] = asyncio.Lock()
        return self.message_locks[user_id]

    async def _send_ai_response(self, messages: List[MessageContext], message: Message) -> None:
        """Process GPT response and send it to the user."""
        user_id = message.sender_id
        
        lock = await self._get_sender_lock(user_id)
        async with lock:  # Ensure only one message at a time per user
            typing_task = None
            try:
                # Start typing task outside the action context to cover the entire process
                typing_task = asyncio.create_task(self._show_typing_indicator(message))
                
                # Convert messages to G4F format
                g4f_messages = [msg.to_dict() for msg in messages]
                logger.debug(f"Requesting AI completion for user {user_id}")
                
                response = await self.g4f_client.chat.completions.create(
                    model=self.config.chat.model,
                    messages=g4f_messages,
                    timeout=self.config.chat.timeout
                )

                if response and response.choices:
                    assistant_response = response.choices[0].message.content
                    await message.reply(assistant_response)
                    logger.debug("Sent response to user")
                else:
                    raise ValueError("No valid response received")

            except Exception as e:
                logger.error(f"AI response error for user {user_id}: {str(e)}", exc_info=True)
                await message.reply("Извини, что-то пошло не так с обработкой запроса. Попробуй позже.")
            finally:
                if typing_task:
                    typing_task.cancel()
                    try:
                        await typing_task
                    except asyncio.CancelledError:
                        pass

    async def _show_typing_indicator(self, message: Message):
        """Keep sending typing status until cancelled."""
        try:
            while True:
                async with message.client.action(message.chat_id, 'typing'):
                    await asyncio.sleep(4.0)  # Slightly less than Telegram's 5-second limit
        except asyncio.CancelledError:
            pass

    async def get_conversation_context(self, message: Message) -> List[MessageContext]:
        """Get conversation context from reply chain and nearby messages."""
        logger.debug(f"Getting conversation context for message {message.id}")
        context_messages: List[MessageContext] = []
        max_context = self.config.chat.max_history - 1
        
        if message.reply_to:
            reply_chain = await self._get_reply_chain(message, max_context // 2)
            context_messages.extend(reply_chain)

        # Sort messages by timestamp to maintain conversation flow
        context_messages.sort(key=lambda x: x.time)
        logger.debug(f"Retrieved {len(context_messages)} context messages")
        return context_messages

    async def _get_reply_chain(self, message: Message, max_depth: int) -> List[MessageContext]:
        """Get context from reply chain."""
        context_messages: List[MessageContext] = []
        current_message = message
        depth = 0
        
        while current_message.reply_to and depth < max_depth:
            reply_to = await current_message.get_reply_message()
            if not reply_to:
                break
                
            logger.debug(f"Adding message to context: {reply_to.id}")
            msg_context = await self._create_user_message(reply_to, None)
            context_messages.insert(0, msg_context)
            current_message = reply_to
            depth += 1

        return context_messages

    async def _handle_chat_error(self, message: Message, error: Exception) -> None:
        """Handle errors during message processing."""
        logger.error("Error in chat handling: %s", str(error), exc_info=True)
        await message.reply("Извини, произошла ошибка при обработке сообщения.")

    async def _send_empty_message_response(self, message: Message) -> None:
        """Send help prompt for empty messages."""
        logger.debug("Empty message, sending help prompt")
        async with message.client.action(message.chat_id, 'typing'):
            help_text = "Мммм... чем могу помочь?"
            await message.reply(help_text)

    def clear_history(self):
        """Clear chat history."""
        self.chat_history.clear()

    def set_bot_id(self, bot_id: int):
        """Set bot ID for message filtering."""
        self.bot_id = bot_id