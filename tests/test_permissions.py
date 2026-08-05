from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from discord.ext import commands

from kevin.cogs.automod import AutoMod
from kevin.cogs.community import Community
from kevin.cogs.configuration import Configuration
from kevin.cogs.moderation import Moderation
from kevin.cogs.stream_alerts import StreamAlerts
from kevin.cogs.video_alerts import VideoAlerts
from kevin.utils.checks import owner_or_guild_permissions


def context(*, owner: bool, permissions: discord.Permissions) -> SimpleNamespace:
    return SimpleNamespace(
        bot=SimpleNamespace(is_owner=AsyncMock(return_value=owner)),
        author=SimpleNamespace(guild_permissions=permissions),
        guild=object(),
    )


async def test_configured_owner_bypasses_guild_permissions() -> None:
    check = owner_or_guild_permissions(administrator=True).predicate

    assert await check(context(owner=True, permissions=discord.Permissions.none()))


async def test_non_owner_still_needs_guild_permissions() -> None:
    check = owner_or_guild_permissions(manage_guild=True).predicate

    with pytest.raises(commands.MissingPermissions):
        await check(context(owner=False, permissions=discord.Permissions.none()))

    allowed = discord.Permissions.none()
    allowed.manage_guild = True
    assert await check(context(owner=False, permissions=allowed))


def test_restricted_hybrid_group_children_repeat_owner_aware_check() -> None:
    # Discord.py does not inherit a hybrid group's text-command checks when a
    # slash subcommand runs, so every restricted child must carry the check.
    groups = (
        AutoMod.automod,
        Community.giveaway,
        Community.reactionrole,
        Community.starboard,
        Configuration.config,
        Moderation.voice,
        Moderation.role,
        StreamAlerts.streamalert,
        VideoAlerts.youtubealert,
        VideoAlerts.tiktokalert,
    )

    for group in groups:
        for child in group.commands:
            assert any(
                "owner_or_guild_permissions" in check.__qualname__
                for check in child.checks
            ), child.qualified_name
