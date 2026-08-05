from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections import deque
from time import monotonic
from typing import Any

import aiohttp

from kevin.config import TelegramSettings
from kevin.openai_chat import (
    MAX_CONVERSATION_TURNS,
    ConversationTurn,
    OpenAIAPIError,
    OpenAIChatClient,
    Source,
    requires_web_search,
    with_conversation_context,
)

log = logging.getLogger(__name__)

TELEGRAM_API_ROOT = "https://api.telegram.org/bot"
MAX_TELEGRAM_LENGTH = 4_096
MAX_SOURCE_BLOCK_LENGTH = 1_200
HELP_TEXT = (
    "Hey, I'm Kevin. Send me a question and I'll answer it, using web search when "
    "useful or when you ask me to look something up. In a group, mention me or reply "
    "to one of my messages. Use /reset to clear our recent conversation context or "
    "/id to see your Telegram user ID."
)


class TelegramAPIError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def format_telegram_reply(text: str, sources: list[Source]) -> str:
    """Fit a plain-text answer and clickable source URLs into Telegram's limit."""
    source_block = ""
    if sources:
        lines: list[str] = []
        for source in sources:
            line = f"• {source.title} — {source.url}"
            candidate = "\n".join([*lines, line])
            if len(f"\n\nSources:\n{candidate}") > MAX_SOURCE_BLOCK_LENGTH:
                continue
            lines.append(line)
        if lines:
            joined_lines = "\n".join(lines)
            source_block = f"\n\nSources:\n{joined_lines}"

    answer_limit = MAX_TELEGRAM_LENGTH - len(source_block)
    if answer_limit < 1:
        source_block = ""
        answer_limit = MAX_TELEGRAM_LENGTH
    if len(text) > answer_limit:
        text = text[: max(1, answer_limit - 1)].rstrip() + "…"
    return text + source_block


def telegram_question(
    message: dict[str, Any], *, bot_id: int, username: str
) -> tuple[str | None, str | None]:
    """Return the question and replied-to Kevin text when this message activates Kevin."""
    text = str(message.get("text") or message.get("caption") or "")
    if not text:
        return None, None

    chat = message.get("chat")
    is_private = isinstance(chat, dict) and chat.get("type") == "private"
    replied = message.get("reply_to_message")
    previous_reply: str | None = None
    if isinstance(replied, dict):
        author = replied.get("from")
        if isinstance(author, dict) and author.get("id") == bot_id:
            previous_reply = str(replied.get("text") or replied.get("caption") or "").strip()

    mention_pattern = re.compile(rf"(?<![\w@])@{re.escape(username)}\b(?:[,:]\s*)?", re.IGNORECASE)
    mentioned = mention_pattern.search(text) is not None
    if not is_private and not mentioned and previous_reply is None:
        return None, None

    question = mention_pattern.sub(" ", text)
    return " ".join(question.split()), previous_reply


class TelegramKevin:
    """A small Telegram long-polling adapter for Kevin's OpenAI chat."""

    def __init__(self, settings: TelegramSettings) -> None:
        self.settings = settings
        self.openai = OpenAIChatClient(settings.openai_api_key, settings.openai_model)
        self.session: aiohttp.ClientSession | None = None
        self.bot_id = 0
        self.username = ""
        self.offset: int | None = None
        self.request_slots = asyncio.Semaphore(3)
        self.cooldowns: dict[int, float] = {}
        self.conversations: dict[tuple[int, int], deque[ConversationTurn]] = {}
        self.conversation_locks: dict[tuple[int, int], asyncio.Lock] = {}
        self.update_tasks: set[asyncio.Task[None]] = set()

    @property
    def api_url(self) -> str:
        return f"{TELEGRAM_API_ROOT}{self.settings.token}"

    async def _call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        if self.session is None:
            raise TelegramAPIError(0, "Telegram client is not started")
        try:
            async with self.session.post(
                f"{self.api_url}/{method}", json=payload or {}
            ) as response:
                data = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError):
            # Do not chain aiohttp's exception: its URL contains the secret bot token.
            raise TelegramAPIError(503, "Telegram request failed") from None

        if not isinstance(data, dict) or not data.get("ok"):
            status = int(data.get("error_code", response.status)) if isinstance(data, dict) else 502
            description = (
                str(data.get("description", "Telegram returned an error"))
                if isinstance(data, dict)
                else "Telegram returned an invalid response"
            )
            raise TelegramAPIError(status, description)
        return data.get("result")

    async def start(self) -> None:
        if self.session is not None:
            return
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40))
        try:
            await self.openai.start()
            profile = await self._call("getMe")
            if (
                not isinstance(profile, dict)
                or not profile.get("id")
                or not profile.get("username")
            ):
                raise TelegramAPIError(502, "Telegram returned an incomplete bot profile")
            self.bot_id = int(profile["id"])
            self.username = str(profile["username"])
            await self._call("deleteWebhook", {"drop_pending_updates": False})
            await self._call(
                "setMyCommands",
                {
                    "commands": [
                        {"command": "start", "description": "Start chatting with Kevin"},
                        {"command": "help", "description": "Show how Kevin works"},
                        {"command": "reset", "description": "Clear recent chat context"},
                        {"command": "id", "description": "Show your Telegram user ID"},
                    ]
                },
            )
        except BaseException:
            await self.close()
            raise
        log.info("Telegram Kevin is ready as @%s (%s)", self.username, self.bot_id)

    async def close(self) -> None:
        await self.openai.close()
        if self.session is not None:
            await self.session.close()
            self.session = None

    def _command(self, text: str) -> str | None:
        first = text.split(maxsplit=1)[0] if text else ""
        if not first.startswith("/"):
            return None
        command, _, target = first[1:].partition("@")
        if target and target.casefold() != self.username.casefold():
            return ""
        return command.casefold()

    def _allowed(self, user_id: int) -> bool:
        allowed = self.settings.allowed_user_ids or set()
        return not allowed or user_id in allowed

    async def _send_message(self, message: dict[str, Any], text: str) -> None:
        chat = message.get("chat", {})
        payload: dict[str, Any] = {
            "chat_id": chat["id"],
            "text": text,
            "link_preview_options": {"is_disabled": True},
            "reply_parameters": {
                "message_id": message["message_id"],
                "allow_sending_without_reply": True,
            },
        }
        if message.get("message_thread_id") is not None:
            payload["message_thread_id"] = message["message_thread_id"]
        await self._call("sendMessage", payload)

    async def _typing(self, message: dict[str, Any]) -> None:
        chat = message.get("chat", {})
        payload: dict[str, Any] = {"chat_id": chat["id"], "action": "typing"}
        if message.get("message_thread_id") is not None:
            payload["message_thread_id"] = message["message_thread_id"]
        while True:
            try:
                await self._call("sendChatAction", payload)
            except TelegramAPIError:
                log.debug("Could not refresh Telegram typing action", exc_info=True)
                return
            await asyncio.sleep(4)

    def _retry_after(self, user_id: int) -> float:
        now = monotonic()
        previous = self.cooldowns.get(user_id, 0.0)
        retry_after = max(0.0, 5.0 - (now - previous))
        if retry_after == 0:
            self.cooldowns[user_id] = now
        if len(self.cooldowns) > 1_000:
            self.cooldowns = {
                key: value for key, value in self.cooldowns.items() if now - value < 3_600
            }
        return retry_after

    async def _answer(
        self, message: dict[str, Any], question: str, previous_reply: str | None
    ) -> None:
        author = message["from"]
        chat = message["chat"]
        user_id = int(author["id"])
        key = (int(chat["id"]), user_id)
        lock = self.conversation_locks.setdefault(key, asyncio.Lock())

        async with lock:
            retry_after = self._retry_after(user_id)
            if retry_after:
                await self._send_message(message, f"Give me {retry_after:.0f}s and ask again.")
                return

            history = self.conversations.get(key, deque())
            prompt = with_conversation_context(question, history, previous_reply)
            search_required = requires_web_search(question)

            typing_task = asyncio.create_task(self._typing(message))
            try:
                async with self.request_slots:
                    # The Responses API may skip an optional tool even when the user
                    # explicitly asks for it. Guarantee search for explicit, current,
                    # or URL-based questions; leave ordinary chat in automatic mode.
                    text, sources = await self.openai.ask(
                        prompt,
                        require_web_search=search_required,
                        require_source_links=True,
                    )
            except OpenAIAPIError as exc:
                log.warning("OpenAI request failed (%s): %s", exc.status, exc)
                if exc.status == 401:
                    reply = "My OpenAI key isn't working—someone needs to check it."
                elif exc.status == 429:
                    reply = "OpenAI's rate limit is busy right now—try me again in a bit."
                else:
                    reply = "I couldn't reach OpenAI just now—try me again in a minute."
                await self._send_message(message, reply)
                return
            finally:
                typing_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await typing_task

            history = self.conversations.setdefault(key, deque(maxlen=MAX_CONVERSATION_TURNS))
            history.append(ConversationTurn(question=question, reply=text))
            await self._send_message(message, format_telegram_reply(text, sources))

    async def process_update(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return
        author = message.get("from")
        chat = message.get("chat")
        if not isinstance(author, dict) or not isinstance(chat, dict) or author.get("is_bot"):
            return
        if "id" not in author or "id" not in chat:
            return

        user_id = int(author["id"])
        text = str(message.get("text") or message.get("caption") or "")
        command = self._command(text)
        if command == "":
            return

        if command == "id":
            await self._send_message(message, f"Your Telegram user ID is {user_id}.")
            return

        if not self._allowed(user_id):
            if chat.get("type") == "private" or command in {"start", "help"}:
                await self._send_message(message, "Sorry, this Kevin bot is private.")
            return

        if command in {"start", "help"}:
            await self._send_message(message, HELP_TEXT)
            return
        if command == "reset":
            self.conversations.pop((int(chat["id"]), user_id), None)
            await self._send_message(message, "Fresh start—I've cleared our recent context.")
            return
        if command is not None:
            await self._send_message(
                message, "I don't know that command. Use /help to get started."
            )
            return

        question, previous_reply = telegram_question(
            message, bot_id=self.bot_id, username=self.username
        )
        if question is None:
            return
        if not question:
            await self._send_message(message, "Yeah? Send me a question and I'll look into it.")
            return
        await self._answer(message, question, previous_reply)

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self.update_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except TelegramAPIError as exc:
            log.warning("Telegram update failed (%s): %s", exc.status, exc)
        except Exception:
            log.exception("Unexpected Telegram update failure")

    async def poll_forever(self) -> None:
        while True:
            try:
                payload: dict[str, Any] = {
                    "limit": 100,
                    "timeout": 30,
                    "allowed_updates": ["message"],
                }
                if self.offset is not None:
                    payload["offset"] = self.offset
                updates = await self._call(
                    "getUpdates",
                    payload,
                )
            except TelegramAPIError as exc:
                if exc.status in {401, 409}:
                    raise
                log.warning("Telegram polling failed (%s): %s", exc.status, exc)
                await asyncio.sleep(2)
                continue

            if not isinstance(updates, list):
                raise TelegramAPIError(502, "Telegram returned invalid updates")
            for update in updates:
                if not isinstance(update, dict) or "update_id" not in update:
                    continue
                self.offset = int(update["update_id"]) + 1
                task = asyncio.create_task(self.process_update(update))
                self.update_tasks.add(task)
                task.add_done_callback(self._task_done)

    async def run(self) -> None:
        await self.start()
        try:
            await self.poll_forever()
        finally:
            for task in self.update_tasks:
                task.cancel()
            if self.update_tasks:
                await asyncio.gather(*self.update_tasks, return_exceptions=True)
            await self.close()
