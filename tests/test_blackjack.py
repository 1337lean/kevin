from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from discord.ext import commands

from kevin.cogs.economy import (
    CARD_RANKS,
    CARD_SUITS,
    MAX_BET,
    BetConverter,
    Economy,
    GrantAmountConverter,
    KashAmountConverter,
    PlayingCard,
    blackjack_deck,
    blackjack_value,
    has_blackjack,
    parse_bet,
    parse_kash_amount,
    resolve_bet,
)


def hand(*ranks: str) -> list[PlayingCard]:
    return [PlayingCard(rank, "♠") for rank in ranks]


def test_blackjack_deck_is_standard_and_unique() -> None:
    deck = blackjack_deck()

    assert len(deck) == 52
    assert len(set(deck)) == 52
    assert {card.rank for card in deck} == set(CARD_RANKS)
    assert {card.suit for card in deck} == set(CARD_SUITS)


def test_blackjack_value_handles_soft_and_hard_aces() -> None:
    assert blackjack_value(hand("A", "6")) == (17, True)
    assert blackjack_value(hand("A", "6", "10")) == (17, False)
    assert blackjack_value(hand("A", "A", "9")) == (21, True)
    assert blackjack_value(hand("A", "A", "9", "K")) == (21, False)


def test_only_two_card_twenty_one_is_a_natural_blackjack() -> None:
    assert has_blackjack(hand("A", "K"))
    assert not has_blackjack(hand("7", "7", "7"))


def test_blackjack_has_text_and_slash_shortcuts() -> None:
    assert "bj" in Economy.blackjack.aliases
    assert Economy.blackjack_shortcut.name == "bj"


def test_blackjack_bet_supports_prefix_and_slash_shorthand() -> None:
    text_parameter = Economy.blackjack.clean_params["bet"]
    assert text_parameter.annotation is BetConverter

    slash_parameter = Economy.blackjack.app_command.parameters[0]
    assert slash_parameter.type is discord.AppCommandOptionType.string

    shortcut_parameter = Economy.blackjack_shortcut.parameters[0]
    assert shortcut_parameter.type is discord.AppCommandOptionType.string


@pytest.mark.parametrize(
    ("argument", "expected"),
    (("4000", 4_000), ("4k", 4_000), ("2.5K", 2_500), ("1m", 1_000_000)),
)
def test_parse_bet_shorthand(argument: str, expected: int) -> None:
    assert parse_bet(argument) == expected


@pytest.mark.parametrize("argument", ("0", "1.5", "lots"))
def test_parse_bet_rejects_invalid_values(argument: str) -> None:
    with pytest.raises(commands.BadArgument):
        parse_bet(argument)


def test_parse_bet_rejects_wagers_over_the_table_limit() -> None:
    assert parse_bet(str(MAX_BET)) == MAX_BET

    with pytest.raises(commands.BadArgument, match="between 1 and"):
        parse_bet(str(MAX_BET + 1))


@pytest.mark.parametrize(
    ("balance", "expected"),
    ((750, 750), (MAX_BET, MAX_BET), (2_500_000, MAX_BET)),
)
async def test_all_bet_uses_the_wallet_up_to_the_table_limit(balance: int, expected: int) -> None:
    db = SimpleNamespace(change_balance=AsyncMock(return_value=balance))
    ctx = SimpleNamespace(
        bot=SimpleNamespace(db=db),
        guild=SimpleNamespace(id=123),
        author=SimpleNamespace(id=456),
    )

    assert await resolve_bet(ctx, " ALL ") == expected
    db.change_balance.assert_awaited_once_with(123, 456, 0)


async def test_all_bet_rejects_an_empty_wallet() -> None:
    ctx = SimpleNamespace(
        bot=SimpleNamespace(db=SimpleNamespace(change_balance=AsyncMock(return_value=0))),
        guild=SimpleNamespace(id=123),
        author=SimpleNamespace(id=456),
    )

    with pytest.raises(commands.BadArgument, match="do not have any Kash"):
        await resolve_bet(ctx, "all")


def test_all_gambling_commands_share_the_unlimited_bet_converter() -> None:
    for command in (Economy.coinflip, Economy.slots, Economy.dice, Economy.blackjack):
        assert command.clean_params["bet"].annotation is BetConverter
        assert command.app_command.get_parameter("bet").type is discord.AppCommandOptionType.string


def test_economy_money_commands_support_compact_amounts() -> None:
    assert Economy.pay.clean_params["amount"].annotation is KashAmountConverter
    assert Economy.addmoney.clean_params["amount"].annotation is GrantAmountConverter
    assert Economy.pay.app_command.get_parameter("amount").type is discord.AppCommandOptionType.string
    assert Economy.addmoney.app_command.get_parameter("amount").type is discord.AppCommandOptionType.string

    assert parse_kash_amount("50k") == 50_000
    with pytest.raises(commands.BadArgument):
        parse_kash_amount("1.1m")


async def test_addmoney_accepts_grants_over_100_million() -> None:
    ctx = SimpleNamespace()

    assert await GrantAmountConverter().convert(ctx, "250m") == 250_000_000


async def test_bot_owner_can_use_addmoney_without_server_administrator() -> None:
    owner_or_admin = next(
        check
        for check in Economy.addmoney.checks
        if "owner_or_guild_permissions" in check.__qualname__
    )
    ctx = SimpleNamespace(
        bot=SimpleNamespace(is_owner=AsyncMock(return_value=True)),
        author=SimpleNamespace(guild_permissions=discord.Permissions.none()),
        guild=object(),
    )

    assert await owner_or_admin(ctx)
    assert Economy.addmoney.app_command.default_permissions is None
