import asyncio
from config import get_config
from logger import setup_logger
from telegram_client import TelegramKeywordBot


def main() -> None:
    logger = setup_logger()
    try:
        config = get_config()
    except Exception as e:
        logger.error(f"Ошибка чтения конфигурации: {e}")
        return
    bot = TelegramKeywordBot(config, logger)
    try:
        asyncio.get_event_loop().run_until_complete(bot.start())
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем")
        asyncio.get_event_loop().run_until_complete(bot.shutdown())
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
        asyncio.get_event_loop().run_until_complete(bot.shutdown())


if __name__ == "__main__":
    main()
