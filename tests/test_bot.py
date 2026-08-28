from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from kevin.bot import KevinBot, presence_activity
from kevin.config import Settings


@pytest.mark.parametrize("content", ["kping", "k ping", "k   ping"])
async def test_text_prefix_accepts_optional_whitespace(content: str) -> None:
    bot = KevinBot(Settings(token="test", default_prefix="k"))
    bot._connection.user = SimpleNamespace(id=999)

    @bot.command()
    async def ping(ctx) -> None:
        pass

    message = SimpleNamespace(
        content=content,
        author=SimpleNamespace(id=1),
        guild=None,
        _state=bot._connection,
    )

    context = await bot.get_context(message)

    assert context.command is ping


async def test_blocked_user_cannot_use_text_or_slash_commands() -> None:
    blocked_user_id = 1189439193861083149
    bot = KevinBot(Settings(token="test"))
    bot.get_context = AsyncMock()

    await bot.process_commands(SimpleNamespace(author=SimpleNamespace(id=blocked_user_id)))

    bot.get_context.assert_not_awaited()
    interaction = SimpleNamespace(user=SimpleNamespace(id=blocked_user_id))
    assert await bot.tree.interaction_check(interaction) is False


async def test_unblocked_user_can_use_slash_commands() -> None:
    bot = KevinBot(Settings(token="test"))
    interaction = SimpleNamespace(user=SimpleNamespace(id=1))

    assert await bot.tree.interaction_check(interaction) is True


def test_presence_is_streaming_when_url_is_configured() -> None:
    activity = presence_activity(
        Settings(
            token="test",
            status="with the community!",
            stream_url="https://twitch.tv/example",
        )
    )

    assert isinstance(activity, discord.Streaming)
    assert activity.type is discord.ActivityType.streaming
    assert activity.name == "with the community!"
    assert activity.url == "https://twitch.tv/example"


def test_presence_falls_back_to_watching_without_stream_url() -> None:
    activity = presence_activity(Settings(token="test", status="keeping the server tidy"))

    assert isinstance(activity, discord.Activity)
    assert activity.type is discord.ActivityType.watching
    assert activity.name == "keeping the server tidy"
