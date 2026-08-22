from datetime import date
from pathlib import Path

import pytest

from kevin.cogs.birthdays import (
    is_birthday_today,
    next_occurrence,
    turning_age,
    validate_birthday,
)
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


def test_validate_birthday_rejects_future_years() -> None:
    with pytest.raises(ValueError):
        validate_birthday(6, 15, date.today().year + 1)


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
