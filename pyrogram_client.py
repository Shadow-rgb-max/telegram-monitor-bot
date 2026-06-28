"""
Клиент мониторинга Telegram-каналов на Pyrogram v2.
Версия 3.2 — умная ротация прокси, статический прокси, MTProto-совместимость.
"""

import asyncio
import os
import json
import time
import logging
import sqlite3
from typing import List, Optional, Dict, Any

from pyrogram import Client, filters, idle, utils as pyrogram_utils
from pyrogram.enums import ChatType, ParseMode
from pyrogram.types import Message
from pyrogram.errors import (
    FloodWait,
    AuthKeyUnregistered,
    SessionPasswordNeeded,
    RPCError,
    ChannelInvalid,
    ChannelPrivate,
    PeerIdInvalid,
    UsernameNotOccupied,
    UsernameInvalid,
)

from config import BotConfig, get_config
from keyword_monitor import KeywordMonitor
from notifier import send_notification, send_error_notification

logger = logging.getLogger("telegram_keyword_monitor")


class PyrogramKeywordBot:
    """
    Бот для мониторинга Telegram-каналов по ключевым словам.
    Версия 3.2:
    - Умная ротация прокси при ошибках (не удаляет сразу)
    - Поддержка статического прокси из env
    - Повторные попытки с разными прокси
    """

    def __init__(self, config: BotConfig, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.monitor = KeywordMonitor(
            config.keywords,
            throttle_seconds=60,
            dedup_window_hours=config.dedup_window_hours
        )

        self._config_path = "config.ini"
        self._config_mtime = os.path.getmtime(self._config_path)

        self._channel_cache_path = "channel_cache.json"
        self._channel_cache = self._load_channel_cache()

        self._resolved_channels: List[int] = []
        self._client: Optional[Client] = None
        self._running = False
        self._tasks: List[asyncio.Task] = []

        self._proxy_connect_attempts = 0
        self._max_proxy_attempts = 10  # 🔧 Увеличили
        self._current_proxy: Optional[Dict[str, Any]] = None
        self._message_handler = None

    def _load_channel_cache(self) -> Dict[str, int]:
        if os.path.exists(self._channel_cache_path):
            try:
                with open(self._channel_cache_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return {str(k): int(v) for k, v in data.items()}
            except Exception as e:
                self.logger.warning(f"Ошибка загрузки кэша каналов: {e}")
                return {}
        return {}

    def _save_channel_cache(self):
        try:
            with open(self._channel_cache_path, "w", encoding="utf-8") as f:
                json.dump(self._channel_cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.logger.error(f"Ошибка сохранения кэша каналов: {e}")

    def _build_client(self, proxy: Optional[Dict[str, Any]] = None) -> Client:
        """Создаёт Pyrogram Client с прокси."""
        client_kwargs = {
            "name": "bot_session",
            "api_id": int(self.config.api_id),
            "api_hash": self.config.api_hash,
            "workdir": ".",
            "no_updates": False,
            "sleep_threshold": 60,
        }

        if proxy:
            self.logger.info(f"🔌 Pyrogram Client с прокси ({proxy.get('scheme')}): {proxy.get('hostname')}:{proxy.get('port')}")
            client_kwargs["proxy"] = proxy
        else:
            self.logger.info("🔌 Pyrogram Client без прокси")

        return Client(**client_kwargs)

    def _get_static_proxy(self) -> Optional[Dict[str, Any]]:
        """Получает статический прокси из env TELEGRAM_PROXY."""
        import os
        from urllib.parse import urlparse

        proxy_url = os.getenv('TELEGRAM_PROXY') or os.getenv('TELEGRAM_PROXY_URL')
        if not proxy_url:
            return None

        parsed = urlparse(proxy_url)
        if not parsed.scheme or not parsed.hostname:
            self.logger.warning(f"⚠️ Некорректный URL прокси: {proxy_url}")
            return None

        proxy_dict = {
            "scheme": parsed.scheme,
            "hostname": parsed.hostname,
            "port": parsed.port or 1080,
        }
        if parsed.username:
            proxy_dict["username"] = parsed.username
        if parsed.password:
            proxy_dict["password"] = parsed.password

        return proxy_dict

    def _ensure_session_schema(self) -> None:
        session_path = "bot_session.session"
        if not os.path.exists(session_path):
            return

        try:
            conn = sqlite3.connect(session_path)
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(peers)")
            columns = {row[1] for row in cursor.fetchall()}

            if "last_update_on" not in columns:
                self.logger.info("🔧 Обновляю схему bot_session.session: добавляю peers.last_update_on")
                cursor.execute(
                    "ALTER TABLE peers ADD COLUMN last_update_on INTEGER NOT NULL DEFAULT 0"
                )
                cursor.execute(
                    "UPDATE peers SET last_update_on = CAST(STRFTIME('%s', 'now') AS INTEGER) WHERE last_update_on IS NULL OR last_update_on = 0"
                )
                conn.commit()

            pyrogram_utils.MIN_CHANNEL_ID = -1004294967296
            self.logger.info("🔧 Расширен диапазон Pyrogram MIN_CHANNEL_ID")
        except sqlite3.Error as e:
            self.logger.warning(f"⚠️ Не удалось проверить/обновить схему: {e}")
        except Exception as e:
            self.logger.warning(f"⚠️ Ошибка: {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    async def _resolve_channel(self, channel: str) -> Optional[int]:
        channel = channel.strip()

        if channel.lstrip("-").isdigit():
            return int(channel)

        # Приватные ссылки
        if channel.startswith("https://t.me/+") or channel.startswith("t.me/+"):
            invite_hash = channel.split("+")[-1]
            try:
                chat = await self._client.join_chat(invite_hash)
                chat_id = chat.id
                self._channel_cache[channel] = chat_id
                self._save_channel_cache()
                self.logger.info(f"✅ Приватный канал {channel} разрешён → ID: {chat_id}")
                return chat_id
            except Exception as e:
                self.logger.error(f"❌ Ошибка join_chat для {channel}: {e}")
                return None

        # === ПУБЛИЧНЫЕ КАНАЛЫ: подписываемся принудительно ===
        username = channel.lstrip("@")
        try:
            # Сначала подписываемся (если ещё не подписан — ошибка, игнорируем)
            try:
                await self._client.join_chat(username)
                self.logger.info(f"✅ Подписался на {channel}")
            except Exception as join_err:
                # USER_ALREADY_PARTICIPANT или другие ошибки — не критично
                self.logger.debug(f"ℹ️ join_chat для {channel}: {join_err}")

            # Теперь resolve для получения ID
            peer = await self._client.resolve_peer(username)
            if hasattr(peer, 'channel_id'):
                chat_id = int(f"-100{peer.channel_id}")
            elif hasattr(peer, 'chat_id'):
                chat_id = -peer.chat_id
            else:
                self.logger.error(f"❌ Неизвестный тип peer для {channel}: {type(peer)}")
                return None

            self._channel_cache[channel] = chat_id
            self._save_channel_cache()
            self.logger.info(f"✅ Канал {channel} разрешён → ID: {chat_id}")
            await asyncio.sleep(0.05)
            return chat_id

        except FloodWait as e:
            self.logger.warning(f"⏱ FloodWait при разрешении {channel}: жду {e.value} сек.")
            await asyncio.sleep(e.value)
            return await self._resolve_channel(channel)
        except (ChannelInvalid, ChannelPrivate, PeerIdInvalid) as e:
            self.logger.warning(f"⚠️ Канал {channel} недоступен: {e}")
            return None
        except (UsernameNotOccupied, UsernameInvalid) as e:
            self.logger.warning(f"⚠️ Username {channel} не существует или истёк: {e}")
            return None
        except Exception as e:
            self.logger.warning(f"⚠️ Ошибка разрешения канала {channel}: {e}")
            return None

    async def _resolve_all_channels(self) -> List[int]:
        resolved = []
        for channel in self.config.channels:
            chat_id = await self._resolve_channel(channel)
            if chat_id:
                resolved.append(chat_id)
            await asyncio.sleep(0.3)
        return resolved

    def _is_supported_chat_type(self, chat_type: Any) -> bool:
        if chat_type is None:
            return False

        if isinstance(chat_type, ChatType):
            return chat_type in {ChatType.CHANNEL, ChatType.SUPERGROUP}

        chat_type_str = str(chat_type).lower()
        return chat_type_str in {"channel", "supergroup", "chattype.channel", "chattype.supergroup"}

    async def _handle_new_message(self, client: Client, message: Message):
        try:
            if not message.chat or not self._is_supported_chat_type(getattr(message.chat, "type", None)):
                return

            chat_id = message.chat.id
            channel_title = message.chat.title or str(chat_id)
            message_text = message.text or message.caption or ""

            self.logger.info(
                f"[MONITOR] Канал: {channel_title} | ID: {message.id} | "
                f"Сообщение: {message_text[:200] or 'Без текста'}"
            )

            matched_keywords = self.monitor.match_keywords(message_text)
            matched_keywords = [
                kw for kw in matched_keywords
                if self.monitor.should_notify(chat_id, kw, message_text)
            ]

            if matched_keywords:
                username = message.chat.username
                if username:
                    message_link = f"https://t.me/{username}/{message.id}"
                else:
                    message_link = f"https://t.me/c/{str(chat_id).replace('-100', '')}/{message.id}"

                await send_notification(
                    client,
                    self._get_channel_id(),
                    channel_title,
                    matched_keywords,
                    message_text,
                    message_link,
                )
                self.logger.info(f"✅ Уведомление отправлено: {matched_keywords}")

        except Exception as e:
            self.logger.error(f"❌ Ошибка обработки сообщения: {e}", exc_info=True)
            try:
                await send_error_notification(
                    client,
                    self._get_channel_id(),
                    f"Ошибка обработки сообщения: {e}",
                )
            except Exception:
                pass

    async def _reload_config_periodically(self):
        while self._running:
            await asyncio.sleep(420)
            try:
                mtime = os.path.getmtime(self._config_path)
                if mtime != self._config_mtime:
                    self.logger.info("📝 Обнаружено изменение config.ini. Перезагружаю...")
                    new_config = get_config(self._config_path)

                    if (new_config.channels != self.config.channels or
                        new_config.keywords != self.config.keywords or
                        new_config.dedup_window_hours != self.config.dedup_window_hours):

                        self.config.channels = new_config.channels
                        self.config.keywords = new_config.keywords
                        self.config.dedup_window_hours = new_config.dedup_window_hours
                        self.monitor.keywords = new_config.keywords
                        self.monitor.dedup_window_hours = new_config.dedup_window_hours

                        self._resolved_channels = await self._resolve_all_channels()
                        self._register_message_handler(self._resolved_channels)
                        self.logger.info(
                            f"🔄 Конфиг обновлён. Каналов: {len(self._resolved_channels)}, "
                            f"Ключевых слов: {len(self.config.keywords)}"
                        )

                    self._config_mtime = mtime
            except Exception as e:
                self.logger.error(f"❌ Ошибка автообновления конфига: {e}")

    async def _cleanup_old_entries_periodically(self):
        while self._running:
            await asyncio.sleep(3600)
            try:
                self.monitor.cleanup_old_entries()
                stats = self.monitor.get_stats()
                self.logger.info(f"🧹 Очистка завершена. Статистика: {stats}")
            except Exception as e:
                self.logger.error(f"❌ Ошибка очистки: {e}")

    async def _test_channel_messages(self, chat_id: int, limit: int = 5):
        try:
            async for msg in self._client.get_chat_history(chat_id, limit=limit):
                text = msg.text or msg.caption or "Без текста"
                self.logger.debug(f"Тест: [{msg.chat.title or chat_id}] {text[:100]} (ID: {msg.id})")
        except Exception as e:
            self.logger.error(f"❌ Ошибка теста канала {chat_id}: {e}")

    async def _send_startup_test(self):
        try:
            proxy_info = "Без прокси"
            if self._current_proxy:
                proxy_info = f"Прокси ({self._current_proxy.get('scheme')}): {self._current_proxy.get('hostname')}:{self._current_proxy.get('port')}"

            await self._client.send_message(
                self._get_channel_id(),
                f"🚀 <b>Бот запущен</b> (Pyrogram v2)\n"
                f"📡 {proxy_info}\n"
                f"📊 Каналов: {len(self._resolved_channels)}\n"
                f"🔍 Ключевых слов: {len(self.config.keywords)}\n"
                    f"⏰ Окно дедупликации: {self.config.dedup_window_hours}ч",
                    parse_mode=ParseMode.HTML
            )
            self.logger.info("✅ Тестовое сообщение отправлено")
        except Exception as e:
            self.logger.error(f"❌ Ошибка отправки тестового сообщения: {e}")

    def _get_channel_id(self):
        try:
            return (
                int(self.config.channel_id)
                if str(self.config.channel_id).lstrip("-").isdigit()
                else self.config.channel_id
            )
        except Exception:
            return self.config.channel_id

    def _register_message_handler(self, resolved_channels: List[int]) -> None:
        if not self._client:
            return

        if self._message_handler is not None:
            try:
                self._client.remove_handler(self._message_handler)
            except Exception:
                pass
            self._message_handler = None

        @self._client.on_message(filters.all)
        async def message_handler(client: Client, message: Message):
            chat_id = message.chat.id if message.chat else "N/A"
            chat_type = message.chat.type if message.chat else "N/A"
            self.logger.info(
                f"[RAW UPDATE] chat_id={chat_id} | type={chat_type} | "
                f"text_preview={str(message.text or message.caption)[:100]}"
            )

            if not self._resolved_channels:
                return
            if message.chat and message.chat.id not in self._resolved_channels:
                return
            await self._handle_new_message(client, message)

        self._message_handler = message_handler
        self.logger.info(
            f"👂 Активные каналы для обработки: {len(self._resolved_channels)} | "
            f"IDs: {self._resolved_channels}"
        )

    async def start(self):
        self._running = True

        # Используем только статический прокси из env
        proxy = self._get_static_proxy()

        if proxy:
            self.logger.info(f"✅ Использую статический прокси: {proxy['hostname']}:{proxy['port']}")
        else:
            self.logger.info("ℹ️ Статический прокси не найден, подключение без прокси")

        self._current_proxy = proxy

        try:
            self._ensure_session_schema()
            self._client = self._build_client(proxy)

            self.logger.info("🔌 Подключение к Telegram через прокси...")

            # Подключаемся
            connected = False
            attempts = 0
            max_attempts = 5

            while not connected and attempts < max_attempts:
                attempts += 1
                try:
                    await self._client.start()
                    connected = True
                    self.logger.info(f"✅ Подключение успешно (попытка {attempts})!")
                except Exception as e:
                    self.logger.warning(f"⚠️ Ошибка подключения (попытка {attempts}/{max_attempts}): {e}")
                    if attempts < max_attempts:
                        await asyncio.sleep(3)

            if not connected:
                self.logger.error(f"❌ Не удалось подключиться после {max_attempts} попыток")
                return

            me = await self._client.get_me()
            self.logger.info(f"✅ Авторизован как: {me.first_name} (@{me.username or 'N/A'})")

            self.logger.info("🔍 Разрешение каналов...")
            self._resolved_channels = await self._resolve_all_channels()

            if not self._resolved_channels:
                self.logger.warning("⚠️ Ни один канал не разрешён!")
                try:
                    await self._client.send_message(
                        self._get_channel_id(),
                        "⚠️ <b>Внимание</b>\nНи один из указанных каналов не доступен.",
                        parse_mode=ParseMode.HTML
                    )
                except Exception:
                    pass

            # Проверяем доступ к каналам
            valid_channels = []
            for chat_id in self._resolved_channels:
                try:
                    chat = await self._client.get_chat(chat_id)
                    self.logger.info(f"✅ Доступ к каналу: {chat.title} (ID: {chat.id})")
                    await self._test_channel_messages(chat_id)
                    valid_channels.append(chat_id)
                except (PeerIdInvalid, ChannelInvalid, ChannelPrivate) as e:
                    self.logger.warning(f"⚠️ Канал {chat_id} недоступен ({e})")
                except Exception as e:
                    self.logger.error(f"❌ Ошибка доступа к каналу {chat_id}: {e}")

            self._resolved_channels = valid_channels

            if self._resolved_channels:
                await self._send_startup_test()

            self._register_message_handler(self._resolved_channels)

            if self._resolved_channels:
                self.logger.info(f"👂 Мониторинг {len(self._resolved_channels)} каналов...")
            else:
                self.logger.info("👂 Режим ожидания...")

            self._tasks.append(asyncio.create_task(self._reload_config_periodically()))
            self._tasks.append(asyncio.create_task(self._cleanup_old_entries_periodically()))

            await idle()

        except AuthKeyUnregistered:
            self.logger.error("🔑 Сессия недействительна. Удалите bot_session.session")
        except SessionPasswordNeeded:
            self.logger.error("🔒 Требуется двухфакторная аутентификация.")
        except FloodWait as e:
            self.logger.error(f"⏱ FloodWait: подождите {e.value} секунд.")
        except Exception as e:
            self.logger.error(f"❌ Критическая ошибка: {e}", exc_info=True)
            raise
        finally:
            await self.shutdown()

    async def shutdown(self):
        self._running = False
        self.logger.info("🛑 Завершение работы бота...")

        for task in self._tasks:
            task.cancel()

        if self._client and self._client.is_connected:
            try:
                await self._client.stop()
                self.logger.info("✅ Клиент Pyrogram отключён")
            except Exception as e:
                self.logger.error(f"❌ Ошибка при отключении: {e}")

        try:
            from proxy_manager import PROXY_MANAGER # pyright: ignore[reportUnknownVariableType]
            if PROXY_MANAGER:
                PROXY_MANAGER.stop()
        except Exception:
            pass

        self.logger.info("👋 Бот остановлен")