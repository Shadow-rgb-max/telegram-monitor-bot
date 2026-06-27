"""Менеджер прокси — только статический из env."""

import os
import logging
from typing import Optional, Dict, Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


def get_static_proxy() -> Optional[Dict[str, Any]]:
    """Получает статический прокси из env TELEGRAM_PROXY."""
    proxy_url = os.getenv('TELEGRAM_PROXY') or os.getenv('TELEGRAM_PROXY_URL')
    if not proxy_url:
        return None

    parsed = urlparse(proxy_url)
    if not parsed.scheme or not parsed.hostname:
        logger.warning(f"Некорректный URL прокси: {proxy_url}")
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

    logger.info(f"Статический прокси ({parsed.scheme}): {proxy_dict['hostname']}:{proxy_dict['port']}")
    return proxy_dict