from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kevin.cogs.leveling import Leveling, level_for_xp, xp_for_level


@pytest.mark.parametrize("level", range(25))
def test_level_boundaries(level: int) -> None:
    threshold = xp_for_level(level)
    assert level_for_xp(threshold) == level
    if level:
        assert level_for_xp(threshold - 1) == level - 1


def test_xp_curve_increases() -> None:
    assert all(xp_for_level(level + 1) > xp_for_level(level) for level in range(100))


async def test_commands_do_not_award_activity_xp() -> None:
    bot = SimpleNamespace(
        get_context=AsyncMock(return_value=SimpleNamespace(valid=True)),
        db=SimpleNamespace(get_settings=AsyncMock()),
    )
    message = SimpleNamespace(
        guild=SimpleNamespace(id=1),
        author=SimpleNamespace(id=2, bot=False),
        content="k setxp <@2> 1000000",
    )

    await Leveling(bot).on_message(message)

    bot.get_context.assert_awaited_once_with(message)
    bot.db.get_settings.assert_not_awaited()
