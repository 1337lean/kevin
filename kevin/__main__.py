from __future__ import annotations

import logging

from kevin.bot import KevinBot
from kevin.config import Settings


def main() -> None:
    settings = Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )
    bot = KevinBot(settings)
    bot.run(settings.token, log_handler=None)


if __name__ == "__main__":
    main()
