from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_DISCORD_BLOCKED_USER_IDS = frozenset({1189439193861083149})


def _optional_int(value: str | None) -> int | None:
    if not value or not value.strip():
        return None
    return int(value)


def _id_set(value: str | None) -> set[int]:
    if not value:
        return set()
    return {int(item.strip()) for item in value.split(",") if item.strip()}


@dataclass(frozen=True, slots=True)
class Settings:
    token: str
    default_prefix: str = "k"
    database_path: Path = Path("data/kevin.sqlite3")
    status: str = "keeping the server tidy"
    stream_url: str | None = None
    owner_ids: set[int] | None = None
    blocked_user_ids: frozenset[int] = DEFAULT_DISCORD_BLOCKED_USER_IDS
    test_guild_id: int | None = None
    ytdlp_cookie_file: Path | None = None
    twitch_client_id: str | None = None
    twitch_client_secret: str | None = None
    youtube_api_key: str | None = None
    openai_api_key: str | None = None
    openai_model: str = "gpt-5.6-luna"
    openai_image_model: str = "gpt-image-1"
    mem0_path: Path = Path("data/mem0")
    mem0_llm_model: str = "gpt-4.1-mini"
    mem0_embedding_model: str = "text-embedding-3-small"
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv()
        token = os.getenv("DISCORD_TOKEN", "").strip()
        if not token:
            raise RuntimeError(
                "DISCORD_TOKEN is missing. Copy .env.example to .env and add your bot token."
            )
        cookie = os.getenv("YTDLP_COOKIE_FILE", "").strip()
        return cls(
            token=token,
            default_prefix=os.getenv("KEVIN_PREFIX", "k").strip() or "k",
            database_path=Path(os.getenv("KEVIN_DATABASE", "data/kevin.sqlite3")),
            status=os.getenv("KEVIN_STATUS", "keeping the server tidy"),
            stream_url=os.getenv("KEVIN_STREAM_URL", "").strip() or None,
            owner_ids=_id_set(os.getenv("KEVIN_OWNER_IDS")),
            blocked_user_ids=frozenset(
                _id_set(
                    os.getenv(
                        "KEVIN_BLOCKED_USER_IDS",
                        ",".join(map(str, DEFAULT_DISCORD_BLOCKED_USER_IDS)),
                    )
                )
            ),
            test_guild_id=_optional_int(os.getenv("KEVIN_TEST_GUILD_ID")),
            ytdlp_cookie_file=Path(cookie) if cookie else None,
            twitch_client_id=os.getenv("TWITCH_CLIENT_ID", "").strip() or None,
            twitch_client_secret=os.getenv("TWITCH_CLIENT_SECRET", "").strip() or None,
            youtube_api_key=os.getenv("YOUTUBE_API_KEY", "").strip() or None,
            openai_api_key=os.getenv("OPENAI_API_KEY", "").strip() or None,
            openai_model=os.getenv("OPENAI_MODEL", "gpt-5.6-luna").strip()
            or "gpt-5.6-luna",
            openai_image_model=os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1").strip()
            or "gpt-image-1",
            mem0_path=Path(os.getenv("MEM0_PATH", "data/mem0")),
            mem0_llm_model=os.getenv("MEM0_LLM_MODEL", "gpt-4.1-mini").strip()
            or "gpt-4.1-mini",
            mem0_embedding_model=os.getenv(
                "MEM0_EMBEDDING_MODEL", "text-embedding-3-small"
            ).strip()
            or "text-embedding-3-small",
            log_level=os.getenv("KEVIN_LOG_LEVEL", "INFO"),
        )


@dataclass(frozen=True, slots=True)
class TelegramSettings:
    token: str
    openai_api_key: str
    openai_model: str = "gpt-5.6-luna"
    allowed_user_ids: set[int] | None = None
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> TelegramSettings:
        load_dotenv()
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError(
                "TELEGRAM_BOT_TOKEN is missing. Create a bot with @BotFather and add "
                "its token to .env."
            )
        openai_api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is required by the Telegram chat bot.")
        return cls(
            token=token,
            openai_api_key=openai_api_key,
            openai_model=os.getenv("OPENAI_MODEL", "gpt-5.6-luna").strip()
            or "gpt-5.6-luna",
            allowed_user_ids=_id_set(os.getenv("TELEGRAM_ALLOWED_USER_IDS")),
            log_level=os.getenv("KEVIN_LOG_LEVEL", "INFO"),
        )
