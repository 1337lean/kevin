from __future__ import annotations

import logging

import discord
from discord.ext import commands

from kevin.config import Settings
from kevin.database import Database
from kevin.utils.formatting import embed

log = logging.getLogger(__name__)

EXTENSIONS = (
    "kevin.cogs.core",
    "kevin.cogs.configuration",
    "kevin.cogs.moderation",
    "kevin.cogs.automod",
    "kevin.cogs.logging",
    "kevin.cogs.economy",
    "kevin.cogs.fun",
    "kevin.cogs.utility",
    "kevin.cogs.community",
    "kevin.cogs.tickets",
    "kevin.cogs.music",
    "kevin.cogs.stream_alerts",
    "kevin.cogs.video_alerts",
    "kevin.cogs.trivia",
    "kevin.cogs.ai",
)


def presence_activity(settings: Settings) -> discord.BaseActivity:
    if settings.stream_url:
        return discord.Streaming(name=settings.status, url=settings.stream_url)
    return discord.Activity(type=discord.ActivityType.watching, name=settings.status)


async def prefix_resolver(bot: KevinBot, message: discord.Message):
    prefix = bot.settings.default_prefix
    if message.guild and bot.db.connection:
        row = await bot.db.fetchone(
            "SELECT prefix FROM guild_settings WHERE guild_id = ?", (message.guild.id,)
        )
        if row and row["prefix"]:
            prefix = row["prefix"]
    return prefix


class KevinBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        intents.moderation = True
        intents.voice_states = True
        super().__init__(
            command_prefix=prefix_resolver,
            strip_after_prefix=True,
            intents=intents,
            help_command=None,
            case_insensitive=True,
            owner_ids=settings.owner_ids or set(),
            allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=False, users=True, replied_user=False
            ),
        )
        self.settings = settings
        self.db = Database(settings.database_path)
        self.started_at = discord.utils.utcnow()

    async def setup_hook(self) -> None:
        await self.db.connect()
        for extension in EXTENSIONS:
            await self.load_extension(extension)
            log.info("Loaded %s", extension)

        if self.settings.test_guild_id:
            guild = discord.Object(id=self.settings.test_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d application commands to development guild", len(synced))
        else:
            synced = await self.tree.sync()
            log.info("Synced %d global application commands", len(synced))

    async def on_ready(self) -> None:
        if self.user is None:
            return
        await self.change_presence(activity=presence_activity(self.settings))
        log.info(
            "K is ready as %s (%s) in %d guilds", self.user, self.user.id, len(self.guilds)
        )

    async def close(self) -> None:
        await self.db.close()
        await super().close()

    async def send_log(self, guild: discord.Guild, event: discord.Embed) -> None:
        row = await self.db.fetchone(
            "SELECT log_channel_id FROM guild_settings WHERE guild_id = ?", (guild.id,)
        )
        if not row or not row["log_channel_id"]:
            return
        channel = guild.get_channel(row["log_channel_id"])
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.send(embed=event)
            except discord.HTTPException:
                log.warning("Could not write to log channel in guild %s", guild.id)

    async def record_case(
        self,
        guild: discord.Guild,
        action: str,
        target: discord.abc.User,
        moderator: discord.abc.User,
        reason: str,
    ) -> int:
        case_id = await self.db.add_case(guild.id, action, target.id, moderator.id, reason)
        event = embed(f"Case #{case_id} · {action.title()}", reason)
        event.add_field(name="Target", value=f"{target} (`{target.id}`)")
        event.add_field(name="Moderator", value=f"{moderator} (`{moderator.id}`)")
        event.timestamp = discord.utils.utcnow()
        await self.send_log(guild, event)
        return case_id
