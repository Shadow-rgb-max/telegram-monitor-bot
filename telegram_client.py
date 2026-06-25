import asyncio
from telethon import TelegramClient, events, connection
from telethon.tl.types import PeerChannel
from telethon.errors import (
    SessionPasswordNeededError,
    AuthKeyUnregisteredError,
    FloodWaitError,
)
from config import BotConfig, get_config
from keyword_monitor import KeywordMonitor
from notifier import send_notification, send_error_notification
import logging
import os
import json
import time


class TelegramKeywordBot:
    """
    Бот для мониторинга Telegram-каналов по ключевым словам и отправки уведомлений в указанный канал.
    - Автоматически обновляет конфиг при изменениях.
    - Логирует все действия и ошибки.
    - Работает асинхронно на базе Telethon.
    - Поддерживает MTProto Proxy для обхода блокировок.
    """

    def __init__(self, config: BotConfig, logger: logging.Logger):
        """
        :param config: Конфигурация бота (BotConfig)
        :param logger: Логгер
        """
        self.config = config
        self.logger = logger

        # Настройка MTProto Proxy если указан
        if config.mtproto_proxy:
            host, port, secret = config.mtproto_proxy
            self.logger.info(f"Используется MTProto Proxy: {host}:{port}")
            self.client = TelegramClient(
                "bot_session", 
                config.api_id, 
                config.api_hash,
                connection=connection.ConnectionTcpMTProxyRandomizedIntermediate,
                proxy=(host, port, secret)
            )
        else:
            self.logger.info("MTProto Proxy не используется (прямое соединение)")
            self.client = TelegramClient("bot_session", config.api_id, config.api_hash)

        self.monitor = KeywordMonitor(
            config.keywords, 
            throttle_seconds=60, 
            dedup_window_hours=config.dedup_window_hours
        )
        self._config_path = "config.ini"
        self._config_mtime = os.path.getmtime(self._config_path)
        self._resolved_channels = []
        self._handler = None
        self._channel_cache_path = "channel_cache.json"
        self._channel_cache = self._load_channel_cache()
        self._rate_limit_delay = 2  # seconds between username resolves

    def _load_channel_cache(self):
        if os.path.exists(self._channel_cache_path):
            try:
                with open(self._channel_cache_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _save_channel_cache(self):
        try:
            with open(self._channel_cache_path, "w", encoding="utf-8") as f:
                json.dump(self._channel_cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.logger.error(f"Ошибка сохранения кэша каналов: {e}")

    async def _resolve_channel(self, channel):
        # Если это уже channel_id (int или строка с цифрами), возвращаем как есть
        if str(channel).lstrip("-").isdigit():
            return int(channel)
        # Если есть в кэше
        if channel in self._channel_cache:
            return self._channel_cache[channel]
        # Разрешаем username через Telegram
        try:
            entity = await self.client.get_entity(channel)
            channel_id = entity.id
            self._channel_cache[channel] = channel_id
            self._save_channel_cache()
            # Rate limiting
            await asyncio.sleep(self._rate_limit_delay)
            return channel_id
        except FloodWaitError as e:
            self.logger.error(f"FloodWait при разрешении {channel}: жду {e.seconds} сек.")
            await asyncio.sleep(e.seconds)
            return await self._resolve_channel(channel)
        except Exception as e:
            self.logger.error(f"Ошибка разрешения username {channel}: {e}")
            return channel

    async def _resolve_all_channels(self, channels):
        resolved = []
        for channel in channels:
            resolved_id = await self._resolve_channel(channel)
            resolved.append(resolved_id)
        return resolved

    async def _reload_config_periodically(self):
        """
        Периодически проверяет изменения в config.ini и обновляет ключевые слова и каналы.
        """
        while True:
            await asyncio.sleep(420)  # 7 минут
            try:
                mtime = os.path.getmtime(self._config_path)
                if mtime != self._config_mtime:
                    new_config = get_config(self._config_path)
                    if (new_config.channels != self.config.channels) or (
                        new_config.keywords != self.config.keywords
                    ):
                        self.logger.info(
                            "Обнаружено изменение config.ini. Обновляю keywords и channels..."
                        )
                        self.config.channels = new_config.channels
                        self.config.keywords = new_config.keywords
                        self.monitor.keywords = new_config.keywords
                        # Перерегистрировать каналы и обработчик
                        self._resolved_channels = []
                        for channel in self.config.channels:
                            try:
                                entity = await self.client.get_entity(channel)
                                self._resolved_channels.append(entity)
                                self.logger.info(
                                    f"Доступ к каналу {getattr(entity, 'title', str(entity))} (ID: {entity.id}) подтвержден (reload)"
                                )
                            except Exception as e:
                                self.logger.error(
                                    f"Ошибка доступа к каналу {channel} при reload: {e}"
                                )
                                await send_error_notification(
                                    self.client,
                                    self._get_channel_id(),
                                    f"Ошибка доступа к каналу {channel} при reload: {e}",
                                )
                        # Перерегистрировать обработчик
                        if self._handler:
                            self.client.remove_event_handler(
                                self._handler, events.NewMessage
                            )

                        @self.client.on(
                            events.NewMessage(chats=self._resolved_channels)
                        )
                        async def handle_new_message(event):
                            try:
                                if isinstance(event.message.peer_id, PeerChannel):
                                    channel_id = event.message.peer_id.channel_id
                                    entity = await self.client.get_entity(
                                        PeerChannel(channel_id)
                                    )
                                    message_text = event.message.message or ""
                                    self.logger.info(
                                        f"[MONITOR] Канал: {getattr(entity, 'title', str(entity))} | ID: {event.message.id} | Сообщение: {message_text or 'Без текста'}"
                                    )
                                    matched_keywords = self.monitor.match_keywords(
                                        message_text
                                    )
                                    matched_keywords = [
                                        kw
                                        for kw in matched_keywords
                                        if self.monitor.should_notify(channel_id, kw, message_text)
                                    ]
                                    if matched_keywords:
                                        message_link = f"t.me/{getattr(entity, 'username', '')}/{event.message.id}"
                                        await send_notification(
                                            self.client,
                                            self._get_channel_id(),
                                            getattr(entity, "title", str(entity)),
                                            matched_keywords,
                                            message_text,
                                            message_link,
                                        )
                                    else:
                                        if matched_keywords:
                                            self.logger.debug(
                                                f"[reload] Сообщение заблокировано дедупликацией или throttling: {message_text[:100]}..."
                                            )
                                        else:
                                            self.logger.debug(
                                                f"[reload] Ключевые слова не найдены: {message_text[:100]}..."
                                            )
                            except Exception as e:
                                self.logger.error(f"Ошибка обработки сообщения: {e}")
                                await send_error_notification(
                                    self.client,
                                    self._get_channel_id(),
                                    f"Ошибка обработки сообщения: {e}",
                                )

                        self._handler = handle_new_message
                        self.logger.info(
                            "Каналы и ключевые слова обновлены из config.ini!"
                        )
                    self._config_mtime = mtime
            except Exception as e:
                self.logger.error(f"Ошибка при автообновлении config.ini: {e}")

    async def _cleanup_old_entries_periodically(self):
        """
        Периодически очищает старые записи для экономии памяти.
        """
        while True:
            await asyncio.sleep(3600)  # Каждый час
            try:
                self.monitor.cleanup_old_entries()
                stats = self.monitor.get_stats()
                self.logger.info(f"Очистка завершена. Статистика: {stats}")
            except Exception as e:
                self.logger.error(f"Ошибка при очистке старых записей: {e}")

    async def test_channel_messages(self, channel, limit: int = 5) -> None:
        """
        Тестирует получение последних сообщений из канала.
        :param channel: Канал
        :param limit: Количество сообщений
        """
        try:
            async for message in self.client.iter_messages(channel, limit=limit):
                self.logger.debug(
                    f"Тест: Сообщение в канале {getattr(channel, 'title', str(channel))}: {getattr(message, 'message', '') or 'Без текста'} (ID: {message.id})"
                )
        except Exception as e:
            self.logger.error(
                f"Ошибка при получении сообщений из канала {getattr(channel, 'title', str(channel))}: {e}"
            )

    async def start(self) -> None:
        """
        Запускает бота, авторизует пользователя, проверяет каналы, регистрирует обработчики.
        """
        try:
            self.logger.debug("Попытка авторизации пользователя")
            await self.client.start()
            self.logger.info("Пользователь успешно авторизован")

            # Групповое разрешение username → channel_id с rate limiting
            self._resolved_channels = await self._resolve_all_channels(self.config.channels)
            self.logger.debug(f"Зарегистрированные каналы (ID): {self._resolved_channels}")

            for channel_id in self._resolved_channels:
                try:
                    entity = await self.client.get_entity(PeerChannel(channel_id))
                    self.logger.info(
                        f"Доступ к каналу {getattr(entity, 'title', str(entity))} (ID: {entity.id}) подтвержден"
                    )
                    await self.test_channel_messages(entity)
                except Exception as e:
                    self.logger.error(f"Ошибка доступа к каналу {channel_id}: {e}")
                    await send_error_notification(
                        self.client,
                        self._get_channel_id(),
                        f"Ошибка доступа к каналу {channel_id}: {e}",
            )

            try:
                await self.client.send_message(
                    self._get_channel_id(),
                    "Тестовое сообщение от пользователя: проверка работоспособности",
                )
                self.logger.info("Тестовое сообщение отправлено в канал с channel_id")
            except Exception as e:
                self.logger.error(f"Ошибка отправки тестового сообщения: {e}")
                await send_error_notification(
                    self.client,
                    self._get_channel_id(),
                    f"Ошибка отправки тестового сообщения: {e}",
                )

            @self.client.on(events.NewMessage(chats=self._resolved_channels))
            async def handle_new_message(event):
                try:
                    if isinstance(event.message.peer_id, PeerChannel):
                        channel_id = event.message.peer_id.channel_id
                        entity = await self.client.get_entity(PeerChannel(channel_id))
                        message_text = event.message.message or ""
                        self.logger.info(
                            f"[MONITOR] Канал: {getattr(entity, 'title', str(entity))} | ID: {event.message.id} | Сообщение: {message_text or 'Без текста'}"
                        )
                        matched_keywords = self.monitor.match_keywords(message_text)
                        matched_keywords = [
                            kw
                            for kw in matched_keywords
                            if self.monitor.should_notify(channel_id, kw, message_text)
                        ]
                        if matched_keywords:
                            message_link = f"t.me/{getattr(entity, 'username', '')}/{event.message.id}"
                            await send_notification(
                                self.client,
                                self._get_channel_id(),
                                getattr(entity, "title", str(entity)),
                                matched_keywords,
                                message_text,
                                message_link,
                            )
                        else:
                            if matched_keywords:
                                self.logger.debug(
                                    f"Сообщение заблокировано дедупликацией или throttling: {message_text[:100]}..."
                                )
                            else:
                                self.logger.debug(
                                    f"Ключевые слова не найдены: {message_text[:100]}..."
                                )
                except Exception as e:
                    self.logger.error(f"Ошибка обработки сообщения: {e}")
                    await send_error_notification(
                        self.client,
                        self._get_channel_id(),
                        f"Ошибка обработки сообщения: {e}",
                    )

            self._handler = handle_new_message

            # Запуск фоновой задачи автообновления конфига
            asyncio.create_task(self._reload_config_periodically())

            # Запуск фоновой задачи очистки старых записей
            asyncio.create_task(self._cleanup_old_entries_periodically())

            self.logger.info(
                "Обработчик сообщений настроен. Ожидание новых сообщений..."
            )
            await self.client.run_until_disconnected()

        except AuthKeyUnregisteredError:
            msg = "Сессия недействительна. Удалите bot_session.session и попробуйте снова."
            self.logger.error(msg)
            await send_error_notification(self.client, self._get_channel_id(), msg)
        except FloodWaitError as e:
            msg = f"Достигнут лимит запросов. Подождите {e.seconds} секунд."
            self.logger.error(msg)
            await send_error_notification(self.client, self._get_channel_id(), msg)
        except SessionPasswordNeededError:
            msg = "Требуется двухфакторная аутентификация. Введите пароль."
            self.logger.error(msg)
            await send_error_notification(self.client, self._get_channel_id(), msg)
        except Exception as e:
            msg = f"Ошибка в основном цикле: {e}"
            self.logger.error(msg)
            await send_error_notification(self.client, self._get_channel_id(), msg)
            raise

    async def shutdown(self) -> None:
        """
        Корректно завершает работу бота и отключает клиента.
        """
        try:
            self.logger.info("Завершение работы бота...")
            await self.client.disconnect()
            self.logger.info("Бот успешно отключен")
        except Exception as e:
            self.logger.error(f"Ошибка при завершении работы: {e}")

    def _get_channel_id(self):
        """
        Возвращает channel_id в нужном формате (int, если возможно, иначе str).
        """
        try:
            return (
                int(self.config.channel_id)
                if str(self.config.channel_id).lstrip("-").isdigit()
                else self.config.channel_id
            )
        except Exception:
            return self.config.channel_id