#!/usr/bin/env python3
"""
Создание Telethon-сессии (edit_config_session.session) через QR-логин,
с подключением через тот же Fake-TLS MTProto прокси / SOCKS5, что и
основной клиент мониторинга (см. telethon_factory.py).

Эта сессия используется отдельным процессом edit_config_bot.py —
обработчиком админ-команд (view / set keywords / set channels / dedup).
Она может принадлежать тому же самому Telegram-аккаунту, что и
bot_session, либо другому — Telethon не ограничивает количество
активных сессий на аккаунт.

Установка:
    pip install telethon TelethonFakeTLS qrcode[pil] --break-system-packages

Запуск (из корня проекта, рядом с config.ini/config.key):
    python3 create_edit_config_session.py

После успешного сканирования QR-кода создастся файл
edit_config_session.session — с этого момента edit_config_bot.py
сможет запускаться без интерактивного ввода (в том числе под supervisord).
"""

import asyncio
import os
import sys

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from config import get_config
from telethon_factory import build_telethon_client

SESSION_NAME = "edit_config_session"
CONFIG_PATH = "config.ini"
KEY_PATH = "config.key"


def show_qr(url: str, save_path: str = "qr_login_edit_config.png"):
    try:
        import qrcode

        qr = qrcode.QRCode(box_size=1, border=1, error_correction=qrcode.constants.ERROR_CORRECT_L)
        qr.add_data(url)
        qr.make(fit=True)
        try:
            qr.print_ascii(invert=True)
            print()
            return
        except AttributeError:
            pass
        img = qr.make_image(fill_color="black", back_color="white")
        img.save(save_path)
        print(f"QR сохранён в {os.path.abspath(save_path)} — откройте и отсканируйте камерой.\n")
        return
    except ImportError:
        print("Для отображения QR в терминале/файлом: pip install qrcode[pil]")

    print("\nИли войдите вручную: Настройки → Устройства → Подключить устройство → вставьте ссылку:")
    print(f"  {url}\n")


async def main():
    try:
        config = get_config(CONFIG_PATH, KEY_PATH)
    except Exception as e:
        print(f"Не удалось прочитать config.ini: {e}")
        sys.exit(1)

    existing = f"{SESSION_NAME}.session"
    if os.path.exists(existing):
        print(f"⚠️  Файл {existing} уже существует.")
        choice = input("Удалить старую сессию и создать новую? (y/n): ").strip().lower()
        if choice != "y":
            print("Отменено.")
            return
        os.remove(existing)
        journal = f"{SESSION_NAME}.session-journal"
        if os.path.exists(journal):
            os.remove(journal)

    import logging
    logger = logging.getLogger("create_edit_config_session")
    logging.basicConfig(level=logging.INFO)

    client = build_telethon_client(SESSION_NAME, config, logger)

    print("=" * 60)
    print("QR-логин для сессии edit_config_session (обработчик админ-команд)")
    print("=" * 60)

    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"Сессия уже авторизована: {me.first_name} (@{me.username or 'без username'})")
        await client.disconnect()
        return

    print("Откройте Telegram на телефоне:")
    print("  Настройки → Устройства → Подключить устройство → Сканировать QR-код\n")

    qr_login = await client.qr_login()

    while True:
        show_qr(qr_login.url)
        print("Ожидание сканирования (QR действует ~60 сек)...")
        try:
            await qr_login.wait()
            break
        except asyncio.TimeoutError:
            print("QR истёк. Генерирую новый...\n")
            qr_login = await client.qr_login()
        except SessionPasswordNeededError:
            print("\nВключена 2FA. Введите пароль:")
            await client.sign_in(password=input())
            break

    me = await client.get_me()
    print(f"\n{'=' * 60}")
    print("✅ УСПЕХ!")
    print(f"{'=' * 60}")
    print(f"Авторизован как: {me.first_name} (@{me.username or 'без username'})")
    print(f"Сессия сохранена: {SESSION_NAME}.session")
    print("\nТеперь можно запускать обработчик команд: python3 edit_config_bot.py")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())