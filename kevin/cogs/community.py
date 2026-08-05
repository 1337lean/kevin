from __future__ import annotations

import random
import time
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from kevin.bot import KevinBot
from kevin.utils.checks import owner_or_guild_permissions
from kevin.utils.formatting import embed, parse_duration, success

RNG = random.SystemRandom()

# Only sweep stale AFK notices once the map is large enough to matter.
AFK_PRUNE_AT = 5_000
AFK_MAX_AGE = 7 * 86400


class GiveawayView(discord.ui.View):
    def __init__(self, bot: KevinBot) -> None:
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Enter giveaway",
        emoji="🎉",
        style=discord.ButtonStyle.success,
        custom_id="kevin:giveaway:enter",
    )
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not interaction.message or interaction.user.bot:
            return
        giveaway = await self.bot.db.fetchone(
            "SELECT ended, end_at FROM giveaways WHERE message_id = ?", (interaction.message.id,)
        )
        if giveaway and not giveaway["ended"]:
            # The worker only sweeps every 20 seconds; do not take entries in that gap.
            deadline = discord.utils.parse_time(giveaway["end_at"])
            expired = deadline is not None and deadline <= discord.utils.utcnow()
        else:
            expired = True
        if expired:
            await interaction.response.send_message("This giveaway has ended.", ephemeral=True)
            return
        await self.bot.db.execute(
            "INSERT OR IGNORE INTO giveaway_entries(message_id, user_id) VALUES (?, ?)",
            (interaction.message.id, interaction.user.id),
        )
        count = await self.bot.db.fetchone(
            "SELECT COUNT(*) AS count FROM giveaway_entries WHERE message_id = ?",
            (interaction.message.id,),
        )
        await interaction.response.send_message(
            f"You're entered! There are **{count['count']}** entries.", ephemeral=True
        )


class Community(commands.Cog):
    """Giveaways, reaction roles, starboard, suggestions, tags, and AFK status."""

    def __init__(self, bot: KevinBot) -> None:
        self.bot = bot
        self.afk_users: dict[tuple[int, int], tuple[str, float]] = {}

    async def cog_load(self) -> None:
        self.bot.add_view(GiveawayView(self.bot))
        self.giveaway_worker.start()

    async def cog_unload(self) -> None:
        self.giveaway_worker.cancel()

    @commands.hybrid_group(
        name="giveaway", fallback="list", description="Create and manage giveaways"
    )
    @commands.guild_only()
    @owner_or_guild_permissions(manage_guild=True)
    async def giveaway(self, ctx: commands.Context) -> None:
        rows = await self.bot.db.fetchall(
            "SELECT * FROM giveaways WHERE guild_id = ? AND ended = 0 ORDER BY end_at LIMIT 10",
            (ctx.guild.id,),
        )
        lines = [
            f"[**{row['prize']}**](https://discord.com/channels/{ctx.guild.id}/{row['channel_id']}/{row['message_id']}) · ends <t:{int(discord.utils.parse_time(row['end_at']).timestamp())}:R>"
            for row in rows
        ]
        await ctx.send(embed=embed("Active giveaways", "\n".join(lines) or "None active."))

    @giveaway.command(name="start", description="Start a button-entry giveaway")
    @app_commands.describe(duration="Examples: 10m, 2h, 3d", winners="Number of winners")
    @owner_or_guild_permissions(manage_guild=True)
    async def giveaway_start(
        self,
        ctx: commands.Context,
        duration: str,
        winners: commands.Range[int, 1, 20],
        *,
        prize: str,
    ) -> None:
        try:
            delta = parse_duration(duration)
        except ValueError as exc:
            raise commands.BadArgument(str(exc)) from exc
        if delta > timedelta(days=30):
            raise commands.BadArgument("Giveaways cannot run longer than 30 days.")
        end_at = discord.utils.utcnow() + delta
        card = embed(
            "🎉 Giveaway",
            f"## {prize[:500]}\nPress the button below to enter.\n\n"
            f"Ends {discord.utils.format_dt(end_at, 'R')} · **{winners} winner{'s' if winners != 1 else ''}**",
        )
        card.set_footer(text=f"Hosted by {ctx.author}")
        message = await ctx.send(embed=card, view=GiveawayView(self.bot))
        await self.bot.db.execute(
            "INSERT INTO giveaways(message_id, guild_id, channel_id, host_id, prize, winner_count, end_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                message.id,
                ctx.guild.id,
                ctx.channel.id,
                ctx.author.id,
                prize[:500],
                winners,
                end_at.isoformat(),
            ),
        )

    @giveaway.command(name="end", description="End a giveaway early")
    @owner_or_guild_permissions(manage_guild=True)
    async def giveaway_end(self, ctx: commands.Context, message_id: str) -> None:
        try:
            giveaway_id = int(message_id)
        except ValueError as exc:
            raise commands.BadArgument("Provide a giveaway message ID.") from exc
        row = await self.bot.db.fetchone(
            "SELECT * FROM giveaways WHERE message_id = ? AND guild_id = ? AND ended = 0",
            (giveaway_id, ctx.guild.id),
        )
        if not row:
            raise commands.BadArgument("That active giveaway was not found.")
        await self.finish_giveaway(dict(row))
        await ctx.send(embed=success("Giveaway ended."), ephemeral=True)

    @giveaway.command(name="reroll", description="Choose new winners for an ended giveaway")
    @owner_or_guild_permissions(manage_guild=True)
    async def giveaway_reroll(self, ctx: commands.Context, message_id: str) -> None:
        try:
            giveaway_id = int(message_id)
        except ValueError as exc:
            raise commands.BadArgument("Provide a giveaway message ID.") from exc
        row = await self.bot.db.fetchone(
            "SELECT * FROM giveaways WHERE message_id = ? AND guild_id = ? AND ended = 1",
            (giveaway_id, ctx.guild.id),
        )
        if not row:
            raise commands.BadArgument("That ended giveaway was not found.")
        entrants = await self.bot.db.fetchall(
            "SELECT user_id FROM giveaway_entries WHERE message_id = ?", (giveaway_id,)
        )
        if not entrants:
            raise commands.CheckFailure("That giveaway had no entrants.")
        winners = RNG.sample(
            [int(item["user_id"]) for item in entrants], min(row["winner_count"], len(entrants))
        )
        await ctx.send(
            f"🎉 New winner{'s' if len(winners) != 1 else ''} for **{row['prize']}**: "
            + ", ".join(f"<@{winner}>" for winner in winners)
        )

    async def finish_giveaway(self, giveaway: dict) -> None:
        entrants = await self.bot.db.fetchall(
            "SELECT user_id FROM giveaway_entries WHERE message_id = ?", (giveaway["message_id"],)
        )
        winner_ids = RNG.sample(
            [int(row["user_id"]) for row in entrants],
            min(int(giveaway["winner_count"]), len(entrants)),
        )
        await self.bot.db.execute(
            "UPDATE giveaways SET ended = 1 WHERE message_id = ?", (giveaway["message_id"],)
        )
        channel = self.bot.get_channel(giveaway["channel_id"])
        if isinstance(channel, discord.abc.Messageable):
            result = (
                ", ".join(f"<@{winner}>" for winner in winner_ids)
                if winner_ids
                else "No valid entries"
            )
            try:
                message = await channel.fetch_message(giveaway["message_id"])
                card = message.embeds[0].copy() if message.embeds else embed("🎉 Giveaway")
                card.description = f"## {giveaway['prize']}\n\n**Ended** · Winners: {result}"
                await message.edit(embed=card, view=None)
                await channel.send(f"🎉 {result} — you won **{giveaway['prize']}**!")
            except discord.HTTPException:
                pass

    @tasks.loop(seconds=20)
    async def giveaway_worker(self) -> None:
        rows = await self.bot.db.fetchall(
            "SELECT * FROM giveaways WHERE ended = 0 AND end_at <= ? LIMIT 20",
            (discord.utils.utcnow().isoformat(),),
        )
        for row in rows:
            await self.finish_giveaway(dict(row))

    @giveaway_worker.before_loop
    async def before_giveaways(self) -> None:
        await self.bot.wait_until_ready()

    @commands.hybrid_group(
        name="reactionrole",
        fallback="list",
        description="Configure message reaction roles",
    )
    @commands.guild_only()
    @owner_or_guild_permissions(manage_roles=True)
    async def reactionrole(self, ctx: commands.Context) -> None:
        rows = await self.bot.db.fetchall(
            "SELECT * FROM reaction_roles WHERE guild_id = ? LIMIT 25", (ctx.guild.id,)
        )
        lines = [f"{row['emoji']} on `{row['message_id']}` → <@&{row['role_id']}>" for row in rows]
        await ctx.send(embed=embed("Reaction roles", "\n".join(lines) or "None configured."))

    @reactionrole.command(name="add", description="Map a message reaction to a role")
    @owner_or_guild_permissions(manage_roles=True)
    @commands.bot_has_guild_permissions(manage_roles=True)
    async def reactionrole_add(
        self, ctx: commands.Context, message_id: str, emoji: str, role: discord.Role
    ) -> None:
        if role >= ctx.guild.me.top_role:
            raise commands.BadArgument("That role must be below K's highest role.")
        try:
            message = await ctx.channel.fetch_message(int(message_id))
            await message.add_reaction(emoji)
        except (ValueError, discord.HTTPException) as exc:
            raise commands.BadArgument("I couldn't find that message or use that emoji.") from exc
        await self.bot.db.execute(
            "INSERT OR REPLACE INTO reaction_roles(guild_id, message_id, emoji, role_id) VALUES (?, ?, ?, ?)",
            (ctx.guild.id, message.id, emoji, role.id),
        )
        await ctx.send(embed=success(f"Reacting with {emoji} now gives {role.mention}."))

    @reactionrole.command(name="remove", description="Remove a reaction-role mapping")
    @owner_or_guild_permissions(manage_roles=True)
    async def reactionrole_remove(self, ctx: commands.Context, message_id: str, emoji: str) -> None:
        try:
            numeric_id = int(message_id)
        except ValueError as exc:
            raise commands.BadArgument("Provide a message ID.") from exc
        await self.bot.db.execute(
            "DELETE FROM reaction_roles WHERE guild_id = ? AND message_id = ? AND emoji = ?",
            (ctx.guild.id, numeric_id, emoji),
        )
        await ctx.send(embed=success("Reaction-role mapping removed."))

    async def apply_reaction_role(self, payload: discord.RawReactionActionEvent, add: bool) -> None:
        if not payload.guild_id or payload.user_id == (self.bot.user.id if self.bot.user else None):
            return
        row = await self.bot.db.fetchone(
            "SELECT role_id FROM reaction_roles WHERE guild_id = ? AND message_id = ? AND emoji = ?",
            (payload.guild_id, payload.message_id, str(payload.emoji)),
        )
        guild = self.bot.get_guild(payload.guild_id)
        if not row or not guild:
            return
        role = guild.get_role(row["role_id"])
        member = payload.member or guild.get_member(payload.user_id)
        if not role or not member or member.bot:
            return
        try:
            if add:
                await member.add_roles(role, reason="K reaction role")
            else:
                await member.remove_roles(role, reason="K reaction role")
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        await self.apply_reaction_role(payload, True)
        if str(payload.emoji) == "⭐":
            await self.update_starboard(payload)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        await self.apply_reaction_role(payload, False)
        if str(payload.emoji) == "⭐":
            await self.update_starboard(payload)

    @commands.hybrid_group(
        name="starboard", fallback="status", description="Configure highlighted messages"
    )
    @commands.guild_only()
    @owner_or_guild_permissions(manage_guild=True)
    async def starboard(self, ctx: commands.Context) -> None:
        settings = await self.bot.db.get_settings(ctx.guild.id)
        channel = (
            f"<#{settings['starboard_channel_id']}>"
            if settings.get("starboard_channel_id")
            else "Disabled"
        )
        await ctx.send(
            embed=embed(
                "Starboard",
                f"Channel: {channel}\nThreshold: **{settings.get('starboard_threshold', 3)} ⭐**",
            )
        )

    @starboard.command(name="set", description="Set the starboard channel and threshold")
    @owner_or_guild_permissions(manage_guild=True)
    async def starboard_set(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel | None,
        threshold: commands.Range[int, 1, 25] = 3,
    ) -> None:
        await self.bot.db.set_setting(
            ctx.guild.id, "starboard_channel_id", channel.id if channel else None
        )
        await self.bot.db.set_setting(ctx.guild.id, "starboard_threshold", threshold)
        await ctx.send(
            embed=success(
                f"Starboard {'set to ' + channel.mention if channel else 'disabled'} at {threshold} stars."
            )
        )

    async def update_starboard(self, payload: discord.RawReactionActionEvent) -> None:
        if not payload.guild_id:
            return
        settings = await self.bot.db.get_settings(payload.guild_id)
        target_id = settings.get("starboard_channel_id")
        if not target_id or payload.channel_id == target_id:
            return
        guild = self.bot.get_guild(payload.guild_id)
        source = guild.get_channel(payload.channel_id) if guild else None
        target = guild.get_channel(target_id) if guild else None
        if not isinstance(source, discord.TextChannel) or not isinstance(
            target, discord.TextChannel
        ):
            return
        try:
            message = await source.fetch_message(payload.message_id)
        except discord.HTTPException:
            return
        reaction = discord.utils.get(message.reactions, emoji="⭐")
        count = reaction.count if reaction else 0
        stored = await self.bot.db.fetchone(
            "SELECT starboard_message_id FROM starred_messages WHERE source_message_id = ?",
            (message.id,),
        )
        threshold = int(settings.get("starboard_threshold") or 3)
        if count < threshold:
            if stored:
                try:
                    star = await target.fetch_message(stored["starboard_message_id"])
                    await star.delete()
                except discord.HTTPException:
                    pass
                await self.bot.db.execute(
                    "DELETE FROM starred_messages WHERE source_message_id = ?", (message.id,)
                )
            return
        card = embed("", message.content[:2000] or "*(attachment)*")
        card.set_author(name=str(message.author), icon_url=message.author.display_avatar.url)
        if (
            message.attachments
            and message.attachments[0].content_type
            and message.attachments[0].content_type.startswith("image/")
        ):
            card.set_image(url=message.attachments[0].url)
        card.add_field(name="Original", value=f"[Jump to message]({message.jump_url})")
        card.timestamp = message.created_at
        content = f"⭐ **{count}** · {source.mention}"
        try:
            if stored:
                star = await target.fetch_message(stored["starboard_message_id"])
                await star.edit(content=content, embed=card)
            else:
                star = await target.send(content, embed=card)
                await self.bot.db.execute(
                    "INSERT INTO starred_messages(source_message_id, guild_id, starboard_message_id) VALUES (?, ?, ?)",
                    (message.id, guild.id, star.id),
                )
        except discord.HTTPException:
            pass

    @commands.hybrid_command(description="Send a suggestion to the configured channel")
    @commands.guild_only()
    @commands.cooldown(1, 60, commands.BucketType.user)
    async def suggest(self, ctx: commands.Context, *, suggestion: str) -> None:
        settings = await self.bot.db.get_settings(ctx.guild.id)
        channel_id = settings.get("suggestion_channel_id")
        channel = ctx.guild.get_channel(channel_id) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            raise commands.CheckFailure("Staff have not configured a suggestion channel.")
        card = embed("Community suggestion", suggestion[:2000])
        card.set_author(name=str(ctx.author), icon_url=ctx.author.display_avatar.url)
        card.set_footer(text=f"User ID: {ctx.author.id}")
        message = await channel.send(embed=card)
        await message.add_reaction("👍")
        await message.add_reaction("👎")
        await ctx.send(embed=success(f"Suggestion posted in {channel.mention}."), ephemeral=True)

    @commands.hybrid_command(description="Set or disable the suggestion channel")
    @commands.guild_only()
    @owner_or_guild_permissions(manage_guild=True)
    async def suggestionchannel(
        self, ctx: commands.Context, channel: discord.TextChannel | None = None
    ) -> None:
        await self.bot.db.set_setting(
            ctx.guild.id, "suggestion_channel_id", channel.id if channel else None
        )
        await ctx.send(
            embed=success(
                f"Suggestions {'will go to ' + channel.mention if channel else 'are disabled'}."
            )
        )

    @commands.hybrid_group(name="tag", fallback="get", description="Use a saved server response")
    @commands.guild_only()
    async def tag(self, ctx: commands.Context, name: str) -> None:
        normalized = name.casefold().strip()[:50]
        row = await self.bot.db.fetchone(
            "SELECT content FROM tags WHERE guild_id = ? AND name = ?", (ctx.guild.id, normalized)
        )
        if not row:
            raise commands.BadArgument("That tag does not exist.")
        await self.bot.db.execute(
            "UPDATE tags SET uses = uses + 1 WHERE guild_id = ? AND name = ?",
            (ctx.guild.id, normalized),
        )
        await ctx.send(row["content"], allowed_mentions=discord.AllowedMentions.none())

    @tag.command(name="create", description="Create a reusable server tag")
    @owner_or_guild_permissions(manage_messages=True)
    async def tag_create(self, ctx: commands.Context, name: str, *, content: str) -> None:
        normalized = name.casefold().strip()[:50]
        if not normalized or not content.strip():
            raise commands.BadArgument("Tag name and content cannot be empty.")
        try:
            await self.bot.db.execute(
                "INSERT INTO tags(guild_id, name, content, owner_id) VALUES (?, ?, ?, ?)",
                (ctx.guild.id, normalized, content[:1800], ctx.author.id),
            )
        except Exception as exc:
            if "UNIQUE" in str(exc):
                raise commands.BadArgument("A tag with that name already exists.") from exc
            raise
        await ctx.send(embed=success(f"Created tag `{normalized}`."))

    @tag.command(name="delete", description="Delete a server tag")
    @owner_or_guild_permissions(manage_messages=True)
    async def tag_delete(self, ctx: commands.Context, name: str) -> None:
        await self.bot.db.execute(
            "DELETE FROM tags WHERE guild_id = ? AND name = ?",
            (ctx.guild.id, name.casefold().strip()),
        )
        await ctx.send(embed=success("Tag deleted."))

    @tag.command(name="list", description="List server tags")
    async def tag_list(self, ctx: commands.Context) -> None:
        rows = await self.bot.db.fetchall(
            "SELECT name, uses FROM tags WHERE guild_id = ? ORDER BY uses DESC, name LIMIT 50",
            (ctx.guild.id,),
        )
        await ctx.send(
            embed=embed(
                "Server tags",
                " · ".join(f"`{row['name']}` ({row['uses']})" for row in rows) or "No tags yet.",
            )
        )

    def prune_afk(self) -> None:
        """Drop week-old notices so the map cannot grow without bound."""
        if len(self.afk_users) < AFK_PRUNE_AT:
            return
        cutoff = time.time() - AFK_MAX_AGE
        for key in [k for k, (_, since) in self.afk_users.items() if since < cutoff]:
            del self.afk_users[key]

    @commands.hybrid_command(description="Set an AFK notice that clears when you return")
    @commands.guild_only()
    async def afk(self, ctx: commands.Context, *, reason: str = "AFK") -> None:
        self.prune_afk()
        self.afk_users[(ctx.guild.id, ctx.author.id)] = (reason[:300], time.time())
        await ctx.send(embed=success(f"AFK status set: {reason[:300]}"))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not message.guild or message.author.bot:
            return
        key = (message.guild.id, message.author.id)
        if key in self.afk_users:
            self.afk_users.pop(key)
            try:
                await message.channel.send(
                    f"Welcome back, {message.author.mention}. I cleared your AFK status.",
                    delete_after=6,
                )
            except discord.HTTPException:
                pass
        notices = []
        for mentioned in message.mentions[:5]:
            afk = self.afk_users.get((message.guild.id, mentioned.id))
            if afk:
                reason, since = afk
                notices.append(
                    f"**{mentioned.display_name}** is AFK: {reason} (<t:{int(since)}:R>)"
                )
        if notices:
            try:
                await message.reply("\n".join(notices), delete_after=12, mention_author=False)
            except discord.HTTPException:
                pass


async def setup(bot: KevinBot) -> None:
    await bot.add_cog(Community(bot))
