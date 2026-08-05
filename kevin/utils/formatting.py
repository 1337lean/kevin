from __future__ import annotations

import re
from datetime import timedelta

import discord

KEVIN_BLUE = 0x36A9E1
SUCCESS_GREEN = 0x57F287
ERROR_RED = 0xED4245

_DURATION_RE = re.compile(r"(?i)(\d+)\s*([smhdw])")


def embed(title: str, description: str = "", *, color: int = KEVIN_BLUE) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=color)


def success(description: str) -> discord.Embed:
    return embed("Done", description, color=SUCCESS_GREEN)


def error(description: str) -> discord.Embed:
    return embed("That didn't work", description, color=ERROR_RED)


def parse_duration(value: str, *, maximum: timedelta | None = None) -> timedelta:
    value = value.strip().lower()
    matches = list(_DURATION_RE.finditer(value))
    if not matches or "".join(
        match.group(0).replace(" ", "") for match in matches
    ) != value.replace(" ", ""):
        raise ValueError("Use a duration like `30m`, `2h`, `3d`, or `1w`.")
    seconds = 0
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    for match in matches:
        seconds += int(match.group(1)) * multipliers[match.group(2).lower()]
    result = timedelta(seconds=seconds)
    if result <= timedelta(0):
        raise ValueError("Duration must be positive.")
    if maximum and result > maximum:
        raise ValueError(f"Duration cannot exceed {human_duration(maximum)}.")
    return result


def human_duration(value: timedelta | int | float) -> str:
    seconds = int(value.total_seconds() if isinstance(value, timedelta) else value)
    units = ((604800, "week"), (86400, "day"), (3600, "hour"), (60, "minute"), (1, "second"))
    parts: list[str] = []
    for size, label in units:
        count, seconds = divmod(seconds, size)
        if count:
            parts.append(f"{count} {label}{'s' if count != 1 else ''}")
        if len(parts) == 2:
            break
    return ", ".join(parts) or "0 seconds"


def progress_bar(current: int, total: int, *, length: int = 10) -> str:
    total = max(total, 1)
    filled = min(length, max(0, round(length * current / total)))
    return "▰" * filled + "▱" * (length - filled)
