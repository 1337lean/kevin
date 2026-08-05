from __future__ import annotations

import ast
import operator
from datetime import UTC, datetime

import discord
from discord import app_commands
from discord.ext import commands, tasks

from kevin.bot import KevinBot
from kevin.utils.formatting import embed, human_duration, parse_duration, success


class PollView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=86400)
        self.votes: dict[int, bool] = {}

    async def update(self, interaction: discord.Interaction, vote: bool) -> None:
        self.votes[interaction.user.id] = vote
        yes = sum(self.votes.values())
        no = len(self.votes) - yes
        await interaction.response.send_message(
            f"Vote recorded. Current tally: ✅ {yes} · ❌ {no}", ephemeral=True
        )

    @discord.ui.button(label="Yes", emoji="✅", style=discord.ButtonStyle.success)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.update(interaction, True)

    @discord.ui.button(label="No", emoji="❌", style=discord.ButtonStyle.danger)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.update(interaction, False)


OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def safe_calculate(expression: str) -> int | float:
    if len(expression) > 100:
        raise ValueError("Expression is too long")

    def evaluate(node: ast.AST) -> int | float:
        if isinstance(node, ast.Expression):
            return evaluate(node.body)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in OPS:
            left, right = evaluate(node.left), evaluate(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 10:
                raise ValueError("Exponent is too large")
            result = OPS[type(node.op)](left, right)
            if abs(result) > 1e100:
                raise ValueError("Result is too large")
            return result
        if isinstance(node, ast.UnaryOp) and type(node.op) in OPS:
            return OPS[type(node.op)](evaluate(node.operand))
        raise ValueError("Only basic arithmetic is allowed")

    return evaluate(ast.parse(expression, mode="eval"))


class Utility(commands.Cog):
    """Information, polls, reminders, and calculation tools."""

    def __init__(self, bot: KevinBot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.reminder_worker.start()

    async def cog_unload(self) -> None:
        self.reminder_worker.cancel()

    @commands.hybrid_command(description="Show information about this server")
    @commands.guild_only()
    async def serverinfo(self, ctx: commands.Context) -> None:
        guild = ctx.guild
        humans = sum(not member.bot for member in guild.members)
        bots = len(guild.members) - humans
        card = embed(guild.name, guild.description or "No server description.")
        if guild.icon:
            card.set_thumbnail(url=guild.icon.url)
        card.add_field(
            name="Owner", value=guild.owner.mention if guild.owner else str(guild.owner_id)
        )
        card.add_field(name="Members", value=f"{humans:,} people · {bots:,} bots")
        card.add_field(
            name="Channels",
            value=f"{len(guild.text_channels)} text · {len(guild.voice_channels)} voice",
        )
        card.add_field(name="Roles", value=str(len(guild.roles)))
        card.add_field(
            name="Boosts",
            value=f"Level {guild.premium_tier} · {guild.premium_subscription_count or 0}",
        )
        card.add_field(name="Created", value=discord.utils.format_dt(guild.created_at, "R"))
        card.set_footer(text=f"Server ID: {guild.id}")
        await ctx.send(embed=card)

    @commands.hybrid_command(description="Show information about a member")
    @commands.guild_only()
    async def userinfo(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        member = member or ctx.author
        roles = [role.mention for role in reversed(member.roles[1:])]
        card = embed(str(member), member.mention)
        card.set_thumbnail(url=member.display_avatar.url)
        card.add_field(
            name="Joined",
            value=discord.utils.format_dt(member.joined_at, "R") if member.joined_at else "Unknown",
        )
        card.add_field(name="Created", value=discord.utils.format_dt(member.created_at, "R"))
        card.add_field(name="Top role", value=member.top_role.mention)
        card.add_field(
            name=f"Roles ({len(roles)})", value=" ".join(roles)[:1000] or "None", inline=False
        )
        card.set_footer(text=f"User ID: {member.id}")
        await ctx.send(embed=card)

    @commands.hybrid_command(description="Show a user's avatar")
    async def avatar(self, ctx: commands.Context, member: discord.User | None = None) -> None:
        member = member or ctx.author
        card = embed(f"Avatar · {member}")
        card.set_image(url=member.display_avatar.with_size(1024).url)
        await ctx.send(embed=card)

    @commands.hybrid_command(description="Create a yes/no poll")
    async def poll(self, ctx: commands.Context, *, question: str) -> None:
        card = embed("📊 Poll", question[:2000])
        card.set_footer(text=f"Started by {ctx.author}")
        await ctx.send(embed=card, view=PollView())

    @commands.hybrid_command(
        aliases=["calc"], description="Safely calculate an arithmetic expression"
    )
    async def calculate(self, ctx: commands.Context, *, expression: str) -> None:
        try:
            result = safe_calculate(expression)
        except (ValueError, SyntaxError, ZeroDivisionError, OverflowError) as exc:
            raise commands.BadArgument(str(exc)) from exc
        await ctx.send(embed=embed("Calculator", f"`{expression}` = **{result:,}**"))

    @commands.hybrid_command(description="Create a personal reminder")
    @app_commands.describe(duration="For example: 10m, 2h, or 3d")
    async def remind(self, ctx: commands.Context, duration: str, *, message: str) -> None:
        try:
            delta = parse_duration(duration)
        except ValueError as exc:
            raise commands.BadArgument(str(exc)) from exc
        if delta.total_seconds() > 365 * 86400:
            raise commands.BadArgument("Reminders cannot be more than one year away.")
        due = discord.utils.utcnow() + delta
        await self.bot.db.execute(
            "INSERT INTO reminders(user_id, channel_id, guild_id, message, due_at) VALUES (?, ?, ?, ?, ?)",
            (
                ctx.author.id,
                ctx.channel.id,
                ctx.guild.id if ctx.guild else None,
                message[:1500],
                due.isoformat(),
            ),
        )
        await ctx.send(
            embed=success(f"I'll remind you in **{human_duration(delta)}**."), ephemeral=True
        )

    @commands.hybrid_command(description="Generate a Discord timestamp")
    @app_commands.choices(
        style=[
            app_commands.Choice(name="Relative", value="R"),
            app_commands.Choice(name="Long date/time", value="F"),
            app_commands.Choice(name="Short date/time", value="f"),
            app_commands.Choice(name="Date", value="D"),
            app_commands.Choice(name="Time", value="T"),
        ]
    )
    async def timestamp(
        self, ctx: commands.Context, unix_time: int | None = None, style: str = "R"
    ) -> None:
        stamp = unix_time or int(datetime.now(UTC).timestamp())
        rendered = f"<t:{stamp}:{style}>"
        await ctx.send(embed=embed("Discord timestamp", f"{rendered}\n`{rendered}`"))

    @tasks.loop(seconds=15)
    async def reminder_worker(self) -> None:
        now = discord.utils.utcnow().isoformat()
        rows = await self.bot.db.fetchall(
            "SELECT * FROM reminders WHERE delivered = 0 AND due_at <= ? ORDER BY due_at LIMIT 25",
            (now,),
        )
        for row in rows:
            channel = self.bot.get_channel(row["channel_id"])
            delivered = False
            if isinstance(channel, discord.abc.Messageable):
                try:
                    await channel.send(f"<@{row['user_id']}> ⏰ **Reminder:** {row['message']}")
                    delivered = True
                except discord.HTTPException:
                    pass
            if not delivered:
                try:
                    user = await self.bot.fetch_user(row["user_id"])
                    await user.send(f"⏰ **Reminder:** {row['message']}")
                    delivered = True
                except discord.HTTPException:
                    pass
            if delivered:
                await self.bot.db.execute(
                    "UPDATE reminders SET delivered = 1 WHERE id = ?", (row["id"],)
                )

    @reminder_worker.before_loop
    async def before_reminders(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: KevinBot) -> None:
    await bot.add_cog(Utility(bot))
