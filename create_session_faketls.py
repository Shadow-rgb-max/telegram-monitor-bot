#!/usr/bin/env python3
"""
Создание Telethon-сессии (bot_session.session) через QR-логин,
с подключением через рабочий Fake-TLS MTProto прокси.

Актуально для регионов, где вход по номеру телефона/SMS-коду
недоступен из-за блокировки Telegram, но проверенный MTProxy работает.

Установка:
    pip install telethon TelethonFakeTLS qrcode[pil] --break-system-packages

Запуск (из корня проекта, рядом с config.ini/config.key):
    python3 create_session_faketls.py

ВАЖНО: старая bot_session.session (созданная под Pyrogram) для Telethon
не подходит — формат SQLite-схемы другой. Скрипт создаст новый файл
с тем же именем, старый будет автоматически удалён (с подтверждением).
"""

import asyncio
import os
import sys

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
import TelethonFakeTLS

SESSION_NAME = "bot_session"

# ==================== ПРОКСИ (Fake-TLS MTProto) ====================
PROXY_SERVER = "personal3.proxytg.space"
PROXY_PORT = 8443
FULL_SECRET_HEX = "eeda92971cea3524eb834ae5a6a93522c6706572736f6e616c332e70726f787974672e7370616365"
SECRET_FOR_LIB = FULL_SECRET_HEX[2:]  # библиотека требует без префикса "ee"

PROXY = (PROXY_SERVER, PROXY_PORT, SECRET_FOR_LIB)
CONNECTION = TelethonFakeTLS.ConnectionTcpMTProxyFakeTLS


def get_api_credentials():
    api_id = os.environ.get("API_ID")
    api_hash = os.environ.get("API_HASH")
    if api_id and api_hash:
        return int(api_id.strip()), api_hash.strip()

    try:
        from cryptography.fernet import Fernet
        import configparser

        with open("config.key", "rb") as f:
            key = f.read().strip()
        with open("config.ini", "rb") as f:
            data = f.read()

        if data.startswith(b"ENCRYPTED\n"):
            data = data[len(b"ENCRYPTED\n"):]
            decrypted = Fernet(key).decrypt(data).decode("utf-8")
        else:
            decrypted = data.decode("utf-8")

        config = configparser.ConfigParser(interpolation=None)
        config.read_string(decrypted)
        return int(config["Telegram"]["api_id"].strip()), config["Telegram"]["api_hash"].strip()
    except Exception as e:
        print(f"Не удалось прочитать api_id/api_hash: {e}")
        print("Задайте их через переменные окружения API_ID и API_HASH.")
        sys.exit(1)


def show_qr(url: str, save_path: str = "qr_login.png"):
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
    api_id, api_hash = get_api_credentials()

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

    print("=" * 60)
    print("QR-логин через Telethon + Fake-TLS MTProxy")
    print("=" * 60)
    print(f"Прокси: {PROXY_SERVER}:{PROXY_PORT}\n")

    client = TelegramClient(
        SESSION_NAME,
        api_id,
        api_hash,
        connection=CONNECTION,
        proxy=PROXY,
    )

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
    print("\nТеперь можно запускать основного бота: python3 main.py")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())