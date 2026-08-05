"""Every wager must be taken from the wallet before any winnings are paid out."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from discord.ext import commands

from kevin.cogs.economy import Economy


class Wallet:
    """Stand-in for Database.change_balance, which only rejects going below zero."""

    def __init__(self, balance: int) -> None:
        self.balance = balance
        self.deltas: list[int] = []

    async def change_balance(self, guild_id: int, user_id: int, delta: int) -> int:
        self.deltas.append(delta)
        if self.balance + delta < 0:
            raise ValueError("Insufficient balance")
        self.balance += delta
        return self.balance


def context(wallet: Wallet) -> SimpleNamespace:
    return SimpleNamespace(
        bot=SimpleNamespace(db=wallet),
        guild=SimpleNamespace(id=1),
        author=SimpleNamespace(id=2),
        send=AsyncMock(),
    )


def force(monkeypatch: pytest.MonkeyPatch, *, choice=None, randints=()) -> None:
    if choice is not None:
        monkeypatch.setattr("kevin.cogs.economy.RNG.choice", lambda seq: choice)
    if randints:
        rolls = iter(randints)
        monkeypatch.setattr("kevin.cogs.economy.RNG.randint", lambda a, b: next(rolls))


async def play(name: str, wallet: Wallet, *args) -> None:
    cog = Economy(SimpleNamespace(db=wallet))
    await getattr(Economy, name).callback(cog, context(wallet), *args)


async def test_coinflip_cannot_be_won_from_an_empty_wallet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    force(monkeypatch, choice="heads")
    wallet = Wallet(0)

    with pytest.raises(commands.BadArgument, match="do not have enough"):
        await play("coinflip", wallet, "heads", 1_000_000)

    assert wallet.balance == 0


async def test_slots_cannot_be_won_from_an_empty_wallet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    force(monkeypatch, choice="💎")
    wallet = Wallet(0)

    with pytest.raises(commands.BadArgument, match="do not have enough"):
        await play("slots", wallet, 1_000_000)

    assert wallet.balance == 0


async def test_dice_cannot_be_won_from_an_empty_wallet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    force(monkeypatch, randints=(6, 1))
    wallet = Wallet(0)

    with pytest.raises(commands.BadArgument, match="do not have enough"):
        await play("dice", wallet, 1_000_000)

    assert wallet.balance == 0


async def test_a_bet_larger_than_the_wallet_is_refused_even_when_it_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    force(monkeypatch, choice="heads")
    wallet = Wallet(500)

    with pytest.raises(commands.BadArgument):
        await play("coinflip", wallet, "heads", 501)

    assert wallet.balance == 500


@pytest.mark.parametrize(
    ("name", "args", "choice", "randints", "expected"),
    (
        ("coinflip", ("heads", 100), "heads", (), 1_098),  # even money, less the rake
        ("coinflip", ("tails", 100), "heads", (), 900),
        ("slots", (100,), "💎", (), 1_400),  # three of a kind pays 5x
        ("dice", (100,), None, (6, 1), 1_098),
        ("dice", (100,), None, (1, 6), 900),
        ("dice", (100,), None, (4, 4), 1_000),  # a tie returns the stake
    ),
)
async def test_payouts_are_unchanged_for_players_who_can_cover_the_bet(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    args: tuple,
    choice: str | None,
    randints: tuple,
    expected: int,
) -> None:
    force(monkeypatch, choice=choice, randints=randints)
    wallet = Wallet(1_000)

    await play(name, wallet, *args)

    assert wallet.balance == expected
    assert wallet.deltas[0] == -args[-1], "the stake must be taken first"


async def test_robbing_an_ineligible_target_refunds_the_cooldown() -> None:
    wallet = Wallet(1_000)
    cog = Economy(SimpleNamespace(db=wallet))
    ctx = context(wallet)
    ctx.command = MagicMock()
    ctx.author = SimpleNamespace(id=2, bot=False)

    with pytest.raises(commands.BadArgument):
        await Economy.rob.callback(cog, ctx, member=ctx.author)

    ctx.command.reset_cooldown.assert_called_once_with(ctx)


async def test_robbing_a_broke_target_refunds_the_cooldown() -> None:
    wallet = Wallet(1_000)
    bot = SimpleNamespace(
        db=SimpleNamespace(
            ensure_member=AsyncMock(),
            fetchone=AsyncMock(return_value={"balance": 100, "bank": 0}),
        )
    )
    cog = Economy(bot)
    ctx = context(wallet)
    ctx.command = MagicMock()

    with pytest.raises(commands.CheckFailure):
        await Economy.rob.callback(cog, ctx, member=SimpleNamespace(id=9, bot=False))

    ctx.command.reset_cooldown.assert_called_once_with(ctx)
