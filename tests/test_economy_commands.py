from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from discord.ext import commands

from kevin.cogs.economy import Economy, RobTargetConverter


def test_bal_registers_top_subcommand() -> None:
    command = Economy.bal

    assert isinstance(command, commands.Group)
    assert command.invoke_without_command
    assert command.get_command("top") is Economy.bal_top


async def test_bal_top_shows_richest_members_in_order() -> None:
    bot = SimpleNamespace(
        db=SimpleNamespace(
            fetchall=AsyncMock(
                return_value=[
                    {"user_id": 10, "wealth": 12_500},
                    {"user_id": 20, "wealth": 8_000},
                ]
            )
        )
    )
    ctx = SimpleNamespace(guild=SimpleNamespace(id=7), send=AsyncMock())

    await Economy(bot).send_richest(ctx)

    bot.db.fetchall.assert_awaited_once()
    sent_embed = ctx.send.await_args.kwargs["embed"]
    assert sent_embed.title == "💵 Richest explorers"
    assert sent_embed.description == (
        "**1.** <@10> — 12,500 Kash\n"
        "**2.** <@20> — 8,000 Kash"
    )


async def test_rob_target_accepts_unambiguous_partial_display_name() -> None:
    niels = SimpleNamespace(display_name="YC NIELS", name="niels_account")
    other = SimpleNamespace(display_name="Someone Else", name="someone")
    ctx = SimpleNamespace(guild=SimpleNamespace(members=[niels, other]))

    with patch.object(
        commands.MemberConverter,
        "convert",
        AsyncMock(side_effect=commands.MemberNotFound("niels")),
    ):
        result = await RobTargetConverter().convert(ctx, "niels")

    assert result is niels


async def test_rob_target_rejects_ambiguous_partial_name() -> None:
    ctx = SimpleNamespace(
        guild=SimpleNamespace(
            members=[
                SimpleNamespace(display_name="Niels One", name="one"),
                SimpleNamespace(display_name="Niels Two", name="two"),
            ]
        )
    )

    with (
        patch.object(
            commands.MemberConverter,
            "convert",
            AsyncMock(side_effect=commands.MemberNotFound("niels")),
        ),
        pytest.raises(commands.BadArgument, match="More than one member matches"),
    ):
        await RobTargetConverter().convert(ctx, "niels")


def test_failed_rob_target_parsing_does_not_consume_cooldown() -> None:
    assert Economy.rob.cooldown_after_parsing


def test_rob_slash_command_keeps_member_picker() -> None:
    assert Economy.rob.app_command.parameters[0].type.name == "user"
