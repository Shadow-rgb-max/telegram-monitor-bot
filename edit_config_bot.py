"""
Отдельный процесс-обработчик админ-команд для config.ini.

В отличие от telethon_client.py (который мониторит каналы под одним
Telegram-аккаунтом и сессией "bot_session"), этот скрипт поднимает СВОЙ
собственный Telethon-клиент с отдельной сессией "edit_config_session".
Это может быть тот же самый Telegram-аккаунт или другой.

Клиент слушает входящие личные сообщения от ЛЮБОГО пользователя (без
проверки admin_id) и позволяет менять config.ini через команды:
    view / set keywords / set channels / dedup
См. admin_commands.py.

⚠️ АВТОРИЗАЦИЯ ТОЛЬКО ЧЕРЕЗ QR-КОД.
Этот скрипт НЕ запрашивает номер телефона/код интерактивно — он рассчитан
на то, что сессия edit_config_session.session уже создана заранее через
QR-логин:

    python3 create_edit_config_session.py

(см. этот файл — по аналогии с create_session_faketls.py для основного
bot_session). Если сессия не найдена или не авторизована, edit_config_bot.py
завершится с ошибкой и подскажет запустить create_edit_config_session.py —
это осознанно, чтобы под supervisord/Docker процесс не зависал в ожидании
ввода, которого никто не увидит.

Изменения, сохранённые через этот процесс, подхватываются процессом
мониторинга (telethon_client.py) автоматически — он периодически
проверяет mtime config.ini (см. _reload_config_periodically).
"""

import asyncio
import logging
import os

from config import get_config
from admin_commands import AdminCommandHandler
from telethon_factory import build_telethon_client

SESSION_NAME = "edit_config_session"
CONFIG_PATH = "config.ini"
KEY_PATH = "config.key"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [EDIT_CONFIG_BOT] %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
    force=True,
)
logger = logging.getLogger("edit_config_bot")


async def main() -> None:
    try:
        config = get_config(CONFIG_PATH, KEY_PATH)
    except Exception as e:
        logger.error(f"❌ Не удалось прочитать config.ini: {e}")
        return

    session_file = f"{SESSION_NAME}.session"
    if not os.path.exists(session_file):
        logger.error(
            f"🔑 Файл сессии {session_file} не найден. "
            f"Сначала выполните QR-логин: python3 create_edit_config_session.py"
        )
        return

    client = build_telethon_client(SESSION_NAME, config, logger)

    logger.info(f"🔌 Подключение к Telegram (сессия '{SESSION_NAME}')...")

    connected = False
    attempts = 0
    max_attempts = 5
    while not connected and attempts < max_attempts:
        attempts += 1
        try:
            await client.connect()
            connected = True
            logger.info(f"✅ Подключение успешно (попытка {attempts})!")
        except Exception as e:
            logger.warning(f"⚠️ Ошибка подключения (попытка {attempts}/{max_attempts}): {e}")
            if attempts < max_attempts:
                await asyncio.sleep(3)

    if not connected:
        logger.error(f"❌ Не удалось подключиться после {max_attempts} попыток")
        return

    if not await client.is_user_authorized():
        logger.error(
            f"🔑 Сессия {session_file} не авторизована. Пересоздайте её через QR-логин: "
            f"python3 create_edit_config_session.py"
        )
        await client.disconnect()
        return

    me = await client.get_me()
    logger.info(f"✅ Авторизован как: {me.first_name} (@{me.username or 'N/A'}), id={me.id}")

    admin_handler = AdminCommandHandler(
        client,
        logger,
        config_path=CONFIG_PATH,
        key_path=KEY_PATH,
    )
    admin_handler.register()

    logger.info(
        "👂 Обработчик команд готов. Любой пользователь может написать этому аккаунту "
        "'view', 'set keywords', 'set channels' или 'dedup' в личные сообщения."
    )

    try:
        await client.run_until_disconnected()
    finally:
        admin_handler.unregister()
        if client.is_connected():
            await client.disconnect()
        logger.info("👋 edit_config_bot остановлен")


if __name__ == "__main__":
    asyncio.run(main())