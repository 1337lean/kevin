from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import re
from dataclasses import replace
from pathlib import Path
from typing import Literal

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from kevin.bot import KevinBot
from kevin.market_data import (
    MarketDataError,
    live_price_reply,
    requested_price_lookup,
)
from kevin.memory import Mem0MemoryStore, Mem0StoreError
from kevin.openai_chat import (
    MemberMemory,
    OpenAIAPIError,
    OpenAIChatClient,
    ServerMessage,
    Source,
    requires_web_search,
    safe_memory_note,
    with_discord_context,
    with_reply_context,
)
from kevin.openai_chat import extract_response as extract_response
from kevin.openai_images import OpenAIImageClient
from kevin.utils.checks import owner_or_guild_permissions

log = logging.getLogger(__name__)

MAX_DISCORD_LENGTH = 2_000
MAX_SOURCE_LINE_LENGTH = 900
MAX_STORED_MESSAGES_PER_CHANNEL = 200
MEMORY_REFRESH_MESSAGES = 24
MAX_MEMORY_PROFILES_PER_PROMPT = 4

_PRIVATE_CONTENT_RE = re.compile(
    r"(?:\b(?:password|passcode|api[ _-]?key|private[ _-]?key|seed phrase|"
    r"recovery phrase|credit card|social security|ssn)\b|"
    r"\b(?:token|secret)\s*[:=]|[\w.+-]+@[\w.-]+\.\w{2,}|\b\d{12,19}\b|"
    r"\bmfa\.[\w-]{20,}|\beyJ[\w-]{10,}\.[\w.-]{10,})",
    re.IGNORECASE,
)
_MEMORY_CANDIDATE_RE = re.compile(
    r"\b(?:i|i'm|i’m|im|i've|i’ve|ive|my|mine|me|call me)\b", re.IGNORECASE
)
_REPLY_CONTEXT_RE = re.compile(
    r"\b(?:repl(?:y|ied|ies|ying)|respond(?:ed|ing)?|what\s+message)\b",
    re.IGNORECASE,
)


def mentioned_question(content: str, bot_user_id: int) -> str | None:
    """Return the text from a direct bot mention, or None when K was not pinged."""
    mentions = (f"<@{bot_user_id}>", f"<@!{bot_user_id}>")
    if not any(mention in content for mention in mentions):
        return None
    question = content
    for mention in mentions:
        question = question.replace(mention, " ")
    return " ".join(question.split())


def format_reply(text: str, sources: list[Source]) -> str:
    """Fit the answer and clickable sources into Discord's message limit."""
    source_line = ""
    if sources:
        links: list[str] = []
        for source in sources:
            link = f"[{source.title}]({source.url})"
            candidate = " · ".join([*links, link])
            if len(f"\n\nSources: {candidate}") > MAX_SOURCE_LINE_LENGTH:
                continue
            links.append(link)
        if links:
            source_line = f"\n\nSources: {' · '.join(links)}"

    answer_limit = MAX_DISCORD_LENGTH - len(source_line)
    if answer_limit < 1:
        source_line = ""
        answer_limit = MAX_DISCORD_LENGTH
    if len(text) > answer_limit:
        text = text[: max(1, answer_limit - 1)].rstrip() + "…"
    return text + source_line


class AI(commands.Cog):
    """Short OpenAI replies when someone mentions K or replies to K."""

    def __init__(self, bot: KevinBot) -> None:
        self.bot = bot
        self.openai = OpenAIChatClient(
            bot.settings.openai_api_key or "",
            getattr(bot.settings, "openai_model", "gpt-5.6-luna"),
        )
        self.images = OpenAIImageClient(
            bot.settings.openai_api_key or "",
            getattr(bot.settings, "openai_image_model", "gpt-image-1"),
        )
        self.memories = Mem0MemoryStore(
            bot.settings.openai_api_key or "",
            getattr(bot.settings, "mem0_path", Path("data/mem0")),
            getattr(bot.settings, "mem0_llm_model", "gpt-4.1-mini"),
            getattr(
                bot.settings,
                "mem0_embedding_model",
                "text-embedding-3-small",
            ),
        )
        self.request_slots = asyncio.Semaphore(3)
        self.memory_slots = asyncio.Semaphore(1)
        self.memory_search_slots = asyncio.Semaphore(3)
        self.image_slots = asyncio.Semaphore(2)
        self.http: aiohttp.ClientSession | None = None
        self.memory_opt_outs: set[tuple[int, int]] = set()
        self.memory_enabled_cache: dict[int, bool] = {}
        self.memory_tasks: set[asyncio.Task[None]] = set()

    async def cog_load(self) -> None:
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        if getattr(self.bot, "db", None) is not None and self.bot.db.connection is not None:
            self.memory_opt_outs = await self.bot.db.get_ai_memory_opt_outs()
        if self.bot.settings.openai_api_key:
            await self.openai.start()
            await self.images.start()
            try:
                await self.memories.start()
            except Mem0StoreError:
                log.exception(
                    "Mem0 initialization failed; recent context remains available but "
                    "durable memory will be unavailable until restart"
                )

    async def cog_unload(self) -> None:
        tasks = list(self.memory_tasks)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.memory_tasks.clear()
        await self.memories.close()
        await self.openai.close()
        await self.images.close()
        if self.http is not None:
            await self.http.close()
            self.http = None

    async def _ask_openai(
        self,
        question: str,
        previous_reply: str | None = None,
        *,
        prompt: str | None = None,
    ) -> tuple[str, list[Source]]:
        """Answer from a live price API when asked for a quote, else from OpenAI.

        Prices come from Coinbase and Yahoo Finance because hosted web search reads
        crawled snapshots, which are stale for anything that ticks.
        """
        price_request = requested_price_lookup(question)
        if price_request is not None and self.http is not None:
            quoted = await live_price_reply(self.http, price_request)
            if quoted is not None:
                return quoted

        return await self.openai.ask(
            prompt or with_reply_context(question, previous_reply),
            require_web_search=requires_web_search(question),
            require_source_links=True,
            reject_native_feeds=True,
        )

    @staticmethod
    def _display_name(author: object) -> str:
        return str(
            getattr(author, "display_name", None)
            or getattr(author, "name", None)
            or getattr(author, "id", "member")
        )

    @staticmethod
    def _observable_content(content: str) -> str | None:
        clean = " ".join(content.split())
        if len(clean) < 2 or _PRIVATE_CONTENT_RE.search(clean):
            return None
        return clean[:1_000]

    def _has_memory_database(self) -> bool:
        database = getattr(self.bot, "db", None)
        return database is not None and database.connection is not None

    def _can_observe_channel(self, message: discord.Message) -> bool:
        if message.guild is None or self.bot.user is None:
            return False
        bot_member = getattr(message.guild, "me", None)
        if bot_member is None:
            get_member = getattr(message.guild, "get_member", None)
            if get_member is not None:
                bot_member = get_member(self.bot.user.id)
        if bot_member is None:
            return False
        try:
            return bool(message.channel.permissions_for(bot_member).view_channel)
        except AttributeError:
            return False

    async def _memory_enabled(self, guild_id: int) -> bool:
        if not self._has_memory_database():
            return False
        if guild_id not in self.memory_enabled_cache:
            self.memory_enabled_cache[guild_id] = await self.bot.db.ai_memory_enabled(guild_id)
        return self.memory_enabled_cache[guild_id]

    async def _record_observation(self, message: discord.Message) -> bool:
        if (
            not self._has_memory_database()
            or message.guild is None
            or not self._can_observe_channel(message)
        ):
            return False
        guild_id = message.guild.id
        user_id = message.author.id
        if not await self._memory_enabled(guild_id):
            return False
        if (guild_id, user_id) in self.memory_opt_outs:
            return False
        content = self._observable_content(message.content)
        if content is None:
            return False
        await self.bot.db.record_ai_chat_message(
            guild_id,
            message.channel.id,
            message.id,
            user_id,
            self._display_name(message.author),
            content,
            message.created_at.isoformat(),
            channel_limit=MAX_STORED_MESSAGES_PER_CHANNEL,
            reply_to_message_id=self._reply_to_message_id(message),
        )
        return _MEMORY_CANDIDATE_RE.search(content) is not None

    async def _record_bot_response(
        self, message: discord.Message, content: str
    ) -> None:
        if (
            not self._has_memory_database()
            or message.guild is None
            or self.bot.user is None
            or not self._can_observe_channel(message)
            or not await self._memory_enabled(message.guild.id)
        ):
            return
        await self.bot.db.record_ai_chat_message(
            message.guild.id,
            message.channel.id,
            message.id,
            self.bot.user.id,
            self._display_name(self.bot.user),
            content[:1_000],
            message.created_at.isoformat(),
            channel_limit=MAX_STORED_MESSAGES_PER_CHANNEL,
            reply_to_message_id=self._reply_to_message_id(message),
        )

    @staticmethod
    def _reply_to_message_id(message: discord.Message) -> int | None:
        reference = getattr(message, "reference", None)
        message_id = getattr(reference, "message_id", None)
        return int(message_id) if message_id is not None else None

    def _server_message(self, row: dict[str, object]) -> ServerMessage:
        bot_id = self.bot.user.id if self.bot.user is not None else 0
        reply_to_message_id = row.get("reply_to_message_id")
        reply_to_user_id = row.get("reply_to_user_id")
        reply_to_display_name = row.get("reply_to_display_name")
        reply_to_content = row.get("reply_to_content")
        return ServerMessage(
            message_id=int(row["message_id"]),
            user_id=int(row["user_id"]),
            display_name=str(row["display_name"]),
            content=str(row["content"]),
            is_bot=int(row["user_id"]) == bot_id,
            reply_to_message_id=(
                int(reply_to_message_id) if reply_to_message_id is not None else None
            ),
            reply_to_user_id=(
                int(reply_to_user_id) if reply_to_user_id is not None else None
            ),
            reply_to_display_name=(
                str(reply_to_display_name)
                if reply_to_display_name is not None
                else None
            ),
            reply_to_content=(
                str(reply_to_content) if reply_to_content is not None else None
            ),
            reply_to_is_bot=(
                int(reply_to_user_id) == bot_id
                if reply_to_user_id is not None
                else False
            ),
        )

    async def _channel_message(
        self, channel: discord.abc.Messageable, message_id: int
    ) -> object | None:
        get_message = getattr(self.bot, "get_message", None)
        cached = get_message(message_id) if get_message is not None else None
        if cached is not None and getattr(getattr(cached, "channel", None), "id", None) == getattr(
            channel, "id", None
        ):
            return cached
        try:
            return await channel.fetch_message(message_id)
        except (discord.HTTPException, AttributeError):
            return None

    async def _enrich_reply_context(
        self,
        message: discord.Message,
        question: str,
        previous_reply: str | None,
        recent: list[ServerMessage],
    ) -> list[ServerMessage]:
        """Recover one relevant Discord reply edge missing from older stored rows."""
        if not recent or not _REPLY_CONTEXT_RE.search(question):
            return recent

        reference_text = f"{question}\n{previous_reply or ''}"
        named = [
            item
            for item in recent
            if self._name_is_referenced(reference_text, item.display_name)
        ]
        candidates = list(reversed(named or recent[-4:]))
        replacement: ServerMessage | None = None

        for item in candidates[:4]:
            if item.reply_to_content is not None:
                break

            reply_to_message_id = item.reply_to_message_id
            referenced: object | None = None
            if reply_to_message_id is None:
                source = await self._channel_message(message.channel, item.message_id)
                if source is None:
                    continue
                reply_to_message_id = self._reply_to_message_id(source)
                if reply_to_message_id is None:
                    continue
                reference = getattr(source, "reference", None)
                referenced = getattr(reference, "resolved", None)
                if referenced is None:
                    referenced = getattr(reference, "cached_message", None)

            if referenced is None:
                referenced = await self._channel_message(
                    message.channel, reply_to_message_id
                )

            reply_to_user_id: int | None = None
            reply_to_display_name: str | None = None
            reply_to_content: str | None = None
            reply_to_is_bot = False
            author = getattr(referenced, "author", None)
            raw_user_id = getattr(author, "id", None)
            if raw_user_id is not None:
                reply_to_user_id = int(raw_user_id)
                guild_id = int(getattr(message.guild, "id", 0))
                if (guild_id, reply_to_user_id) not in self.memory_opt_outs:
                    reply_to_display_name = self._display_name(author)
                    raw_content = getattr(referenced, "content", None)
                    if isinstance(raw_content, str):
                        reply_to_content = self._observable_content(raw_content)
                    reply_to_is_bot = bool(
                        self.bot.user is not None
                        and reply_to_user_id == self.bot.user.id
                    )

            replacement = replace(
                item,
                reply_to_message_id=reply_to_message_id,
                reply_to_user_id=reply_to_user_id,
                reply_to_display_name=reply_to_display_name,
                reply_to_content=reply_to_content,
                reply_to_is_bot=reply_to_is_bot,
            )
            break

        if replacement is None:
            return recent
        return [
            replacement if item.message_id == replacement.message_id else item
            for item in recent
        ]

    @staticmethod
    def _name_is_referenced(question: str, display_name: str) -> bool:
        name = " ".join(display_name.casefold().split()).strip("@")
        if len(name) < 2:
            return False
        return re.search(rf"(?<!\w){re.escape(name)}(?!\w)", question.casefold()) is not None

    async def _search_memory_profile(
        self,
        guild_id: int,
        user_id: int,
        display_name: str,
        question: str,
    ) -> MemberMemory | None:
        try:
            async with self.memory_search_slots:
                raw_notes = await self.memories.search_member(
                    guild_id, user_id, question, limit=6
                )
        except Mem0StoreError as exc:
            log.warning("Mem0 retrieval failed: %s", exc)
            return None

        notes: list[str] = []
        seen: set[str] = set()
        for raw_note in raw_notes:
            note = safe_memory_note(raw_note)
            if note is None or note.casefold() in seen:
                continue
            notes.append(note)
            seen.add(note.casefold())
        if not notes:
            return None
        return MemberMemory(user_id, display_name, tuple(notes))

    async def _discord_prompt(
        self,
        message: discord.Message,
        question: str,
        previous_reply: str | None,
    ) -> str:
        speaker_id = message.author.id
        speaker_name = self._display_name(message.author)
        recent: list[ServerMessage] = []
        profiles: list[MemberMemory] = []
        guild_id = int(getattr(message.guild, "id", 0))
        if guild_id and self._can_observe_channel(message) and await self._memory_enabled(guild_id):
            rows = await self.bot.db.get_ai_chat_messages(
                guild_id,
                message.channel.id,
                limit=MEMORY_REFRESH_MESSAGES,
                exclude_message_id=message.id,
            )
            recent = [
                self._server_message(row)
                for row in rows
                if (guild_id, int(row["user_id"])) not in self.memory_opt_outs
            ]
            recent = await self._enrich_reply_context(
                message, question, previous_reply, recent
            )
            if self.memories.ready:
                identity_rows = await self.bot.db.get_ai_memory_members(guild_id)
                identity_names = {
                    int(row["user_id"]): str(row["display_name"])
                    for row in identity_rows
                }
                identity_names[speaker_id] = speaker_name
                target_ids = [speaker_id]
                target_ids.extend(
                    int(match) for match in re.findall(r"<@!?(\d+)>", question)
                )
                target_ids.extend(
                    user_id
                    for user_id, display_name in identity_names.items()
                    if self._name_is_referenced(question, display_name)
                )
                target_ids = list(dict.fromkeys(target_ids))[
                    :MAX_MEMORY_PROFILES_PER_PROMPT
                ]

                profile_requests = []
                for user_id in target_ids:
                    if (guild_id, user_id) in self.memory_opt_outs:
                        continue
                    display_name = identity_names.get(user_id)
                    if display_name is None:
                        get_member = getattr(message.guild, "get_member", None)
                        member = get_member(user_id) if get_member is not None else None
                        display_name = (
                            self._display_name(member) if member is not None else str(user_id)
                        )
                    profile_requests.append(
                        self._search_memory_profile(
                            guild_id, user_id, display_name, question
                        )
                    )
                if profile_requests:
                    profiles = [
                        profile
                        for profile in await asyncio.gather(*profile_requests)
                        if profile is not None
                    ]
        return with_discord_context(
            question,
            speaker_id=speaker_id,
            speaker_name=speaker_name,
            recent_messages=recent,
            member_memories=profiles,
            previous_reply=previous_reply,
        )

    async def _refresh_memories(self, guild_id: int, channel_id: int) -> None:
        if (
            not self.bot.settings.openai_api_key
            or not self.memories.ready
            or not await self._memory_enabled(guild_id)
        ):
            return
        async with self.memory_slots:
            # Erasure commands use this same lock. Recheck state after acquiring it so
            # a queued refresh cannot recreate data that was just forgotten.
            if not self.memories.ready or not await self._memory_enabled(guild_id):
                return
            rows = await self.bot.db.get_ai_chat_messages(
                guild_id, channel_id, limit=MEMORY_REFRESH_MESSAGES
            )
            watermark = await self.bot.db.get_ai_memory_watermark(guild_id, channel_id)
            new_rows = [row for row in rows if int(row["message_id"]) > watermark]
            if not new_rows:
                return
            latest_message_id = max(int(row["message_id"]) for row in new_rows)
            messages = [
                self._server_message(row)
                for row in new_rows
                if (guild_id, int(row["user_id"])) not in self.memory_opt_outs
            ]
            candidate_ids = list(
                dict.fromkeys(
                    message.user_id
                    for message in reversed(messages)
                    if not message.is_bot and _MEMORY_CANDIDATE_RE.search(message.content)
                )
            )[:8]
            if not candidate_ids:
                await self.bot.db.set_ai_memory_watermark(
                    guild_id, channel_id, latest_message_id
                )
                return

            latest_names = {
                message.user_id: message.display_name
                for message in messages
                if message.user_id in candidate_ids
            }
            try:
                for user_id in candidate_ids:
                    if (guild_id, user_id) in self.memory_opt_outs:
                        continue
                    await self.memories.add_member_messages(
                        guild_id,
                        user_id,
                        latest_names.get(user_id, str(user_id)),
                        (
                            item.content
                            for item in messages
                            if item.user_id == user_id
                            and not item.is_bot
                            and _MEMORY_CANDIDATE_RE.search(item.content)
                        ),
                    )
            except Mem0StoreError as exc:
                log.warning("Mem0 refresh failed: %s", exc)
                return
            await self.bot.db.set_ai_memory_watermark(
                guild_id, channel_id, latest_message_id
            )

    def _schedule_memory_refresh(self, guild_id: int, channel_id: int) -> None:
        if not self._has_memory_database() or not self.memories.ready:
            return
        task = asyncio.create_task(self._run_memory_refresh(guild_id, channel_id))
        self.memory_tasks.add(task)
        task.add_done_callback(self.memory_tasks.discard)

    async def _run_memory_refresh(self, guild_id: int, channel_id: int) -> None:
        try:
            await self._refresh_memories(guild_id, channel_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Unexpected Discord memory refresh failure")

    async def _bot_reply_context(self, message: discord.Message) -> str | None:
        """Return K's referenced message text, or None when this is not a reply to K."""
        reference = message.reference
        if reference is None or reference.message_id is None:
            return None

        referenced = reference.resolved
        if not isinstance(referenced, discord.Message):
            referenced = reference.cached_message
        if referenced is None and reference.channel_id == message.channel.id:
            try:
                referenced = await message.channel.fetch_message(reference.message_id)
            except (discord.HTTPException, AttributeError):
                return None

        if not isinstance(referenced, discord.Message) or self.bot.user is None:
            return None
        if referenced.author.id != self.bot.user.id:
            return None
        return referenced.content.strip()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or self.bot.user is None:
            return
        if message.author.id == self.bot.user.id or getattr(message.author, "bot", False):
            return

        memory_candidate = await self._record_observation(message)
        if memory_candidate:
            # Self-disclosures should become durable even when the member is merely
            # chatting and does not ping K. The watermark and memory lock make repeated
            # refresh tasks cheap no-ops once the observation has been processed.
            self._schedule_memory_refresh(message.guild.id, message.channel.id)

        question = mentioned_question(message.content, self.bot.user.id)
        previous_reply = await self._bot_reply_context(message)
        if question is None and previous_reply is None:
            return
        if question is None:
            question = " ".join(message.content.split())
        if not question:
            await message.reply(
                "yeah? ping me or reply with a question and I'll look into it.",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if not self.bot.settings.openai_api_key:
            await message.reply(
                "my OpenAI key isn't set up yet—add `OPENAI_API_KEY` and restart me.",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        prompt = await self._discord_prompt(message, question, previous_reply)
        async with self.request_slots, message.channel.typing():
            try:
                text, sources = await self._ask_openai(
                    question, previous_reply, prompt=prompt
                )
            except MarketDataError as exc:
                log.warning("Verified market-data request failed: %s", exc)
                await message.reply(
                    "I couldn't get a verified live price just now—try again shortly.",
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            except OpenAIAPIError as exc:
                log.warning("OpenAI request failed (%s): %s", exc.status, exc)
                if exc.status == 401:
                    reply = "my OpenAI key isn't working—someone needs to check it."
                elif exc.status == 429:
                    reply = "OpenAI's rate limit is busy right now—try me again in a bit."
                else:
                    reply = "I couldn't reach OpenAI just now—try me again in a minute."
                await message.reply(
                    reply,
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

        reply_content = format_reply(text, sources)
        sent_message = await message.reply(
            reply_content,
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
            suppress_embeds=True,
        )
        if self._has_memory_database() and self._can_observe_channel(message):
            if isinstance(sent_message, discord.Message):
                await self._record_bot_response(sent_message, reply_content)
            self._schedule_memory_refresh(message.guild.id, message.channel.id)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if self._has_memory_database():
            await self.bot.db.delete_ai_chat_message(payload.message_id)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(
        self, payload: discord.RawBulkMessageDeleteEvent
    ) -> None:
        if self._has_memory_database():
            await self.bot.db.delete_ai_chat_messages(payload.message_ids)

    async def _send_memory_message(
        self, ctx: commands.Context, content: str, *, private: bool = False
    ) -> None:
        if ctx.interaction is not None:
            await ctx.send(content, ephemeral=True)
            return
        if private:
            try:
                await ctx.author.send(content)
            except discord.HTTPException:
                await ctx.send("I couldn't DM your memory to you. Try the slash command instead.")
            return
        await ctx.send(content)

    @commands.hybrid_group(
        name="memory",
        fallback="show",
        description="See or control what K remembers about you",
        invoke_without_command=True,
    )
    @commands.guild_only()
    async def memory(self, ctx: commands.Context) -> None:
        guild_id = ctx.guild.id
        key = (guild_id, ctx.author.id)
        if not await self._memory_enabled(guild_id):
            await self._send_memory_message(ctx, "AI memory is disabled in this server.")
            return
        if key in self.memory_opt_outs:
            await self._send_memory_message(
                ctx, "Memory is off for you in this server. Use `/memory on` to opt back in."
            )
            return
        if not self.memories.ready:
            await self._send_memory_message(
                ctx,
                "Durable Mem0 memory is unavailable right now. Ask an admin to check the bot "
                "logs and restart me.",
                private=True,
            )
            return
        # A member can ask immediately after making a self-disclosure. Wait for any
        # scheduled refresh (or process the pending observation ourselves) before
        # listing Mem0 so `kmemory show` cannot race the durable write.
        await self._run_memory_refresh(guild_id, ctx.channel.id)
        try:
            raw_notes = await self.memories.list_member(guild_id, ctx.author.id)
        except Mem0StoreError as exc:
            log.warning("Mem0 list failed: %s", exc)
            await self._send_memory_message(
                ctx,
                "I couldn't read your durable memory right now. Try again shortly.",
                private=True,
            )
            return
        notes = [note for raw_note in raw_notes if (note := safe_memory_note(raw_note))]
        if not notes:
            await self._send_memory_message(
                ctx,
                "I don't have any durable notes about you yet. I may still use a small amount "
                "of recent same-channel context when you talk to me.",
                private=True,
            )
            return
        heading = "Here’s what I remember about you in this server:\n"
        lines: list[str] = []
        for note in notes:
            line = f"• {note}"
            if len(heading) + len("\n".join([*lines, line])) > MAX_DISCORD_LENGTH:
                lines.append("…")
                break
            lines.append(line)
        await self._send_memory_message(
            ctx, heading + "\n".join(lines), private=True
        )

    @memory.command(name="forget", description="Erase your notes and recent chat observations")
    async def memory_forget(self, ctx: commands.Context) -> None:
        erased = True
        async with self.memory_slots:
            await self.bot.db.forget_ai_user(ctx.guild.id, ctx.author.id)
            try:
                await self.memories.forget_member(ctx.guild.id, ctx.author.id)
            except Mem0StoreError as exc:
                erased = False
                log.warning("Mem0 member erase failed: %s", exc)
        if not erased:
            await self._send_memory_message(
                ctx,
                "I erased your recent observations, but the durable Mem0 erase failed. "
                "Please retry after an admin restarts me.",
            )
            return
        await self._send_memory_message(
            ctx,
            "Done—I erased your notes and recent chat observations in this server. "
            "Memory stays on for future messages.",
        )

    @memory.command(name="off", description="Opt out and erase your AI memory")
    async def memory_off(self, ctx: commands.Context) -> None:
        key = (ctx.guild.id, ctx.author.id)
        erased = True
        async with self.memory_slots:
            await self.bot.db.set_ai_memory_opt_out(*key, opted_out=True)
            self.memory_opt_outs.add(key)
            try:
                await self.memories.forget_member(*key)
            except Mem0StoreError as exc:
                erased = False
                log.warning("Mem0 opt-out erase failed: %s", exc)
        if not erased:
            await self._send_memory_message(
                ctx,
                "Memory is off and I won't use or add data for you. The durable Mem0 erase "
                "failed, though, so please retry `/memory off` after an admin restarts me.",
            )
            return
        await self._send_memory_message(
            ctx,
            "Memory is off for you here, and I erased your saved notes and observations. "
            "I’ll still know you’re the person asking when you directly talk to me.",
        )

    @memory.command(name="on", description="Opt back into personalized AI memory")
    async def memory_on(self, ctx: commands.Context) -> None:
        if not self.memories.ready:
            await self._send_memory_message(
                ctx,
                "I can't turn durable memory on while Mem0 is unavailable. Ask an admin to "
                "check the bot logs and restart me.",
            )
            return
        key = (ctx.guild.id, ctx.author.id)
        async with self.memory_slots:
            await self.bot.db.set_ai_memory_opt_out(*key, opted_out=False)
            self.memory_opt_outs.discard(key)
        await self._send_memory_message(
            ctx, "Memory is on for you here. I’ll start fresh from future public messages."
        )

    @memory.command(name="server", description="Enable or disable AI memory for this server")
    @app_commands.describe(enabled="Whether K may remember chat in channels it can view")
    @owner_or_guild_permissions(manage_guild=True)
    async def memory_server(self, ctx: commands.Context, enabled: bool) -> None:
        guild_id = ctx.guild.id
        if enabled and not self.memories.ready:
            await self._send_memory_message(
                ctx,
                "I can't enable durable memory while Mem0 is unavailable. Check the bot logs "
                "and restart me first.",
            )
            return
        if enabled:
            async with self.memory_slots:
                await self.bot.db.set_ai_memory_enabled(guild_id, True)
                self.memory_enabled_cache[guild_id] = True
            await self._send_memory_message(ctx, "AI memory is now enabled.")
            return
        erased = True
        async with self.memory_slots:
            await self.bot.db.set_ai_memory_enabled(guild_id, False)
            self.memory_enabled_cache[guild_id] = False
            try:
                await self.memories.forget_guild(guild_id)
            except Mem0StoreError as exc:
                erased = False
                log.warning("Mem0 server erase failed: %s", exc)
        detail = (
            "disabled and all server AI memory was erased"
            if erased
            else "disabled, but the durable Mem0 erase failed; retry after restarting me"
        )
        await self._send_memory_message(ctx, f"AI memory is now {detail}.")

    @commands.hybrid_command(description="Generate an image with OpenAI")
    @commands.is_owner()
    @app_commands.describe(
        prompt="What the image should look like",
        size="Image dimensions (default: square)",
        quality="Image quality (default: high)",
    )
    async def imagine(
        self,
        ctx: commands.Context,
        *,
        prompt: str,
        size: Literal["1024x1024", "1536x1024", "1024x1536"] = "1024x1024",
        quality: Literal["low", "medium", "high"] = "high",
    ) -> None:
        if not self.bot.settings.openai_api_key:
            await ctx.reply(
                "my OpenAI key isn't set up yet—add `OPENAI_API_KEY` and restart me.",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if not prompt.strip():
            await ctx.reply(
                "tell me what to draw first.",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        await ctx.defer()
        short_prompt = prompt.strip()[:200]
        status = await ctx.reply(
            f"drawing “{short_prompt}”… this can take a minute.",
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

        # ctx.typing() keeps the "K is typing…" indicator alive for prefix
        # invocations; defer alone only pings typing once and it fades in ~10s.
        try:
            async with self.image_slots, ctx.typing():
                image_bytes = await self.images.generate(
                    prompt.strip(), size=size, quality=quality
                )
        except OpenAIAPIError as exc:
            log.warning("OpenAI image request failed (%s): %s", exc.status, exc)
            if exc.status == 401:
                reply = "my OpenAI key isn't working—someone needs to check it."
            elif exc.status == 429:
                reply = "OpenAI's rate limit is busy right now—try me again in a bit."
            elif exc.status == 400:
                detail = " ".join(str(exc).split())[:200]
                reply = f"OpenAI refused that one: {detail}" if detail else (
                    "OpenAI wouldn't draw that one—try a different prompt."
                )
            else:
                reply = "I couldn't reach OpenAI just now—try me again in a minute."
            await status.edit(content=reply)
            return

        file = discord.File(io.BytesIO(image_bytes), filename="kevin-image.png")
        await status.edit(
            content=f"here you go — “{short_prompt}”",
            attachments=[file],
        )


async def setup(bot: KevinBot) -> None:
    await bot.add_cog(AI(bot))
