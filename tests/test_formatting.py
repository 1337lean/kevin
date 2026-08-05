from datetime import timedelta

import pytest

from kevin.utils.formatting import human_duration, parse_duration, progress_bar


def test_parse_compound_duration() -> None:
    assert parse_duration("1h 30m") == timedelta(minutes=90)
    assert parse_duration("2d") == timedelta(days=2)


@pytest.mark.parametrize("value", ["", "later", "5x", "-2h"])
def test_rejects_bad_duration(value: str) -> None:
    with pytest.raises(ValueError):
        parse_duration(value)


def test_duration_limit() -> None:
    with pytest.raises(ValueError):
        parse_duration("29d", maximum=timedelta(days=28))


def test_human_duration_and_progress() -> None:
    assert human_duration(3660) == "1 hour, 1 minute"
    assert progress_bar(50, 100, length=10) == "▰▰▰▰▰▱▱▱▱▱"
