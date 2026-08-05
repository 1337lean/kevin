from __future__ import annotations

import hashlib
import random

import discord
from discord import app_commands
from discord.ext import commands

from kevin.bot import KevinBot
from kevin.utils.formatting import embed

RNG = random.SystemRandom()


class Fun(commands.Cog):
    """Lightweight social commands and games."""

    def __init__(self, bot: KevinBot) -> None:
        self.bot = bot

    @commands.hybrid_command(aliases=["8ball"], description="Ask the magic 8-ball a question")
    async def eightball(self, ctx: commands.Context, *, question: str) -> None:
        responses = (
            "It is certain.",
            "Without a doubt.",
            "Signs point to yes.",
            "Most likely.",
            "Ask again later.",
            "Better not tell you now.",
            "Cannot predict now.",
            "Don't count on it.",
            "My sources say no.",
            "Very doubtful.",
        )
        await ctx.send(
            embed=embed(
                "🎱 The 8-ball says…",
                f"**Question:** {question[:500]}\n**Answer:** {RNG.choice(responses)}",
            )
        )

    @commands.hybrid_command(description="Roll dice using notation such as 2d20")
    async def roll(self, ctx: commands.Context, dice: str = "1d6") -> None:
        try:
            count_text, sides_text = dice.lower().split("d", 1)
            count, sides = int(count_text), int(sides_text)
        except (ValueError, AttributeError) as exc:
            raise commands.BadArgument("Use dice notation such as `2d20`.") from exc
        if not 1 <= count <= 50 or not 2 <= sides <= 10_000:
            raise commands.BadArgument("Use 1–50 dice with 2–10,000 sides.")
        results = [RNG.randint(1, sides) for _ in range(count)]
        await ctx.send(
            embed=embed(
                f"🎲 {dice}", f"{', '.join(map(str, results))}\n**Total: {sum(results):,}**"
            )
        )

    @commands.hybrid_command(description="Choose one option separated by vertical bars")
    @app_commands.describe(options="Example: pizza | tacos | sushi")
    async def choose(self, ctx: commands.Context, *, options: str) -> None:
        choices = [choice.strip() for choice in options.split("|") if choice.strip()]
        if len(choices) < 2:
            raise commands.BadArgument("Give at least two choices separated with `|`.")
        await ctx.send(embed=embed("K chooses…", f"**{RNG.choice(choices)[:1000]}**"))

    @commands.hybrid_command(description="Play rock, paper, scissors")
    async def rps(self, ctx: commands.Context, choice: str) -> None:
        choice = choice.lower()
        options = {"rock": "🪨", "paper": "📄", "scissors": "✂️"}
        if choice not in options:
            raise commands.BadArgument("Choose rock, paper, or scissors.")
        kevin = RNG.choice(tuple(options))
        beats = {"rock": "scissors", "paper": "rock", "scissors": "paper"}
        result = (
            "Tie!" if choice == kevin else "You win!" if beats[choice] == kevin else "K wins!"
        )
        await ctx.send(
            embed=embed(
                "Rock, paper, scissors",
                f"You: {options[choice]} · K: {options[kevin]}\n**{result}**",
            )
        )

    @commands.hybrid_command(description="Get a truly questionable joke")
    async def joke(self, ctx: commands.Context) -> None:
        jokes = (
            "Why did the developer go broke? They used up all their cache.",
            "I told my computer I needed a break. It said: no problem, I'll go to sleep.",
            "There are 10 kinds of people: those who understand binary and those who don't.",
            "Why did the robot join Discord? It was looking for a byte-sized community.",
            "A SQL query walks into a bar, sees two tables, and asks: may I join you?",
        )
        await ctx.send(embed=embed("Certified K joke", RNG.choice(jokes)))

    @commands.hybrid_command(description="Generate a reproducible compatibility score")
    async def ship(
        self, ctx: commands.Context, first: discord.Member, second: discord.Member
    ) -> None:
        low, high = sorted((first.id, second.id))
        digest = hashlib.sha256(f"{ctx.guild.id}:{low}:{high}".encode()).digest()
        score = digest[0] * 100 // 255
        hearts = "❤️" * round(score / 20) + "🖤" * (5 - round(score / 20))
        await ctx.send(
            embed=embed(
                "Compatibility meter",
                f"{first.mention} × {second.mention}\n**{score}%** · {hearts}",
            )
        )

    @commands.hybrid_command(description="Give something K's completely scientific rating")
    async def rate(self, ctx: commands.Context, *, thing: str) -> None:
        digest = hashlib.sha256(f"{ctx.guild.id}:{thing.casefold()}".encode()).digest()
        score = digest[0] % 11
        await ctx.send(embed=embed("K rates it", f"**{thing[:500]}** gets **{score}/10**."))

    @commands.hybrid_command(description="Turn text into alternating case")
    async def mock(self, ctx: commands.Context, *, text: str) -> None:
        result = "".join(
            c.upper() if index % 2 else c.lower() for index, c in enumerate(text[:1500])
        )
        await ctx.send(result, allowed_mentions=discord.AllowedMentions.none())

    @commands.hybrid_command(description="Send an action to another member")
    @app_commands.choices(
        action=[
            app_commands.Choice(name="hug", value="hugged"),
            app_commands.Choice(name="high five", value="high-fived"),
            app_commands.Choice(name="boop", value="booped"),
            app_commands.Choice(name="wave", value="waved at"),
        ]
    )
    async def action(self, ctx: commands.Context, action: str, member: discord.Member) -> None:
        await ctx.send(
            embed=embed("Social", f"{ctx.author.mention} **{action}** {member.mention}!")
        )


async def setup(bot: KevinBot) -> None:
    await bot.add_cog(Fun(bot))
