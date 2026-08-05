from __future__ import annotations

import logging
import traceback

import discord
from discord.ext import commands

from kevin.bot import KevinBot
from kevin.utils.formatting import embed

log = logging.getLogger(__name__)


def render_template(template: str, member: discord.Member) -> str:
    return (
        template.replace("{user}", member.mention)
        .replace("{server}", member.guild.name)
        .replace("{count}", str(member.guild.member_count or 0))
    )


class Logging(commands.Cog):
    """Welcome messages, autoroles, and server event logging."""

    def __init__(self, bot: KevinBot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        settings = await self.bot.db.get_settings(member.guild.id)
        role_id = settings.get("autorole_id")
        if role_id:
            role = member.guild.get_role(role_id)
            if role:
                try:
                    await member.add_roles(role, reason="K autorole")
                except discord.HTTPException:
                    pass
        channel_id = settings.get("welcome_channel_id")
        channel = member.guild.get_channel(channel_id) if channel_id else None
        if isinstance(channel, discord.TextChannel):
            template = (
                settings.get("welcome_message")
                or "Welcome {user} to **{server}**! You're member #{count}."
            )
            try:
                await channel.send(render_template(template, member))
            except discord.HTTPException:
                pass
        event = embed("Member joined", f"{member.mention} (`{member.id}`)")
        event.add_field(
            name="Account created", value=discord.utils.format_dt(member.created_at, "R")
        )
        event.set_thumbnail(url=member.display_avatar.url)
        await self.bot.send_log(member.guild, event)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        settings = await self.bot.db.get_settings(member.guild.id)
        channel_id = settings.get("goodbye_channel_id")
        channel = member.guild.get_channel(channel_id) if channel_id else None
        if isinstance(channel, discord.TextChannel):
            template = (
                settings.get("goodbye_message")
                or "**{user}** left **{server}**. We now have {count} members."
            )
            try:
                await channel.send(render_template(template, member))
            except discord.HTTPException:
                pass
        event = embed("Member left", f"{member} (`{member.id}`)")
        event.set_thumbnail(url=member.display_avatar.url)
        await self.bot.send_log(member.guild, event)

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message) -> None:
        if not message.guild or message.author.bot:
            return
        event = embed("Message deleted", message.content[:1500] or "*(no text content)*")
        event.add_field(name="Author", value=f"{message.author} (`{message.author.id}`)")
        event.add_field(name="Channel", value=message.channel.mention)
        if message.attachments:
            event.add_field(
                name="Attachments",
                value="\n".join(a.url for a in message.attachments[:3]),
                inline=False,
            )
        await self.bot.send_log(message.guild, event)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if not before.guild or before.author.bot or before.content == after.content:
            return
        event = embed("Message edited")
        event.add_field(name="Author", value=f"{before.author} (`{before.author.id}`)")
        event.add_field(name="Channel", value=before.channel.mention)
        event.add_field(name="Before", value=before.content[:900] or "*(empty)*", inline=False)
        event.add_field(name="After", value=after.content[:900] or "*(empty)*", inline=False)
        event.add_field(name="Jump", value=f"[Open message]({after.jump_url})")
        await self.bot.send_log(before.guild, event)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if before.roles != after.roles:
            added = [role.mention for role in after.roles if role not in before.roles]
            removed = [role.mention for role in before.roles if role not in after.roles]
            event = embed("Member roles changed", f"{after.mention} (`{after.id}`)")
            if added:
                event.add_field(name="Added", value=" ".join(added)[:1000], inline=False)
            if removed:
                event.add_field(name="Removed", value=" ".join(removed)[:1000], inline=False)
            await self.bot.send_log(after.guild, event)
        elif before.nick != after.nick:
            event = embed("Nickname changed", f"{after.mention} (`{after.id}`)")
            event.add_field(name="Before", value=before.nick or before.name)
            event.add_field(name="After", value=after.nick or after.name)
            await self.bot.send_log(after.guild, event)

    @commands.Cog.listener()
    async def on_kevin_error(self, ctx: commands.Context, exception: Exception) -> None:
        log.error(
            "Unhandled command error in %s by %s: %s",
            ctx.command,
            ctx.author,
            "".join(traceback.format_exception(exception)),
        )


async def setup(bot: KevinBot) -> None:
    await bot.add_cog(Logging(bot))
