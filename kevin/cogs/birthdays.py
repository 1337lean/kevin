from __future__ import annotations

import calendar
from datetime import date

import discord
from discord import app_commands
from discord.ext import commands, tasks

from kevin.bot import KevinBot
from kevin.utils.formatting import embed, success


def validate_birthday(month: int, day: int, year: int | None) -> None:
    """Raise ValueError unless the date is a real calendar date."""
    today = date.today()
    if not 1 <= month <= 12:
        raise ValueError("Month must be between 1 and 12.")
    longest = 29 if month == 2 else calendar.monthrange(2001, month)[1]
    if not 1 <= day <= longest:
        raise ValueError(f"Day must be between 1 and {longest} for that month.")
    if year is not None and not 1900 <= year <= today.year:
        raise ValueError(f"Year must be between 1900 and {today.year}.")
    if year is not None:
        try:
            birth_date = date(year, month, day)
        except ValueError as exc:
            raise ValueError("That birth date is not a real calendar date.") from exc
        if birth_date > today:
            raise ValueError("Birth date cannot be in the future.")


def is_birthday_today(month: int, day: int, today: date) -> bool:
    """Feb 29 birthdays are celebrated on Feb 28 in non-leap years."""
    if month == 2 and day == 29 and not calendar.isleap(today.year):
        return (today.month, today.day) == (2, 28)
    return (today.month, today.day) == (month, day)


def next_occurrence(month: int, day: int, today: date) -> date:
    """The next date this birthday is celebrated, today included."""
    year = today.year
    while True:
        try:
            candidate = date(year, month, day)
        except ValueError:
            # Feb 29 in a non-leap year is celebrated on Feb 28.
            candidate = date(year, 2, 28)
        if candidate >= today:
            return candidate
        year += 1


def turning_age(year: int | None, today: date) -> int | None:
    if year is None:
        return None
    return today.year - year


class Birthdays(commands.Cog):
    """Birthday tracking with daily announcements."""

    def __init__(self, bot: KevinBot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.birthday_worker.start()

    async def cog_unload(self) -> None:
        self.birthday_worker.cancel()

    @commands.hybrid_group(
        name="birthday", fallback="show", description="Track and celebrate birthdays"
    )
    @commands.guild_only()
    async def birthday(self, ctx: commands.Context) -> None:
        row = await self.bot.db.get_birthday(ctx.guild.id, ctx.author.id)
        if row is None:
            await ctx.send(
                embed=embed(
                    "Birthdays",
                    "You have no birthday set. Add one with `/birthday set month day [year]`.",
                )
            )
            return
        today = discord.utils.utcnow().date()
        next_date = next_occurrence(row["month"], row["day"], today)
        age = turning_age(row["year"], next_date)
        detail = f" You'll turn **{age}**." if age is not None else ""
        await ctx.send(
            embed=embed(
                "Your birthday",
                f"**{row['month']}/{row['day']}**"
                + (f"/{row['year']}" if row["year"] else "")
                + f" · next on **{next_date.strftime('%B %d')}**.{detail}",
            )
        )

    @birthday.command(name="set", description="Save your birthday for this server")
    @app_commands.describe(
        month="Month as a number, 1–12",
        day="Day of the month",
        year="Optional birth year, so K can announce ages",
    )
    async def birthday_set(
        self,
        ctx: commands.Context,
        month: commands.Range[int, 1, 12],
        day: commands.Range[int, 1, 31],
        year: commands.Range[int, 1900, 2100] | None = None,
    ) -> None:
        try:
            validate_birthday(month, day, year)
        except ValueError as exc:
            raise commands.BadArgument(str(exc)) from exc
        await self.bot.db.set_birthday(ctx.guild.id, ctx.author.id, month, day, year)
        shown = f"{month}/{day}" + (f"/{year}" if year else "")
        await ctx.send(embed=success(f"Birthday saved as **{shown}**."), ephemeral=True)

    @birthday.command(name="remove", description="Delete your birthday from this server")
    async def birthday_remove(self, ctx: commands.Context) -> None:
        removed = await self.bot.db.remove_birthday(ctx.guild.id, ctx.author.id)
        if removed:
            await ctx.send(embed=success("Your birthday was forgotten."), ephemeral=True)
        else:
            await ctx.send(embed=embed("Birthdays", "You had no birthday set."), ephemeral=True)

    @birthday.command(name="list", description="Show upcoming birthdays")
    async def birthday_list(self, ctx: commands.Context) -> None:
        rows = await self.bot.db.list_birthdays(ctx.guild.id)
        if not rows:
            await ctx.send(embed=embed("Birthdays", "No one has set a birthday yet."))
            return
        today = discord.utils.utcnow().date()
        upcoming = sorted(rows, key=lambda row: next_occurrence(row["month"], row["day"], today))
        lines = []
        for row in upcoming[:15]:
            when = next_occurrence(row["month"], row["day"], today)
            label = when.strftime("%b %d")
            if when == today:
                label = "Today!"
            member = ctx.guild.get_member(row["user_id"])
            name = member.display_name if member else f"<@{row['user_id']}>"
            lines.append(f"**{label}** · {name}")
        await ctx.send(embed=embed("Upcoming birthdays", "\n".join(lines)))

    @tasks.loop(minutes=10)
    async def birthday_worker(self) -> None:
        today = discord.utils.utcnow().date()
        rows = await self.bot.db.fetchall(
            "SELECT * FROM birthdays WHERE announced_year < ?", (today.year,)
        )
        by_guild: dict[int, list[dict]] = {}
        for row in rows:
            if is_birthday_today(row["month"], row["day"], today):
                by_guild.setdefault(row["guild_id"], []).append(dict(row))
        for guild_id, celebrants in by_guild.items():
            guild = self.bot.get_guild(guild_id)
            settings = await self.bot.db.get_settings(guild_id)
            channel_id = settings.get("birthday_channel_id")
            channel = self.bot.get_channel(channel_id) if channel_id else None
            if guild is None or not isinstance(channel, discord.abc.Messageable):
                continue
            for row in celebrants:
                member = guild.get_member(row["user_id"])
                if member is None:
                    continue
                age = turning_age(row["year"], today)
                suffix = f" They turn **{age}** today!" if age is not None else ""
                try:
                    await channel.send(
                        embed=embed("🎂 Birthday!", f"Happy birthday {member.mention}!{suffix}")
                    )
                    await self.bot.db.mark_birthday_announced(guild_id, row["user_id"], today.year)
                except discord.HTTPException:
                    pass

    @birthday_worker.before_loop
    async def before_birthdays(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: KevinBot) -> None:
    await bot.add_cog(Birthdays(bot))
