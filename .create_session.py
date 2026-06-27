#!/usr/bin/env python3
"""
Создание сессии Pyrogram через QR-логин.

Работает в России при блокировках — QR-код сканируется через Telegram Desktop/Mobile,
SMS-код НЕ требуется.

Алгоритм:
  1. Telethon создает QR-код и ждет сканирования
  2. Сохраняет .session файл (SQLite)
  3. Конвертируем SQLite Telethon -> SQLite Pyrogram напрямую
  4. Переименовываем в bot_session.session

Требования:
  pip install pyrogram tgcrypto telethon cryptography qrcode[pil] pillow

Использование:
  python3 create_session.py

Примечание: старая сессия (bot_session.session) будет удалена автоматически.
"""

import asyncio
import os
import sys
import tempfile
import shutil
import sqlite3
import struct
import base64
import time

# ==================== КОНФИГУРАЦИЯ ====================

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
    """Читает api_id и api_hash из config.ini или env."""
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

        import configparser
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


# ==================== QR-КОД В ТЕРМИНАЛЕ ====================

def show_qr(url: str, save_path: str = "qr_login.png"):
    """Показывает QR-код в терминале или сохраняет в PNG."""
    try:
        import qrcode # type: ignore
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
        print(f"\nQR-код сохранен в {os.path.abspath(save_path)}")
        print("Откройте файл и отсканируйте камерой телефона.\n")
        return

    except ImportError:
        print(f"\nДля QR-кода установите: pip install qrcode[pil]")
    except Exception as e:
        print(f"\nОшибка генерации QR: {e}")

    print("\nОткройте в Telegram: Настройки -> Устройства -> Подключить устройство")
    print("и введите эту ссылку вручную или отсканируйте через «Сканировать QR»:\n")
    print(f"  {url}\n")


# ==================== КОНВЕРТАЦИЯ SQLITE TELETHON -> PYROGRAM ====================

def convert_telethon_session_to_pyrogram(telethon_session_path: str, api_id: int, api_hash: str, output_name: str = "bot_session"):
    """
    Конвертирует SQLite файл сессии Telethon в формат Pyrogram.
    """
    print("\n=== Конвертация Telethon SQLite -> Pyrogram SQLite ===")

    # Telethon session файл может быть с расширением .session или без
    telethon_db = telethon_session_path + ".session"
    if not os.path.exists(telethon_db):
        if os.path.exists(telethon_session_path):
            telethon_db = telethon_session_path
        else:
            raise FileNotFoundError(f"Telethon session не найден: {telethon_session_path}")

    output_path = os.path.abspath(f"{output_name}.session")

    # Удаляем старый Pyrogram session если есть
    if os.path.exists(output_path):
        os.remove(output_path)

    # Читаем данные из Telethon
    print(f"Читаем Telethon session: {telethon_db}")
    conn_telethon = sqlite3.connect(telethon_db)
    cursor = conn_telethon.cursor()

    # Получаем auth_key и dc_id из Telethon
    cursor.execute("SELECT dc_id, server_address, port, auth_key FROM sessions")
    row = cursor.fetchone()
    if not row:
        raise ValueError("Не найдена сессия в Telethon SQLite")

    dc_id, server_address, port, auth_key = row
    print(f"  Telethon: dc_id={dc_id}, server={server_address}:{port}, auth_key_len={len(auth_key) if auth_key else 0}")

    # Получаем user_id из entities (если есть)
    user_id = None
    try:
        cursor.execute("SELECT id, name FROM entities WHERE id > 0 LIMIT 1")
        entity_row = cursor.fetchone()
        if entity_row:
            user_id = entity_row[0]
            print(f"  Найден user_id в entities: {user_id}")
    except Exception as e:
        print(f"  Не удалось получить user_id из entities: {e}")

    conn_telethon.close()

    # Если не нашли user_id в entities, получим через Telethon API
    is_bot = False
    if not user_id:
        print("  User ID не найден в SQLite, получаем через API...")
        from telethon import TelegramClient

        async def get_user_info():
            client = TelegramClient(telethon_session_path, api_id, api_hash)
            await client.connect()
            me = await client.get_me()
            uid = me.id
            bot = me.bot
            await client.disconnect()
            return uid, bot

        user_id, is_bot = asyncio.run(get_user_info())
        print(f"  Получен через API: user_id={user_id}, is_bot={is_bot}")

    # Создаем Pyrogram SQLite session
    print(f"Создаем Pyrogram session: {output_path}")
    conn_pyrogram = sqlite3.connect(output_path)
    cursor_p = conn_pyrogram.cursor()

    # Pyrogram schema (v2)
    cursor_p.execute("CREATE TABLE sessions (dc_id INTEGER PRIMARY KEY, test_mode INTEGER, auth_key BLOB, date INTEGER NOT NULL, user_id INTEGER, is_bot INTEGER)")
    cursor_p.execute(
        "CREATE TABLE peers (id INTEGER PRIMARY KEY, access_hash INTEGER NOT NULL, type TEXT NOT NULL, username TEXT, phone_number TEXT, last_name TEXT, name TEXT, last_update_on INTEGER NOT NULL DEFAULT (CAST(STRFTIME('%s', 'now') AS INTEGER)))"
    )
    cursor_p.execute("CREATE TABLE version (number INTEGER PRIMARY KEY)")

    # Вставляем данные сессии
    cursor_p.execute(
        "INSERT INTO sessions (dc_id, test_mode, auth_key, date, user_id, is_bot) VALUES (?, ?, ?, ?, ?, ?)",
        (dc_id, 0, auth_key, int(time.time()), user_id, 1 if is_bot else 0)
    )

    cursor_p.execute("INSERT INTO version (number) VALUES (?)", (2,))

    conn_pyrogram.commit()
    conn_pyrogram.close()

    print(f"✅ Pyrogram session создан: {output_path}")
    print(f"   dc_id={dc_id}, user_id={user_id}, is_bot={is_bot}")

    return output_path


# ==================== TELETHON QR-LOGIN ====================

async def telethon_qr_login(api_id: str, api_hash: str, temp_session: str):
    """
    Создает сессию через Telethon с QR-логином.
    """
    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError

    print("=== QR-логин через Telethon ===\n")

    client = TelegramClient(temp_session, api_id, api_hash)
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"Сессия уже авторизована: {me.phone}")
        await client.disconnect()
        return temp_session

    print("Откройте Telegram на телефоне:")
    print("  Настройки -> Устройства -> Подключить устройство -> Сканировать QR-код\n")

    while True:
        qr_login = await client.qr_login()
        show_qr(qr_login.url)
        print("Ожидание сканирования (QR действует ~60 сек)...")

        try:
            await qr_login.wait()
            break
        except asyncio.TimeoutError:
            print("QR истек. Генерирую новый...\n")
        except SessionPasswordNeededError:
            print("\nВключена 2FA. Введите пароль:")
            await client.sign_in(password=input())
            break

    me = await client.get_me()
    print(f"\nУспешно: {me.phone} (@{getattr(me, 'username', '') or 'без username'})")
    await client.disconnect()
    return temp_session


# ==================== ОСНОВНАЯ ЛОГИКА ====================

async def main():
    print("=" * 60)
    print("Создание сессии Pyrogram через QR-логин")
    print("=" * 60)

    api_id, api_hash = get_api_credentials()
    print(f"API ID: {api_id}")
    print(f"Сессия будет сохранена в: {SESSION_NAME}.session\n")

    # Проверяем существующую сессию
    existing_session = f"{SESSION_NAME}.session"
    if os.path.exists(existing_session):
        print(f"⚠️  Файл {existing_session} уже существует.")
        choice = input("Удалить старую сессию и создать новую? (y/n): ").strip().lower()
        if choice == 'y':
            os.remove(existing_session)
            if os.path.exists(f"{SESSION_NAME}.session-journal"):
                os.remove(f"{SESSION_NAME}.session-journal")
        else:
            print("Отменено.")
            return

    temp_session = tempfile.mktemp(prefix="telethon_qr_")

    try:
        # Шаг 1: QR-логин через Telethon
        print("[1/2] QR-логин через Telethon...")
        telethon_session = await telethon_qr_login(api_id, api_hash, temp_session)

        # Шаг 2: Конвертация SQLite Telethon -> SQLite Pyrogram
        print("[2/2] Конвертация в Pyrogram...")
        pyrogram_path = convert_telethon_session_to_pyrogram(
            telethon_session,
            int(api_id),
            api_hash,
            output_name=SESSION_NAME
        )

        print(f"\n{'='*60}")
        print("✅ УСПЕХ!")
        print(f"{'='*60}")
        print(f"Pyrogram сессия: {pyrogram_path}")
        print(f"\nЗапустите основной бот:")
        print(f"  python3 main.py")

    except Exception as e:
        print(f"\n❌ Ошибка: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        # Очистка временных файлов Telethon
        for ext in ["", ".session", ".session-journal", ".db"]:
            temp_file = temp_session + ext
            if os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except Exception:
                    pass


if __name__ == "__main__":
    asyncio.run(main())