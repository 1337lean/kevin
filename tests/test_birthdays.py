from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kevin.cogs.birthdays import (
    Birthdays,
    is_birthday_today,
    next_occurrence,
    turning_age,
    validate_birthday,
)
from kevin.cogs.configuration import Configuration
from kevin.database import Database


@pytest.fixture
async def database(tmp_path: Path):
    db = Database(tmp_path / "test.sqlite3")
    await db.connect()
    yield db
    await db.close()


def test_validate_birthday_accepts_real_dates() -> None:
    validate_birthday(1, 31, None)
    validate_birthday(2, 29, 2000)
    validate_birthday(4, 30, 1995)


def test_validate_birthday_rejects_impossible_dates() -> None:
    with pytest.raises(ValueError):
        validate_birthday(13, 1, None)
    with pytest.raises(ValueError):
        validate_birthday(0, 10, None)
    with pytest.raises(ValueError):
        validate_birthday(4, 31, None)
    with pytest.raises(ValueError):
        validate_birthday(2, 30, None)


def test_validate_birthday_rejects_future_dates(monkeypatch: pytest.MonkeyPatch) -> None:
    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 8, 22)

    monkeypatch.setattr("kevin.cogs.birthdays.date", FixedDate)

    with pytest.raises(ValueError):
        validate_birthday(6, 15, 2027)
    with pytest.raises(ValueError, match="future"):
        validate_birthday(12, 1, 2026)
    with pytest.raises(ValueError, match="real calendar date"):
        validate_birthday(2, 29, 2026)

    validate_birthday(8, 22, 2026)


def test_is_birthday_today_matches_month_and_day() -> None:
    assert is_birthday_today(7, 4, date(2026, 7, 4))
    assert not is_birthday_today(7, 4, date(2026, 7, 5))


def test_feb_29_celebrated_on_leap_day_and_feb_28_otherwise() -> None:
    assert is_birthday_today(2, 29, date(2028, 2, 29))
    # Non-leap year: celebrate on the 28th instead.
    assert is_birthday_today(2, 29, date(2027, 2, 28))
    assert not is_birthday_today(2, 29, date(2027, 2, 27))
    assert not is_birthday_today(2, 29, date(2027, 3, 1))


def test_next_occurrence_handles_past_present_and_future() -> None:
    today = date(2026, 8, 22)
    assert next_occurrence(8, 21, today) == date(2027, 8, 21)
    assert next_occurrence(8, 22, today) == today
    assert next_occurrence(12, 1, today) == date(2026, 12, 1)


def test_next_occurrence_moves_feb_29_off_non_leap_years() -> None:
    assert next_occurrence(2, 29, date(2027, 1, 1)) == date(2027, 2, 28)
    assert next_occurrence(2, 29, date(2028, 1, 1)) == date(2028, 2, 29)


def test_turning_age_needs_a_year() -> None:
    assert turning_age(None, date(2026, 8, 22)) is None
    assert turning_age(2000, date(2026, 8, 22)) == 26


async def test_birthday_round_trip(database: Database) -> None:
    await database.set_birthday(1, 10, 3, 14, 2000)
    row = await database.get_birthday(1, 10)
    assert (row["month"], row["day"], row["year"]) == (3, 14, 2000)

    await database.mark_birthday_announced(1, 10, 2026)
    row = await database.get_birthday(1, 10)
    assert row["announced_year"] == 2026

    # Re-setting clears the announcement marker for the new date.
    await database.set_birthday(1, 10, 6, 21, None)
    row = await database.get_birthday(1, 10)
    assert (row["month"], row["day"], row["year"], row["announced_year"]) == (6, 21, None, 0)

    assert await database.remove_birthday(1, 10)
    assert not await database.remove_birthday(1, 10)
    assert await database.get_birthday(1, 10) is None


async def test_list_birthdays_is_calendar_ordered(database: Database) -> None:
    await database.set_birthday(1, 10, 12, 1, None)
    await database.set_birthday(1, 20, 1, 2, None)
    rows = await database.list_birthdays(1)
    assert [(row["month"], row["day"]) for row in rows] == [(1, 2), (12, 1)]


async def test_birthday_commands_cover_set_show_list_and_remove(database: Database) -> None:
    member = SimpleNamespace(id=10, display_name="Alex")
    guild = SimpleNamespace(id=1, get_member=lambda user_id: member if user_id == 10 else None)
    ctx = SimpleNamespace(guild=guild, author=member, send=AsyncMock())
    cog = Birthdays(SimpleNamespace(db=database))

    await Birthdays.birthday.callback(cog, ctx)
    assert ctx.send.await_args.kwargs["embed"].title == "Birthdays"

    await Birthdays.birthday_set.callback(cog, ctx, 3, 14, 2000)
    saved = await database.get_birthday(1, 10)
    assert (saved["month"], saved["day"], saved["year"]) == (3, 14, 2000)
    assert ctx.send.await_args.kwargs["ephemeral"] is True

    await Birthdays.birthday.callback(cog, ctx)
    assert ctx.send.await_args.kwargs["embed"].title == "Your birthday"

    await Birthdays.birthday_list.callback(cog, ctx)
    upcoming = ctx.send.await_args.kwargs["embed"]
    assert upcoming.title == "Upcoming birthdays"
    assert "Alex" in upcoming.description

    await Birthdays.birthday_remove.callback(cog, ctx)
    assert await database.get_birthday(1, 10) is None
    assert ctx.send.await_args.kwargs["ephemeral"] is True


async def test_birthday_channel_config_can_be_set_and_disabled(database: Database) -> None:
    ctx = SimpleNamespace(guild=SimpleNamespace(id=1), send=AsyncMock())
    cog = Configuration(SimpleNamespace(db=database))
    channel = SimpleNamespace(id=20, mention="<#20>")

    await Configuration.birthdays.callback(cog, ctx, channel)
    assert (await database.get_settings(1))["birthday_channel_id"] == 20

    await Configuration.birthdays.callback(cog, ctx, None)
    assert (await database.get_settings(1))["birthday_channel_id"] is None
