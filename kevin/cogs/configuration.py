from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from kevin.bot import KevinBot
from kevin.utils.checks import owner_or_guild_permissions
from kevin.utils.formatting import embed, success


class Configuration(commands.Cog):
    """Per-server configuration."""

    def __init__(self, bot: KevinBot) -> None:
        self.bot = bot

    @commands.hybrid_group(
        name="config", fallback="show", description="Configure K for this server"
    )
    @commands.guild_only()
    @owner_or_guild_permissions(manage_guild=True)
    async def config(self, ctx: commands.Context) -> None:
        settings = await self.bot.db.get_settings(ctx.guild.id)

        def channel(key: str) -> str:
            return f"<#{settings[key]}>" if settings.get(key) else "Not set"

        role = f"<@&{settings['autorole_id']}>" if settings.get("autorole_id") else "Not set"
        card = embed("K configuration")
        card.add_field(
            name="Prefix", value=f"`{settings.get('prefix') or self.bot.settings.default_prefix}`"
        )
        card.add_field(name="Log channel", value=channel("log_channel_id"))
        card.add_field(name="Autorole", value=role)
        card.add_field(name="Welcome channel", value=channel("welcome_channel_id"))
        card.add_field(name="Goodbye channel", value=channel("goodbye_channel_id"))
        card.add_field(name="Leveling", value="On" if settings.get("levels_enabled") else "Off")
        card.add_field(name="Economy", value="On" if settings.get("economy_enabled") else "Off")
        card.add_field(name="Automod", value="On" if settings.get("automod_enabled") else "Off")
        await ctx.send(embed=card)

    @config.command(name="prefix", description="Change K's text-command prefix")
    @app_commands.describe(prefix="A prefix between 1 and 5 characters")
    @owner_or_guild_permissions(manage_guild=True)
    async def prefix(self, ctx: commands.Context, prefix: str) -> None:
        if not 1 <= len(prefix) <= 5 or prefix.isspace():
            raise commands.BadArgument("Prefix must be 1–5 visible characters.")
        await self.bot.db.set_setting(ctx.guild.id, "prefix", prefix)
        await ctx.send(embed=success(f"Text-command prefix set to `{prefix}`."))

    @config.command(name="logs", description="Set or disable the event log channel")
    @owner_or_guild_permissions(manage_guild=True)
    async def logs(self, ctx: commands.Context, channel: discord.TextChannel | None = None) -> None:
        await self.bot.db.set_setting(
            ctx.guild.id, "log_channel_id", channel.id if channel else None
        )
        await ctx.send(
            embed=success(f"Log channel {'set to ' + channel.mention if channel else 'disabled'}.")
        )

    @config.command(name="welcome", description="Configure welcome messages")
    @app_commands.describe(message="Use {user}, {server}, and {count}; omit to keep the default")
    @owner_or_guild_permissions(manage_guild=True)
    async def welcome(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel | None = None,
        *,
        message: str | None = None,
    ) -> None:
        await self.bot.db.set_setting(
            ctx.guild.id, "welcome_channel_id", channel.id if channel else None
        )
        if message is not None:
            await self.bot.db.set_setting(ctx.guild.id, "welcome_message", message[:1000])
        await ctx.send(
            embed=success(
                f"Welcome messages {'will go to ' + channel.mention if channel else 'are disabled'}."
            )
        )

    @config.command(name="goodbye", description="Configure goodbye messages")
    @app_commands.describe(message="Use {user}, {server}, and {count}; omit to keep the default")
    @owner_or_guild_permissions(manage_guild=True)
    async def goodbye(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel | None = None,
        *,
        message: str | None = None,
    ) -> None:
        await self.bot.db.set_setting(
            ctx.guild.id, "goodbye_channel_id", channel.id if channel else None
        )
        if message is not None:
            await self.bot.db.set_setting(ctx.guild.id, "goodbye_message", message[:1000])
        await ctx.send(
            embed=success(
                f"Goodbye messages {'will go to ' + channel.mention if channel else 'are disabled'}."
            )
        )

    @config.command(name="autorole", description="Set the role given to new members")
    @owner_or_guild_permissions(manage_guild=True)
    async def autorole(self, ctx: commands.Context, role: discord.Role | None = None) -> None:
        if role and role >= ctx.guild.me.top_role:
            raise commands.BadArgument("That role must be below K's highest role.")
        await self.bot.db.set_setting(ctx.guild.id, "autorole_id", role.id if role else None)
        await ctx.send(
            embed=success(f"Autorole {'set to ' + role.mention if role else 'disabled'}.")
        )

    @config.command(name="leveling", description="Enable or disable XP and levels")
    @owner_or_guild_permissions(manage_guild=True)
    async def leveling(self, ctx: commands.Context, enabled: bool) -> None:
        await self.bot.db.set_setting(ctx.guild.id, "levels_enabled", int(enabled))
        await ctx.send(embed=success(f"Leveling is now **{'on' if enabled else 'off'}**."))

    @config.command(name="economy", description="Enable or disable the economy")
    @owner_or_guild_permissions(manage_guild=True)
    async def economy(self, ctx: commands.Context, enabled: bool) -> None:
        await self.bot.db.set_setting(ctx.guild.id, "economy_enabled", int(enabled))
        await ctx.send(embed=success(f"Economy is now **{'on' if enabled else 'off'}**."))


async def setup(bot: KevinBot) -> None:
    await bot.add_cog(Configuration(bot))
