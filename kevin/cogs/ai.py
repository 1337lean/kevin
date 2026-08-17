from __future__ import annotations

import asyncio
import io
import logging
import time
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands

from kevin.bot import KevinBot
from kevin.openai_chat import (
    OpenAIAPIError,
    OpenAIChatClient,
    Source,
    with_reply_context,
)
from kevin.openai_chat import extract_response as extract_response
from kevin.openai_images import OpenAIImageClient

log = logging.getLogger(__name__)

MAX_DISCORD_LENGTH = 2_000
MAX_SOURCE_LINE_LENGTH = 900
PROGRESS_BAR_WIDTH = 12
# The Images API reports no progress, so the bar estimates from elapsed time.
ESTIMATED_IMAGE_SECONDS = 60


def image_progress(elapsed: float) -> str:
    """Render an elapsed-time progress bar for an in-flight image generation."""
    ratio = min(elapsed / ESTIMATED_IMAGE_SECONDS, 0.95)
    filled = round(ratio * PROGRESS_BAR_WIDTH)
    bar = "█" * filled + "░" * (PROGRESS_BAR_WIDTH - filled)
    return f"{bar} {int(elapsed)}s"


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
        self.request_slots = asyncio.Semaphore(3)
        self.image_slots = asyncio.Semaphore(2)

    async def cog_load(self) -> None:
        if self.bot.settings.openai_api_key:
            await self.openai.start()
            await self.images.start()

    async def cog_unload(self) -> None:
        await self.openai.close()
        await self.images.close()

    async def _ask_openai(self, question: str) -> tuple[str, list[Source]]:
        return await self.openai.ask(question)

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
        if message.author.id == self.bot.user.id:
            return

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

        async with self.request_slots, message.channel.typing():
            try:
                text, sources = await self._ask_openai(
                    with_reply_context(question, previous_reply)
                )
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

        await message.reply(
            format_reply(text, sources),
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
            suppress_embeds=True,
        )

    @commands.hybrid_command(description="Generate an image with OpenAI")
    @app_commands.describe(
        prompt="What the image should look like",
        size="Image dimensions (default: square)",
    )
    async def imagine(
        self,
        ctx: commands.Context,
        *,
        prompt: str,
        size: Literal["1024x1024", "1536x1024", "1024x1536"] = "1024x1024",
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
            f"drawing “{short_prompt}”…\n{image_progress(0)}",
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

        started = time.monotonic()

        async def update_progress() -> None:
            while True:
                await asyncio.sleep(5)
                try:
                    await status.edit(
                        content=f"drawing “{short_prompt}”…\n"
                        f"{image_progress(time.monotonic() - started)}"
                    )
                except discord.HTTPException:
                    pass  # A missed edit is fine; the next tick tries again.

        progress_task = asyncio.create_task(update_progress())
        # ctx.typing() keeps the "K is typing…" indicator alive for prefix
        # invocations; defer alone only pings typing once and it fades in ~10s.
        try:
            async with self.image_slots, ctx.typing():
                image_bytes = await self.images.generate(prompt.strip(), size=size)
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
        finally:
            progress_task.cancel()

        file = discord.File(io.BytesIO(image_bytes), filename="kevin-image.png")
        await status.edit(
            content=f"here you go — “{short_prompt}”",
            attachments=[file],
        )


async def setup(bot: KevinBot) -> None:
    await bot.add_cog(AI(bot))
