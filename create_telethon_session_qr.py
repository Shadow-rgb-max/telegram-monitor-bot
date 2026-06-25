#!/usr/bin/env python3
"""
Создание сессии Telethon через QR-код (как в Telegram Desktop).

Не требует SMS/кода — отсканируйте QR-код в Telegram:
  Настройки → Устройства → Подключить устройство → Сканировать QR-код

Запускайте ЛОКАЛЬНО (не в Docker). Сессия сохранится в bot_session.session.

Использование:
  python3 create_telethon_session_qr.py
"""

import asyncio
import configparser
import os
import sys

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError


CONFIG_PATH = "config.ini"
KEY_PATH = "config.key"
SESSION_NAME = "bot_session"


def load_key(key_path: str = KEY_PATH) -> bytes:
    if not os.path.exists(key_path):
        raise FileNotFoundError(f"Файл ключа не найден: {key_path}")
    return open(key_path, "rb").read().strip()


def decrypt_config(enc_path: str, key_path: str = KEY_PATH) -> str:
    from cryptography.fernet import Fernet
    key = load_key(key_path)
    f = Fernet(key)
    with open(enc_path, "rb") as f_enc:
        data = f_enc.read()
    if data.startswith(b"ENCRYPTED\n"):
        data = data[len(b"ENCRYPTED\n"):]
        decrypted = f.decrypt(data)
        return decrypted.decode("utf-8")
    return data.decode("utf-8")


def get_api_credentials():
    """Читает api_id и api_hash из config.ini или из env."""
    api_id = os.environ.get("API_ID")
    api_hash = os.environ.get("API_HASH")
    if api_id and api_hash:
        return api_id.strip(), api_hash.strip()

    if not os.path.exists(CONFIG_PATH):
        print(f"Файл {CONFIG_PATH} не найден. Задайте API_ID и API_HASH в окружении.")
        sys.exit(1)

    try:
        with open(CONFIG_PATH, "rb") as f:
            first_line = f.readline()
        if first_line.startswith(b"ENCRYPTED"):
            if not os.path.exists(KEY_PATH):
                print(f"Для расшифровки config.ini нужен файл {KEY_PATH}.")
                sys.exit(1)
            config_str = decrypt_config(CONFIG_PATH, KEY_PATH)
        else:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                config_str = f.read()

        config = configparser.ConfigParser(interpolation=None)
        config.read_string(config_str)
        api_id = config["Telegram"].get("api_id", "").strip()
        api_hash = config["Telegram"].get("api_hash", "").strip()
        if not api_id or not api_hash:
            print("В config.ini не найдены [Telegram] api_id и api_hash.")
            sys.exit(1)
        return api_id, api_hash
    except Exception as e:
        print(f"Ошибка чтения конфигурации: {e}")
        sys.exit(1)


def show_qr(url: str):
    """Показывает QR-код в терминале, сохраняет в PNG или выводит URL."""
    try:
        import qrcode
        # Пробуем ASCII в терминал
        qr = qrcode.QRCode(box_size=1, border=1)
        qr.add_data(url)
        qr.make(fit=True)
        if hasattr(qr, "print_ascii"):
            qr.print_ascii(invert=True)
        else:
            img = qr.make_image()
            path = "qr_login.png"
            img.save(path)
            print(f"\nQR-код сохранён в {os.path.abspath(path)}")
            print("Откройте файл и отсканируйте камерой телефона.\n")
        return
    except ImportError:
        pass
    except Exception as e:
        try:
            import qrcode
            img = qrcode.make(url)
            path = "qr_login.png"
            img.save(path)
            print(f"\nQR-код сохранён в {os.path.abspath(path)}")
            print("Откройте файл и отсканируйте камерой телефона.\n")
            return
        except Exception:
            pass

    print("\nДля QR-кода установите: pip install qrcode[pil]")
    print("Пока откройте в Telegram: Настройки → Устройства → Подключить устройство")
    print("и введите эту ссылку вручную или отсканируйте через «Сканировать QR»:\n")
    print(f"  {url}\n")


async def main():
    print("=== Создание сессии Telethon через QR-код ===\n")
    api_id, api_hash = get_api_credentials()
    print(f"Сессия будет сохранена в: {SESSION_NAME}.session\n")

    client = TelegramClient(SESSION_NAME, api_id, api_hash)
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"Сессия уже авторизована: {me.phone}")
        await client.disconnect()
        print(f"\nФайл {SESSION_NAME}.session готов.")
        return

    print("Откройте Telegram на телефоне:")
    print("  Настройки → Устройства → Подключить устройство → Сканировать QR-код\n")

    while True:
        qr_login = await client.qr_login()
        show_qr(qr_login.url)
        print("Ожидание сканирования (QR действует ~60 сек)...")

        try:
            await qr_login.wait()
            break
        except asyncio.TimeoutError:
            print("QR истёк. Генерирую новый...\n")
        except SessionPasswordNeededError:
            print("\nВключена 2FA. Введите пароль:")
            await client.sign_in(password=input())
            break

    me = await client.get_me()
    print(f"\nУспешно: {me.phone} ({getattr(me, 'username', '') or 'без username'})")
    await client.disconnect()

    session_path = os.path.abspath(f"{SESSION_NAME}.session")
    print(f"\nСессия сохранена: {session_path}")
    print("Запустите основной бот — он подхватит эту сессию.")


if __name__ == "__main__":
    asyncio.run(main())
