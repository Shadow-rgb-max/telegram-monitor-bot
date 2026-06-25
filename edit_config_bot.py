import asyncio
import configparser
import os
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from cryptography.fernet import Fernet
import re
import logging

BOT_TOKEN = "7759801792:AAG890Wu4jGsMhonv299pUwaVi6KQYACuiI"
CONFIG_PATH = "config.ini"
KEY_PATH = "config.key"

# --- Вспомогательные функции для работы с config.ini ---
def load_key(key_path: str = KEY_PATH) -> bytes:
    if not os.path.exists(key_path):
        raise FileNotFoundError(f"Key file {key_path} not found.")
    return open(key_path, "rb").read().strip()

def decrypt_config(enc_path: str, key_path: str = KEY_PATH) -> str:
    key = load_key(key_path)
    f = Fernet(key)
    with open(enc_path, "rb") as f_enc:
        data = f_enc.read()
    if data.startswith(b"ENCRYPTED\n"):
        data = data[len(b"ENCRYPTED\n"):]
        decrypted = f.decrypt(data)
        return decrypted.decode("utf-8")
    else:
        return data.decode("utf-8")

def encrypt_config(config_str: str, enc_path: str, key_path: str = KEY_PATH):
    try:
        logger.info(f"[ENCRYPT_CONFIG] Начинаю шифрование config.ini, путь: {enc_path}")
        key = load_key(key_path)
        f = Fernet(key)
        enc = f.encrypt(config_str.encode("utf-8"))
        logger.info(f"[ENCRYPT_CONFIG] Данные зашифрованы, размер: {len(enc)} байт")
        with open(enc_path, "wb") as fout:
            fout.write(b"ENCRYPTED\n" + enc)
        logger.info(f"[ENCRYPT_CONFIG] Файл {enc_path} успешно записан")
        # Проверяем, что файл действительно записался
        if os.path.exists(enc_path):
            file_size = os.path.getsize(enc_path)
            logger.info(f"[ENCRYPT_CONFIG] Проверка: файл {enc_path} существует, размер: {file_size} байт")
        else:
            logger.error(f"[ENCRYPT_CONFIG] КРИТИЧЕСКАЯ ОШИБКА: файл {enc_path} не существует после записи!")
            raise IOError(f"Файл {enc_path} не был создан после записи")
    except Exception as e:
        logger.error(f"[ENCRYPT_CONFIG] ОШИБКА при шифровании и записи config.ini: {e}", exc_info=True)
        raise

def get_config_parser() -> configparser.ConfigParser:
    config_str = decrypt_config(CONFIG_PATH, KEY_PATH)
    config = configparser.ConfigParser(interpolation=None)
    config.read_string(config_str)
    return config

def save_config_parser(config: configparser.ConfigParser):
    from io import StringIO
    try:
        buf = StringIO()
        config.write(buf)
        config_str = buf.getvalue()
        logger.info(f"[SAVE_CONFIG] Сохраняю config.ini, размер конфигурации: {len(config_str)} байт")
        encrypt_config(config_str, CONFIG_PATH, KEY_PATH)
        logger.info(f"[SAVE_CONFIG] config.ini успешно зашифрован и сохранен в {CONFIG_PATH}")
    except Exception as e:
        logger.error(f"[SAVE_CONFIG] ОШИБКА при сохранении config.ini: {e}", exc_info=True)
        raise

# --- Telegram Bot ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


class ConfigEditStates(StatesGroup):
    waiting_channels = State()
    waiting_keywords = State()
    waiting_dedup_window = State()

# --- Настройка логирования ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [EDIT_CONFIG_BOT] %(levelname)s %(message)s',
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()  # Вывод в stdout для docker logs
    ],
    force=True  # Перезаписываем существующую конфигурацию
)
logger = logging.getLogger(__name__)

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    logger.info(f"Пользователь {message.from_user.id} вызвал /start")
    await message.answer("✅ Бот успешно запущен и готов к работе!\n\nЯ бот для редактирования зашифрованного config.ini.\nИспользуйте /help для списка команд.")

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    logger.info(f"Пользователь {message.from_user.id} вызвал /help")
    await message.answer(
        "Доступные команды:\n"
        "/view — посмотреть текущие значения monitored channels и keywords\n"
        "/set_channels — изменить monitored channels (введите список через запятую)\n"
        "/set_keywords — изменить keywords (введите список через запятую)\n"
        "/set_dedup_window — изменить окно дедупликации (в часах, по умолчанию 24)\n"
        "/help — показать это сообщение"
    )

@dp.message(Command("view"))
async def cmd_view(message: types.Message):
    logger.info(f"Пользователь {message.from_user.id} вызвал /view")
    try:
        config = get_config_parser()
        channels = config["Settings"].get("channels", "")
        keywords = config["Settings"].get("keywords", "")
        dedup_window = config["Settings"].get("dedup_window_hours", "24")
        await message.answer(
            f"Текущие monitored channels:\n<code>{channels}</code>\n\n"
            f"Текущие keywords:\n<code>{keywords}</code>\n\n"
            f"Окно дедупликации: <code>{dedup_window} часов</code>",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Ошибка в /view: {e}")
        await message.answer(f"Ошибка: {e}")

@dp.message(Command("set_channels"))
async def cmd_set_channels(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username or "без username"
    logger.info(f"[SET_CHANNELS] Пользователь {user_id} (@{username}) вызвал /set_channels")
    await state.set_state(ConfigEditStates.waiting_channels)
    await message.answer("Введите новый список каналов через запятую (например: @chan1, @chan2):")


@dp.message(ConfigEditStates.waiting_channels)
async def process_channels(msg: types.Message, state: FSMContext):
    user_id = msg.from_user.id
    username = msg.from_user.username or "без username"
    user_message = msg.text.strip()
    try:
        await state.clear()
        channels_raw = [ch.strip() for ch in user_message.split(",") if ch.strip()]
        logger.info(f"[SET_CHANNELS] Пользователь {user_id} (@{username}) отправил сообщение для редактирования config.ini: '{user_message}'")
        logger.info(f"[SET_CHANNELS] Распарсенные каналы для обработки: {channels_raw}")

        config = get_config_parser()
        old_channels = config["Settings"].get("channels", "")
        logger.info(f"[SET_CHANNELS] Текущие каналы в config.ini: '{old_channels}'")

        new_channels = ", ".join(channels_raw)
        logger.info(f"[SET_CHANNELS] Подготавливаю сохранение: устанавливаю channels = '{new_channels}'")
        config["Settings"]["channels"] = new_channels

        logger.info(f"[SET_CHANNELS] Сохраняю config.ini...")
        save_config_parser(config)
        logger.info(f"[SET_CHANNELS] config.ini сохранен, проверяю результат...")

        verify_config = get_config_parser()
        saved_channels = verify_config["Settings"].get("channels", "")
        logger.info(f"[SET_CHANNELS] Проверка сохранения: каналы в config.ini после сохранения: '{saved_channels}'")

        if saved_channels != new_channels:
            logger.error(f"[SET_CHANNELS] КРИТИЧЕСКАЯ ОШИБКА: Изменения не сохранились! Ожидалось: '{new_channels}', получено: '{saved_channels}'")
            await msg.answer(f"Ошибка: изменения не сохранились в config.ini. Ожидалось: {new_channels}, сохранено: {saved_channels}")
            return

        logger.info(f"[SET_CHANNELS] УСПЕХ: Пользователь {user_id} (@{username}) успешно обновил config.ini")
        logger.info(f"[SET_CHANNELS] Изменение каналов: '{old_channels}' → '{new_channels}'")
        logger.info("[SET_CHANNELS] Разрешение каналов передано основному боту (он сам конвертирует username/ссылки в ID)")

        await msg.answer(f"Список каналов обновлён!\n\nСохранённые каналы:\n" + "\n".join(f"• {ch}" for ch in channels_raw))
    except Exception as e:
        logger.error(f"[SET_CHANNELS] ОШИБКА: Пользователь {user_id} (@{username}) - не удалось обновить config.ini")
        logger.error(f"[SET_CHANNELS] Сообщение пользователя: '{user_message}'")
        logger.error(f"[SET_CHANNELS] Детали ошибки: {e}", exc_info=True)
        await msg.answer(f"Ошибка: {e}")
        await state.clear()


@dp.message(Command("set_keywords"))
async def cmd_set_keywords(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username or "без username"
    logger.info(f"[SET_KEYWORDS] Пользователь {user_id} (@{username}) вызвал /set_keywords")
    await state.set_state(ConfigEditStates.waiting_keywords)
    await message.answer("Введите новый список ключевых слов через запятую:")


@dp.message(ConfigEditStates.waiting_keywords)
async def process_keywords(msg: types.Message, state: FSMContext):
    user_id = msg.from_user.id
    username = msg.from_user.username or "без username"
    user_message = msg.text.strip()
    try:
        await state.clear()
        logger.info(f"[SET_KEYWORDS] Пользователь {user_id} (@{username}) отправил сообщение для редактирования config.ini: '{user_message}'")

        config = get_config_parser()
        old_keywords = config["Settings"].get("keywords", "")
        logger.info(f"[SET_KEYWORDS] Текущие ключевые слова в config.ini: '{old_keywords}'")

        new_keywords = user_message
        config["Settings"]["keywords"] = new_keywords
        save_config_parser(config)

        logger.info(f"[SET_KEYWORDS] УСПЕХ: Пользователь {user_id} (@{username}) успешно обновил config.ini")
        logger.info(f"[SET_KEYWORDS] Изменение ключевых слов: '{old_keywords}' → '{new_keywords}'")

        await msg.answer("Список ключевых слов обновлён!")
    except Exception as e:
        logger.error(f"[SET_KEYWORDS] ОШИБКА: Пользователь {user_id} (@{username}) - не удалось обновить config.ini")
        logger.error(f"[SET_KEYWORDS] Сообщение пользователя: '{user_message}'")
        logger.error(f"[SET_KEYWORDS] Детали ошибки: {e}", exc_info=True)
        await msg.answer(f"Ошибка: {e}")
        await state.clear()


@dp.message(Command("set_dedup_window"))
async def cmd_set_dedup_window(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username or "без username"
    logger.info(f"[SET_DEDUP_WINDOW] Пользователь {user_id} (@{username}) вызвал /set_dedup_window")
    await state.set_state(ConfigEditStates.waiting_dedup_window)
    await message.answer("Введите новое значение окна дедупликации в часах (например: 24):")


@dp.message(ConfigEditStates.waiting_dedup_window)
async def process_dedup_window(msg: types.Message, state: FSMContext):
    user_id = msg.from_user.id
    username = msg.from_user.username or "без username"
    user_message = msg.text.strip()
    try:
        await state.clear()
        logger.info(f"[SET_DEDUP_WINDOW] Пользователь {user_id} (@{username}) отправил сообщение для редактирования config.ini: '{user_message}'")

        config = get_config_parser()
        old_dedup_window = config["Settings"].get("dedup_window_hours", "24")
        logger.info(f"[SET_DEDUP_WINDOW] Текущее окно дедупликации в config.ini: '{old_dedup_window} часов'")

        hours = int(user_message)
        if hours < 1 or hours > 168:  # От 1 часа до 1 недели
            logger.warning(f"[SET_DEDUP_WINDOW] ОШИБКА ВАЛИДАЦИИ: Пользователь {user_id} (@{username}) ввел недопустимое значение: {hours} часов (допустимо: 1-168)")
            await msg.answer("Значение должно быть от 1 до 168 часов!")
            await state.set_state(ConfigEditStates.waiting_dedup_window)
            return

        config["Settings"]["dedup_window_hours"] = str(hours)
        save_config_parser(config)

        logger.info(f"[SET_DEDUP_WINDOW] УСПЕХ: Пользователь {user_id} (@{username}) успешно обновил config.ini")
        logger.info(f"[SET_DEDUP_WINDOW] Изменение окна дедупликации: '{old_dedup_window} часов' → '{hours} часов'")

        await msg.answer(f"Окно дедупликации обновлено на {hours} часов!")
    except ValueError:
        logger.error(f"[SET_DEDUP_WINDOW] ОШИБКА: Пользователь {user_id} (@{username}) - введено нецелое число для окна дедупликации")
        logger.error(f"[SET_DEDUP_WINDOW] Сообщение пользователя: '{user_message}'")
        await msg.answer("Пожалуйста, введите целое число!")
        await state.set_state(ConfigEditStates.waiting_dedup_window)
    except Exception as e:
        logger.error(f"[SET_DEDUP_WINDOW] ОШИБКА: Пользователь {user_id} (@{username}) - не удалось обновить config.ini")
        logger.error(f"[SET_DEDUP_WINDOW] Сообщение пользователя: '{user_message}'")
        logger.error(f"[SET_DEDUP_WINDOW] Детали ошибки: {e}", exc_info=True)
        await msg.answer(f"Ошибка: {e}")
        await state.clear()

if __name__ == "__main__":
    logger.info("Запуск edit_config_bot.py")
    asyncio.run(dp.start_polling(bot))
