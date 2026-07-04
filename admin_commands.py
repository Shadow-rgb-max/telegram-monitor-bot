"""
Обработчик админ-команд для редактирования config.ini.

Реагирует на личные сообщения от ЛЮБОГО пользователя (без проверки
admin_id) и позволяет менять config.ini через простые текстовые команды:

    view           — посмотреть текущие настройки
    set keywords   — задать список ключевых слов
    set channels   — задать список каналов
    dedup          — задать окно дедупликации (в часах)

Любое другое сообщение показывает список доступных команд.

⚠️ Так как проверки отправителя нет, любой, кто напишет этому аккаунту
в личные сообщения, сможет менять список каналов, ключевых слов и окно
дедупликации. Если это нежелательно, добавьте свою проверку в
_handle_message (например, по user_id, списку разрешённых username
и т.д.) — этот файл сделан так, чтобы такую проверку было легко добавить.
"""

import asyncio
import configparser
from io import StringIO
from typing import Callable, Optional

from cryptography.fernet import Fernet
from telethon import events

CONFIG_PATH_DEFAULT = "config.ini"
KEY_PATH_DEFAULT = "config.key"

HELP_TEXT = (
    "🤖 <b>Доступные команды</b>\n\n"
    "<code>view</code> — посмотреть текущие настройки\n"
    "<code>set keywords</code> — изменить список ключевых слов\n"
    "<code>set channels</code> — изменить список каналов\n"
    "<code>dedup</code> — изменить окно дедупликации (часы)\n\n"
    "Отправьте одно из этих слов, чтобы начать."
)

STATE_NONE = None
STATE_WAITING_KEYWORDS = "waiting_keywords"
STATE_WAITING_CHANNELS = "waiting_channels"
STATE_WAITING_DEDUP = "waiting_dedup"


class AdminCommandHandler:
    """Регистрирует на TelegramClient обработчик входящих личных сообщений
    и позволяет через них редактировать зашифрованный config.ini."""

    def __init__(
        self,
        client,
        logger,
        on_config_saved: Optional[Callable] = None,
        config_path: str = CONFIG_PATH_DEFAULT,
        key_path: str = KEY_PATH_DEFAULT,
    ):
        self.client = client
        self.logger = logger
        self.on_config_saved = on_config_saved  # вызывается после успешного сохранения
        self.config_path = config_path
        self.key_path = key_path
        # Состояние диалога — общее (не per-user), т.к. предполагается
        # редкое использование; если нужно параллельно обслуживать
        # нескольких пользователей — замените на dict[user_id] -> state.
        self.state = STATE_NONE
        self._handler = None

    # ---------- шифрование ----------

    def _load_key(self) -> bytes:
        with open(self.key_path, "rb") as f:
            return f.read().strip()

    def _decrypt_config(self) -> str:
        key = self._load_key()
        f = Fernet(key)
        with open(self.config_path, "rb") as fin:
            data = fin.read()
        if data.startswith(b"ENCRYPTED\n"):
            data = data[len(b"ENCRYPTED\n"):]
            return f.decrypt(data).decode("utf-8")
        return data.decode("utf-8")

    def _encrypt_config(self, config_str: str) -> None:
        key = self._load_key()
        f = Fernet(key)
        enc = f.encrypt(config_str.encode("utf-8"))
        with open(self.config_path, "wb") as fout:
            fout.write(b"ENCRYPTED\n" + enc)

    def _get_config_parser(self) -> configparser.ConfigParser:
        config_str = self._decrypt_config()
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(config_str)
        return parser

    def _save_config_parser(self, parser: configparser.ConfigParser) -> None:
        buf = StringIO()
        parser.write(buf)
        self._encrypt_config(buf.getvalue())

    # ---------- регистрация ----------

    def register(self) -> None:
        # incoming=True — только входящие сообщения;
        # func=is_private — только личные чаты (не группы и не каналы).
        @self.client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
        async def _handler(event):
            await self._handle_message(event)

        self._handler = _handler
        self.logger.info(
            "🛠 Обработчик админ-команд зарегистрирован (личные сообщения от любого пользователя)"
        )

    def unregister(self) -> None:
        if self._handler is not None:
            try:
                self.client.remove_event_handler(self._handler)
            except Exception:
                pass
            self._handler = None

    # ---------- диалог ----------

    async def _handle_message(self, event) -> None:
        text = (event.raw_text or "").strip()
        low = text.lower()
        sender_id = event.sender_id

        try:
            if self.state == STATE_WAITING_KEYWORDS:
                await self._process_keywords(event, text)
                return
            if self.state == STATE_WAITING_CHANNELS:
                await self._process_channels(event, text)
                return
            if self.state == STATE_WAITING_DEDUP:
                await self._process_dedup(event, text)
                return

            if low == "view":
                self.logger.info(f"[VIEW] Запрошено пользователем {sender_id}")
                await self._cmd_view(event)
            elif low == "set keywords":
                self.logger.info(f"[SET_KEYWORDS] Инициировано пользователем {sender_id}")
                self.state = STATE_WAITING_KEYWORDS
                await event.respond("Введите новый список ключевых слов через запятую:")
            elif low == "set channels":
                self.logger.info(f"[SET_CHANNELS] Инициировано пользователем {sender_id}")
                self.state = STATE_WAITING_CHANNELS
                await event.respond(
                    "Введите новый список каналов через запятую (например: @chan1, @chan2):"
                )
            elif low == "dedup":
                self.logger.info(f"[SET_DEDUP] Инициировано пользователем {sender_id}")
                self.state = STATE_WAITING_DEDUP
                await event.respond("Введите новое значение окна дедупликации в часах (1-168):")
            else:
                await event.respond(HELP_TEXT, parse_mode="html")
        except Exception as e:
            self.logger.error(f"❌ Ошибка обработки админ-команды: {e}", exc_info=True)
            self.state = STATE_NONE
            await event.respond(f"Ошибка: {e}")

    async def _cmd_view(self, event) -> None:
        parser = self._get_config_parser()
        channels = parser["Settings"].get("channels", "")
        keywords = parser["Settings"].get("keywords", "")
        dedup = parser["Settings"].get("dedup_window_hours", "24")
        await event.respond(
            f"📡 Каналы:\n<code>{channels}</code>\n\n"
            f"🔍 Ключевые слова:\n<code>{keywords}</code>\n\n"
            f"⏰ Окно дедупликации: <code>{dedup} часов</code>",
            parse_mode="html",
        )

    async def _process_keywords(self, event, text: str) -> None:
        self.state = STATE_NONE
        parser = self._get_config_parser()
        old = parser["Settings"].get("keywords", "")
        parser["Settings"]["keywords"] = text
        self._save_config_parser(parser)
        self.logger.info(f"[SET_KEYWORDS] '{old}' → '{text}' (от {event.sender_id})")
        await event.respond("✅ Список ключевых слов обновлён!")
        await self._notify_config_saved()

    async def _process_channels(self, event, text: str) -> None:
        self.state = STATE_NONE
        channels_raw = [ch.strip() for ch in text.split(",") if ch.strip()]
        if not channels_raw:
            await event.respond("⚠️ Список каналов не может быть пустым. Отправьте 'set channels' заново.")
            return
        parser = self._get_config_parser()
        old = parser["Settings"].get("channels", "")
        new_channels = ", ".join(channels_raw)
        parser["Settings"]["channels"] = new_channels
        self._save_config_parser(parser)
        self.logger.info(f"[SET_CHANNELS] '{old}' → '{new_channels}' (от {event.sender_id})")
        await event.respond(
            "✅ Список каналов обновлён!\n\n" + "\n".join(f"• {ch}" for ch in channels_raw)
        )
        await self._notify_config_saved()

    async def _process_dedup(self, event, text: str) -> None:
        self.state = STATE_NONE
        try:
            hours = int(text.strip())
        except ValueError:
            await event.respond("⚠️ Нужно целое число. Отправьте 'dedup' заново.")
            return
        if hours < 1 or hours > 168:
            await event.respond("⚠️ Значение должно быть от 1 до 168 часов. Отправьте 'dedup' заново.")
            return
        parser = self._get_config_parser()
        old = parser["Settings"].get("dedup_window_hours", "24")
        parser["Settings"]["dedup_window_hours"] = str(hours)
        self._save_config_parser(parser)
        self.logger.info(f"[SET_DEDUP] '{old}' → '{hours}' (от {event.sender_id})")
        await event.respond(f"✅ Окно дедупликации обновлено на {hours} часов!")
        await self._notify_config_saved()

    async def _notify_config_saved(self) -> None:
        if self.on_config_saved is None:
            return
        try:
            result = self.on_config_saved()
            if asyncio.iscoroutine(result):
                await result
        except Exception as e:
            self.logger.error(f"❌ Ошибка callback on_config_saved: {e}", exc_info=True)