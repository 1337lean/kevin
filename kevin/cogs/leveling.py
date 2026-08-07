from __future__ import annotations

import random
import time

import discord
from discord.ext import commands

from kevin.bot import KevinBot
from kevin.utils.checks import owner_or_guild_permissions
from kevin.utils.formatting import embed, progress_bar, success

XP_COOLDOWN = 60.0
# Only sweep the cooldown map once it is large enough to matter.
COOLDOWN_PRUNE_AT = 10_000


def xp_for_level(level: int) -> int:
    """Total XP required to reach a level."""
    return 50 * level * level + 50 * level


def level_for_xp(xp: int) -> int:
    level = 0
    while xp_for_level(level + 1) <= xp:
        level += 1
    return level


class Leveling(commands.Cog):
    """Activity XP, ranks, and leaderboards."""

    def __init__(self, bot: KevinBot) -> None:
        self.bot = bot
        self.cooldowns: dict[tuple[int, int], float] = {}

    def prune_cooldowns(self, now: float) -> None:
        """Drop expired entries so a busy bot's cooldown map cannot grow forever."""
        if len(self.cooldowns) < COOLDOWN_PRUNE_AT:
            return
        for key in [k for k, stamp in self.cooldowns.items() if now - stamp >= XP_COOLDOWN]:
            del self.cooldowns[key]

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not message.guild or message.author.bot or len(message.content.strip()) < 3:
            return
        # Commands are processed in a separate task from this listener. Awarding
        # activity XP for them can race with commands such as setxp: the listener
        # may read the old value, setxp writes the requested value, and then this
        # listener overwrites it with the old value plus the message award.
        ctx = await self.bot.get_context(message)
        if ctx.valid:
            return
        key = (message.guild.id, message.author.id)
        now = time.monotonic()
        previous_award = self.cooldowns.get(key)
        if previous_award is not None and now - previous_award < XP_COOLDOWN:
            return
        # Claim the slot before the first await below. Every message in a burst
        # otherwise clears this check while the earlier ones are still awaiting, and
        # each one goes on to collect its own award.
        self.cooldowns[key] = now
        self.prune_cooldowns(now)
        settings = await self.bot.db.get_settings(message.guild.id)
        if not settings.get("levels_enabled"):
            return
        new_xp, old_level, new_level = await self.bot.db.add_xp(
            *key, random.randint(15, 25), level_for_xp
        )
        if new_level > old_level:
            try:
                await message.channel.send(
                    embed=embed(
                        "Level up!",
                        f"{message.author.mention} reached **level {new_level}**!",
                    ),
                    delete_after=12,
                )
            except discord.HTTPException:
                pass

    @commands.hybrid_command(description="Show your or another member's rank")
    @commands.guild_only()
    async def rank(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        member = member or ctx.author
        await self.bot.db.ensure_member(ctx.guild.id, member.id)
        row = await self.bot.db.fetchone(
            "SELECT xp, level FROM members WHERE guild_id = ? AND user_id = ?",
            (ctx.guild.id, member.id),
        )
        placement = await self.bot.db.fetchone(
            "SELECT COUNT(*) + 1 AS rank FROM members WHERE guild_id = ? AND xp > ?",
            (ctx.guild.id, row["xp"]),
        )
        level = int(row["level"])
        start, end = xp_for_level(level), xp_for_level(level + 1)
        card = embed(f"Rank · {member.display_name}")
        card.set_thumbnail(url=member.display_avatar.url)
        card.add_field(name="Server rank", value=f"#{placement['rank']}")
        card.add_field(name="Level", value=str(level))
        card.add_field(name="Total XP", value=f"{row['xp']:,}")
        card.add_field(
            name="Progress",
            value=f"{progress_bar(int(row['xp']) - start, end - start)}\n{int(row['xp']) - start:,} / {end - start:,} XP",
            inline=False,
        )
        await ctx.send(embed=card)

    @commands.hybrid_command(aliases=["levels", "lb"], description="Show the server XP leaderboard")
    @commands.guild_only()
    async def leaderboard(self, ctx: commands.Context) -> None:
        rows = await self.bot.db.fetchall(
            "SELECT user_id, xp, level FROM members WHERE guild_id = ? ORDER BY xp DESC LIMIT 10",
            (ctx.guild.id,),
        )
        if not rows:
            await ctx.send(embed=embed("XP leaderboard", "No one has earned XP yet."))
            return
        lines = [
            f"**{index}.** <@{row['user_id']}> — level {row['level']} · {row['xp']:,} XP"
            for index, row in enumerate(rows, 1)
        ]
        await ctx.send(embed=embed("XP leaderboard", "\n".join(lines)))

    @commands.hybrid_command(description="Set a member's XP (administrator)")
    @commands.guild_only()
    @owner_or_guild_permissions(administrator=True)
    async def setxp(self, ctx: commands.Context, member: discord.Member, xp: int) -> None:
        if not 0 <= xp <= 100_000_000:
            raise commands.BadArgument("XP must be between 0 and 100,000,000.")
        await self.bot.db.ensure_member(ctx.guild.id, member.id)
        await self.bot.db.execute(
            "UPDATE members SET xp = ?, level = ? WHERE guild_id = ? AND user_id = ?",
            (xp, level_for_xp(xp), ctx.guild.id, member.id),
        )
        await ctx.send(embed=success(f"Set **{member}** to {xp:,} XP."))


async def setup(bot: KevinBot) -> None:
    await bot.add_cog(Leveling(bot))
