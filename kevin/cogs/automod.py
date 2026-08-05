from __future__ import annotations

import re
from collections import defaultdict, deque
from datetime import timedelta

import discord
from discord.ext import commands

from kevin.bot import KevinBot
from kevin.utils.checks import owner_or_guild_permissions
from kevin.utils.formatting import embed, success

INVITE_RE = re.compile(r"(?i)(?:discord\.gg|discord(?:app)?\.com/invite)/[a-z0-9-]+")
URL_RE = re.compile(r"(?i)https?://\S+|www\.\S+")


class AutoMod(commands.Cog):
    """Configurable anti-spam and content filters."""

    def __init__(self, bot: KevinBot) -> None:
        self.bot = bot
        self.recent: dict[tuple[int, int], deque[float]] = defaultdict(lambda: deque(maxlen=8))

    @commands.hybrid_group(
        name="automod", fallback="status", description="Configure automatic moderation"
    )
    @commands.guild_only()
    @owner_or_guild_permissions(manage_guild=True)
    async def automod(self, ctx: commands.Context) -> None:
        settings = await self.bot.db.get_settings(ctx.guild.id)
        card = embed(
            "Automod status", "Filters are applied only to members without Manage Messages."
        )
        for label, key in (
            ("Master switch", "automod_enabled"),
            ("Spam", "automod_spam"),
            ("Invites", "automod_invites"),
            ("Links", "automod_links"),
            ("Excess caps", "automod_caps"),
            ("Mass mentions", "automod_mentions"),
        ):
            card.add_field(name=label, value="On" if settings.get(key) else "Off")
        words = [w for w in settings.get("automod_bad_words", "").split("|") if w]
        card.add_field(name="Blocked terms", value=str(len(words)))
        card.add_field(name="Mention limit", value=str(settings.get("automod_max_mentions", 5)))
        await ctx.send(embed=card)

    @automod.command(name="enabled", description="Turn automod on or off")
    @owner_or_guild_permissions(manage_guild=True)
    async def enabled(self, ctx: commands.Context, value: bool) -> None:
        await self.bot.db.set_setting(ctx.guild.id, "automod_enabled", int(value))
        await ctx.send(embed=success(f"Automod is now **{'on' if value else 'off'}**."))

    @automod.command(name="filter", description="Enable or disable an automod filter")
    @owner_or_guild_permissions(manage_guild=True)
    async def filter(self, ctx: commands.Context, feature: str, enabled: bool) -> None:
        key = feature.lower().replace("-", "_")
        allowed = {"spam", "invites", "links", "caps", "mentions"}
        if key not in allowed:
            raise commands.BadArgument("Filter must be spam, invites, links, caps, or mentions.")
        await self.bot.db.set_setting(ctx.guild.id, f"automod_{key}", int(enabled))
        await ctx.send(
            embed=success(f"Automod **{key}** filter is now **{'on' if enabled else 'off'}**.")
        )

    @automod.command(name="mentions", description="Set the mentions allowed per message")
    @owner_or_guild_permissions(manage_guild=True)
    async def mentions(self, ctx: commands.Context, limit: int) -> None:
        if not 1 <= limit <= 25:
            raise commands.BadArgument("Mention limit must be between 1 and 25.")
        await self.bot.db.set_setting(ctx.guild.id, "automod_max_mentions", limit)
        await ctx.send(embed=success(f"Mass-mention limit set to **{limit}**."))

    @automod.command(name="word-add", description="Add a term to the blocked-word list")
    @owner_or_guild_permissions(manage_guild=True)
    async def word_add(self, ctx: commands.Context, *, word: str) -> None:
        word = word.strip().lower().replace("|", "")[:60]
        if not word:
            raise commands.BadArgument("Provide a word or phrase.")
        settings = await self.bot.db.get_settings(ctx.guild.id)
        words = {w for w in settings.get("automod_bad_words", "").split("|") if w}
        words.add(word)
        await self.bot.db.set_setting(ctx.guild.id, "automod_bad_words", "|".join(sorted(words)))
        await ctx.send(embed=success(f"Added `{word}` to the blocked list."), ephemeral=True)

    @automod.command(name="word-remove", description="Remove a term from the blocked-word list")
    @owner_or_guild_permissions(manage_guild=True)
    async def word_remove(self, ctx: commands.Context, *, word: str) -> None:
        settings = await self.bot.db.get_settings(ctx.guild.id)
        words = {w for w in settings.get("automod_bad_words", "").split("|") if w}
        words.discard(word.strip().lower())
        await self.bot.db.set_setting(ctx.guild.id, "automod_bad_words", "|".join(sorted(words)))
        await ctx.send(embed=success(f"Removed `{word}` from the blocked list."), ephemeral=True)

    async def _take_action(
        self, message: discord.Message, reason: str, *, apply_timeout: bool = False
    ) -> None:
        try:
            await message.delete()
        except discord.HTTPException:
            pass
        if apply_timeout and isinstance(message.author, discord.Member):
            try:
                await message.author.timeout(
                    timedelta(minutes=5), reason=f"K automod: {reason}"
                )
            except discord.HTTPException:
                pass
        try:
            notice = await message.channel.send(
                f"{message.author.mention}, your message was removed: **{reason}**.",
                delete_after=6,
            )
        except discord.HTTPException:
            notice = None
        event = embed("Automod action", reason)
        event.add_field(name="Member", value=f"{message.author} (`{message.author.id}`)")
        event.add_field(name="Channel", value=message.channel.mention)
        event.add_field(
            name="Content", value=(message.content[:800] or "*(no text)*"), inline=False
        )
        await self.bot.send_log(message.guild, event)
        _ = notice

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if (
            not message.guild
            or message.author.bot
            or not isinstance(message.author, discord.Member)
        ):
            return
        if message.author.guild_permissions.manage_messages:
            return
        settings = await self.bot.db.get_settings(message.guild.id)
        if not settings.get("automod_enabled"):
            return

        content = message.content
        lowered = content.casefold()
        if settings.get("automod_invites") and INVITE_RE.search(content):
            await self._take_action(message, "Discord invite links are blocked")
            return
        if settings.get("automod_links") and URL_RE.search(content):
            await self._take_action(message, "Links are blocked")
            return
        words = [word for word in settings.get("automod_bad_words", "").split("|") if word]
        if any(word.casefold() in lowered for word in words):
            await self._take_action(message, "Blocked word or phrase")
            return
        mentions = len(set(message.mentions)) + len(set(message.role_mentions))
        if settings.get("automod_mentions") and mentions > settings.get("automod_max_mentions", 5):
            await self._take_action(message, "Too many mentions", apply_timeout=True)
            return
        letters = [character for character in content if character.isalpha()]
        if (
            settings.get("automod_caps")
            and len(letters) >= 12
            and sum(character.isupper() for character in letters) / len(letters) >= 0.8
        ):
            await self._take_action(message, "Excessive capital letters")
            return
        if settings.get("automod_spam"):
            key = (message.guild.id, message.author.id)
            now = message.created_at.timestamp()
            bucket = self.recent[key]
            bucket.append(now)
            if len(bucket) >= 6 and now - bucket[-6] < 8:
                bucket.clear()
                await self._take_action(message, "Message spam", apply_timeout=True)


async def setup(bot: KevinBot) -> None:
    await bot.add_cog(AutoMod(bot))
