from __future__ import annotations

import asyncio
import random
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import discord
from discord import app_commands
from discord.ext import commands

from kevin.bot import KevinBot
from kevin.utils.checks import owner_or_guild_permissions
from kevin.utils.formatting import embed, error, human_duration, success

CURRENCY = "Kash"
ICON = "💵"
MAX_TRANSACTION = 1_000_000
# A single wager is capped too, so one hand cannot swing an unbounded amount.
MAX_BET = 1_000_000
# Administrator grants stay well inside SQLite's signed 64-bit integer column.
MAX_GRANT = 1_000_000_000_000
# Rake on even-money winnings. Without it /coinflip and /dice are exactly break-even,
# which makes martingale grinding risk-free and never drains the money supply.
HOUSE_EDGE_BPS = 250

DAILY_COOLDOWN = timedelta(hours=24)
WORK_COOLDOWN = timedelta(minutes=10)
ROB_COOLDOWN = timedelta(minutes=30)

RNG = random.SystemRandom()

CARD_SUITS = ("♠", "♥", "♦", "♣")
CARD_RANKS = ("A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K")


def parse_compact_amount(argument: str, *, maximum: int | None, noun: str) -> int:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([km]?)", argument.strip().lower().replace(",", ""))
    if not match:
        raise commands.BadArgument(f"Enter {noun.lower()} like `4000`, `4k`, or `1m`.")

    number, suffix = match.groups()
    multiplier = {"": 1, "k": 1_000, "m": 1_000_000}[suffix]
    value = Decimal(number) * multiplier
    if value != value.to_integral_value():
        raise commands.BadArgument(f"{noun} must be a whole number of {CURRENCY}.")

    amount = int(value)
    if amount < 1:
        raise commands.BadArgument(f"{noun} must be at least 1 {CURRENCY}.")
    if maximum is not None and amount > maximum:
        raise commands.BadArgument(f"{noun} must be between 1 and {maximum:,} {CURRENCY}.")
    return amount


def parse_bet(argument: str) -> int:
    """Parse a wager, accepting shorthand such as 4k, 2.5k, or 1m."""
    return parse_compact_amount(argument, maximum=MAX_BET, noun="Bet")


async def resolve_bet(ctx: commands.Context, argument: str) -> int:
    """Resolve a wager, including ``all`` for the user's available wallet balance."""
    if argument.strip().casefold() != "all":
        return parse_bet(argument)
    if ctx.guild is None:
        raise commands.NoPrivateMessage()

    balance = await ctx.bot.db.change_balance(ctx.guild.id, ctx.author.id, 0)
    if balance < 1:
        raise commands.BadArgument(f"You do not have any {CURRENCY} to bet.")
    return min(balance, MAX_BET)


def even_money_profit(bet: int) -> int:
    """Profit kept on an even-money win, after the house rake."""
    return bet - bet * HOUSE_EDGE_BPS // 10_000


def parse_kash_amount(argument: str, *, maximum: int = MAX_TRANSACTION) -> int:
    """Parse a Kash amount with optional k/m shorthand."""
    return parse_compact_amount(argument, maximum=maximum, noun="Amount")


class BetConverter(commands.Converter[int]):
    async def convert(self, ctx: commands.Context, argument: str) -> int:
        return await resolve_bet(ctx, argument)


class KashAmountConverter(commands.Converter[int]):
    async def convert(self, ctx: commands.Context, argument: str) -> int:
        return parse_kash_amount(argument)


class GrantAmountConverter(commands.Converter[int]):
    async def convert(self, ctx: commands.Context, argument: str) -> int:
        return parse_compact_amount(argument, maximum=MAX_GRANT, noun="Amount")


class RobTargetConverter(commands.MemberConverter, app_commands.Transformer):
    """Resolve exact member names first, then an unambiguous partial name."""

    @property
    def type(self) -> discord.AppCommandOptionType:
        """Keep Discord's native member picker for the slash command."""
        return discord.AppCommandOptionType.user

    async def transform(
        self, interaction: discord.Interaction, value: discord.Member | discord.User
    ) -> discord.Member | discord.User:
        return value

    async def convert(self, ctx: commands.Context, argument: str) -> discord.Member:
        try:
            return await super().convert(ctx, argument)
        except commands.MemberNotFound:
            if ctx.guild is None:
                raise

        query = argument.casefold().strip()
        matches = [
            member
            for member in ctx.guild.members
            if query in member.display_name.casefold() or query in member.name.casefold()
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise commands.BadArgument(
                f'More than one member matches "{argument}". Mention the person instead.'
            )
        raise commands.MemberNotFound(argument)


ROB_TARGET_PARAMETER = commands.parameter(converter=RobTargetConverter)


@dataclass(frozen=True, slots=True)
class PlayingCard:
    rank: str
    suit: str

    @property
    def points(self) -> int:
        if self.rank == "A":
            return 11
        if self.rank in {"J", "Q", "K"}:
            return 10
        return int(self.rank)

    def __str__(self) -> str:
        return f"`{self.rank}{self.suit}`"


def blackjack_deck() -> list[PlayingCard]:
    """Return a shuffled, standard 52-card blackjack deck."""
    deck = [PlayingCard(rank, suit) for suit in CARD_SUITS for rank in CARD_RANKS]
    RNG.shuffle(deck)
    return deck


def blackjack_value(hand: list[PlayingCard]) -> tuple[int, bool]:
    """Return the best hand value and whether it is soft (an ace still counts as 11)."""
    value = sum(card.points for card in hand)
    aces = sum(card.rank == "A" for card in hand)
    while value > 21 and aces:
        value -= 10
        aces -= 1
    return value, bool(aces)


def has_blackjack(hand: list[PlayingCard]) -> bool:
    return len(hand) == 2 and blackjack_value(hand)[0] == 21


class BlackjackView(discord.ui.View):
    """Controls and game state for one round of blackjack."""

    def __init__(
        self,
        bot: KevinBot,
        author: discord.abc.User,
        guild_id: int,
        bet: int,
        balance: int,
        release: Callable[[], None],
    ) -> None:
        super().__init__(timeout=90)
        self.bot = bot
        self.author = author
        self.guild_id = guild_id
        self.bet = bet
        self.balance = balance
        self.release = release
        self.deck = blackjack_deck()
        self.player = [self.deck.pop(), self.deck.pop()]
        self.dealer = [self.deck.pop(), self.deck.pop()]
        self.message: discord.Message | None = None
        self.finished = False
        self.action_lock = asyncio.Lock()
        self.double_down.disabled = balance < bet

    @staticmethod
    def cards(hand: list[PlayingCard]) -> str:
        return " ".join(str(card) for card in hand)

    def game_embed(
        self,
        title: str = "🃏 Blackjack · Your turn",
        result: str | None = None,
        *,
        reveal_dealer: bool = False,
    ) -> discord.Embed:
        player_value, player_soft = blackjack_value(self.player)
        if reveal_dealer:
            dealer_cards = self.cards(self.dealer)
            dealer_value, dealer_soft = blackjack_value(self.dealer)
            dealer_score = f"**{dealer_value}**{' · soft' if dealer_soft else ''}"
        else:
            dealer_cards = f"`?` {self.dealer[1]}"
            dealer_score = "**?**"

        description = (
            f"### Dealer\n{dealer_cards}\nValue: {dealer_score}\n\n"
            f"### {self.author.display_name}\n{self.cards(self.player)}\n"
            f"Value: **{player_value}**{' · soft' if player_soft else ''}"
        )
        if result:
            description += f"\n\n{result}"
        card = embed(title, description)
        card.set_footer(
            text=f"Bet: {self.bet:,} {CURRENCY} · Balance: {self.balance:,} · Dealer stands on 17"
        )
        return card

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.author.id:
            return True
        await interaction.response.send_message(
            "Those cards belong to someone else—start your own game with `/blackjack` or `/bj`!",
            ephemeral=True,
        )
        return False

    def disable_controls(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

    async def settle(self, payout: int, title: str, result: str) -> discord.Embed:
        if payout:
            self.balance = await self.bot.db.change_balance(
                self.guild_id, self.author.id, payout
            )
        self.finished = True
        self.disable_controls()
        self.release()
        self.stop()
        return self.game_embed(title, result, reveal_dealer=True)

    async def play_dealer(self) -> tuple[int, str, str]:
        dealer_value, _ = blackjack_value(self.dealer)
        while dealer_value < 17:
            self.dealer.append(self.deck.pop())
            dealer_value, _ = blackjack_value(self.dealer)

        player_value, _ = blackjack_value(self.player)
        if dealer_value > 21:
            return (
                self.bet * 2,
                "🎉 Blackjack · You win!",
                f"The dealer busted with **{dealer_value}**. You won **{self.bet:,} {CURRENCY}**!",
            )
        if player_value > dealer_value:
            return (
                self.bet * 2,
                "🎉 Blackjack · You win!",
                f"**{player_value}** beats **{dealer_value}**. You won **{self.bet:,} {CURRENCY}**!",
            )
        if player_value == dealer_value:
            return (
                self.bet,
                "🤝 Blackjack · Push",
                f"You both have **{player_value}**. Your **{self.bet:,} {CURRENCY}** bet was returned.",
            )
        return (
            0,
            "💥 Blackjack · Dealer wins",
            f"The dealer's **{dealer_value}** beats your **{player_value}**. "
            f"You lost **{self.bet:,} {CURRENCY}**.",
        )

    async def finish_dealer_turn(self, interaction: discord.Interaction) -> None:
        payout, title, result = await self.play_dealer()
        card = await self.settle(payout, title, result)
        await interaction.response.edit_message(embed=card, view=self)

    @discord.ui.button(label="Hit", emoji="➕", style=discord.ButtonStyle.primary)
    async def hit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        async with self.action_lock:
            if self.finished:
                if not interaction.response.is_done():
                    await interaction.response.send_message("That game is already over.", ephemeral=True)
                return
            self.player.append(self.deck.pop())
            self.double_down.disabled = True
            value, _ = blackjack_value(self.player)
            if value > 21:
                card = await self.settle(
                    0,
                    "💥 Blackjack · Bust",
                    f"You went over 21 and lost **{self.bet:,} {CURRENCY}**.",
                )
                await interaction.response.edit_message(embed=card, view=self)
            elif value == 21:
                await self.finish_dealer_turn(interaction)
            else:
                await interaction.response.edit_message(embed=self.game_embed(), view=self)

    @discord.ui.button(label="Stand", emoji="✋", style=discord.ButtonStyle.secondary)
    async def stand(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        async with self.action_lock:
            if self.finished:
                if not interaction.response.is_done():
                    await interaction.response.send_message("That game is already over.", ephemeral=True)
                return
            await self.finish_dealer_turn(interaction)

    @discord.ui.button(label="Double Down", emoji="💰", style=discord.ButtonStyle.success)
    async def double_down(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        async with self.action_lock:
            if self.finished:
                if not interaction.response.is_done():
                    await interaction.response.send_message("That game is already over.", ephemeral=True)
                return
            # Greying the button out is only a client-side hint: a crafted component
            # interaction still lands here, so the two-card rule is enforced again.
            if len(self.player) != 2:
                await interaction.response.send_message(
                    "You can only double down on your first two cards.", ephemeral=True
                )
                return
            try:
                self.balance = await self.bot.db.change_balance(
                    self.guild_id, self.author.id, -self.bet
                )
            except ValueError:
                await interaction.response.send_message(
                    f"You need another **{self.bet:,} {CURRENCY}** to double down.",
                    ephemeral=True,
                )
                return
            self.bet *= 2
            self.player.append(self.deck.pop())
            value, _ = blackjack_value(self.player)
            if value > 21:
                card = await self.settle(
                    0,
                    "💥 Blackjack · Bust",
                    f"You doubled down, busted, and lost **{self.bet:,} {CURRENCY}**.",
                )
                await interaction.response.edit_message(embed=card, view=self)
                return
            await self.finish_dealer_turn(interaction)

    async def on_timeout(self) -> None:
        async with self.action_lock:
            if self.finished:
                return
            self.finished = True
            self.disable_controls()
            self.release()
            if self.message:
                card = self.game_embed(
                    "⌛ Blackjack · Folded",
                    f"Time ran out, so you lost **{self.bet:,} {CURRENCY}**.",
                    reveal_dealer=True,
                )
                try:
                    await self.message.edit(embed=card, view=self)
                except discord.HTTPException:
                    pass

PASS_LINE = "pass"
DONT_PASS = "dont"
LINE_LABELS = {PASS_LINE: "Pass line", DONT_PASS: "Don't pass"}
CRAPS_POINTS = (4, 5, 6, 8, 9, 10)
# True odds paid on a point, as (numerator, denominator) for the pass line.
CRAPS_ODDS = {4: (2, 1), 5: (3, 2), 6: (6, 5), 8: (6, 5), 9: (3, 2), 10: (2, 1)}
DIE_FACES = {1: "⚀", 2: "⚁", 3: "⚂", 4: "⚃", 5: "⚄", 6: "⚅"}


def parse_craps_line(argument: str) -> str:
    """Normalize a craps line bet, accepting spellings such as `dont pass` or `dp`."""
    key = re.sub(r"[\s'_-]", "", argument.casefold())
    if key in {"pass", "passline", "p", "with", "for"}:
        return PASS_LINE
    if key in {"dont", "dontpass", "dontpassline", "dp", "d", "no", "against"}:
        return DONT_PASS
    raise commands.BadArgument("Bet the `pass` line or `dontpass`.")


class CrapsLineConverter(commands.Converter[str]):
    async def convert(self, ctx: commands.Context, argument: str) -> str:
        return parse_craps_line(argument)


CRAPS_LINE_PARAMETER = commands.parameter(
    converter=CrapsLineConverter, default=PASS_LINE, displayed_default="pass"
)


@dataclass(frozen=True, slots=True)
class DiceRoll:
    first: int
    second: int

    @property
    def total(self) -> int:
        return self.first + self.second

    def __str__(self) -> str:
        return f"{DIE_FACES[self.first]} {DIE_FACES[self.second]} · **{self.total}**"


def roll_dice() -> DiceRoll:
    return DiceRoll(RNG.randint(1, 6), RNG.randint(1, 6))


def come_out_outcome(total: int, line: str) -> str:
    """Return `win`, `lose`, `push`, or `point` for a come-out roll."""
    if total in CRAPS_POINTS:
        return "point"
    if line == PASS_LINE:
        return "win" if total in {7, 11} else "lose"
    if total == 12:
        return "push"
    return "win" if total in {2, 3} else "lose"


def point_outcome(total: int, point: int, line: str) -> str | None:
    """Return `win` or `lose` once a point round ends, or None to keep rolling."""
    if total == point:
        return "win" if line == PASS_LINE else "lose"
    if total == 7:
        return "lose" if line == PASS_LINE else "win"
    return None


def odds_profit(point: int, amount: int, line: str) -> int:
    """Profit on an odds bet, paid at true odds and laid the other way on don't pass."""
    numerator, denominator = CRAPS_ODDS[point]
    if line == PASS_LINE:
        return amount * numerator // denominator
    return amount * denominator // numerator


class CrapsView(discord.ui.View):
    """Controls and game state for one round of craps."""

    def __init__(
        self,
        bot: KevinBot,
        author: discord.abc.User,
        guild_id: int,
        bet: int,
        balance: int,
        line: str,
        release: Callable[[], None],
    ) -> None:
        super().__init__(timeout=90)
        self.bot = bot
        self.author = author
        self.guild_id = guild_id
        self.bet = bet
        self.balance = balance
        self.line = line
        self.release = release
        self.point: int | None = None
        self.odds = 0
        self.history: list[DiceRoll] = []
        self.message: discord.Message | None = None
        self.finished = False
        self.action_lock = asyncio.Lock()
        self.take_odds.label = "Lay Odds" if line == DONT_PASS else "Take Odds"
        self.take_odds.disabled = True

    @property
    def line_label(self) -> str:
        return LINE_LABELS[self.line]

    @property
    def last_roll(self) -> DiceRoll | None:
        return self.history[-1] if self.history else None

    @property
    def stake(self) -> int:
        return self.bet + self.odds

    def goal(self) -> str:
        if self.line == PASS_LINE:
            return f"Roll **{self.point}** again before a **7**."
        return f"Roll a **7** before **{self.point}**."

    def game_embed(self, title: str | None = None, result: str | None = None) -> discord.Embed:
        if title is None:
            title = (
                f"🎲 Craps · Point is {self.point}"
                if self.point
                else "🎲 Craps · Come-out roll"
            )
        sections = []
        if self.last_roll:
            sections.append(f"### Roll\n{self.last_roll}")
        if self.point and not self.finished:
            sections.append(self.goal())
        if len(self.history) > 1:
            recent = " → ".join(str(roll.total) for roll in self.history[-8:])
            sections.append(f"Rolls: {recent}")
        if result:
            sections.append(result)

        card = embed(title, "\n\n".join(sections))
        stake = f"{self.bet:,} {CURRENCY}"
        if self.odds:
            stake += f" + {self.odds:,} odds"
        card.set_footer(text=f"{self.line_label} · Bet: {stake} · Balance: {self.balance:,}")
        return card

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.author.id:
            return True
        await interaction.response.send_message(
            "Those dice belong to someone else—start your own round with `/craps`!",
            ephemeral=True,
        )
        return False

    def disable_controls(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

    async def settle(self, payout: int, title: str, result: str) -> discord.Embed:
        if payout:
            self.balance = await self.bot.db.change_balance(
                self.guild_id, self.author.id, payout
            )
        self.finished = True
        self.disable_controls()
        self.release()
        self.stop()
        return self.game_embed(title, result)

    def come_out(self) -> tuple[int, str, str] | None:
        """Roll the come-out pair, returning a settlement unless it establishes a point."""
        roll = roll_dice()
        self.history.append(roll)
        outcome = come_out_outcome(roll.total, self.line)
        if outcome == "point":
            self.point = roll.total
            self.take_odds.disabled = self.balance < self.bet
            return None
        if outcome == "push":
            return (
                self.bet,
                "🤝 Craps · Push",
                f"**12** is barred on don't pass. Your **{self.bet:,} {CURRENCY}** bet "
                "was returned.",
            )
        if outcome == "win":
            flavor = "A natural" if roll.total in {7, 11} else "Craps"
            return (
                self.bet * 2,
                "🎉 Craps · You win!",
                f"{flavor}—**{roll.total}** on the come-out pays the "
                f"{self.line_label.lower()}. You won **{self.bet:,} {CURRENCY}**!",
            )
        flavor = "Craps" if roll.total in {2, 3, 12} else "A natural"
        return (
            0,
            "💥 Craps · You lose",
            f"{flavor}—**{roll.total}** on the come-out sinks the "
            f"{self.line_label.lower()}. You lost **{self.bet:,} {CURRENCY}**.",
        )

    def point_roll(self) -> tuple[int, str, str] | None:
        """Roll during a point round, returning a settlement once the round ends."""
        assert self.point is not None
        roll = roll_dice()
        self.history.append(roll)
        outcome = point_outcome(roll.total, self.point, self.line)
        if outcome is None:
            return None
        if outcome == "win":
            odds_win = odds_profit(self.point, self.odds, self.line) if self.odds else 0
            profit = self.bet + odds_win
            detail = f"You won **{profit:,} {CURRENCY}**"
            if self.odds:
                detail += f" (line **{self.bet:,}** + odds **{odds_win:,}**)"
            headline = (
                f"You hit the point with **{self.point}**!"
                if self.line == PASS_LINE
                else f"Seven out before **{self.point}**!"
            )
            return (self.stake + profit, "🎉 Craps · You win!", f"{headline} {detail}.")
        headline = (
            f"Seven out—**{self.point}** never came."
            if self.line == PASS_LINE
            else f"The point **{self.point}** landed first."
        )
        return (
            0,
            "💥 Craps · You lose",
            f"{headline} You lost **{self.stake:,} {CURRENCY}**.",
        )

    async def guard(self, interaction: discord.Interaction) -> bool:
        if not self.finished:
            return True
        if not interaction.response.is_done():
            await interaction.response.send_message("That round is already over.", ephemeral=True)
        return False

    @discord.ui.button(label="Roll", emoji="🎲", style=discord.ButtonStyle.primary)
    async def roll(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        async with self.action_lock:
            if not await self.guard(interaction):
                return
            settlement = self.point_roll()
            if settlement is None:
                await interaction.response.edit_message(embed=self.game_embed(), view=self)
                return
            card = await self.settle(*settlement)
            await interaction.response.edit_message(embed=card, view=self)

    @discord.ui.button(label="Take Odds", emoji="💰", style=discord.ButtonStyle.success)
    async def take_odds(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        async with self.action_lock:
            if not await self.guard(interaction):
                return
            if self.point is None or self.odds:
                await interaction.response.send_message(
                    "You already have odds behind that point.", ephemeral=True
                )
                return
            try:
                self.balance = await self.bot.db.change_balance(
                    self.guild_id, self.author.id, -self.bet
                )
            except ValueError:
                await interaction.response.send_message(
                    f"You need another **{self.bet:,} {CURRENCY}** to back that point.",
                    ephemeral=True,
                )
                return
            self.odds = self.bet
            button.disabled = True
            payout = odds_profit(self.point, self.odds, self.line)
            verb = "Took" if self.line == PASS_LINE else "Laid"
            await interaction.response.edit_message(
                embed=self.game_embed(
                    result=f"{verb} **{self.odds:,} {CURRENCY}** odds—they pay "
                    f"**{payout:,}** at true odds."
                ),
                view=self,
            )

    async def on_timeout(self) -> None:
        async with self.action_lock:
            if self.finished:
                return
            self.finished = True
            self.disable_controls()
            self.release()
            if self.message:
                card = self.game_embed(
                    "⌛ Craps · Walked away",
                    f"Time ran out, so you lost **{self.stake:,} {CURRENCY}**.",
                )
                try:
                    await self.message.edit(embed=card, view=self)
                except discord.HTTPException:
                    pass


SHOP = {
    "chocolate": ("Chocolate stash", 250, "A favorite snack. Collectible."),
    "badge": ("Grape soda badge", 1_000, "A shiny collectible badge."),
    "binoculars": ("Explorer binoculars", 2_500, "Improves work rewards by 10%."),
    "shield": ("Vault shield", 5_000, "Protects you from one successful robbery."),
    "plush": ("K plush", 10_000, "A prestigious shelf companion."),
    "golden_relic": ("Golden relic", 50_000, "The ultimate K collectible."),
}


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class Economy(commands.Cog):
    """Server-local currency, jobs, collectibles, and gambling."""

    def __init__(self, bot: KevinBot) -> None:
        self.bot = bot
        self.active_blackjack: set[tuple[int, int]] = set()
        self.active_craps: set[tuple[int, int]] = set()
        # /bj is a plain app command, so the hybrid /blackjack cooldown decorator does
        # not cover it. Both entry points share this mapping instead.
        self.blackjack_cooldown = commands.CooldownMapping.from_cooldown(
            1, 5.0, lambda ctx: ctx.author.id
        )

    async def cog_check(self, ctx: commands.Context) -> bool:
        if not ctx.guild:
            raise commands.NoPrivateMessage()
        settings = await self.bot.db.get_settings(ctx.guild.id)
        if not settings.get("economy_enabled"):
            raise commands.CheckFailure("The economy is disabled in this server.")
        return True

    async def member_row(self, guild_id: int, user_id: int):
        await self.bot.db.ensure_member(guild_id, user_id)
        return await self.bot.db.fetchone(
            "SELECT * FROM members WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        )

    async def quantity(self, guild_id: int, user_id: int, item: str) -> int:
        row = await self.bot.db.fetchone(
            "SELECT quantity FROM inventory WHERE guild_id = ? AND user_id = ? AND item_key = ?",
            (guild_id, user_id, item),
        )
        return int(row["quantity"]) if row else 0

    async def send_balance(
        self, ctx: commands.Context, member: discord.Member | None = None
    ) -> None:
        """Render a member's balance for the balance command and its text shortcut."""
        member = member or ctx.author
        row = await self.member_row(ctx.guild.id, member.id)
        card = embed(f"{ICON} {member.display_name}'s balance")
        card.add_field(name="Wallet", value=f"{row['balance']:,} {CURRENCY}")
        card.add_field(name="Bank", value=f"{row['bank']:,} {CURRENCY}")
        card.add_field(name="Net worth", value=f"{row['balance'] + row['bank']:,} {CURRENCY}")
        await ctx.send(embed=card)

    async def send_richest(self, ctx: commands.Context) -> None:
        """Render the ten largest wallet-and-bank totals in the current server."""
        rows = await self.bot.db.fetchall(
            "SELECT user_id, balance + bank AS wealth FROM members "
            "WHERE guild_id = ? ORDER BY wealth DESC LIMIT 10",
            (ctx.guild.id,),
        )
        lines = [
            f"**{i}.** <@{r['user_id']}> — {r['wealth']:,} {CURRENCY}"
            for i, r in enumerate(rows, 1)
        ]
        await ctx.send(
            embed=embed(f"{ICON} Richest explorers", "\n".join(lines) or "No balances yet.")
        )

    @commands.hybrid_command(aliases=["wallet"], description="Show an economy balance")
    async def balance(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        await self.send_balance(ctx, member)

    @commands.group(name="bal", invoke_without_command=True)
    async def bal(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        """Show a balance, or use a balance subcommand."""
        await self.send_balance(ctx, member)

    @bal.command(name="top")
    async def bal_top(self, ctx: commands.Context) -> None:
        """Show the richest people in the server."""
        await self.send_richest(ctx)

    @commands.hybrid_command(description="Claim your daily Kash")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def daily(self, ctx: commands.Context) -> None:
        row = await self.member_row(ctx.guild.id, ctx.author.id)
        now = datetime.now(UTC)
        last = parse_dt(row["last_daily"])
        if last and now - last < DAILY_COOLDOWN:
            remaining = DAILY_COOLDOWN - (now - last)
            raise commands.CheckFailure(
                f"Your next daily is available in {human_duration(remaining)}."
            )
        reward = RNG.randint(350, 550)
        claimed = await self.bot.db.claim_reward(
            ctx.guild.id,
            ctx.author.id,
            reward,
            timestamp=now.isoformat(),
            eligible_before=(now - DAILY_COOLDOWN).isoformat(),
            column="last_daily",
        )
        if claimed is None:
            raise commands.CheckFailure("Your daily reward was already claimed.")
        await ctx.send(
            embed=success(f"Your daily expedition found **{reward} {CURRENCY}** {ICON}.")
        )

    @commands.hybrid_command(description="Do a quick job for Kash")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def work(self, ctx: commands.Context) -> None:
        jobs = (
            "organized a wilderness map",
            "guarded a supply cache",
            "sorted the adventure gear",
            "scouted a mysterious trail",
            "helped an elderly explorer",
            "repaired the airship",
        )
        # The real limit lives in the database: an in-memory cooldown reset on every
        # restart, which turned the shortest-cooldown earner into a money printer.
        row = await self.member_row(ctx.guild.id, ctx.author.id)
        now = datetime.now(UTC)
        last = parse_dt(row["last_work"])
        if last and now - last < WORK_COOLDOWN:
            remaining = WORK_COOLDOWN - (now - last)
            raise commands.CheckFailure(f"You can work again in {human_duration(remaining)}.")
        reward = RNG.randint(80, 220)
        if await self.quantity(ctx.guild.id, ctx.author.id, "binoculars"):
            reward = round(reward * 1.1)
        claimed = await self.bot.db.claim_reward(
            ctx.guild.id,
            ctx.author.id,
            reward,
            timestamp=now.isoformat(),
            eligible_before=(now - WORK_COOLDOWN).isoformat(),
            column="last_work",
        )
        if claimed is None:
            raise commands.CheckFailure("You have already worked recently.")
        await ctx.send(
            embed=success(f"You {RNG.choice(jobs)} and earned **{reward} {CURRENCY}** {ICON}.")
        )

    @commands.hybrid_command(description="Give Kash to another member")
    async def pay(
        self,
        ctx: commands.Context,
        member: discord.Member,
        amount: int = commands.parameter(converter=KashAmountConverter),
    ) -> None:
        if member.bot or member == ctx.author:
            raise commands.BadArgument("Choose another human member.")
        try:
            await self.bot.db.transfer_balance(ctx.guild.id, ctx.author.id, member.id, amount)
        except ValueError as exc:
            raise commands.BadArgument("You do not have enough Kash.") from exc
        await ctx.send(embed=success(f"Sent **{amount:,} {CURRENCY}** to {member.mention}."))

    @commands.hybrid_command(
        aliases=["grantmoney"], description="Add Kash to a member's wallet (administrator)"
    )
    @commands.guild_only()
    @owner_or_guild_permissions(administrator=True)
    async def addmoney(
        self,
        ctx: commands.Context,
        member: discord.Member,
        amount: int = commands.parameter(converter=GrantAmountConverter),
    ) -> None:
        balance = await self.bot.db.change_balance(ctx.guild.id, member.id, amount)
        await ctx.send(
            embed=success(
                f"Added **{amount:,} {CURRENCY}** to {member.mention}. "
                f"Their wallet now has **{balance:,} {CURRENCY}**."
            )
        )

    @commands.hybrid_command(description="Remove Kash from a member's wallet (administrator)")
    @commands.guild_only()
    @commands.has_guild_permissions(administrator=True)
    async def removemoney(
        self,
        ctx: commands.Context,
        member: discord.Member,
        amount: int = commands.parameter(converter=GrantAmountConverter),
    ) -> None:
        try:
            balance = await self.bot.db.change_balance(ctx.guild.id, member.id, -amount)
        except ValueError as exc:
            raise commands.BadArgument("That member does not have enough Kash.") from exc
        await ctx.send(
            embed=success(
                f"Removed **{amount:,} {CURRENCY}** from {member.mention}. "
                f"Their wallet now has **{balance:,} {CURRENCY}**."
            )
        )

    @commands.hybrid_command(description="Deposit Kash from your wallet into your bank")
    async def deposit(
        self, ctx: commands.Context, amount: int = commands.parameter(converter=KashAmountConverter)
    ) -> None:
        try:
            await self.bot.db.move_bank(ctx.guild.id, ctx.author.id, amount, to_bank=True)
        except ValueError as exc:
            raise commands.BadArgument("You do not have enough Kash in your wallet.") from exc
        await ctx.send(embed=success(f"Deposited **{amount:,} {CURRENCY}**."))

    @commands.hybrid_command(description="Withdraw Kash from your bank")
    async def withdraw(
        self, ctx: commands.Context, amount: int = commands.parameter(converter=KashAmountConverter)
    ) -> None:
        try:
            await self.bot.db.move_bank(ctx.guild.id, ctx.author.id, amount, to_bank=False)
        except ValueError as exc:
            raise commands.BadArgument("You do not have enough Kash in the bank.") from exc
        await ctx.send(embed=success(f"Withdrew **{amount:,} {CURRENCY}**."))

    async def settle_wager(self, ctx: commands.Context, bet: int, payout: int) -> int:
        """Take the stake before paying out so an empty wallet cannot win unbacked bets."""
        try:
            balance = await self.bot.db.change_balance(ctx.guild.id, ctx.author.id, -bet)
        except ValueError as exc:
            raise commands.BadArgument(
                f"You do not have enough {CURRENCY} for that bet."
            ) from exc
        if payout:
            balance = await self.bot.db.change_balance(ctx.guild.id, ctx.author.id, payout)
        return balance

    @commands.hybrid_command(description="Flip a coin and bet on the result")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def coinflip(
        self,
        ctx: commands.Context,
        choice: str,
        bet: int = commands.parameter(converter=BetConverter),
    ) -> None:
        choice = choice.lower()
        if choice not in {"heads", "tails"}:
            raise commands.BadArgument("Choose `heads` or `tails`.")
        result = RNG.choice(("heads", "tails"))
        won = choice == result
        profit = even_money_profit(bet)
        outcome = (
            f"won **{profit:,} {CURRENCY}**" if won else f"lost **{bet:,} {CURRENCY}**"
        )
        balance = await self.settle_wager(ctx, bet, bet + profit if won else 0)
        await ctx.send(
            embed=embed(
                "🪙 Coin flip",
                f"It landed **{result}**. You {outcome}.\nBalance: {balance:,}",
            )
        )

    @commands.hybrid_command(description="Spin the jungle slot machine")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def slots(
        self, ctx: commands.Context, bet: int = commands.parameter(converter=BetConverter)
    ) -> None:
        symbols = ("💎", "🍫", "🥤", "🚀", "🌈", "🎈")
        spin = [RNG.choice(symbols) for _ in range(3)]
        if len(set(spin)) == 1:
            multiplier = 5
        elif len(set(spin)) == 2:
            multiplier = 2
        else:
            multiplier = 0
        delta = bet * (multiplier - 1)
        balance = await self.settle_wager(ctx, bet, bet * multiplier)
        result = f"**{' '.join(spin)}**\n"
        result += f"{'Jackpot' if multiplier == 5 else 'Winner' if multiplier else 'No match'} — "
        result += f"{'won ' + f'{delta:,}' if delta >= 0 else 'lost ' + f'{-delta:,}'} {CURRENCY}.\nBalance: {balance:,}"
        await ctx.send(embed=embed("Jungle slots", result))

    @commands.hybrid_command(description="Bet that your die beats K's")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def dice(
        self, ctx: commands.Context, bet: int = commands.parameter(converter=BetConverter)
    ) -> None:
        user_roll, kevin_roll = RNG.randint(1, 6), RNG.randint(1, 6)
        if user_roll > kevin_roll:
            profit = even_money_profit(bet)
            payout, outcome = bet + profit, f"You win **{profit:,} {CURRENCY}**!"
        elif user_roll < kevin_roll:
            payout, outcome = 0, f"K wins. You lost **{bet:,} {CURRENCY}**."
        else:
            payout, outcome = bet, "Tie—your bet was returned."
        balance = await self.settle_wager(ctx, bet, payout)
        await ctx.send(
            embed=embed(
                "🎲 High roll",
                f"You: **{user_roll}** · K: **{kevin_roll}**\n{outcome}\nBalance: {balance:,}",
            )
        )

    async def start_blackjack(self, ctx: commands.Context, bet: int) -> None:
        if not ctx.guild:
            await ctx.send(embed=error("Blackjack can only be played in a server."), ephemeral=True)
            return
        settings = await self.bot.db.get_settings(ctx.guild.id)
        if not settings.get("economy_enabled"):
            await ctx.send(embed=error("The economy is disabled in this server."), ephemeral=True)
            return

        bucket = self.blackjack_cooldown.get_bucket(ctx)
        retry_after = bucket.update_rate_limit() if bucket else None
        if retry_after:
            await ctx.send(
                embed=error(f"Slow down—try again in **{retry_after:.1f}s**."), ephemeral=True
            )
            return

        game_key = (ctx.guild.id, ctx.author.id)
        if game_key in self.active_blackjack:
            await ctx.send(
                embed=embed(
                    "🃏 Blackjack",
                    "Finish your current hand before dealing another one.",
                ),
                ephemeral=True,
            )
            return

        self.active_blackjack.add(game_key)

        def release() -> None:
            self.active_blackjack.discard(game_key)

        try:
            balance = await self.bot.db.change_balance(ctx.guild.id, ctx.author.id, -bet)
        except ValueError:
            release()
            await ctx.send(
                embed=error(f"You do not have enough {CURRENCY} for that bet."), ephemeral=True
            )
            return
        except Exception:
            release()
            raise

        game: BlackjackView | None = None
        try:
            game = BlackjackView(self.bot, ctx.author, ctx.guild.id, bet, balance, release)
            player_blackjack = has_blackjack(game.player)
            dealer_blackjack = has_blackjack(game.dealer)

            if player_blackjack and dealer_blackjack:
                card = await game.settle(
                    bet,
                    "🤝 Blackjack · Push",
                    "You and the dealer both have blackjack. Your bet was returned.",
                )
                await ctx.send(embed=card)
                return
            if player_blackjack:
                profit = bet * 3 // 2
                card = await game.settle(
                    bet + profit,
                    "✨ Natural blackjack!",
                    f"Blackjack pays **3:2**—you won **{profit:,} {CURRENCY}**!",
                )
                await ctx.send(embed=card)
                return
            if dealer_blackjack:
                card = await game.settle(
                    0,
                    "💥 Blackjack · Dealer wins",
                    f"The dealer has blackjack. You lost **{bet:,} {CURRENCY}**.",
                )
                await ctx.send(embed=card)
                return

            game.message = await ctx.send(embed=game.game_embed(), view=game)
        except Exception:
            if game is None or not game.finished:
                await self.bot.db.change_balance(ctx.guild.id, ctx.author.id, bet)
                release()
            raise

    @commands.hybrid_command(
        aliases=["bj"], description="Play an interactive hand of blackjack"
    )
    @app_commands.describe(bet=f"Your wager in {CURRENCY} (blackjack pays 3:2)")
    async def blackjack(
        self, ctx: commands.Context, bet: int = commands.parameter(converter=BetConverter)
    ) -> None:
        await self.start_blackjack(ctx, bet)

    @app_commands.command(name="bj", description="Play an interactive hand of blackjack")
    @app_commands.describe(bet=f"Your wager in {CURRENCY} (blackjack pays 3:2)")
    @app_commands.guild_only()
    async def blackjack_shortcut(
        self, interaction: discord.Interaction, bet: str
    ) -> None:
        """Expose /bj too; text-command aliases are not registered as Discord slash commands."""
        try:
            ctx = await commands.Context.from_interaction(interaction)
            parsed_bet = await resolve_bet(ctx, bet)
        except commands.BadArgument as exc:
            await interaction.response.send_message(embed=error(str(exc)), ephemeral=True)
            return
        await self.start_blackjack(ctx, parsed_bet)

    @commands.hybrid_command(description="Roll the craps table on the pass or don't pass line")
    @app_commands.describe(
        bet=f"Your wager in {CURRENCY}",
        line="`pass` to bet with the shooter, `dontpass` to bet against them",
    )
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def craps(
        self,
        ctx: commands.Context,
        bet: int = commands.parameter(converter=BetConverter),
        line: str = CRAPS_LINE_PARAMETER,
    ) -> None:
        game_key = (ctx.guild.id, ctx.author.id)
        if game_key in self.active_craps:
            await ctx.send(
                embed=embed("🎲 Craps", "Finish your current round before shooting again."),
                ephemeral=True,
            )
            return

        self.active_craps.add(game_key)

        def release() -> None:
            self.active_craps.discard(game_key)

        try:
            balance = await self.bot.db.change_balance(ctx.guild.id, ctx.author.id, -bet)
        except ValueError as exc:
            release()
            raise commands.BadArgument(
                f"You do not have enough {CURRENCY} for that bet."
            ) from exc
        except Exception:
            release()
            raise

        game: CrapsView | None = None
        try:
            game = CrapsView(self.bot, ctx.author, ctx.guild.id, bet, balance, line, release)
            settlement = game.come_out()
            if settlement is not None:
                await ctx.send(embed=await game.settle(*settlement))
                return
            game.message = await ctx.send(embed=game.game_embed(), view=game)
        except Exception:
            if game is None or not game.finished:
                await self.bot.db.change_balance(ctx.guild.id, ctx.author.id, bet)
                release()
            raise

    @commands.hybrid_command(
        description="Attempt to steal Kash from another member",
        cooldown_after_parsing=True,
    )
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def rob(
        self,
        ctx: commands.Context,
        *,
        member: discord.Member = ROB_TARGET_PARAMETER,
    ) -> None:
        if member.bot or member == ctx.author:
            # Rejected before any dice are rolled, so keep the anti-spam cooldown unspent.
            ctx.command.reset_cooldown(ctx)
            raise commands.BadArgument("Choose another human member.")
        victim = await self.member_row(ctx.guild.id, member.id)
        robber = await self.member_row(ctx.guild.id, ctx.author.id)
        if victim["balance"] < 500 or robber["balance"] < 250:
            ctx.command.reset_cooldown(ctx)
            raise commands.CheckFailure(
                "Both players need at least 250 Kash, and the target needs 500."
            )
        now = datetime.now(UTC)
        last = parse_dt(robber["last_rob"])
        if last and now - last < ROB_COOLDOWN:
            remaining = ROB_COOLDOWN - (now - last)
            raise commands.CheckFailure(
                f"You can attempt another robbery in {human_duration(remaining)}."
            )
        # Spend the cooldown before rolling, so a failed attempt cannot be retried and a
        # restart no longer hands everyone a fresh one the way an in-memory cooldown did.
        spent = await self.bot.db.claim_reward(
            ctx.guild.id,
            ctx.author.id,
            0,
            timestamp=now.isoformat(),
            eligible_before=(now - ROB_COOLDOWN).isoformat(),
            column="last_rob",
        )
        if spent is None:
            raise commands.CheckFailure("You have already attempted a robbery recently.")

        if RNG.random() < 0.45:
            shield = await self.quantity(ctx.guild.id, member.id, "shield")
            if shield:
                await self.bot.db.execute(
                    "UPDATE inventory SET quantity = quantity - 1 WHERE guild_id = ? AND user_id = ? AND item_key = 'shield'",
                    (ctx.guild.id, member.id),
                )
                await ctx.send(
                    embed=embed("Robbery foiled", f"{member.mention}'s vault shield blocked you.")
                )
                return
            amount = RNG.randint(100, min(1_000, int(victim["balance"]) // 4))
            # Only the wallet is exposed; the bank is what makes savings worth using.
            stolen = await self.bot.db.seize_balance(
                ctx.guild.id, member.id, ctx.author.id, amount
            )
            await ctx.send(
                embed=success(f"You escaped with **{stolen} {CURRENCY}** from {member.mention}.")
            )
        else:
            # The fine scales with everything the robber owns and is collected from the
            # bank when the wallet is short. Capping it at the wallet let a rich player
            # bank all but the 250 minimum and rob at a large profit.
            net_worth = int(robber["balance"]) + int(robber["bank"])
            fine = RNG.randint(100, min(1_000, max(250, net_worth // 4)))
            paid = await self.bot.db.seize_balance(
                ctx.guild.id, ctx.author.id, member.id, fine, include_bank=True
            )
            await ctx.send(
                embed=embed("Caught!", f"You paid {member.mention} a **{paid} {CURRENCY}** fine.")
            )

    @commands.hybrid_command(description="Browse the K collectible shop")
    async def shop(self, ctx: commands.Context) -> None:
        card = embed(f"{ICON} K's trading post")
        for key, (name, price, description) in SHOP.items():
            card.add_field(
                name=f"{name} · {price:,}", value=f"`{key}` — {description}", inline=False
            )
        card.set_footer(text="Use /buy item:<key> quantity:<number>")
        await ctx.send(embed=card)

    @commands.hybrid_command(description="Buy an item from the shop")
    async def buy(
        self, ctx: commands.Context, item: str, quantity: commands.Range[int, 1, 100] = 1
    ) -> None:
        key = item.lower().replace(" ", "_")
        if key not in SHOP:
            raise commands.BadArgument("That item is not in the shop.")
        name, price, _ = SHOP[key]
        try:
            balance = await self.bot.db.buy_item(ctx.guild.id, ctx.author.id, key, price, quantity)
        except ValueError as exc:
            raise commands.BadArgument("You do not have enough Kash.") from exc
        await ctx.send(
            embed=success(f"Bought **{quantity}× {name}**. Balance: {balance:,} {CURRENCY}.")
        )

    @commands.hybrid_command(aliases=["inv"], description="Show a member's collectibles")
    async def inventory(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        member = member or ctx.author
        rows = await self.bot.db.fetchall(
            "SELECT item_key, quantity FROM inventory WHERE guild_id = ? AND user_id = ? AND quantity > 0",
            (ctx.guild.id, member.id),
        )
        lines = [
            f"**{SHOP.get(r['item_key'], (r['item_key'], 0, ''))[0]}** × {r['quantity']}"
            for r in rows
        ]
        await ctx.send(
            embed=embed(f"Inventory · {member.display_name}", "\n".join(lines) or "No items yet.")
        )

    @commands.hybrid_command(name="richest", description="Show the server economy leaderboard")
    async def richest(self, ctx: commands.Context) -> None:
        await self.send_richest(ctx)


async def setup(bot: KevinBot) -> None:
    await bot.add_cog(Economy(bot))
