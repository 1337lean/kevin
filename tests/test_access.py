from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from kevin.bot import KevinBot
from kevin.cogs.ai import AI
from kevin.config import Settings

SADREW = 272282285141786625
OWNER = 1267893879919738991
BLOCKED_USER = 1189439193861083149


@pytest.mark.parametrize("user_id,allowed", [(SADREW, True), (OWNER, True), (BLOCKED_USER, False)])
async def test_prefix_commands_gate_only_blocked_account(user_id, allowed):
    bot = KevinBot(Settings(token="test"))
    message = SimpleNamespace(author=SimpleNamespace(id=user_id))
    with patch("discord.ext.commands.Bot.process_commands", new_callable=AsyncMock) as process:
        await bot.process_commands(message)
    assert process.await_count == int(allowed)


@pytest.mark.parametrize("interaction_type", [2, 3, 4, 5])
@pytest.mark.parametrize("in_guild", [False, True])
@pytest.mark.parametrize("user_id,allowed", [(SADREW, True), (OWNER, True), (BLOCKED_USER, False)])
def test_interactions_gate_before_discord_routing(interaction_type, in_guild, user_id, allowed):
    bot = KevinBot(Settings(token="test"))
    bot._interaction_parser = Mock()
    user = {"id": str(user_id)}
    payload = {"type": interaction_type}
    payload.update({"member": {"user": user}} if in_guild else {"user": user})

    bot._connection.parsers["INTERACTION_CREATE"](payload)

    assert bot._interaction_parser.call_count == int(allowed)


@pytest.mark.parametrize("content", ["<@999> hello", "Kevin hello", "reply to Kevin"])
async def test_blocked_ai_message_never_observed_or_answered(content):
    cog = AI(SimpleNamespace(user=SimpleNamespace(id=999), settings=Settings(token="test")))
    cog._record_observation = AsyncMock()
    cog._ask_openai = AsyncMock()
    message = SimpleNamespace(author=SimpleNamespace(id=BLOCKED_USER), content=content, reply=AsyncMock())

    await cog.on_message(message)

    cog._record_observation.assert_not_awaited()
    cog._ask_openai.assert_not_awaited()
    message.reply.assert_not_awaited()
