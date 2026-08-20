from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id INTEGER PRIMARY KEY,
    prefix TEXT,
    log_channel_id INTEGER,
    welcome_channel_id INTEGER,
    welcome_message TEXT,
    goodbye_channel_id INTEGER,
    goodbye_message TEXT,
    autorole_id INTEGER,
    starboard_channel_id INTEGER,
    starboard_threshold INTEGER NOT NULL DEFAULT 3,
    suggestion_channel_id INTEGER,
    economy_enabled INTEGER NOT NULL DEFAULT 1,
    automod_enabled INTEGER NOT NULL DEFAULT 0,
    automod_invites INTEGER NOT NULL DEFAULT 0,
    automod_links INTEGER NOT NULL DEFAULT 0,
    automod_spam INTEGER NOT NULL DEFAULT 1,
    automod_caps INTEGER NOT NULL DEFAULT 0,
    automod_mentions INTEGER NOT NULL DEFAULT 1,
    automod_max_mentions INTEGER NOT NULL DEFAULT 5,
    automod_bad_words TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    target_id INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_cases_guild ON cases(guild_id, id DESC);

CREATE TABLE IF NOT EXISTS warnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_warnings_user ON warnings(guild_id, user_id);

CREATE TABLE IF NOT EXISTS members (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    balance INTEGER NOT NULL DEFAULT 250,
    bank INTEGER NOT NULL DEFAULT 0,
    last_daily TEXT,
    last_work TEXT,
    last_rob TEXT,
    PRIMARY KEY (guild_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_members_balance ON members(guild_id, balance DESC);

CREATE TABLE IF NOT EXISTS inventory (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    item_key TEXT NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id, item_key)
);

CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    guild_id INTEGER,
    message TEXT NOT NULL,
    due_at TEXT NOT NULL,
    delivered INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(delivered, due_at);

CREATE TABLE IF NOT EXISTS reaction_roles (
    guild_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    emoji TEXT NOT NULL,
    role_id INTEGER NOT NULL,
    PRIMARY KEY (message_id, emoji)
);

CREATE TABLE IF NOT EXISTS tickets (
    channel_id INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    owner_id INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS tags (
    guild_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    content TEXT NOT NULL,
    owner_id INTEGER NOT NULL,
    uses INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (guild_id, name)
);

CREATE TABLE IF NOT EXISTS giveaways (
    message_id INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    host_id INTEGER NOT NULL,
    prize TEXT NOT NULL,
    winner_count INTEGER NOT NULL DEFAULT 1,
    end_at TEXT NOT NULL,
    ended INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_giveaways_due ON giveaways(ended, end_at);

CREATE TABLE IF NOT EXISTS giveaway_entries (
    message_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (message_id, user_id),
    FOREIGN KEY (message_id) REFERENCES giveaways(message_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS starred_messages (
    source_message_id INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    starboard_message_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS stream_alerts (
    guild_id INTEGER NOT NULL,
    twitch_login TEXT NOT NULL,
    twitch_user_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    channel_id INTEGER NOT NULL,
    mention_role_id INTEGER,
    custom_message TEXT,
    last_stream_id TEXT,
    PRIMARY KEY (guild_id, twitch_login)
);
CREATE INDEX IF NOT EXISTS idx_stream_alerts_user ON stream_alerts(twitch_user_id);

CREATE TABLE IF NOT EXISTS youtube_alerts (
    guild_id INTEGER NOT NULL,
    youtube_channel_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    discord_channel_id INTEGER NOT NULL,
    mention_role_id INTEGER,
    custom_message TEXT,
    last_video_id TEXT,
    PRIMARY KEY (guild_id, youtube_channel_id)
);
CREATE INDEX IF NOT EXISTS idx_youtube_alerts_channel
ON youtube_alerts(youtube_channel_id);

CREATE TABLE IF NOT EXISTS tiktok_alerts (
    guild_id INTEGER NOT NULL,
    username TEXT NOT NULL,
    discord_channel_id INTEGER NOT NULL,
    mention_role_id INTEGER,
    live_enabled INTEGER NOT NULL DEFAULT 1,
    posts_enabled INTEGER NOT NULL DEFAULT 1,
    custom_message TEXT,
    last_live_id TEXT,
    last_post_id TEXT,
    PRIMARY KEY (guild_id, username)
);
CREATE INDEX IF NOT EXISTS idx_tiktok_alerts_username ON tiktok_alerts(username);

CREATE TABLE IF NOT EXISTS trivia_stats (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    answered INTEGER NOT NULL DEFAULT 0,
    correct INTEGER NOT NULL DEFAULT 0,
    streak INTEGER NOT NULL DEFAULT 0,
    best_streak INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_trivia_stats_leaders
ON trivia_stats(guild_id, correct DESC, answered ASC);

CREATE TABLE IF NOT EXISTS ai_chat_messages (
    message_id INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    display_name TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_chat_channel
ON ai_chat_messages(guild_id, channel_id, message_id DESC);
CREATE INDEX IF NOT EXISTS idx_ai_chat_user
ON ai_chat_messages(guild_id, user_id, message_id DESC);

CREATE TABLE IF NOT EXISTS ai_member_memories (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    display_name TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (guild_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_ai_memories_updated
ON ai_member_memories(guild_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS ai_memory_opt_outs (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS ai_memory_settings (
    guild_id INTEGER PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1
);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = await aiosqlite.connect(self.path)
        self.connection.row_factory = aiosqlite.Row
        await self.connection.executescript(SCHEMA)
        await self.connection.commit()

    async def close(self) -> None:
        if self.connection:
            await self.connection.close()
            self.connection = None

    def _conn(self) -> aiosqlite.Connection:
        if self.connection is None:
            raise RuntimeError("Database is not connected")
        return self.connection

    async def execute(self, sql: str, parameters: Iterable[Any] = ()) -> int:
        async with self._lock:
            cursor = await self._conn().execute(sql, tuple(parameters))
            await self._conn().commit()
            return cursor.lastrowid or 0

    async def executescript(self, sql: str) -> None:
        async with self._lock:
            await self._conn().executescript(sql)
            await self._conn().commit()

    async def fetchone(self, sql: str, parameters: Iterable[Any] = ()) -> aiosqlite.Row | None:
        cursor = await self._conn().execute(sql, tuple(parameters))
        return await cursor.fetchone()

    async def fetchall(self, sql: str, parameters: Iterable[Any] = ()) -> list[aiosqlite.Row]:
        cursor = await self._conn().execute(sql, tuple(parameters))
        return list(await cursor.fetchall())

    async def ensure_member(self, guild_id: int, user_id: int) -> None:
        await self.execute(
            "INSERT OR IGNORE INTO members(guild_id, user_id) VALUES (?, ?)",
            (guild_id, user_id),
        )

    async def get_settings(self, guild_id: int) -> dict[str, Any]:
        await self.execute("INSERT OR IGNORE INTO guild_settings(guild_id) VALUES (?)", (guild_id,))
        row = await self.fetchone("SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,))
        return dict(row) if row else {}

    async def set_setting(self, guild_id: int, column: str, value: Any) -> None:
        allowed = {
            "prefix",
            "log_channel_id",
            "welcome_channel_id",
            "welcome_message",
            "goodbye_channel_id",
            "goodbye_message",
            "autorole_id",
            "starboard_channel_id",
            "starboard_threshold",
            "suggestion_channel_id",
            "economy_enabled",
            "automod_enabled",
            "automod_invites",
            "automod_links",
            "automod_spam",
            "automod_caps",
            "automod_mentions",
            "automod_max_mentions",
            "automod_bad_words",
        }
        if column not in allowed:
            raise ValueError(f"Unknown guild setting: {column}")
        await self.execute("INSERT OR IGNORE INTO guild_settings(guild_id) VALUES (?)", (guild_id,))
        await self.execute(
            f"UPDATE guild_settings SET {column} = ? WHERE guild_id = ?", (value, guild_id)
        )

    async def record_ai_chat_message(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
        user_id: int,
        display_name: str,
        content: str,
        created_at: str,
        *,
        channel_limit: int = 200,
    ) -> None:
        """Store one public chat observation and bound storage per channel."""
        async with self._lock:
            conn = self._conn()
            setting = await (
                await conn.execute(
                    "SELECT enabled FROM ai_memory_settings WHERE guild_id = ?", (guild_id,)
                )
            ).fetchone()
            opted_out = await (
                await conn.execute(
                    "SELECT 1 FROM ai_memory_opt_outs WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id),
                )
            ).fetchone()
            if (setting is not None and not bool(setting[0])) or opted_out is not None:
                return
            await conn.execute(
                "INSERT OR IGNORE INTO ai_chat_messages("
                "message_id, guild_id, channel_id, user_id, display_name, content, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    message_id,
                    guild_id,
                    channel_id,
                    user_id,
                    display_name[:100],
                    content[:1_000],
                    created_at,
                ),
            )
            await conn.execute(
                "DELETE FROM ai_chat_messages WHERE guild_id = ? AND channel_id = ? "
                "AND message_id NOT IN ("
                "SELECT message_id FROM ai_chat_messages "
                "WHERE guild_id = ? AND channel_id = ? "
                "ORDER BY message_id DESC LIMIT ?)",
                (guild_id, channel_id, guild_id, channel_id, channel_limit),
            )
            await conn.commit()

    async def get_ai_chat_messages(
        self,
        guild_id: int,
        channel_id: int,
        *,
        limit: int = 30,
        exclude_message_id: int | None = None,
    ) -> list[dict[str, Any]]:
        parameters: list[Any] = [guild_id, channel_id]
        exclusion = ""
        if exclude_message_id is not None:
            exclusion = " AND message_id != ?"
            parameters.append(exclude_message_id)
        parameters.append(limit)
        rows = await self.fetchall(
            "SELECT message_id, user_id, display_name, content, created_at "
            "FROM ai_chat_messages WHERE guild_id = ? AND channel_id = ?"
            f"{exclusion} ORDER BY message_id DESC LIMIT ?",
            parameters,
        )
        return [dict(row) for row in reversed(rows)]

    async def delete_ai_chat_message(self, message_id: int) -> None:
        await self.execute("DELETE FROM ai_chat_messages WHERE message_id = ?", (message_id,))

    async def delete_ai_chat_messages(self, message_ids: Iterable[int]) -> None:
        ids = list(message_ids)
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        await self.execute(
            f"DELETE FROM ai_chat_messages WHERE message_id IN ({placeholders})",
            ids,
        )

    async def get_ai_member_memories(
        self, guild_id: int, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        rows = await self.fetchall(
            "SELECT user_id, display_name, notes, updated_at FROM ai_member_memories "
            "WHERE guild_id = ? ORDER BY updated_at DESC LIMIT ?",
            (guild_id, limit),
        )
        memories: list[dict[str, Any]] = []
        for row in rows:
            try:
                notes = json.loads(str(row["notes"]))
            except (TypeError, ValueError):
                notes = []
            memories.append(
                {
                    "user_id": int(row["user_id"]),
                    "display_name": str(row["display_name"]),
                    "notes": [str(note) for note in notes if isinstance(note, str)],
                    "updated_at": str(row["updated_at"]),
                }
            )
        return memories

    async def get_ai_member_memory(self, guild_id: int, user_id: int) -> dict[str, Any] | None:
        row = await self.fetchone(
            "SELECT user_id, display_name, notes, updated_at FROM ai_member_memories "
            "WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        if row is None:
            return None
        try:
            notes = json.loads(str(row["notes"]))
        except (TypeError, ValueError):
            notes = []
        return {
            "user_id": int(row["user_id"]),
            "display_name": str(row["display_name"]),
            "notes": [str(note) for note in notes if isinstance(note, str)],
            "updated_at": str(row["updated_at"]),
        }

    async def replace_ai_member_memory(
        self,
        guild_id: int,
        user_id: int,
        display_name: str,
        notes: Iterable[str],
    ) -> None:
        clean_notes = [" ".join(note.split())[:160] for note in notes if note.strip()][:8]
        async with self._lock:
            conn = self._conn()
            setting = await (
                await conn.execute(
                    "SELECT enabled FROM ai_memory_settings WHERE guild_id = ?", (guild_id,)
                )
            ).fetchone()
            opted_out = await (
                await conn.execute(
                    "SELECT 1 FROM ai_memory_opt_outs WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id),
                )
            ).fetchone()
            if (setting is not None and not bool(setting[0])) or opted_out is not None:
                return
            if not clean_notes:
                await conn.execute(
                    "DELETE FROM ai_member_memories WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id),
                )
            else:
                await conn.execute(
                    "INSERT INTO ai_member_memories(guild_id, user_id, display_name, notes) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(guild_id, user_id) DO UPDATE SET "
                    "display_name = excluded.display_name, notes = excluded.notes, "
                    "updated_at = CURRENT_TIMESTAMP",
                    (guild_id, user_id, display_name[:100], json.dumps(clean_notes)),
                )
            await conn.commit()

    async def ai_memory_enabled(self, guild_id: int) -> bool:
        row = await self.fetchone(
            "SELECT enabled FROM ai_memory_settings WHERE guild_id = ?", (guild_id,)
        )
        return row is None or bool(row["enabled"])

    async def set_ai_memory_enabled(self, guild_id: int, enabled: bool) -> None:
        async with self._lock:
            conn = self._conn()
            await conn.execute(
                "INSERT INTO ai_memory_settings(guild_id, enabled) VALUES (?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET enabled = excluded.enabled",
                (guild_id, int(enabled)),
            )
            if not enabled:
                await conn.execute(
                    "DELETE FROM ai_chat_messages WHERE guild_id = ?", (guild_id,)
                )
                await conn.execute(
                    "DELETE FROM ai_member_memories WHERE guild_id = ?", (guild_id,)
                )
            await conn.commit()

    async def get_ai_memory_opt_outs(self) -> set[tuple[int, int]]:
        rows = await self.fetchall("SELECT guild_id, user_id FROM ai_memory_opt_outs")
        return {(int(row["guild_id"]), int(row["user_id"])) for row in rows}

    async def set_ai_memory_opt_out(self, guild_id: int, user_id: int, opted_out: bool) -> None:
        async with self._lock:
            conn = self._conn()
            if opted_out:
                await conn.execute(
                    "INSERT OR IGNORE INTO ai_memory_opt_outs(guild_id, user_id) VALUES (?, ?)",
                    (guild_id, user_id),
                )
                await conn.execute(
                    "DELETE FROM ai_chat_messages WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id),
                )
                await conn.execute(
                    "DELETE FROM ai_member_memories WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id),
                )
            else:
                await conn.execute(
                    "DELETE FROM ai_memory_opt_outs WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id),
                )
            await conn.commit()

    async def forget_ai_user(self, guild_id: int, user_id: int) -> None:
        async with self._lock:
            conn = self._conn()
            await conn.execute(
                "DELETE FROM ai_chat_messages WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            await conn.execute(
                "DELETE FROM ai_member_memories WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            await conn.commit()

    async def clear_ai_guild_memory(self, guild_id: int) -> None:
        async with self._lock:
            conn = self._conn()
            await conn.execute("DELETE FROM ai_chat_messages WHERE guild_id = ?", (guild_id,))
            await conn.execute("DELETE FROM ai_member_memories WHERE guild_id = ?", (guild_id,))
            await conn.commit()

    async def add_case(
        self, guild_id: int, action: str, target_id: int, moderator_id: int, reason: str
    ) -> int:
        return await self.execute(
            "INSERT INTO cases(guild_id, action, target_id, moderator_id, reason) VALUES (?, ?, ?, ?, ?)",
            (guild_id, action, target_id, moderator_id, reason),
        )

    async def record_trivia_answer(
        self, guild_id: int, user_id: int, *, correct: bool
    ) -> dict[str, int]:
        async with self._lock:
            conn = self._conn()
            await conn.execute(
                "INSERT OR IGNORE INTO trivia_stats(guild_id, user_id) VALUES (?, ?)",
                (guild_id, user_id),
            )
            row = await (
                await conn.execute(
                    "SELECT answered, correct, streak, best_streak FROM trivia_stats "
                    "WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id),
                )
            ).fetchone()
            answered = int(row[0]) + 1
            correct_total = int(row[1]) + int(correct)
            streak = int(row[2]) + 1 if correct else 0
            best_streak = max(int(row[3]), streak)
            await conn.execute(
                "UPDATE trivia_stats SET answered = ?, correct = ?, streak = ?, best_streak = ? "
                "WHERE guild_id = ? AND user_id = ?",
                (answered, correct_total, streak, best_streak, guild_id, user_id),
            )
            await conn.commit()
            return {
                "answered": answered,
                "correct": correct_total,
                "streak": streak,
                "best_streak": best_streak,
            }

    async def seize_balance(
        self,
        guild_id: int,
        payer_id: int,
        recipient_id: int,
        amount: int,
        *,
        include_bank: bool = False,
    ) -> int:
        """Move up to ``amount`` between members, returning what was actually collected.

        Unlike :meth:`transfer_balance` this never fails on a short payer; it takes what
        is there. ``include_bank`` also draws on savings, so a penalty cannot be dodged
        by parking the money in the bank first.
        """
        if amount <= 0:
            raise ValueError("Amount must be positive")
        async with self._lock:
            conn = self._conn()
            await conn.execute(
                "INSERT OR IGNORE INTO members(guild_id, user_id) VALUES (?, ?), (?, ?)",
                (guild_id, payer_id, guild_id, recipient_id),
            )
            row = await (
                await conn.execute(
                    "SELECT balance, bank FROM members WHERE guild_id = ? AND user_id = ?",
                    (guild_id, payer_id),
                )
            ).fetchone()
            balance, bank = int(row[0]), int(row[1])
            available = balance + bank if include_bank else balance
            collected = min(amount, max(available, 0))
            if collected <= 0:
                return 0
            from_wallet = min(collected, balance)
            await conn.execute(
                "UPDATE members SET balance = balance - ?, bank = bank - ? "
                "WHERE guild_id = ? AND user_id = ?",
                (from_wallet, collected - from_wallet, guild_id, payer_id),
            )
            await conn.execute(
                "UPDATE members SET balance = balance + ? WHERE guild_id = ? AND user_id = ?",
                (collected, guild_id, recipient_id),
            )
            await conn.commit()
            return collected

    async def change_balance(self, guild_id: int, user_id: int, delta: int) -> int:
        async with self._lock:
            conn = self._conn()
            await conn.execute(
                "INSERT OR IGNORE INTO members(guild_id, user_id) VALUES (?, ?)",
                (guild_id, user_id),
            )
            row = await (
                await conn.execute(
                    "SELECT balance FROM members WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id),
                )
            ).fetchone()
            current = int(row[0])
            if current + delta < 0:
                raise ValueError("Insufficient balance")
            await conn.execute(
                "UPDATE members SET balance = balance + ? WHERE guild_id = ? AND user_id = ?",
                (delta, guild_id, user_id),
            )
            await conn.commit()
            return current + delta

    async def transfer_balance(
        self, guild_id: int, sender_id: int, recipient_id: int, amount: int
    ) -> None:
        if amount <= 0:
            raise ValueError("Amount must be positive")
        async with self._lock:
            conn = self._conn()
            await conn.execute(
                "INSERT OR IGNORE INTO members(guild_id, user_id) VALUES (?, ?), (?, ?)",
                (guild_id, sender_id, guild_id, recipient_id),
            )
            row = await (
                await conn.execute(
                    "SELECT balance FROM members WHERE guild_id = ? AND user_id = ?",
                    (guild_id, sender_id),
                )
            ).fetchone()
            if int(row[0]) < amount:
                raise ValueError("Insufficient balance")
            await conn.execute(
                "UPDATE members SET balance = balance - ? WHERE guild_id = ? AND user_id = ?",
                (amount, guild_id, sender_id),
            )
            await conn.execute(
                "UPDATE members SET balance = balance + ? WHERE guild_id = ? AND user_id = ?",
                (amount, guild_id, recipient_id),
            )
            await conn.commit()

    async def buy_item(
        self, guild_id: int, user_id: int, item_key: str, price: int, quantity: int
    ) -> int:
        if quantity <= 0:
            raise ValueError("Quantity must be positive")
        cost = price * quantity
        async with self._lock:
            conn = self._conn()
            await conn.execute(
                "INSERT OR IGNORE INTO members(guild_id, user_id) VALUES (?, ?)",
                (guild_id, user_id),
            )
            row = await (
                await conn.execute(
                    "SELECT balance FROM members WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id),
                )
            ).fetchone()
            if int(row[0]) < cost:
                raise ValueError("Insufficient balance")
            await conn.execute(
                "UPDATE members SET balance = balance - ? WHERE guild_id = ? AND user_id = ?",
                (cost, guild_id, user_id),
            )
            await conn.execute(
                """INSERT INTO inventory(guild_id, user_id, item_key, quantity)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(guild_id, user_id, item_key)
                   DO UPDATE SET quantity = quantity + excluded.quantity""",
                (guild_id, user_id, item_key, quantity),
            )
            await conn.commit()
            return int(row[0]) - cost

    async def move_bank(
        self, guild_id: int, user_id: int, amount: int, *, to_bank: bool
    ) -> tuple[int, int]:
        if amount <= 0:
            raise ValueError("Amount must be positive")
        source = "balance" if to_bank else "bank"
        target = "bank" if to_bank else "balance"
        async with self._lock:
            conn = self._conn()
            await conn.execute(
                "INSERT OR IGNORE INTO members(guild_id, user_id) VALUES (?, ?)",
                (guild_id, user_id),
            )
            cursor = await conn.execute(
                f"UPDATE members SET {source} = {source} - ?, {target} = {target} + ? "
                f"WHERE guild_id = ? AND user_id = ? AND {source} >= ?",
                (amount, amount, guild_id, user_id, amount),
            )
            if cursor.rowcount != 1:
                await conn.rollback()
                raise ValueError("Insufficient funds")
            row = await (
                await conn.execute(
                    "SELECT balance, bank FROM members WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id),
                )
            ).fetchone()
            await conn.commit()
            return int(row[0]), int(row[1])

    async def claim_reward(
        self,
        guild_id: int,
        user_id: int,
        reward: int,
        *,
        timestamp: str,
        eligible_before: str,
        column: str,
    ) -> int | None:
        if column not in {"last_daily", "last_work", "last_rob"}:
            raise ValueError("Invalid cooldown column")
        async with self._lock:
            conn = self._conn()
            await conn.execute(
                "INSERT OR IGNORE INTO members(guild_id, user_id) VALUES (?, ?)",
                (guild_id, user_id),
            )
            cursor = await conn.execute(
                f"UPDATE members SET balance = balance + ?, {column} = ? "
                f"WHERE guild_id = ? AND user_id = ? "
                f"AND ({column} IS NULL OR {column} <= ?)",
                (reward, timestamp, guild_id, user_id, eligible_before),
            )
            if cursor.rowcount != 1:
                await conn.rollback()
                return None
            row = await (
                await conn.execute(
                    "SELECT balance FROM members WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id),
                )
            ).fetchone()
            await conn.commit()
            return int(row[0])
