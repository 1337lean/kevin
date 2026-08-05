from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from discord.ext import commands

from kevin.cogs.economy import (
    CRAPS_POINTS,
    DONT_PASS,
    PASS_LINE,
    BetConverter,
    CrapsLineConverter,
    CrapsView,
    DiceRoll,
    Economy,
    come_out_outcome,
    odds_profit,
    parse_craps_line,
    point_outcome,
    roll_dice,
)


def view(line: str = PASS_LINE, bet: int = 100, balance: int = 900) -> CrapsView:
    return CrapsView(
        SimpleNamespace(db=SimpleNamespace(change_balance=AsyncMock())),
        SimpleNamespace(id=456, display_name="Roger"),
        123,
        bet,
        balance,
        line,
        lambda: None,
    )


@pytest.mark.parametrize(
    ("argument", "expected"),
    (
        ("pass", PASS_LINE),
        ("PASS LINE", PASS_LINE),
        ("p", PASS_LINE),
        ("dontpass", DONT_PASS),
        ("don't pass", DONT_PASS),
        ("Dont-Pass Line", DONT_PASS),
        ("dp", DONT_PASS),
    ),
)
def test_parse_craps_line_accepts_common_spellings(argument: str, expected: str) -> None:
    assert parse_craps_line(argument) == expected


@pytest.mark.parametrize("argument", ("come", "field", ""))
def test_parse_craps_line_rejects_other_bets(argument: str) -> None:
    with pytest.raises(commands.BadArgument):
        parse_craps_line(argument)


@pytest.mark.parametrize(
    ("total", "expected"),
    ((7, "win"), (11, "win"), (2, "lose"), (3, "lose"), (12, "lose"), (4, "point"), (10, "point")),
)
def test_pass_line_come_out(total: int, expected: str) -> None:
    assert come_out_outcome(total, PASS_LINE) == expected


@pytest.mark.parametrize(
    ("total", "expected"),
    ((2, "win"), (3, "win"), (12, "push"), (7, "lose"), (11, "lose"), (6, "point")),
)
def test_dont_pass_come_out_bars_twelve(total: int, expected: str) -> None:
    assert come_out_outcome(total, DONT_PASS) == expected


def test_point_rounds_only_end_on_the_point_or_a_seven() -> None:
    assert point_outcome(6, 6, PASS_LINE) == "win"
    assert point_outcome(7, 6, PASS_LINE) == "lose"
    assert point_outcome(6, 6, DONT_PASS) == "lose"
    assert point_outcome(7, 6, DONT_PASS) == "win"
    for total in (2, 3, 4, 5, 8, 9, 10, 11, 12):
        assert point_outcome(total, 6, PASS_LINE) is None


@pytest.mark.parametrize(
    ("point", "taken", "laid"),
    ((4, 200, 50), (10, 200, 50), (5, 150, 66), (9, 150, 66), (6, 120, 83), (8, 120, 83)),
)
def test_odds_pay_true_odds_both_ways(point: int, taken: int, laid: int) -> None:
    assert odds_profit(point, 100, PASS_LINE) == taken
    assert odds_profit(point, 100, DONT_PASS) == laid


def test_roll_dice_stays_within_two_six_sided_dice() -> None:
    rolls = [roll_dice() for _ in range(200)]

    assert all(1 <= roll.first <= 6 and 1 <= roll.second <= 6 for roll in rolls)
    assert all(roll.total == roll.first + roll.second for roll in rolls)


def test_come_out_establishes_a_point_and_unlocks_odds(monkeypatch: pytest.MonkeyPatch) -> None:
    game = view()
    monkeypatch.setattr("kevin.cogs.economy.roll_dice", lambda: DiceRoll(4, 2))

    assert game.come_out() is None
    assert game.point == 6
    assert not game.take_odds.disabled


def test_odds_button_stays_locked_without_a_second_stake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    game = view(balance=99)
    monkeypatch.setattr("kevin.cogs.economy.roll_dice", lambda: DiceRoll(4, 2))

    game.come_out()

    assert game.take_odds.disabled


@pytest.mark.parametrize(
    ("first", "second", "line", "payout"),
    (
        (3, 4, PASS_LINE, 200),
        (5, 6, PASS_LINE, 200),
        (1, 1, PASS_LINE, 0),
        (1, 1, DONT_PASS, 200),
        (6, 6, DONT_PASS, 100),
        (3, 4, DONT_PASS, 0),
    ),
)
def test_come_out_settles_naturals_and_craps(
    monkeypatch: pytest.MonkeyPatch, first: int, second: int, line: str, payout: int
) -> None:
    game = view(line)
    monkeypatch.setattr("kevin.cogs.economy.roll_dice", lambda: DiceRoll(first, second))

    settlement = game.come_out()

    assert settlement is not None
    assert settlement[0] == payout
    assert game.point is None


def test_hitting_the_point_returns_the_stake_and_odds(monkeypatch: pytest.MonkeyPatch) -> None:
    game = view()
    monkeypatch.setattr("kevin.cogs.economy.roll_dice", lambda: DiceRoll(2, 2))
    game.come_out()
    game.odds = 100

    settlement = game.point_roll()

    assert settlement is not None
    # 100 line + 100 odds staked, plus 100 line profit and 200 taken-odds profit.
    assert settlement[0] == 500


def test_seven_out_forfeits_the_line_and_odds(monkeypatch: pytest.MonkeyPatch) -> None:
    game = view()
    monkeypatch.setattr("kevin.cogs.economy.roll_dice", lambda: DiceRoll(2, 2))
    game.come_out()
    game.odds = 100
    monkeypatch.setattr("kevin.cogs.economy.roll_dice", lambda: DiceRoll(3, 4))

    settlement = game.point_roll()

    assert settlement is not None
    assert settlement[0] == 0
    assert "1,000" not in settlement[2]
    assert "200 Kash" in settlement[2]


def test_dont_pass_wins_the_point_round_on_a_seven(monkeypatch: pytest.MonkeyPatch) -> None:
    game = view(DONT_PASS)
    monkeypatch.setattr("kevin.cogs.economy.roll_dice", lambda: DiceRoll(5, 5))
    game.come_out()
    game.odds = 100
    monkeypatch.setattr("kevin.cogs.economy.roll_dice", lambda: DiceRoll(1, 6))

    settlement = game.point_roll()

    assert settlement is not None
    # 200 staked, plus 100 line profit and 50 laid-odds profit on the 10.
    assert settlement[0] == 350


def test_point_rounds_keep_rolling_until_resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    game = view()
    monkeypatch.setattr("kevin.cogs.economy.roll_dice", lambda: DiceRoll(4, 4))
    game.come_out()
    monkeypatch.setattr("kevin.cogs.economy.roll_dice", lambda: DiceRoll(2, 3))

    assert game.point_roll() is None
    assert game.point == 8
    assert len(game.history) == 2


def test_every_point_has_a_true_odds_payout() -> None:
    for point in CRAPS_POINTS:
        assert odds_profit(point, 100, PASS_LINE) > 0
        assert odds_profit(point, 100, DONT_PASS) > 0


def test_craps_shares_the_gambling_bet_converter_and_line_choice() -> None:
    assert Economy.craps.clean_params["bet"].annotation is BetConverter
    assert Economy.craps.clean_params["line"].annotation is CrapsLineConverter
    assert Economy.craps.app_command.get_parameter("bet").type is discord.AppCommandOptionType.string
    assert not Economy.craps.app_command.get_parameter("line").required
