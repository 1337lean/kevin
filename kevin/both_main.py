from __future__ import annotations

import asyncio
import logging

from kevin.bot import KevinBot
from kevin.config import Settings, TelegramSettings
from kevin.telegram_bot import TelegramKevin


async def run_both(discord_settings: Settings, telegram_settings: TelegramSettings) -> None:
    discord_bot = KevinBot(discord_settings)
    telegram_bot = TelegramKevin(telegram_settings)
    try:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(discord_bot.start(discord_settings.token))
            tasks.create_task(telegram_bot.run())
    finally:
        if not discord_bot.is_closed():
            await discord_bot.close()
        await telegram_bot.close()


def main() -> None:
    discord_settings = Settings.from_env()
    telegram_settings = TelegramSettings.from_env()
    logging.basicConfig(
        level=getattr(logging, discord_settings.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )
    asyncio.run(run_both(discord_settings, telegram_settings))


if __name__ == "__main__":
    main()
