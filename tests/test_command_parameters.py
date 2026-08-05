"""Prefix commands must only use annotations that ext.commands can convert.

`app_commands.Range` (and friends) resolve to a bare Transformer, which the text
command parser cannot call — invoking the command with a prefix fails with
`Converting to "RangeTransformer" failed`. `commands.Range` works for both.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil

import pytest
from discord import app_commands
from discord.ext import commands

import kevin.cogs


def text_commands() -> list[tuple[str, commands.Command]]:
    found: list[tuple[str, commands.Command]] = []
    for info in pkgutil.iter_modules(kevin.cogs.__path__):
        module = importlib.import_module(f"kevin.cogs.{info.name}")
        for cog in vars(module).values():
            if not (inspect.isclass(cog) and issubclass(cog, commands.Cog)):
                continue
            for attribute in vars(cog).values():
                if isinstance(attribute, commands.Command):
                    found.append((f"{info.name}.{attribute.qualified_name}", attribute))
    return found


def test_the_cog_scan_actually_finds_commands() -> None:
    names = [name for name, _ in text_commands()]

    assert len(names) > 50
    assert any(name.endswith("purge") for name in names)


@pytest.mark.parametrize(("name", "command"), text_commands(), ids=lambda v: getattr(v, "", v))
def test_prefix_commands_avoid_app_only_transformers(
    name: str, command: commands.Command
) -> None:
    for parameter in command.clean_params.values():
        converter = parameter.converter
        if not isinstance(converter, app_commands.Transformer):
            continue
        # A converter may also subclass commands.Converter, which text invocation handles.
        assert isinstance(converter, commands.Converter) or (
            inspect.isclass(converter) and issubclass(converter, commands.Converter)
        ), (
            f"{name} parameter '{parameter.name}' uses "
            f"{type(converter).__name__}, which prefix invocation cannot convert. "
            "Use commands.Range instead of app_commands.Range."
        )


@pytest.mark.parametrize(
    ("cog_name", "command_name", "parameter", "low", "high"),
    (
        ("moderation", "purge", "amount", 1, 500),
        ("moderation", "slowmode", "seconds", 0, 21600),
        ("moderation", "ban", "delete_days", 0, 7),
        ("music", "volume", "percent", 1, 100),
        ("community", "giveaway start", "winners", 1, 20),
        ("community", "starboard set", "threshold", 1, 25),
        ("economy", "buy", "quantity", 1, 100),
    ),
)
def test_bounded_numbers_keep_their_slash_limits(
    cog_name: str, command_name: str, parameter: str, low: int, high: int
) -> None:
    command = next(c for name, c in text_commands() if name == f"{cog_name}.{command_name}")
    option = command.app_command.get_parameter(parameter)

    assert (option.min_value, option.max_value) == (low, high)


async def test_purge_amount_converts_from_a_prefix_message() -> None:
    from discord.ext.commands.converter import run_converters

    command = next(c for name, c in text_commands() if name == "moderation.purge")
    amount = command.clean_params["amount"]

    assert await run_converters(None, amount.converter, "10", amount) == 10

    with pytest.raises(commands.RangeError):
        await run_converters(None, amount.converter, "501", amount)
