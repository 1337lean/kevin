from __future__ import annotations

import asyncio
import hashlib
import random

import discord
from discord import app_commands
from discord.ext import commands

from kevin.bot import KevinBot
from kevin.utils.formatting import embed

RNG = random.SystemRandom()

C4_ROWS = 6
C4_COLUMNS = 7
C4_EMPTY = 0
C4_RED = 1
C4_YELLOW = 2
C4_DISCS = {C4_EMPTY: "⚪", C4_RED: "🔴", C4_YELLOW: "🟡"}
C4_NUMBERS = ("1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣")


def new_c4_board() -> list[list[int]]:
    return [[C4_EMPTY] * C4_COLUMNS for _ in range(C4_ROWS)]


def c4_drop(board: list[list[int]], column: int, player: int) -> int | None:
    """Drop a disc into a column, returning the row it landed in or None if full."""
    for row in range(C4_ROWS - 1, -1, -1):
        if board[row][column] == C4_EMPTY:
            board[row][column] = player
            return row
    return None


def c4_winning_cells(
    board: list[list[int]], player: int
) -> tuple[tuple[int, int], ...] | None:
    """Return the cells of a four-in-a-row line for player, or None."""
    directions = ((0, 1), (1, 0), (1, 1), (1, -1))
    for row in range(C4_ROWS):
        for column in range(C4_COLUMNS):
            if board[row][column] != player:
                continue
            for step_row, step_column in directions:
                cells = tuple(
                    (row + index * step_row, column + index * step_column) for index in range(4)
                )
                if all(
                    0 <= cell_row < C4_ROWS
                    and 0 <= cell_column < C4_COLUMNS
                    and board[cell_row][cell_column] == player
                    for cell_row, cell_column in cells
                ):
                    return cells
    return None


def c4_is_full(board: list[list[int]]) -> bool:
    return all(cell != C4_EMPTY for row in board for cell in row)


def render_c4_board(board: list[list[int]]) -> str:
    rows = [" ".join(C4_DISCS[cell] for cell in row) for row in board]
    return "\n".join(rows) + "\n" + " ".join(C4_NUMBERS)


class ConnectFourChallenge(discord.ui.View):
    """Accept or decline a Connect 4 challenge."""

    def __init__(self, challenger: discord.Member, opponent: discord.Member) -> None:
        super().__init__(timeout=120)
        self.challenger = challenger
        self.opponent = opponent
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.opponent.id:
            return True
        if interaction.user.id == self.challenger.id:
            await interaction.response.send_message(
                "Wait for your opponent to accept!", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "This challenge belongs to someone else—start your own with `/connect4`!",
                ephemeral=True,
            )
        return False

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        if self.message is not None:
            try:
                await self.message.edit(
                    embed=embed(
                        "Connect 4",
                        f"{self.opponent.mention} did not respond to "
                        f"{self.challenger.mention}'s challenge.",
                    ),
                    view=self,
                )
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, emoji="🎮")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        game = ConnectFourGame(self.challenger, self.opponent)
        await interaction.response.edit_message(embed=game.game_embed(), view=game)
        game.message = self.message

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            embed=embed(
                "Connect 4",
                f"{self.opponent.mention} declined {self.challenger.mention}'s challenge.",
            ),
            view=self,
        )
        self.stop()


class ConnectFourGame(discord.ui.View):
    """Column buttons and state for one Connect 4 match."""

    def __init__(self, red_player: discord.Member, yellow_player: discord.Member) -> None:
        super().__init__(timeout=300)
        self.players = {C4_RED: red_player, C4_YELLOW: yellow_player}
        self.board = new_c4_board()
        self.turn = C4_RED
        self.finished = False
        self.message: discord.Message | None = None
        self.move_lock = asyncio.Lock()
        self.update_controls()

    def current_player(self) -> discord.Member:
        return self.players[self.turn]

    def update_controls(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.label and child.label.isdigit():
                column = int(child.label) - 1
                full = all(row[column] != C4_EMPTY for row in self.board)
                child.disabled = self.finished or full

    def game_embed(self, result: str | None = None) -> discord.Embed:
        title = (
            f"🔴 {self.players[C4_RED].display_name} vs "
            f"🟡 {self.players[C4_YELLOW].display_name}"
        )
        description = render_c4_board(self.board)
        if not self.finished:
            description += f"\n\n{self.current_player().mention}, pick a column!"
        if result:
            description += f"\n\n{result}"
        return embed(title, description)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id in (player.id for player in self.players.values()):
            return True
        await interaction.response.send_message(
            "This match belongs to someone else—start your own with `/connect4`!",
            ephemeral=True,
        )
        return False

    async def on_timeout(self) -> None:
        self.finished = True
        self.update_controls()
        if self.message is not None:
            try:
                await self.message.edit(
                    embed=self.game_embed("The match expired from inactivity."), view=self
                )
            except discord.HTTPException:
                pass

    @discord.ui.button(label="1", style=discord.ButtonStyle.primary)
    async def column_one(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.play(interaction, 0)

    @discord.ui.button(label="2", style=discord.ButtonStyle.primary)
    async def column_two(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.play(interaction, 1)

    @discord.ui.button(label="3", style=discord.ButtonStyle.primary)
    async def column_three(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self.play(interaction, 2)

    @discord.ui.button(label="4", style=discord.ButtonStyle.primary)
    async def column_four(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self.play(interaction, 3)

    @discord.ui.button(label="5", style=discord.ButtonStyle.primary)
    async def column_five(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self.play(interaction, 4)

    @discord.ui.button(label="6", style=discord.ButtonStyle.primary, row=1)
    async def column_six(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self.play(interaction, 5)

    @discord.ui.button(label="7", style=discord.ButtonStyle.primary, row=1)
    async def column_seven(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self.play(interaction, 6)

    async def play(self, interaction: discord.Interaction, column: int) -> None:
        if interaction.user.id != self.current_player().id:
            await interaction.response.send_message("It's not your turn yet!", ephemeral=True)
            return
        async with self.move_lock:
            if self.finished or c4_drop(self.board, column, self.turn) is None:
                await interaction.response.send_message("That column is full!", ephemeral=True)
                return
            winner_cells = c4_winning_cells(self.board, self.turn)
            if winner_cells is not None:
                self.finished = True
                result = f"{self.current_player().mention} wins! 🎉"
            elif c4_is_full(self.board):
                self.finished = True
                result = "It's a draw—board is full!"
            else:
                self.turn = C4_YELLOW if self.turn == C4_RED else C4_RED
                result = None
            self.update_controls()
            await interaction.response.edit_message(embed=self.game_embed(result), view=self)
        if self.finished:
            self.stop()


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

    @commands.hybrid_command(aliases=["c4"], description="Challenge someone to Connect 4")
    @app_commands.describe(opponent="The member you want to play against")
    async def connect4(self, ctx: commands.Context, opponent: discord.Member) -> None:
        if opponent.id == ctx.author.id:
            raise commands.BadArgument("You cannot challenge yourself.")
        if opponent.bot:
            raise commands.BadArgument("K's circuits are no match for a bot opponent.")
        view = ConnectFourChallenge(ctx.author, opponent)
        message = await ctx.send(
            embed=embed(
                "Connect 4",
                f"{opponent.mention}, {ctx.author.mention} challenges you to Connect 4! "
                "🔴 goes first.",
            ),
            view=view,
        )
        view.message = message

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
