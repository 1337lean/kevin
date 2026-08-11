from __future__ import annotations

import platform
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from kevin import __version__
from kevin.bot import KevinBot
from kevin.utils.formatting import embed, error, human_duration


class Core(commands.Cog):
    """Essential commands and global error handling."""

    def __init__(self, bot: KevinBot) -> None:
        self.bot = bot

    @commands.hybrid_command(name="help", description="Show K's command guide")
    @app_commands.describe(command="Show detailed help for one command")
    async def help_command(self, ctx: commands.Context, *, command: str | None = None) -> None:
        if command:
            found = self.bot.get_command(command)
            if not found or found.hidden:
                await ctx.send(
                    embed=error(f"I couldn't find a command named `{command}`."), ephemeral=True
                )
                return
            usage = f"{ctx.clean_prefix}{found.qualified_name} {found.signature}".rstrip()
            card = embed(
                f"Help · {found.qualified_name}",
                found.help or found.description or "No description.",
            )
            card.add_field(name="Usage", value=f"`{usage}`", inline=False)
            if found.aliases:
                card.add_field(name="Aliases", value=", ".join(f"`{a}`" for a in found.aliases))
            await ctx.send(embed=card)
            return

        categories: dict[str, list[str]] = {}
        for cog_name, cog in self.bot.cogs.items():
            visible = sorted(
                command.qualified_name
                for command in cog.get_commands()
                if not command.hidden and command.enabled
            )
            if visible:
                categories[cog_name] = visible
        card = embed(
            "K's command guide",
            f"Use `/help command:<name>` or `{ctx.clean_prefix}help <name>` for details. "
            "Slash commands are recommended.",
        )
        for name, command_names in categories.items():
            card.add_field(
                name=name,
                value=" · ".join(f"`{item}`" for item in command_names),
                inline=False,
            )
        card.set_footer(text=f"K v{__version__} · {len(self.bot.commands)} command groups")
        await ctx.send(embed=card)

    @commands.hybrid_command(description="Check K's response time")
    async def ping(self, ctx: commands.Context) -> None:
        await ctx.send(
            embed=embed("Pong!", f"Gateway latency: **{self.bot.latency * 1000:.0f} ms**")
        )

    @commands.hybrid_command(description="Show information about K")
    async def about(self, ctx: commands.Context) -> None:
        uptime = discord.utils.utcnow() - self.bot.started_at
        card = embed(
            "About K",
            "A modular all-in-one bot for moderation, community tools, music, and games.",
        )
        card.add_field(name="Servers", value=str(len(self.bot.guilds)))
        card.add_field(name="Users", value=f"{sum(g.member_count or 0 for g in self.bot.guilds):,}")
        card.add_field(
            name="Uptime", value=human_duration(timedelta(seconds=int(uptime.total_seconds())))
        )
        card.add_field(name="Python", value=platform.python_version())
        card.add_field(name="discord.py", value=discord.__version__)
        card.add_field(name="Version", value=__version__)
        if self.bot.user:
            card.set_thumbnail(url=self.bot.user.display_avatar.url)
        await ctx.send(embed=card)

    @commands.hybrid_command(description="Get K's server invite link")
    async def invite(self, ctx: commands.Context) -> None:
        if not self.bot.user:
            return
        permissions = discord.Permissions(
            manage_guild=True,
            manage_roles=True,
            manage_channels=True,
            kick_members=True,
            ban_members=True,
            moderate_members=True,
            manage_messages=True,
            view_audit_log=True,
            connect=True,
            speak=True,
        )
        url = discord.utils.oauth_url(
            self.bot.user.id,
            permissions=permissions,
            scopes=("bot", "applications.commands"),
        )
        await ctx.send(embed=embed("Invite K", f"[Add K to a server]({url})"))

    @commands.Cog.listener()
    async def on_command_error(
        self, ctx: commands.Context, exception: commands.CommandError
    ) -> None:
        if ctx.command and ctx.command.has_error_handler():
            return
        exception = getattr(exception, "original", exception)
        if isinstance(exception, commands.CommandNotFound):
            return
        if isinstance(exception, commands.CommandOnCooldown):
            message = f"Slow down—try again in **{exception.retry_after:.1f}s**."
        elif isinstance(exception, commands.MissingPermissions):
            message = "You need: " + ", ".join(
                p.replace("_", " ") for p in exception.missing_permissions
            )
        elif isinstance(exception, commands.BotMissingPermissions):
            message = "K needs: " + ", ".join(
                p.replace("_", " ") for p in exception.missing_permissions
            )
        elif isinstance(exception, commands.NoPrivateMessage):
            message = "That command can only be used in a server."
        elif isinstance(exception, (commands.MissingRequiredArgument, commands.BadArgument)):
            usage = (
                f"{ctx.clean_prefix}{ctx.command.qualified_name} {ctx.command.signature}"
                if ctx.command
                else ""
            )
            message = f"{exception}\nUsage: `{usage.rstrip()}`"
        elif isinstance(exception, commands.CheckFailure):
            message = str(exception) or "You cannot use that command here."
        else:
            message = "Something unexpected happened. The error has been logged."
            self.bot.dispatch("kevin_error", ctx, exception)
        await ctx.send(embed=error(message), ephemeral=True)


async def setup(bot: KevinBot) -> None:
    await bot.add_cog(Core(bot))
