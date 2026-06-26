"""
Клиент мониторинга Telegram-каналов на Pyrogram v2.
Версия 3.0 — принудительный режим прокси, улучшенная обработка ошибок.
"""

import asyncio
import os
import json
import time
import logging
import sqlite3
from typing import List, Optional, Dict, Any

from pyrogram import Client, filters, idle, utils as pyrogram_utils
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
from proxy_manager import get_proxy_manager

logger = logging.getLogger("telegram_keyword_monitor")


class PyrogramKeywordBot:
    """
    Бот для мониторинга Telegram-каналов по ключевым словам.
    Версия 3.0:
    - Работает ТОЛЬКО через прокси (нет fallback на прямое соединение)
    - Улучшенная обработка ошибок подключения
    - Повторные попытки при ошибках прокси
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

        # Счётчик попыток подключения через прокси
        self._proxy_connect_attempts = 0
        self._max_proxy_attempts = 5

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

    def _build_client(self) -> Client:
        proxy_manager = get_proxy_manager()
        proxy = proxy_manager.get_proxy(wait_seconds=60)

        if proxy:
            self.logger.info(f"🔌 Pyrogram Client с SOCKS5 прокси: {proxy.get('hostname')}:{proxy.get('port')}")
        else:
            # Это не должно произойти в нормальном режиме, но на всякий случай
            self.logger.error("🔌 Pyrogram Client без прокси — КРИТИЧЕСКАЯ ОШИБКА: пул прокси пуст!")
            raise RuntimeError("Невозможно создать клиент: пул прокси пуст")

        return Client(
            name="bot_session",
            api_id=int(self.config.api_id),
            api_hash=self.config.api_hash,
            proxy=proxy,
            workdir=".",
            no_updates=False,
            sleep_threshold=60,
        )

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

            # Всегда расширяем MIN_CHANNEL_ID
            pyrogram_utils.MIN_CHANNEL_ID = -1004294967296
            self.logger.info("🔧 Расширен диапазон Pyrogram MIN_CHANNEL_ID")
        except sqlite3.Error as e:
            self.logger.warning(
                f"⚠️ Не удалось проверить/обновить схему bot_session.session: {e}"
            )
        except Exception as e:
            self.logger.warning(
                f"⚠️ Ошибка при проверке Pyrogram внутреннего диапазона peer id: {e}"
            )
        finally:
            try:
                conn.close()
            except Exception:
                pass

    async def _resolve_channel(self, channel: str) -> Optional[int]:
        """Разрешает username/ссылку канала в chat_id."""
        channel = channel.strip()

        if channel.lstrip("-").isdigit():
            return int(channel)

        if channel.startswith("https://t.me/+") or channel.startswith("t.me/+"):
            invite_hash = channel.split("+")[-1]
            try:
                chat = await self._client.join_chat(invite_hash)
                chat_id = chat.id
                self._channel_cache[channel] = chat_id
                self._save_channel_cache()
                self.logger.info(f"✅ Приватный канал {channel} разрешён → ID: {chat_id}")
                return chat_id
            except (UsernameInvalid, UsernameNotOccupied) as e:
                self.logger.warning(f"⚠️ Приватная ссылка {channel} недействительна: {e}")
                return None
            except Exception as e:
                self.logger.error(f"❌ Ошибка join_chat для {channel}: {e}")
                return None

        try:
            peer = await self._client.resolve_peer(channel.lstrip("@"))
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
            await asyncio.sleep(1)
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
            await asyncio.sleep(1.5)
        return resolved

    async def _handle_new_message(self, client: Client, message: Message):
        try:
            if not message.chat or message.chat.type not in ("channel", "supergroup"):
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
            else:
                if matched_keywords:
                    self.logger.debug(
                        f"Сообщение заблокировано дедупликацией/throttling: {message_text[:100]}..."
                    )
                else:
                    self.logger.debug(
                        f"Ключевые слова не найдены: {message_text[:100]}..."
                    )

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
            proxy_manager = get_proxy_manager()
            stats = proxy_manager.get_stats()
            proxy_info = f"SOCKS5 пул: {stats['pool_size']} прокси (режим: ТОЛЬКО ПРОКСИ)"

            await self._client.send_message(
                self._get_channel_id(),
                f"🚀 <b>Бот запущен</b> (Pyrogram v2, proxy-only)\n"
                f"📡 {proxy_info}\n"
                f"📊 Каналов: {len(self._resolved_channels)}\n"
                f"🔍 Ключевых слов: {len(self.config.keywords)}\n"
                f"⏰ Окно дедупликации: {self.config.dedup_window_hours}ч",
                parse_mode="HTML"
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

    async def start(self):
        self._running = True

        # Сначала инициализируем proxy manager (блокирующее, ждём прокси)
        self.logger.info("🔒 Инициализация менеджера прокси (принудительный режим)...")
        proxy_manager = get_proxy_manager()

        # Проверяем, что прокси доступны
        proxy = proxy_manager.get_proxy(wait_seconds=90)
        if not proxy:
            self.logger.error("❌ КРИТИЧЕСКАЯ ОШИБКА: Не удалось получить прокси за 90 секунд!")
            self.logger.error("❌ Бот не может работать без прокси в принудительном режиме.")
            return

        self.logger.info(f"✅ Прокси получен: {proxy['hostname']}:{proxy['port']}")

        try:
            self._ensure_session_schema()
            self._client = self._build_client()

            self.logger.info("🔌 Подключение к Telegram через прокси (Pyrogram)...")

            # Попытка подключения с повторными попытками при ошибках прокси
            connected = False
            while not connected and self._proxy_connect_attempts < self._max_proxy_attempts:
                self._proxy_connect_attempts += 1
                try:
                    await self._client.start()
                    connected = True
                except Exception as e:
                    error_str = str(e).lower()
                    if 'proxy' in error_str or 'sock' in error_str or 'connection' in error_str:
                        self.logger.warning(f"⚠️ Ошибка прокси при подключении (попытка {self._proxy_connect_attempts}/{self._max_proxy_attempts}): {e}")
                        proxy_manager.report_failure(proxy)
                        # Пробуем другой прокси
                        proxy = proxy_manager.get_proxy(wait_seconds=30)
                        if not proxy:
                            self.logger.error("❌ Нет доступных прокси для повторной попытки")
                            return
                        self.logger.info(f"🔄 Пробую другой прокси: {proxy['hostname']}:{proxy['port']}")
                        # Пересоздаём клиент с новым прокси
                        try:
                            await self._client.stop()
                        except:
                            pass
                        self._client = self._build_client()
                    else:
                        raise

            if not connected:
                self.logger.error(f"❌ Не удалось подключиться после {self._max_proxy_attempts} попыток")
                return

            me = await self._client.get_me()
            self.logger.info(f"✅ Авторизован как: {me.first_name} (@{me.username or 'N/A'})")

            self.logger.info("🔍 Разрешение каналов...")
            self._resolved_channels = await self._resolve_all_channels()

            if not self._resolved_channels:
                self.logger.warning("⚠️ Ни один канал не разрешён! Бот продолжит работу в режиме ожидания.")
                try:
                    await self._client.send_message(
                        self._get_channel_id(),
                        "⚠️ <b>Внимание</b>\n"
                        "Ни один из указанных каналов не доступен.\n"
                        "Возможные причины:\n"
                        "• Каналы удалены или переименованы\n"
                        "• Username каналов истёк\n"
                        "• Бот не подписан на приватные каналы\n"
                        "Проверьте config.ini и перезапустите бота.",
                        parse_mode="HTML"
                    )
                except Exception as e:
                    self.logger.error(f"❌ Не удалось отправить уведомление об ошибке: {e}")

            # Проверяем доступ к каналам
            valid_channels = []
            for chat_id in self._resolved_channels:
                try:
                    chat = await self._client.get_chat(chat_id)
                    self.logger.info(f"✅ Доступ к каналу подтверждён: {chat.title} (ID: {chat.id})")
                    await self._test_channel_messages(chat_id)
                    valid_channels.append(chat_id)
                except (PeerIdInvalid, ChannelInvalid, ChannelPrivate) as e:
                    self.logger.warning(f"⚠️ Канал {chat_id} недоступен ({e}), пропускаю...")
                except Exception as e:
                    self.logger.error(f"❌ Ошибка доступа к каналу {chat_id}: {e}")

            self._resolved_channels = valid_channels

            if not self._resolved_channels:
                self.logger.warning("⚠️ Ни один канал не доступен для мониторинга. Бот работает в режиме ожидания.")
            else:
                await self._send_startup_test()

            @self._client.on_message(
                filters.chat(self._resolved_channels) if self._resolved_channels else filters.all
            )
            async def message_handler(client: Client, message: Message):
                if not self._resolved_channels:
                    return
                if message.chat and message.chat.id not in self._resolved_channels:
                    return
                await self._handle_new_message(client, message)

            if self._resolved_channels:
                self.logger.info(
                    f"👂 Обработчик сообщений настроен. Ожидание сообщений из {len(self._resolved_channels)} каналов..."
                )
            else:
                self.logger.info("👂 Бот работает в режиме ожидания. Добавьте валидные каналы в config.ini.")

            self._tasks.append(asyncio.create_task(self._reload_config_periodically()))
            self._tasks.append(asyncio.create_task(self._cleanup_old_entries_periodically()))

            await idle()

        except AuthKeyUnregistered:
            msg = "🔑 Сессия недействительна. Удалите bot_session.session и авторизуйтесь заново."
            self.logger.error(msg)
        except SessionPasswordNeeded:
            msg = "🔒 Требуется двухфакторная аутентификация."
            self.logger.error(msg)
        except FloodWait as e:
            msg = f"⏱ FloodWait: подождите {e.value} секунд."
            self.logger.error(msg)
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
        elif self._client:
            self.logger.info("✅ Клиент Pyrogram уже был отключён")

        try:
            from proxy_manager import PROXY_MANAGER
            if PROXY_MANAGER:
                PROXY_MANAGER.stop()
        except Exception:
            pass

        self.logger.info("👋 Бот остановлен")