"""
Клиент мониторинга Telegram-каналов на Telethon.
Версия 4.0 — миграция с Pyrogram на Telethon для поддержки
Fake-TLS MTProto прокси (в дополнение к обычному SOCKS5 через xray).

Версия 4.2 — прокси-логика вынесена в telethon_factory.py (переиспользуется
также отдельным процессом edit_config_bot.py, который обслуживает
админ-команды под собственной сессией).
"""

import asyncio
import os
import json
import logging
from typing import List, Optional, Dict, Any

from telethon import TelegramClient, events, errors, utils as telethon_utils
from telethon.tl.types import Channel
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest

from config import BotConfig, get_config
from keyword_monitor import KeywordMonitor
from notifier import send_notification, send_error_notification
from telethon_factory import build_telethon_client, get_proxy_and_connection

logger = logging.getLogger("telegram_keyword_monitor")

SESSION_NAME = "bot_session"


class TelethonKeywordBot:
    """
    Бот для мониторинга Telegram-каналов по ключевым словам (Telethon).

    Редактирование config.ini (view / set keywords / set channels / dedup)
    вынесено в отдельный процесс edit_config_bot.py со своей сессией —
    см. этот файл и admin_commands.py. Данный класс отвечает только за
    мониторинг каналов и отправку уведомлений; изменения config.ini,
    сохранённые другим процессом, подхватываются здесь автоматически
    через периодический опрос mtime файла (см. _reload_config_periodically).
    """

    def __init__(self, config: BotConfig, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.monitor = KeywordMonitor(
            config.keywords,
            throttle_seconds=60,
            dedup_window_hours=config.dedup_window_hours,
        )

        self._config_path = "config.ini"
        self._config_mtime = os.path.getmtime(self._config_path)

        self._channel_cache_path = "channel_cache.json"
        self._channel_cache = self._load_channel_cache()

        self._resolved_channels: List[int] = []
        self._client: Optional[TelegramClient] = None
        self._running = False
        self._tasks: List[asyncio.Task] = []
        self._message_handler = None

    # ==================== Кэш каналов ====================

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

    # ==================== Прокси / клиент ====================

    def _get_proxy_and_connection(self):
        return get_proxy_and_connection(self.config, self.logger)

    def _build_client(self) -> TelegramClient:
        return build_telethon_client(SESSION_NAME, self.config, self.logger)

    # ==================== Разрешение каналов ====================

    async def _resolve_channel(self, channel: str) -> Optional[int]:
        channel = channel.strip()

        if channel.lstrip("-").isdigit():
            return int(channel)

        # Приватные ссылки-приглашения
        if channel.startswith("https://t.me/+") or channel.startswith("t.me/+") or channel.startswith("+"):
            invite_hash = channel.split("+")[-1]
            try:
                result = await self._client(ImportChatInviteRequest(invite_hash))
                chat = result.chats[0]
                chat_id = telethon_utils.get_peer_id(chat)
                self._channel_cache[channel] = chat_id
                self._save_channel_cache()
                self.logger.info(f"✅ Приватный канал {channel} разрешён → ID: {chat_id}")
                return chat_id
            except errors.UserAlreadyParticipantError:
                cached = self._channel_cache.get(channel)
                if cached:
                    return cached
                self.logger.warning(
                    f"ℹ️ Уже участник {channel}, но ID не в кэше — удалите канал из группы и "
                    "переприсоединитесь, либо укажите числовой ID вручную"
                )
                return None
            except Exception as e:
                self.logger.error(f"❌ Ошибка присоединения к {channel}: {e}")
                return None

        # Публичные каналы: подписываемся принудительно
        username = channel.lstrip("@")
        try:
            try:
                await self._client(JoinChannelRequest(username))
                self.logger.info(f"✅ Подписался на {channel}")
            except errors.UserAlreadyParticipantError:
                self.logger.debug(f"ℹ️ Уже подписан на {channel}")
            except Exception as join_err:
                self.logger.debug(f"ℹ️ join_channel для {channel}: {join_err}")

            entity = await self._client.get_entity(username)
            chat_id = telethon_utils.get_peer_id(entity)

            self._channel_cache[channel] = chat_id
            self._save_channel_cache()
            self.logger.info(f"✅ Канал {channel} разрешён → ID: {chat_id}")
            await asyncio.sleep(0.05)
            return chat_id

        except errors.FloodWaitError as e:
            self.logger.warning(f"⏱ FloodWait при разрешении {channel}: жду {e.seconds} сек.")
            await asyncio.sleep(e.seconds)
            return await self._resolve_channel(channel)
        except (errors.ChannelInvalidError, errors.ChannelPrivateError) as e:
            self.logger.warning(f"⚠️ Канал {channel} недоступен: {e}")
            return None
        except (errors.UsernameNotOccupiedError, errors.UsernameInvalidError) as e:
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

    # ==================== Обработка сообщений ====================

    @staticmethod
    def _is_supported_chat(chat: Any) -> bool:
        """Каналы и супергруппы в Telethon — это один и тот же TL-тип Channel."""
        return isinstance(chat, Channel)

    async def _handle_new_message(self, event: events.NewMessage.Event):
        try:
            chat = await event.get_chat()
            if not self._is_supported_chat(chat):
                return

            chat_id = event.chat_id
            channel_title = getattr(chat, "title", None) or str(chat_id)

            message = event.message
            message_text = message.message or ""

            log_preview = message_text[:200] if message_text else "Без текста"
            try:
                log_preview.encode("utf-8")
            except UnicodeEncodeError:
                log_preview = message_text.encode("utf-8", "replace").decode("utf-8")[:200]

            self.logger.info(
                f"[MONITOR] Канал: {channel_title} | ID: {message.id} | Сообщение: {log_preview}"
            )

            matched_keywords = self.monitor.match_keywords(message_text)
            matched_keywords = [
                kw for kw in matched_keywords
                if self.monitor.should_notify(chat_id, kw, message_text)
            ]

            if matched_keywords:
                username = getattr(chat, "username", None)
                if username:
                    message_link = f"https://t.me/{username}/{message.id}"
                else:
                    message_link = f"https://t.me/c/{str(chat_id).replace('-100', '')}/{message.id}"

                await send_notification(
                    self._client,
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
                    self._client, self._get_channel_id(), f"Ошибка обработки сообщения: {e}"
                )
            except Exception:
                pass

    def _register_message_handler(self, resolved_channels: List[int]) -> None:
        if not self._client:
            return

        if self._message_handler is not None:
            try:
                self._client.remove_event_handler(self._message_handler)
            except Exception:
                pass
            self._message_handler = None

        if not resolved_channels:
            self.logger.info("👂 Нет каналов для мониторинга — обработчик не регистрируется")
            return

        async def message_handler(event: events.NewMessage.Event):
            await self._handle_new_message(event)

        self._client.add_event_handler(message_handler, events.NewMessage(chats=resolved_channels))
        self._message_handler = message_handler
        self.logger.info(
            f"👂 Активные каналы для обработки: {len(resolved_channels)} | IDs: {resolved_channels}"
        )

    # ==================== Периодические задачи ====================

    async def _reload_config_now(self) -> None:
        """Перечитывает config.ini и, если что-то изменилось, применяет новые
        значения "на лету" (без перезапуска процесса)."""
        try:
            mtime = os.path.getmtime(self._config_path)
            new_config = get_config(self._config_path)

            changed = (
                new_config.channels != self.config.channels
                or new_config.keywords != self.config.keywords
                or new_config.dedup_window_hours != self.config.dedup_window_hours
            )

            if changed:
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
            self.logger.error(f"❌ Ошибка обновления конфига: {e}")

    async def _reload_config_periodically(self):
        while self._running:
            await asyncio.sleep(420)
            try:
                mtime = os.path.getmtime(self._config_path)
                if mtime != self._config_mtime:
                    self.logger.info(
                        "📝 Обнаружено изменение config.ini (вероятно, через edit_config_bot). "
                        "Перезагружаю..."
                    )
                    await self._reload_config_now()
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

    # ==================== Вспомогательное ====================

    async def _test_channel_messages(self, chat_id: int, limit: int = 5):
        try:
            messages = await self._client.get_messages(chat_id, limit=limit)
            for msg in messages:
                text = msg.message or "Без текста"
                self.logger.debug(f"Тест: [{chat_id}] {text[:100]} (ID: {msg.id})")
        except Exception as e:
            self.logger.error(f"❌ Ошибка теста канала {chat_id}: {e}")

    async def _send_startup_test(self):
        try:
            proxy, connection = self._get_proxy_and_connection()
            if proxy and connection:
                proxy_info = f"Fake-TLS MTProxy: {proxy[0]}:{proxy[1]}"
            elif proxy:
                proxy_info = f"SOCKS5: {proxy[1]}:{proxy[2]}"
            else:
                proxy_info = "Без прокси"

            await self._client.send_message(
                self._get_channel_id(),
                f"🚀 <b>Бот запущен</b> (Telethon)\n"
                f"📡 {proxy_info}\n"
                f"📊 Каналов: {len(self._resolved_channels)}\n"
                f"🔍 Ключевых слов: {len(self.config.keywords)}\n"
                f"⏰ Окно дедупликации: {self.config.dedup_window_hours}ч",
                parse_mode="html",
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

    # ==================== Основной цикл ====================

    async def start(self):
        self._running = True

        try:
            self._client = self._build_client()
            self.logger.info("🔌 Подключение к Telegram...")

            connected = False
            attempts = 0
            max_attempts = 5

            while not connected and attempts < max_attempts:
                attempts += 1
                try:
                    await self._client.connect()
                    connected = True
                    self.logger.info(f"✅ Подключение успешно (попытка {attempts})!")
                except Exception as e:
                    self.logger.warning(f"⚠️ Ошибка подключения (попытка {attempts}/{max_attempts}): {e}")
                    if attempts < max_attempts:
                        await asyncio.sleep(3)

            if not connected:
                self.logger.error(f"❌ Не удалось подключиться после {max_attempts} попыток")
                return

            if not await self._client.is_user_authorized():
                self.logger.error(
                    "🔑 Сессия не авторизована. Создайте bot_session.session заранее через "
                    "create_session_faketls.py (QR-логин), затем перезапустите бота."
                )
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
                        parse_mode="html",
                    )
                except Exception:
                    pass

            valid_channels = []
            for chat_id in self._resolved_channels:
                try:
                    entity = await self._client.get_entity(chat_id)
                    title = getattr(entity, "title", chat_id)
                    self.logger.info(f"✅ Доступ к каналу: {title} (ID: {chat_id})")
                    await self._test_channel_messages(chat_id)
                    valid_channels.append(chat_id)
                except (errors.ChannelPrivateError, errors.ChannelInvalidError) as e:
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

            await self._client.run_until_disconnected()

        except errors.AuthKeyUnregisteredError:
            self.logger.error("🔑 Сессия недействительна. Удалите bot_session.session и создайте заново.")
        except errors.SessionPasswordNeededError:
            self.logger.error("🔒 Требуется двухфакторная аутентификация.")
        except errors.FloodWaitError as e:
            self.logger.error(f"⏱ FloodWait: подождите {e.seconds} секунд.")
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

        if self._client and self._client.is_connected():
            try:
                await self._client.disconnect()
                self.logger.info("✅ Клиент Telethon отключён")
            except Exception as e:
                self.logger.error(f"❌ Ошибка при отключении: {e}")

        self.logger.info("👋 Бот остановлен")