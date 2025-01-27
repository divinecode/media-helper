import logging
import re
from typing import List, Dict, Optional
import importlib
import asyncio
from dataclasses import dataclass
from telegram import Message, PhotoSize
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

class ChatAssistant:
    def __init__(self, config: Config):
        """Initialize ChatAssistant with configuration."""
        self.config = config
        self.chat_history: Dict[int, List[dict]] = {}
        self.bot_id = config.bot_id
        self.g4f_client = self._initialize_client()
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

    async def handle_message(self, message: Message, images: List[str], conversation_context: List[dict]) -> None:
        """Handle chat message and generate response."""
        if not self._is_valid_message(message):
            return

        try:
            typing_task = asyncio.create_task(self._show_typing_indicator(message))
            try:
                await self._generate_and_send_response(message, images, conversation_context)
            finally:
                await self._stop_typing_indicator(typing_task)
        except Exception as e:
            await self._handle_chat_error(message, e)

    def _is_valid_message(self, message: Message) -> bool:
        """Check if message should be processed."""
        if message.from_user.is_bot:
            logger.debug("Skipping bot message from %d", message.from_user.id)
            return False
        return True

    async def _generate_and_send_response(self, message: Message, images: List[str], conversation_context: List[dict]) -> None:
        """Generate and send AI response."""
        user_id = message.from_user.id
        logger.debug(f"Processing message from user {user_id} with {len(images)} images and {len(conversation_context)} context messages")
        
        message_text = self._extract_message_text(message)
        logger.debug(f"Extracted message text ({len(message_text)} chars): {message_text[:100]}")
        
        if not message_text and not images:
            logger.debug("Empty message detected, sending help response")
            await self._send_empty_message_response(message)
            return

        chat_messages = await self._build_chat_messages(message_text, images, conversation_context, message)
        logger.debug(f"Built conversation context with {len(chat_messages)} messages")
        for idx, msg in enumerate(chat_messages):
            logger.debug(f"Message {idx}: role={msg.get('role')}, content_length={len(msg.get('content', ''))}")
        
        await self._send_ai_response(chat_messages, message)

    def _extract_message_text(self, message: Message) -> str:
        """Extract and clean message text."""
        return (message.text or message.caption or "").strip()

    async def _build_chat_messages(self, message_text: str, images: List[str], conversation_context: List[dict], message: Message) -> List[dict]:
        """Build complete message context for AI."""
        logger.debug("Building chat messages context")
        messages = [{"role": "system", "content": self.config.chat.system_prompt}]
        
        if conversation_context:
            logger.debug(f"Adding {len(conversation_context)} context messages")
            messages.extend(conversation_context)
        
        current_context = await self._create_user_message(message, message_text, images)
        logger.debug(f"Created user message context: {current_context.__dict__}")
        messages.append(current_context.__dict__)
        
        return messages

    async def _create_user_message(self, message: Message, text: str, images: List[str]) -> MessageContext:
        """Create user message context."""
        logger.debug(f"Creating user message context for message_id={message.message_id}")
        msg_dict = self._create_message_data(message, text)
        context = MessageContext(**msg_dict)

        if images:
            logger.debug(f"Processing {len(images)} images for the message")
            context.images = await self._download_images(message, images)
            logger.debug(f"Successfully processed {len(context.images)} images")
        
        return context

    def _create_message_data(self, message: Message, custom_text: str = None) -> dict:
        """Create standardized message data."""
        user = message.from_user
        text = custom_text if custom_text is not None else (message.text or message.caption or "")
        role = "user" if user.id != self.bot_id else "assistant"
        
        return {
            "role": role,
            "content": text,
            "name": user.full_name if role == "user" else "Assistant",
            "tag": f"@{user.username}" if user.username else None,
            "msg_id": message.message_id,
            "rpl_to": message.reply_to_message.message_id if message.reply_to_message else None,
            "time": message.date.timestamp()
        }

    async def _download_images(self, message: Message, images: List[str]) -> List[bytes]:
        """Process and download images."""
        image_data = []
        for i, file_id in enumerate(images):
            try:
                file = await message.get_bot().get_file(file_id)
                image_bytes = await file.download_as_bytearray()
                image_data.append(image_bytes)
                logger.debug("Successfully processed image %d/%d", i+1, len(images))
            except Exception as e:
                logger.error("Failed to process image %d: %s", i+1, e)
        return image_data

    async def _send_ai_response(self, messages: List[dict], message: Message) -> None:
        """Process GPT response and send it to the user."""
        user_id = message.from_user.id
        try:
            logger.debug(f"Requesting AI completion for user {user_id}")
            logger.debug(f"Using model: {self.config.chat.model}, timeout: {self.config.chat.timeout}s")
            for idx, msg in enumerate(messages):
                logger.debug(f"#{idx}: {msg}")
            
            response = await self.g4f_client.chat.completions.create(
                model=self.config.chat.model,
                messages=messages,
                timeout=self.config.chat.timeout
            )

            if response and response.choices:
                assistant_response = response.choices[0].message.content
                response_length = len(assistant_response)
                logger.debug(f"Got response ({response_length} chars): {assistant_response[:100]}")
                
                await self._update_chat_history(user_id, messages[-1], assistant_response)
                logger.debug("Updated chat history")
                
                await message.reply_text(
                    assistant_response,
                    reply_to_message_id=message.message_id
                )
                logger.debug("Sent response to user")
            else:
                logger.warning("Received empty response from AI")
                raise ValueError("No valid response received")

        except Exception as e:
            logger.error(f"AI response error for user {user_id}: {str(e)}", exc_info=True)
            await message.reply_text(
                "Извини, что-то пошло не так с обработкой запроса. Попробуй позже.",
                reply_to_message_id=message.message_id
            )

    async def _update_chat_history(self, user_id: int, user_message: dict, assistant_response: str):
        """Update chat history for the user."""
        logger.debug(f"Updating chat history for user {user_id}")
        if user_id not in self.chat_history:
            self.chat_history[user_id] = [{
                "role": "system",
                "content": self.config.chat.system_prompt
            }]
        
        self.chat_history[user_id].append(user_message)
        self.chat_history[user_id].append({
            "role": "assistant",
            "content": assistant_response
        })
        
        if len(self.chat_history[user_id]) > self.config.chat.max_history + 1:
            self.chat_history[user_id] = [
                self.chat_history[user_id][0],
                *self.chat_history[user_id][-(self.config.chat.max_history):]
            ]
        logger.debug(f"Chat history updated, new length: {len(self.chat_history[user_id])}")

    async def get_conversation_context(self, message: Message) -> List[dict]:
        """Get conversation context from reply chain and nearby messages."""
        logger.debug(f"Getting conversation context for message {message.message_id}")
        context_messages = []
        max_context = self.config.chat.max_history - 1
        
        if message.reply_to_message:
            reply_chain = await self._get_reply_chain(message, max_context // 2)
            context_messages.extend(reply_chain)

        # Sort messages by timestamp to maintain conversation flow
        context_messages.sort(key=lambda x: x.get('timestamp', 0))
        logger.debug(f"Retrieved {len(context_messages)} context messages")
        return context_messages

    async def _get_reply_chain(self, message: Message, max_depth: int) -> List[dict]:
        """Get context from reply chain."""
        context_messages = []
        current_message = message
        
        while current_message.reply_to_message and len(context_messages) < max_depth:
            logger.debug(f"Adding message to context: {current_message.reply_to_message.message_id}")
            current_message = current_message.reply_to_message
            context_messages.insert(0, self._create_message_data(current_message))

        return context_messages

    async def _show_typing_indicator(self, message: Message):
        """Keep sending typing status until cancelled."""
        typing_interval = 4.5  # Slightly less than Telegram's 5-second typing status
        try:
            while True:
                try:
                    await message.chat.send_action(ChatAction.TYPING)
                    await asyncio.sleep(typing_interval)
                except Exception as e:
                    # If message was deleted or we can't send typing status, stop the loop
                    if "message was deleted" in str(e).lower() or "not enough rights" in str(e).lower():
                        break
                    logger.warning(f"Error sending typing status: {e}")
                    await asyncio.sleep(1)  # Wait a bit before retrying
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

    async def _handle_chat_error(self, message: Message, error: Exception) -> None:
        """Handle errors during message processing."""
        logger.error("Error in chat handling: %s", str(error), exc_info=True)
        await message.reply_text(
            "Извини, произошла ошибка при обработке сообщения.",
            reply_to_message_id=message.message_id
        )

    async def _send_empty_message_response(self, message: Message) -> None:
        """Send help prompt for empty messages."""
        logger.debug("Empty message, sending help prompt")
        await message.reply_text(
            "Мммм... чем могу помочь?",
            reply_to_message_id=message.message_id
        )

    def clear_history(self):
        """Clear chat history."""
        self.chat_history.clear()
