from pathlib import Path

import pytest

from kevin.database import Database


@pytest.fixture
async def database(tmp_path: Path):
    db = Database(tmp_path / "test.sqlite3")
    await db.connect()
    yield db
    await db.close()


async def test_member_starts_with_balance(database: Database) -> None:
    await database.ensure_member(1, 2)
    row = await database.fetchone("SELECT balance FROM members WHERE guild_id = 1 AND user_id = 2")
    assert row["balance"] == 250


async def test_balance_cannot_go_negative(database: Database) -> None:
    with pytest.raises(ValueError):
        await database.change_balance(1, 2, -251)
    row = await database.fetchone("SELECT balance FROM members WHERE guild_id = 1 AND user_id = 2")
    assert row["balance"] == 250


async def test_transfer_is_atomic(database: Database) -> None:
    await database.transfer_balance(1, 10, 20, 100)
    sender = await database.fetchone(
        "SELECT balance FROM members WHERE guild_id = 1 AND user_id = 10"
    )
    recipient = await database.fetchone(
        "SELECT balance FROM members WHERE guild_id = 1 AND user_id = 20"
    )
    assert sender["balance"] == 150
    assert recipient["balance"] == 350

    with pytest.raises(ValueError):
        await database.transfer_balance(1, 10, 20, 151)
    unchanged = await database.fetchone(
        "SELECT balance FROM members WHERE guild_id = 1 AND user_id = 20"
    )
    assert unchanged["balance"] == 350


async def test_buy_item_is_atomic(database: Database) -> None:
    remaining = await database.buy_item(1, 2, "chocolate", 100, 2)
    assert remaining == 50
    item = await database.fetchone(
        "SELECT quantity FROM inventory WHERE guild_id = 1 AND user_id = 2"
    )
    assert item["quantity"] == 2
    with pytest.raises(ValueError):
        await database.buy_item(1, 2, "chocolate", 100, 1)


async def test_bank_moves_are_atomic(database: Database) -> None:
    assert await database.move_bank(1, 2, 200, to_bank=True) == (50, 200)
    with pytest.raises(ValueError):
        await database.move_bank(1, 2, 51, to_bank=True)
    assert await database.move_bank(1, 2, 100, to_bank=False) == (150, 100)


async def test_trivia_stats_track_streaks(database: Database) -> None:
    first = await database.record_trivia_answer(1, 2, correct=True)
    second = await database.record_trivia_answer(1, 2, correct=True)
    missed = await database.record_trivia_answer(1, 2, correct=False)

    assert first == {"answered": 1, "correct": 1, "streak": 1, "best_streak": 1}
    assert second == {"answered": 2, "correct": 2, "streak": 2, "best_streak": 2}
    assert missed == {"answered": 3, "correct": 2, "streak": 0, "best_streak": 2}


async def test_cooldown_reward_can_only_be_claimed_once(database: Database) -> None:
    first = await database.claim_reward(
        1,
        2,
        500,
        timestamp="2026-01-02T00:00:00+00:00",
        eligible_before="2026-01-01T00:00:00+00:00",
        column="last_daily",
    )
    second = await database.claim_reward(
        1,
        2,
        500,
        timestamp="2026-01-02T00:00:00+00:00",
        eligible_before="2026-01-01T00:00:00+00:00",
        column="last_daily",
    )
    assert first == 750
    assert second is None
