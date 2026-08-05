from __future__ import annotations

import asyncio
import random
from collections import defaultdict, deque
from dataclasses import dataclass

import discord
from discord import app_commands
from discord.ext import commands

from kevin.bot import KevinBot
from kevin.utils.formatting import embed

RNG = random.SystemRandom()
CATEGORIES = ("general", "science", "history", "geography", "entertainment", "technology")
DIFFICULTIES = ("easy", "medium", "hard")


@dataclass(frozen=True, slots=True)
class TriviaQuestion:
    category: str
    difficulty: str
    prompt: str
    answer: str
    distractors: tuple[str, str, str]


QUESTIONS = (
    TriviaQuestion("general", "easy", "How many sides does a hexagon have?", "6", ("5", "7", "8")),
    TriviaQuestion("general", "easy", "Which chess piece moves in an L shape?", "Knight", ("Bishop", "Rook", "Queen")),
    TriviaQuestion("general", "medium", "What is the Roman numeral for 50?", "L", ("C", "D", "X")),
    TriviaQuestion("general", "medium", "Which language has the most native speakers?", "Mandarin Chinese", ("English", "Spanish", "Hindi")),
    TriviaQuestion("general", "hard", "What is the study of flags called?", "Vexillology", ("Heraldry", "Numismatics", "Cartography")),
    TriviaQuestion("general", "hard", "Which number is the smallest perfect number?", "6", ("4", "8", "10")),
    TriviaQuestion("science", "easy", "Which planet is known as the Red Planet?", "Mars", ("Venus", "Jupiter", "Mercury")),
    TriviaQuestion("science", "easy", "What gas do plants absorb during photosynthesis?", "Carbon dioxide", ("Oxygen", "Nitrogen", "Hydrogen")),
    TriviaQuestion("science", "medium", "What is the chemical symbol for silver?", "Ag", ("Si", "Au", "Sr")),
    TriviaQuestion("science", "medium", "Which part of a cell contains most of its genetic material?", "Nucleus", ("Ribosome", "Cell membrane", "Cytoplasm")),
    TriviaQuestion("science", "hard", "What particle carries the electromagnetic force?", "Photon", ("Gluon", "Neutrino", "Boson W")),
    TriviaQuestion("science", "hard", "What is the SI unit of electrical capacitance?", "Farad", ("Henry", "Tesla", "Weber")),
    TriviaQuestion("history", "easy", "Which ancient civilization built Machu Picchu?", "Inca", ("Maya", "Aztec", "Roman")),
    TriviaQuestion("history", "easy", "The Renaissance began in which country?", "Italy", ("France", "England", "Spain")),
    TriviaQuestion("history", "medium", "In what year did the Berlin Wall fall?", "1989", ("1987", "1991", "1993")),
    TriviaQuestion("history", "medium", "Who was the first emperor of Rome?", "Augustus", ("Julius Caesar", "Nero", "Trajan")),
    TriviaQuestion("history", "hard", "The Peace of Westphalia ended which major conflict?", "Thirty Years' War", ("Seven Years' War", "War of the Roses", "Crimean War")),
    TriviaQuestion("history", "hard", "Which empire used the administrative language Quechua?", "Inca Empire", ("Mali Empire", "Ottoman Empire", "Khmer Empire")),
    TriviaQuestion("geography", "easy", "What is the capital of Canada?", "Ottawa", ("Toronto", "Vancouver", "Montreal")),
    TriviaQuestion("geography", "easy", "Which is the largest ocean on Earth?", "Pacific Ocean", ("Atlantic Ocean", "Indian Ocean", "Arctic Ocean")),
    TriviaQuestion("geography", "medium", "Which river runs through Budapest?", "Danube", ("Rhine", "Seine", "Elbe")),
    TriviaQuestion("geography", "medium", "Which country completely surrounds Lesotho?", "South Africa", ("Botswana", "Namibia", "Zimbabwe")),
    TriviaQuestion("geography", "hard", "What is the capital of Bhutan?", "Thimphu", ("Paro", "Kathmandu", "Dhaka")),
    TriviaQuestion("geography", "hard", "Which strait separates Asia from North America?", "Bering Strait", ("Bosporus", "Strait of Malacca", "Davis Strait")),
    TriviaQuestion("entertainment", "easy", "Which film features the song “Let It Go”?", "Frozen", ("Moana", "Tangled", "Encanto")),
    TriviaQuestion("entertainment", "easy", "What is the name of Mario's brother?", "Luigi", ("Wario", "Yoshi", "Toad")),
    TriviaQuestion("entertainment", "medium", "Who directed the film Spirited Away?", "Hayao Miyazaki", ("Satoshi Kon", "Makoto Shinkai", "Isao Takahata")),
    TriviaQuestion("entertainment", "medium", "Which band released the album Abbey Road?", "The Beatles", ("The Rolling Stones", "Queen", "Pink Floyd")),
    TriviaQuestion("entertainment", "hard", "Which composer wrote the opera The Magic Flute?", "Wolfgang Amadeus Mozart", ("Ludwig van Beethoven", "Giuseppe Verdi", "Richard Wagner")),
    TriviaQuestion("entertainment", "hard", "In Greek mythology, who is the muse of epic poetry?", "Calliope", ("Clio", "Erato", "Thalia")),
    TriviaQuestion("technology", "easy", "What does CPU stand for?", "Central Processing Unit", ("Computer Primary Utility", "Core Processing User", "Central Program Unit")),
    TriviaQuestion("technology", "easy", "Which company maintains the Android operating system?", "Google", ("Apple", "Microsoft", "IBM")),
    TriviaQuestion("technology", "medium", "What does HTTP status code 404 mean?", "Not Found", ("Unauthorized", "Server Error", "Request Timeout")),
    TriviaQuestion("technology", "medium", "Which data structure uses first-in, first-out order?", "Queue", ("Stack", "Tree", "Heap")),
    TriviaQuestion("technology", "hard", "Who introduced the concept of a universal Turing machine?", "Alan Turing", ("John von Neumann", "Claude Shannon", "Alonzo Church")),
    TriviaQuestion("technology", "hard", "What is the default port for secure HTTPS traffic?", "443", ("80", "22", "8080")),
)


def matching_questions(category: str, difficulty: str) -> list[tuple[int, TriviaQuestion]]:
    return [
        (index, question)
        for index, question in enumerate(QUESTIONS)
        if (category == "any" or question.category == category)
        and (difficulty == "any" or question.difficulty == difficulty)
    ]


class TriviaAnswerButton(discord.ui.Button["TriviaView"]):
    def __init__(self, index: int, label: str) -> None:
        super().__init__(label=label, style=discord.ButtonStyle.secondary, row=index // 2)
        self.answer_index = index

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.view:
            await self.view.answer(interaction, self.answer_index)


class TriviaView(discord.ui.View):
    def __init__(
        self,
        bot: KevinBot,
        author: discord.abc.User,
        guild_id: int,
        question: TriviaQuestion,
    ) -> None:
        super().__init__(timeout=30)
        self.bot = bot
        self.author = author
        self.guild_id = guild_id
        self.question = question
        self.answers = [question.answer, *question.distractors]
        RNG.shuffle(self.answers)
        self.correct_index = self.answers.index(question.answer)
        self.message: discord.Message | None = None
        self.answered = False
        self.answer_lock = asyncio.Lock()
        for index, answer in enumerate(self.answers):
            self.add_item(TriviaAnswerButton(index, answer))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.author.id:
            return True
        await interaction.response.send_message(
            "This question belongs to someone else—start your own with `/trivia play`!",
            ephemeral=True,
        )
        return False

    def disable_answers(self, selected: int | None = None) -> None:
        for item in self.children:
            if not isinstance(item, TriviaAnswerButton):
                continue
            item.disabled = True
            if item.answer_index == self.correct_index:
                item.style = discord.ButtonStyle.success
            elif item.answer_index == selected:
                item.style = discord.ButtonStyle.danger

    async def answer(self, interaction: discord.Interaction, selected: int) -> None:
        async with self.answer_lock:
            if self.answered:
                if not interaction.response.is_done():
                    await interaction.response.send_message("That question is already over.", ephemeral=True)
                return
            self.answered = True
            is_correct = selected == self.correct_index
            stats = await self.bot.db.record_trivia_answer(
                self.guild_id, self.author.id, correct=is_correct
            )
            self.disable_answers(selected)
            result = (
                f"✅ **Correct!** Your streak is **{stats['streak']}**."
                if is_correct
                else f"❌ **Not quite.** The answer was **{self.question.answer}**."
            )
            card = embed(
                "Trivia",
                f"## {self.question.prompt}\n\n{result}",
            )
            accuracy = stats["correct"] / stats["answered"] * 100
            card.set_footer(
                text=f"{self.question.category.title()} · {self.question.difficulty.title()} · "
                f"Accuracy {accuracy:.0f}%"
            )
            await interaction.response.edit_message(embed=card, view=self)
            self.stop()

    async def on_timeout(self) -> None:
        if self.answered:
            return
        self.answered = True
        self.disable_answers()
        if self.message:
            card = embed(
                "Trivia · Time's up",
                f"## {self.question.prompt}\n\nThe answer was **{self.question.answer}**.",
            )
            card.set_footer(
                text=f"{self.question.category.title()} · {self.question.difficulty.title()}"
            )
            try:
                await self.message.edit(embed=card, view=self)
            except discord.HTTPException:
                pass


class Trivia(commands.Cog):
    """Interactive multiple-choice trivia and scores."""

    def __init__(self, bot: KevinBot) -> None:
        self.bot = bot
        self.recent: dict[tuple[int, int], deque[int]] = defaultdict(lambda: deque(maxlen=5))

    @commands.hybrid_group(name="trivia", fallback="play", description="Play a trivia question")
    @commands.guild_only()
    @commands.cooldown(1, 8, commands.BucketType.user)
    @app_commands.choices(
        category=[
            app_commands.Choice(name="Any", value="any"),
            *[app_commands.Choice(name=value.title(), value=value) for value in CATEGORIES],
        ],
        difficulty=[
            app_commands.Choice(name="Any", value="any"),
            *[app_commands.Choice(name=value.title(), value=value) for value in DIFFICULTIES],
        ],
    )
    async def trivia(
        self, ctx: commands.Context, category: str = "any", difficulty: str = "any"
    ) -> None:
        category = category.casefold()
        difficulty = difficulty.casefold()
        if category not in {"any", *CATEGORIES}:
            raise commands.BadArgument("Choose: any, " + ", ".join(CATEGORIES) + ".")
        if difficulty not in {"any", *DIFFICULTIES}:
            raise commands.BadArgument("Choose easy, medium, hard, or any.")
        candidates = matching_questions(category, difficulty)
        recent = self.recent[(ctx.guild.id, ctx.author.id)]
        fresh = [item for item in candidates if item[0] not in recent] or candidates
        question_id, question = RNG.choice(fresh)
        recent.append(question_id)
        card = embed("Trivia", f"## {question.prompt}\n\nChoose an answer below. You have **30 seconds**.")
        card.set_footer(text=f"{question.category.title()} · {question.difficulty.title()}")
        view = TriviaView(self.bot, ctx.author, ctx.guild.id, question)
        view.message = await ctx.send(embed=card, view=view)

    @trivia.command(name="stats", description="Show a member's trivia statistics")
    async def trivia_stats(
        self, ctx: commands.Context, member: discord.Member | None = None
    ) -> None:
        member = member or ctx.author
        row = await self.bot.db.fetchone(
            "SELECT answered, correct, streak, best_streak FROM trivia_stats "
            "WHERE guild_id = ? AND user_id = ?",
            (ctx.guild.id, member.id),
        )
        if not row:
            await ctx.send(embed=embed("Trivia stats", f"{member.mention} hasn't answered any questions yet."))
            return
        accuracy = int(row["correct"]) / int(row["answered"]) * 100
        card = embed("Trivia stats", member.mention)
        card.add_field(name="Correct", value=f"{row['correct']} / {row['answered']}")
        card.add_field(name="Accuracy", value=f"{accuracy:.1f}%")
        card.add_field(name="Current streak", value=str(row["streak"]))
        card.add_field(name="Best streak", value=str(row["best_streak"]))
        await ctx.send(embed=card)

    @trivia.command(name="leaderboard", description="Show this server's trivia leaders")
    async def trivia_leaderboard(self, ctx: commands.Context) -> None:
        rows = await self.bot.db.fetchall(
            "SELECT user_id, answered, correct, best_streak FROM trivia_stats "
            "WHERE guild_id = ? ORDER BY correct DESC, answered ASC LIMIT 10",
            (ctx.guild.id,),
        )
        lines = []
        for index, row in enumerate(rows, 1):
            accuracy = int(row["correct"]) / int(row["answered"]) * 100
            lines.append(
                f"**{index}.** <@{row['user_id']}> — **{row['correct']}** correct "
                f"({accuracy:.0f}%) · best streak {row['best_streak']}"
            )
        await ctx.send(embed=embed("Trivia leaderboard", "\n".join(lines) or "No scores yet."))


async def setup(bot: KevinBot) -> None:
    await bot.add_cog(Trivia(bot))
