import asyncio
from config import get_config
from logger import setup_logger
from pyrogram_client import PyrogramKeywordBot


def main() -> None:
    logger = setup_logger()
    try:
        config = get_config()
    except Exception as e:
        logger.error(f"Ошибка чтения конфигурации: {e}")
        return

    bot = PyrogramKeywordBot(config, logger)

    try:
        asyncio.run(bot.start())
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем (Ctrl+C)")
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")


if __name__ == "__main__":
    main()