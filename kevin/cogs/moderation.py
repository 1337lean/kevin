from __future__ import annotations

from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from kevin.bot import KevinBot
from kevin.utils.checks import owner_or_guild_permissions
from kevin.utils.formatting import embed, human_duration, parse_duration, success

PURGE_SEARCH_LIMIT = 1000


async def require_permission(ctx: commands.Context, permission: str) -> None:
    """Gate one subcommand on a permission its shared group check does not cover.

    The `voice` group gates on Mute Members because that is what most of it needs;
    moving and deafening are separate Discord permissions and must be asked for
    separately rather than riding along on the group's check.
    """
    if await ctx.bot.is_owner(ctx.author):
        return
    if not getattr(ctx.author.guild_permissions, permission, False):
        raise commands.MissingPermissions([permission])


def hierarchy_check(ctx: commands.Context, member: discord.Member) -> None:
    if not ctx.guild or not isinstance(ctx.author, discord.Member):
        return
    if member == ctx.author:
        raise commands.BadArgument("You cannot moderate yourself.")
    if member == ctx.guild.owner:
        raise commands.BadArgument("The server owner cannot be moderated.")
    if (
        ctx.author != ctx.guild.owner
        and ctx.author.id not in (ctx.bot.owner_ids or ())
        and member.top_role >= ctx.author.top_role
    ):
        raise commands.BadArgument("That member's highest role is equal to or above yours.")
    if ctx.guild.me and member.top_role >= ctx.guild.me.top_role:
        raise commands.BadArgument("That member's role is above K's role.")


class Moderation(commands.Cog):
    """Member and channel moderation."""

    def __init__(self, bot: KevinBot) -> None:
        self.bot = bot

    @commands.hybrid_command(description="Kick a member")
    @commands.guild_only()
    @owner_or_guild_permissions(kick_members=True)
    @commands.bot_has_guild_permissions(kick_members=True)
    async def kick(
        self, ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"
    ) -> None:
        hierarchy_check(ctx, member)
        await member.kick(reason=f"{ctx.author}: {reason}")
        case = await self.bot.record_case(ctx.guild, "kick", member, ctx.author, reason)
        await ctx.send(embed=success(f"Kicked **{member}** · Case #{case}\n{reason}"))

    @commands.hybrid_command(description="Ban a member and optionally delete recent messages")
    @commands.guild_only()
    @owner_or_guild_permissions(ban_members=True)
    @commands.bot_has_guild_permissions(ban_members=True)
    @app_commands.describe(delete_days="Delete 0–7 days of recent messages")
    async def ban(
        self,
        ctx: commands.Context,
        member: discord.Member,
        delete_days: commands.Range[int, 0, 7] = 0,
        *,
        reason: str = "No reason provided",
    ) -> None:
        hierarchy_check(ctx, member)
        await ctx.guild.ban(
            member, reason=f"{ctx.author}: {reason}", delete_message_seconds=delete_days * 86400
        )
        case = await self.bot.record_case(ctx.guild, "ban", member, ctx.author, reason)
        await ctx.send(embed=success(f"Banned **{member}** · Case #{case}\n{reason}"))

    @commands.hybrid_command(description="Unban a user by ID")
    @commands.guild_only()
    @owner_or_guild_permissions(ban_members=True)
    @commands.bot_has_guild_permissions(ban_members=True)
    async def unban(
        self, ctx: commands.Context, user_id: str, *, reason: str = "No reason provided"
    ) -> None:
        try:
            user = await self.bot.fetch_user(int(user_id.strip("<@!>")))
        except (ValueError, discord.NotFound) as exc:
            raise commands.BadArgument("Provide a valid banned user ID.") from exc
        await ctx.guild.unban(user, reason=f"{ctx.author}: {reason}")
        case = await self.bot.record_case(ctx.guild, "unban", user, ctx.author, reason)
        await ctx.send(embed=success(f"Unbanned **{user}** · Case #{case}"))

    @commands.hybrid_command(description="Ban and immediately unban a member to clear messages")
    @commands.guild_only()
    @owner_or_guild_permissions(ban_members=True)
    @commands.bot_has_guild_permissions(ban_members=True)
    async def softban(
        self,
        ctx: commands.Context,
        member: discord.Member,
        *,
        reason: str = "No reason provided",
    ) -> None:
        hierarchy_check(ctx, member)
        await ctx.guild.ban(
            member,
            reason=f"Softban by {ctx.author}: {reason}",
            delete_message_seconds=7 * 86400,
        )
        await ctx.guild.unban(member, reason=f"Softban completed by {ctx.author}")
        case = await self.bot.record_case(ctx.guild, "softban", member, ctx.author, reason)
        await ctx.send(embed=success(f"Softbanned **{member}** · Case #{case}"))

    @commands.hybrid_command(description="Ban several users from a space-separated list of IDs")
    @commands.guild_only()
    @owner_or_guild_permissions(ban_members=True)
    @commands.bot_has_guild_permissions(ban_members=True)
    async def massban(
        self, ctx: commands.Context, user_ids: str, *, reason: str = "No reason provided"
    ) -> None:
        raw_ids = user_ids.replace(",", " ").split()
        if not 1 <= len(raw_ids) <= 20:
            raise commands.BadArgument("Provide between 1 and 20 space-separated user IDs.")
        if ctx.interaction:
            await ctx.defer(ephemeral=True)
        banned: list[int] = []
        failed: list[str] = []
        for raw_id in raw_ids:
            try:
                user_id = int(raw_id.strip("<@!>"))
                member = ctx.guild.get_member(user_id)
                if member:
                    hierarchy_check(ctx, member)
                    target: discord.abc.User = member
                else:
                    target = discord.Object(id=user_id)
                await ctx.guild.ban(target, reason=f"Massban by {ctx.author}: {reason}")
                await self.bot.db.add_case(ctx.guild.id, "ban", user_id, ctx.author.id, reason)
                banned.append(user_id)
            except (ValueError, discord.HTTPException, commands.BadArgument):
                failed.append(raw_id)
        result = f"Banned **{len(banned)}** user(s)."
        if failed:
            result += f" Failed: `{', '.join(failed)}`"
        await ctx.send(embed=success(result), ephemeral=bool(ctx.interaction))

    @commands.hybrid_command(aliases=["mute"], description="Temporarily time out a member")
    @commands.guild_only()
    @owner_or_guild_permissions(moderate_members=True)
    @commands.bot_has_guild_permissions(moderate_members=True)
    async def timeout(
        self,
        ctx: commands.Context,
        member: discord.Member,
        duration: str,
        *,
        reason: str = "No reason provided",
    ) -> None:
        hierarchy_check(ctx, member)
        try:
            delta = parse_duration(duration, maximum=timedelta(days=28))
        except ValueError as exc:
            raise commands.BadArgument(str(exc)) from exc
        await member.timeout(delta, reason=f"{ctx.author}: {reason}")
        case = await self.bot.record_case(ctx.guild, "timeout", member, ctx.author, reason)
        await ctx.send(
            embed=success(f"Timed out **{member}** for {human_duration(delta)} · Case #{case}")
        )

    @commands.hybrid_command(aliases=["unmute"], description="Remove a member's timeout")
    @commands.guild_only()
    @owner_or_guild_permissions(moderate_members=True)
    @commands.bot_has_guild_permissions(moderate_members=True)
    async def untimeout(
        self, ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"
    ) -> None:
        hierarchy_check(ctx, member)
        await member.timeout(None, reason=f"{ctx.author}: {reason}")
        case = await self.bot.record_case(ctx.guild, "untimeout", member, ctx.author, reason)
        await ctx.send(embed=success(f"Removed **{member}**'s timeout · Case #{case}"))

    @commands.hybrid_command(aliases=["clear"], description="Bulk-delete messages")
    @commands.guild_only()
    @owner_or_guild_permissions(manage_messages=True)
    @commands.bot_has_guild_permissions(manage_messages=True, read_message_history=True)
    @app_commands.describe(
        amount="How many messages to delete",
        member="Only delete this member's messages",
    )
    async def purge(
        self,
        ctx: commands.Context,
        amount: commands.Range[int, 1, 500],
        member: discord.Member | None = None,
    ) -> None:
        if not isinstance(ctx.channel, discord.TextChannel):
            raise commands.BadArgument("Use this in a text channel.")
        if ctx.interaction:
            await ctx.defer(ephemeral=True)
        before = ctx.message if not ctx.interaction else None
        reason = f"Purge by {ctx.author}"

        if member is None:
            deleted = await ctx.channel.purge(limit=amount, before=before, reason=reason)
            await ctx.send(
                embed=success(f"Deleted **{len(deleted)}** messages."),
                ephemeral=True,
                delete_after=5,
            )
            return

        deleted = await self.purge_member_messages(ctx, member, amount, before, reason)
        note = f"Deleted **{len(deleted)}** messages from {member.mention}."
        if len(deleted) < amount:
            note += (
                f"\nThat is all they had in the last {PURGE_SEARCH_LIMIT:,} messages here."
            )
        await ctx.send(embed=success(note), ephemeral=True, delete_after=5)

    async def purge_member_messages(
        self,
        ctx: commands.Context,
        member: discord.Member,
        amount: int,
        before: discord.Message | None,
        reason: str,
    ) -> list[discord.Message]:
        """Delete a member's most recent `amount` messages, however far back they sit.

        The first pass walks history only until enough of their messages are found, then
        the delete is bounded to that window so purge never rescans the whole channel.
        """
        targets: set[int] = set()
        oldest: discord.Message | None = None
        async for message in ctx.channel.history(limit=PURGE_SEARCH_LIMIT, before=before):
            if message.author.id == member.id:
                targets.add(message.id)
                oldest = message
                if len(targets) == amount:
                    break
        if oldest is None:
            return []
        return await ctx.channel.purge(
            limit=None,
            check=lambda message: message.id in targets,
            before=before,
            after=discord.Object(id=oldest.id - 1),
            reason=reason,
        )

    @commands.hybrid_command(description="Set this channel's slowmode")
    @commands.guild_only()
    @owner_or_guild_permissions(manage_channels=True)
    @commands.bot_has_guild_permissions(manage_channels=True)
    async def slowmode(
        self, ctx: commands.Context, seconds: commands.Range[int, 0, 21600]
    ) -> None:
        await ctx.channel.edit(slowmode_delay=seconds, reason=f"Changed by {ctx.author}")
        await ctx.send(embed=success(f"Slowmode set to **{seconds} seconds**."))

    @commands.hybrid_command(description="Prevent members from sending messages here")
    @commands.guild_only()
    @owner_or_guild_permissions(manage_channels=True)
    @commands.bot_has_guild_permissions(manage_channels=True)
    async def lock(self, ctx: commands.Context) -> None:
        overwrite = ctx.channel.overwrites_for(ctx.guild.default_role)
        overwrite.send_messages = False
        await ctx.channel.set_permissions(
            ctx.guild.default_role, overwrite=overwrite, reason=f"Locked by {ctx.author}"
        )
        await ctx.send(embed=success("Channel locked."))

    @commands.hybrid_command(description="Allow members to send messages here")
    @commands.guild_only()
    @owner_or_guild_permissions(manage_channels=True)
    @commands.bot_has_guild_permissions(manage_channels=True)
    async def unlock(self, ctx: commands.Context) -> None:
        overwrite = ctx.channel.overwrites_for(ctx.guild.default_role)
        overwrite.send_messages = None
        await ctx.channel.set_permissions(
            ctx.guild.default_role, overwrite=overwrite, reason=f"Unlocked by {ctx.author}"
        )
        await ctx.send(embed=success("Channel unlocked."))

    @commands.hybrid_command(description="Change or reset a member's nickname")
    @commands.guild_only()
    @owner_or_guild_permissions(manage_nicknames=True)
    @commands.bot_has_guild_permissions(manage_nicknames=True)
    async def nick(
        self, ctx: commands.Context, member: discord.Member, *, nickname: str | None = None
    ) -> None:
        hierarchy_check(ctx, member)
        await member.edit(nick=nickname, reason=f"Changed by {ctx.author}")
        await ctx.send(
            embed=success(f"Nickname {'changed' if nickname else 'reset'} for **{member}**.")
        )

    @commands.hybrid_group(
        name="voice", fallback="kick", description="Moderate members in voice channels"
    )
    @commands.guild_only()
    @owner_or_guild_permissions(mute_members=True)
    @commands.bot_has_guild_permissions(mute_members=True, move_members=True, deafen_members=True)
    async def voice(self, ctx: commands.Context, member: discord.Member) -> None:
        await require_permission(ctx, "move_members")
        hierarchy_check(ctx, member)
        if not member.voice:
            raise commands.BadArgument("That member is not in a voice channel.")
        await member.move_to(None, reason=f"Voice kick by {ctx.author}")
        case = await self.bot.record_case(
            ctx.guild, "voice kick", member, ctx.author, "Removed from voice"
        )
        await ctx.send(embed=success(f"Removed **{member}** from voice · Case #{case}"))

    @voice.command(name="mute", description="Server-mute a voice member")
    @owner_or_guild_permissions(mute_members=True)
    async def voice_mute(self, ctx: commands.Context, member: discord.Member) -> None:
        hierarchy_check(ctx, member)
        if not member.voice:
            raise commands.BadArgument("That member is not in a voice channel.")
        await member.edit(mute=True, reason=f"Voice mute by {ctx.author}")
        await ctx.send(embed=success(f"Voice-muted **{member}**."))

    @voice.command(name="unmute", description="Remove a member's server voice mute")
    @owner_or_guild_permissions(mute_members=True)
    async def voice_unmute(self, ctx: commands.Context, member: discord.Member) -> None:
        hierarchy_check(ctx, member)
        await member.edit(mute=False, reason=f"Voice unmute by {ctx.author}")
        await ctx.send(embed=success(f"Voice-unmuted **{member}**."))

    @voice.command(name="deafen", description="Server-deafen a voice member")
    @owner_or_guild_permissions(mute_members=True)
    async def voice_deafen(self, ctx: commands.Context, member: discord.Member) -> None:
        await require_permission(ctx, "deafen_members")
        hierarchy_check(ctx, member)
        if not member.voice:
            raise commands.BadArgument("That member is not in a voice channel.")
        await member.edit(deafen=True, reason=f"Voice deafen by {ctx.author}")
        await ctx.send(embed=success(f"Server-deafened **{member}**."))

    @voice.command(name="undeafen", description="Remove a member's server deafen")
    @owner_or_guild_permissions(mute_members=True)
    async def voice_undeafen(self, ctx: commands.Context, member: discord.Member) -> None:
        await require_permission(ctx, "deafen_members")
        hierarchy_check(ctx, member)
        await member.edit(deafen=False, reason=f"Voice undeafen by {ctx.author}")
        await ctx.send(embed=success(f"Server-undeafened **{member}**."))

    @voice.command(name="move", description="Move a member to another voice channel")
    @owner_or_guild_permissions(mute_members=True)
    async def voice_move(
        self, ctx: commands.Context, member: discord.Member, channel: discord.VoiceChannel
    ) -> None:
        await require_permission(ctx, "move_members")
        hierarchy_check(ctx, member)
        if not member.voice:
            raise commands.BadArgument("That member is not in a voice channel.")
        await member.move_to(channel, reason=f"Moved by {ctx.author}")
        await ctx.send(embed=success(f"Moved **{member}** to {channel.mention}."))

    @commands.hybrid_group(name="role", fallback="info", description="Manage or inspect roles")
    @commands.guild_only()
    @owner_or_guild_permissions(manage_roles=True)
    async def role(self, ctx: commands.Context, role: discord.Role) -> None:
        card = embed(f"Role · {role.name}")
        card.add_field(name="ID", value=str(role.id))
        card.add_field(name="Members", value=str(len(role.members)))
        card.add_field(name="Color", value=str(role.color))
        card.add_field(name="Created", value=discord.utils.format_dt(role.created_at, "R"))
        await ctx.send(embed=card)

    @role.command(name="add", description="Add a role to a member")
    @owner_or_guild_permissions(manage_roles=True)
    @commands.bot_has_guild_permissions(manage_roles=True)
    async def role_add(
        self, ctx: commands.Context, member: discord.Member, role: discord.Role
    ) -> None:
        if role >= ctx.guild.me.top_role:
            raise commands.BadArgument("That role must be below K's highest role.")
        await member.add_roles(role, reason=f"Added by {ctx.author}")
        await ctx.send(embed=success(f"Added {role.mention} to {member.mention}."))

    @role.command(name="remove", description="Remove a role from a member")
    @owner_or_guild_permissions(manage_roles=True)
    @commands.bot_has_guild_permissions(manage_roles=True)
    async def role_remove(
        self, ctx: commands.Context, member: discord.Member, role: discord.Role
    ) -> None:
        if role >= ctx.guild.me.top_role:
            raise commands.BadArgument("That role must be below K's highest role.")
        await member.remove_roles(role, reason=f"Removed by {ctx.author}")
        await ctx.send(embed=success(f"Removed {role.mention} from {member.mention}."))

    @commands.hybrid_command(description="Warn a member")
    @commands.guild_only()
    @owner_or_guild_permissions(moderate_members=True)
    async def warn(
        self, ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"
    ) -> None:
        hierarchy_check(ctx, member)
        warning_id = await self.bot.db.execute(
            "INSERT INTO warnings(guild_id, user_id, moderator_id, reason) VALUES (?, ?, ?, ?)",
            (ctx.guild.id, member.id, ctx.author.id, reason),
        )
        case = await self.bot.record_case(ctx.guild, "warn", member, ctx.author, reason)
        try:
            await member.send(embed=embed(f"Warning from {ctx.guild.name}", reason))
        except discord.HTTPException:
            pass
        await ctx.send(embed=success(f"Warned **{member}** · Warning #{warning_id}, Case #{case}"))

    @commands.hybrid_command(description="List a member's warnings")
    @commands.guild_only()
    @owner_or_guild_permissions(moderate_members=True)
    async def warnings(self, ctx: commands.Context, member: discord.Member) -> None:
        rows = await self.bot.db.fetchall(
            "SELECT id, moderator_id, reason, created_at FROM warnings WHERE guild_id = ? AND user_id = ? ORDER BY id DESC LIMIT 15",
            (ctx.guild.id, member.id),
        )
        if not rows:
            await ctx.send(embed=embed(f"Warnings · {member}", "No warnings."))
            return
        text = "\n".join(
            f"**#{r['id']}** · <@{r['moderator_id']}> · {r['reason'][:120]}" for r in rows
        )
        await ctx.send(embed=embed(f"Warnings · {member}", text))

    @commands.hybrid_command(description="Clear all warnings for a member")
    @commands.guild_only()
    @owner_or_guild_permissions(moderate_members=True)
    async def clearwarnings(self, ctx: commands.Context, member: discord.Member) -> None:
        await self.bot.db.execute(
            "DELETE FROM warnings WHERE guild_id = ? AND user_id = ?", (ctx.guild.id, member.id)
        )
        await ctx.send(embed=success(f"Cleared warnings for **{member}**."))

    @commands.hybrid_command(description="Show a moderation case")
    @commands.guild_only()
    @owner_or_guild_permissions(moderate_members=True)
    async def case(self, ctx: commands.Context, case_id: int) -> None:
        row = await self.bot.db.fetchone(
            "SELECT * FROM cases WHERE guild_id = ? AND id = ?", (ctx.guild.id, case_id)
        )
        if not row:
            raise commands.BadArgument("That case does not exist in this server.")
        card = embed(f"Case #{row['id']} · {row['action'].title()}", row["reason"])
        card.add_field(name="Target", value=f"<@{row['target_id']}> (`{row['target_id']}`)")
        card.add_field(
            name="Moderator", value=f"<@{row['moderator_id']}> (`{row['moderator_id']}`)"
        )
        card.set_footer(text=row["created_at"] + " UTC")
        await ctx.send(embed=card)

    @commands.hybrid_command(description="Show recent moderation cases for a member")
    @commands.guild_only()
    @owner_or_guild_permissions(moderate_members=True)
    async def history(self, ctx: commands.Context, member: discord.Member) -> None:
        rows = await self.bot.db.fetchall(
            "SELECT id, action, moderator_id, reason FROM cases WHERE guild_id = ? AND target_id = ? ORDER BY id DESC LIMIT 15",
            (ctx.guild.id, member.id),
        )
        lines = [
            f"**#{row['id']} {row['action'].title()}** · <@{row['moderator_id']}> · {row['reason'][:100]}"
            for row in rows
        ]
        await ctx.send(
            embed=embed(f"Moderation history · {member}", "\n".join(lines) or "No cases.")
        )

    @commands.hybrid_command(description="Update the reason on a moderation case")
    @commands.guild_only()
    @owner_or_guild_permissions(moderate_members=True)
    async def reason(self, ctx: commands.Context, case_id: int, *, reason: str) -> None:
        row = await self.bot.db.fetchone(
            "SELECT id FROM cases WHERE guild_id = ? AND id = ?", (ctx.guild.id, case_id)
        )
        if not row:
            raise commands.BadArgument("That case does not exist in this server.")
        await self.bot.db.execute(
            "UPDATE cases SET reason = ? WHERE guild_id = ? AND id = ?",
            (reason[:1000], ctx.guild.id, case_id),
        )
        await ctx.send(embed=success(f"Updated case #{case_id}."))


async def setup(bot: KevinBot) -> None:
    await bot.add_cog(Moderation(bot))
