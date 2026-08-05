from __future__ import annotations

import asyncio
import logging

from kevin.config import TelegramSettings
from kevin.telegram_bot import TelegramKevin


def main() -> None:
    settings = TelegramSettings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )
    asyncio.run(TelegramKevin(settings).run())


if __name__ == "__main__":
    main()
