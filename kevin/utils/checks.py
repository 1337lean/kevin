from __future__ import annotations

from typing import Any

from discord.ext import commands


def owner_or_guild_permissions(**permissions: Any) -> commands.Check[Any]:
    """Require guild permissions, while always allowing a configured bot owner."""
    guild_permissions = commands.has_guild_permissions(**permissions).predicate

    async def predicate(ctx: commands.Context) -> bool:
        if await ctx.bot.is_owner(ctx.author):
            return True
        return await guild_permissions(ctx)

    return commands.check(predicate)
