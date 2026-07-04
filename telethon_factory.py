"""
Общая фабрика для создания Telethon-клиентов с учётом настроек прокси
из config.ini.

Используется двумя независимыми процессами:
  - telethon_client.py  — основной клиент, мониторит каналы (сессия "bot_session")
  - edit_config_bot.py  — клиент-обработчик админ-команд (сессия "edit_config_session")

У каждого своя сессия (свой файл .session, возможно даже другой Telegram-
аккаунт), но правила выбора прокси одинаковые, поэтому логика вынесена сюда.
"""

import logging
from typing import Any, Dict

from telethon import TelegramClient

try:
    import TelethonFakeTLS
    _FAKETLS_AVAILABLE = True
except ImportError:
    _FAKETLS_AVAILABLE = False

import socks  # pysocks, для обычного SOCKS5-прокси (xray)

from proxy_manager import get_static_proxy


def get_proxy_and_connection(config, logger: logging.Logger):
    """
    Приоритет выбора прокси:
      1. Fake-TLS MTProxy из config.ini [Settings] mtproto_proxy = host:port:secret
      2. Обычный SOCKS5 из переменной окружения TELEGRAM_PROXY (например, локальный xray)
      3. Без прокси
    """
    if getattr(config, "mtproto_proxy", None):
        host, port, secret = config.mtproto_proxy
        if not _FAKETLS_AVAILABLE:
            logger.error(
                "❌ В config.ini задан mtproto_proxy, но пакет TelethonFakeTLS не установлен "
                "(pip install TelethonFakeTLS). Прокси проигнорирован."
            )
        else:
            secret_clean = secret[2:] if secret.lower().startswith("ee") else secret
            logger.info(f"🔌 Использую Fake-TLS MTProxy: {host}:{port}")
            return (host, port, secret_clean), TelethonFakeTLS.ConnectionTcpMTProxyFakeTLS

    static_proxy = get_static_proxy()  # TELEGRAM_PROXY / TELEGRAM_PROXY_URL, обычный SOCKS5
    if static_proxy:
        logger.info(f"🔌 Использую SOCKS5: {static_proxy['hostname']}:{static_proxy['port']}")
        proxy_tuple = (socks.SOCKS5, static_proxy["hostname"], static_proxy["port"])
        if static_proxy.get("username"):
            proxy_tuple = proxy_tuple + (
                True,
                static_proxy.get("username"),
                static_proxy.get("password"),
            )
        return proxy_tuple, None

    logger.info("🔌 Подключение без прокси")
    return None, None


def build_telethon_client(session_name: str, config, logger: logging.Logger) -> TelegramClient:
    """Создаёт TelegramClient с заданным именем сессии и прокси из config.ini."""
    proxy, connection = get_proxy_and_connection(config, logger)

    kwargs: Dict[str, Any] = dict(
        session=session_name,
        api_id=int(config.api_id),
        api_hash=config.api_hash,
    )
    if proxy:
        kwargs["proxy"] = proxy
    if connection:
        kwargs["connection"] = connection

    return TelegramClient(**kwargs)