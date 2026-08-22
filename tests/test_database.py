from pathlib import Path

import aiosqlite
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


async def test_ai_chat_memory_is_bounded_and_server_scoped(database: Database) -> None:
    for message_id in range(1, 4):
        await database.record_ai_chat_message(
            10,
            20,
            message_id,
            30,
            "Alex",
            f"message {message_id}",
            f"2026-01-0{message_id}T00:00:00+00:00",
            channel_limit=2,
        )
    await database.replace_ai_member_memory(10, 30, "Alex", ["Likes co-op games"])
    await database.replace_ai_member_memory(11, 30, "Alex", ["Likes chess"])

    messages = await database.get_ai_chat_messages(10, 20)
    profile = await database.get_ai_member_memory(10, 30)

    assert [message["message_id"] for message in messages] == [2, 3]
    assert profile is not None
    assert profile["notes"] == ["Likes co-op games"]
    assert (await database.get_ai_member_memory(11, 30))["notes"] == ["Likes chess"]


async def test_ai_chat_memory_resolves_discord_reply_targets(database: Database) -> None:
    await database.record_ai_chat_message(
        10,
        20,
        100,
        30,
        "Alex",
        "The original message",
        "2026-01-01T00:00:00+00:00",
    )
    await database.record_ai_chat_message(
        10,
        20,
        101,
        40,
        "Piss",
        "Thats dope",
        "2026-01-01T00:01:00+00:00",
        reply_to_message_id=100,
    )

    messages = await database.get_ai_chat_messages(10, 20)

    assert messages[1]["reply_to_message_id"] == 100
    assert messages[1]["reply_to_user_id"] == 30
    assert messages[1]["reply_to_display_name"] == "Alex"
    assert messages[1]["reply_to_content"] == "The original message"


async def test_database_migrates_existing_ai_reply_schema(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    async with aiosqlite.connect(path) as connection:
        await connection.execute(
            "CREATE TABLE ai_chat_messages ("
            "message_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, "
            "channel_id INTEGER NOT NULL, user_id INTEGER NOT NULL, "
            "display_name TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        await connection.commit()

    database = Database(path)
    await database.connect()
    try:
        columns = await database.fetchall("PRAGMA table_info(ai_chat_messages)")
        assert "reply_to_message_id" in {str(column["name"]) for column in columns}
    finally:
        await database.close()


async def test_database_migrates_existing_birthday_channel_schema(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    async with aiosqlite.connect(path) as connection:
        await connection.execute("CREATE TABLE guild_settings (guild_id INTEGER PRIMARY KEY)")
        await connection.execute("INSERT INTO guild_settings(guild_id) VALUES (10)")
        await connection.commit()

    database = Database(path)
    await database.connect()
    try:
        columns = await database.fetchall("PRAGMA table_info(guild_settings)")
        assert "birthday_channel_id" in {str(column["name"]) for column in columns}

        await database.set_setting(10, "birthday_channel_id", 20)
        settings = await database.get_settings(10)
        assert settings["birthday_channel_id"] == 20
    finally:
        await database.close()


async def test_ai_memory_tracks_member_names_and_mem0_watermarks(database: Database) -> None:
    await database.record_ai_chat_message(
        10, 20, 1, 30, "Alex", "I like co-op games", "2026-01-01T00:00:00+00:00"
    )
    await database.record_ai_chat_message(
        10, 20, 2, 30, "Alexandra", "call me Alex", "2026-01-02T00:00:00+00:00"
    )

    assert await database.get_ai_memory_members(10) == [
        {"user_id": 30, "display_name": "Alexandra"}
    ]
    assert await database.get_ai_memory_watermark(10, 20) == 0

    await database.set_ai_memory_watermark(10, 20, 2)
    await database.set_ai_memory_watermark(10, 20, 1)

    assert await database.get_ai_memory_watermark(10, 20) == 2


async def test_ai_memory_opt_out_erases_user_data(database: Database) -> None:
    await database.record_ai_chat_message(
        10, 20, 1, 30, "Alex", "I like co-op games", "2026-01-01T00:00:00+00:00"
    )
    await database.replace_ai_member_memory(10, 30, "Alex", ["Likes co-op games"])

    await database.set_ai_memory_opt_out(10, 30, opted_out=True)

    assert (10, 30) in await database.get_ai_memory_opt_outs()
    assert await database.get_ai_member_memory(10, 30) is None
    assert await database.get_ai_chat_messages(10, 20) == []
    assert await database.get_ai_memory_members(10) == []

    await database.record_ai_chat_message(
        10, 20, 2, 30, "Alex", "late message", "2026-01-02T00:00:00+00:00"
    )
    await database.replace_ai_member_memory(10, 30, "Alex", ["Late note"])
    assert await database.get_ai_chat_messages(10, 20) == []
    assert await database.get_ai_member_memory(10, 30) is None

    await database.set_ai_memory_opt_out(10, 30, opted_out=False)
    assert (10, 30) not in await database.get_ai_memory_opt_outs()


async def test_disabling_server_ai_memory_erases_observations_and_notes(
    database: Database,
) -> None:
    await database.record_ai_chat_message(
        10, 20, 1, 30, "Alex", "I like co-op games", "2026-01-01T00:00:00+00:00"
    )
    await database.replace_ai_member_memory(10, 30, "Alex", ["Likes co-op games"])

    await database.set_ai_memory_enabled(10, False)

    assert await database.ai_memory_enabled(10) is False
    assert await database.get_ai_chat_messages(10, 20) == []
    assert await database.get_ai_member_memory(10, 30) is None
    assert await database.get_ai_memory_members(10) == []

    await database.record_ai_chat_message(
        10, 20, 2, 30, "Alex", "late message", "2026-01-02T00:00:00+00:00"
    )
    await database.replace_ai_member_memory(10, 30, "Alex", ["Late note"])
    assert await database.get_ai_chat_messages(10, 20) == []
    assert await database.get_ai_member_memory(10, 30) is None
