"""Менеджер SOCKS5 прокси с автоматическим подбором рабочих прокси."""

import os
import time
import json
import threading
import concurrent.futures
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from urllib.parse import urlparse
import logging

import requests

logger = logging.getLogger(__name__)

# ==================== КОНФИГУРАЦИЯ ====================

class ProxyConfig:
    """Конфигурация прокси из переменных окружения."""
    USE_PROXY_POOL = os.getenv('USE_PROXY_POOL', 'false').lower() == 'true'
    TELEGRAM_PROXY = os.getenv('TELEGRAM_PROXY') or os.getenv('TELEGRAM_PROXY_URL')
    PROXY_POOL_URL = os.getenv(
        'PROXY_POOL_URL',
        'https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&proxy_format=protocolipport&format=text'
    )
    PROXY_REFRESH_INTERVAL = int(os.getenv('PROXY_REFRESH_INTERVAL', '300'))
    PROXY_POOL_SIZE = int(os.getenv('PROXY_POOL_SIZE', '10'))
    PROXY_TIMEOUT = int(os.getenv('PROXY_TIMEOUT', '30'))
    # Формат SOCKS5 для Pyrogram: {"scheme": "socks5", "hostname": "...", "port": ..., "username": "...", "password": "..."}


# ==================== МЕНЕДЖЕР ПРОКСИ ====================

class ProxyManager:
    """
    Менеджер SOCKS5 прокси с автоматическим подбором.
    Поддерживает:
    - Статический SOCKS5 прокси (TELEGRAM_PROXY)
    - Автоматический пул SOCKS5 прокси (USE_PROXY_POOL)
    - Graceful fallback на прямое соединение если пул пуст
    """

    def __init__(self):
        self.config = ProxyConfig()
        self._static_proxy: Optional[Dict[str, Any]] = None
        self._pool: List[Dict[str, Any]] = []
        self._current_index = 0
        self._lock = threading.Lock()
        self._last_refresh = datetime.now() - timedelta(hours=1)
        self._running = True
        self._proxy_ready = threading.Event()
        self._session = requests.Session()
        self._session.headers.update({'User-Agent': 'Mozilla/5.0'})

        if self.config.USE_PROXY_POOL:
            self._refresh_thread = threading.Thread(target=self._refresh_loop, daemon=True)
            self._refresh_thread.start()
            self.refresh_pool()
        else:
            if self.config.TELEGRAM_PROXY:
                self._static_proxy = self._parse_proxy_url(self.config.TELEGRAM_PROXY)
                if self._static_proxy:
                    logger.info(f"🔒 Используется статический SOCKS5 прокси: {self.config.TELEGRAM_PROXY}")
                else:
                    logger.warning("⚠️ Не удалось распарсить TELEGRAM_PROXY, работаем без прокси")
                self._proxy_ready.set()
            else:
                logger.info("🔒 Прокси не используется (прямое соединение)")
                self._proxy_ready.set()

    def _parse_proxy_url(self, url: str) -> Optional[Dict[str, Any]]:
        """
        Парсит URL прокси в формат Pyrogram.
        Поддерживает: socks5://user:pass@host:port, http://host:port, host:port
        """
        url = url.strip()
        if not url:
            return None

        # Добавляем схему если нет
        if '://' not in url:
            url = f"socks5://{url}"

        try:
            parsed = urlparse(url)
            scheme = parsed.scheme.lower()

            if scheme not in ('socks5', 'socks5h', 'socks4', 'http', 'https'):
                scheme = 'socks5'

            # Для Pyrogram используем "socks5" (без "h")
            if scheme == 'socks5h':
                scheme = 'socks5'

            proxy_dict = {
                "scheme": scheme,
                "hostname": parsed.hostname,
                "port": parsed.port or 1080,
            }

            if parsed.username:
                proxy_dict["username"] = parsed.username
            if parsed.password:
                proxy_dict["password"] = parsed.password

            return proxy_dict
        except Exception as e:
            logger.error(f"Ошибка парсинга прокси URL {url}: {e}")
            return None

    def _fetch_proxies(self) -> List[str]:
        """Загружает список SOCKS5 прокси из различных источников."""
        # Сначала пробуем основной источник
        try:
            resp = self._session.get(self.config.PROXY_POOL_URL, timeout=15)
            if resp.status_code == 200:
                proxies = []
                for line in resp.text.strip().split('\n'):
                    line = line.strip()
                    if line and (line.startswith('socks5://') or line.startswith('http://')):
                        proxies.append(line)
                    elif line and ':' in line and not line.startswith('#'):
                        # Формат host:port
                        proxies.append(f"socks5://{line}")
                if proxies:
                    logger.info(f"📡 Загружено {len(proxies)} прокси с основного источника")
                    return proxies
        except Exception as e:
            logger.warning(f"⚠️ Ошибка основного источника прокси: {e}")

        # Резервные источники GitHub
        github_sources = [
            "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
            "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
            "https://raw.githubusercontent.com/zevtyardt/proxy-list/main/socks5.txt",
            "https://raw.githubusercontent.com/ALIILAPRO/Proxy/main/socks5.txt",
            "https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS5_RAW.txt",
        ]

        for source in github_sources:
            try:
                resp = self._session.get(source, timeout=15)
                if resp.status_code == 200:
                    proxies = []
                    for line in resp.text.strip().split('\n'):
                        line = line.strip()
                        if not line or line.startswith('#'):
                            continue
                        if not line.startswith('http') and ':' in line:
                            line = f"socks5://{line}"
                        proxies.append(line)
                    if proxies:
                        logger.info(f"📡 Загружено {len(proxies)} прокси с резервного источника: {source.split('/')[-1]}")
                        return proxies
            except Exception:
                logger.debug(f"⚠️ Резервный источник недоступен: {source[:60]}...")
                continue

        logger.error("❌ Все источники прокси недоступны")
        return []

    def _test_proxy(self, proxy_url: str) -> bool:
        """Проверяет работоспособность SOCKS5 прокси через Telegram API."""
        try:
            proxy_dict = self._parse_proxy_url(proxy_url)
            if not proxy_dict:
                return False

            # Проверяем через Telegram getMe (лёгкий эндпоинт)
            # Используем requests с PySocks для проверки
            proxies = {}
            scheme = proxy_dict.get('scheme', 'socks5')
            host = proxy_dict.get('hostname')
            port = proxy_dict.get('port', 1080)
            user = proxy_dict.get('username', '')
            passwd = proxy_dict.get('password', '')

            if user and passwd:
                proxy_str = f"{scheme}://{user}:{passwd}@{host}:{port}"
            else:
                proxy_str = f"{scheme}://{host}:{port}"

            proxies = {'http': proxy_str, 'https': proxy_str}

            resp = self._session.get(
                "https://api.telegram.org",
                proxies=proxies,
                timeout=self.config.PROXY_TIMEOUT,
                verify=False
            )
            return resp.status_code in (200, 401, 404)  # Telegram отвечает даже без токена
        except Exception:
            return False

    def refresh_pool(self):
        """Обновляет пул рабочих SOCKS5 прокси."""
        if not self.config.USE_PROXY_POOL:
            return

        try:
            with self._lock:
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(self._fetch_proxies)
                    try:
                        all_proxies = future.result(timeout=30)
                    except concurrent.futures.TimeoutError:
                        logger.error("⏱ Таймаут загрузки списка прокси (>30 сек)")
                        return
                    except Exception as e:
                        logger.error(f"Ошибка загрузки прокси: {e}")
                        return

                if not all_proxies:
                    logger.warning("⚠️ Нет прокси для проверки")
                    return

                # Тестируем прокси (максимум 50 для скорости)
                working = []
                test_candidates = all_proxies[:50]

                with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
                    futures = {executor.submit(self._test_proxy, p): p for p in test_candidates}
                    for future in concurrent.futures.as_completed(futures):
                        proxy_url = futures[future]
                        try:
                            if future.result(timeout=10):
                                proxy_dict = self._parse_proxy_url(proxy_url)
                                if proxy_dict:
                                    working.append(proxy_dict)
                                    if len(working) >= self.config.PROXY_POOL_SIZE:
                                        # Отменяем оставшиеся
                                        for f in futures:
                                            f.cancel()
                                        break
                        except concurrent.futures.TimeoutError:
                            continue
                        except Exception:
                            continue

                if working:
                    self._pool = working
                    self._current_index = 0
                    logger.info(f"✅ Пул SOCKS5 прокси обновлён: {len(self._pool)} рабочих")
                    for i, p in enumerate(self._pool[:3], 1):
                        logger.debug(f"   {i}. {p['hostname']}:{p['port']}")
                    self._proxy_ready.set()
                else:
                    logger.warning("⚠️ Не найдено рабочих SOCKS5 прокси")
                    if not self._pool:
                        self._proxy_ready.clear()

                self._last_refresh = datetime.now()
        except Exception as e:
            logger.error(f"Неожиданная ошибка в refresh_pool: {e}", exc_info=True)

    def _refresh_loop(self):
        """Фоновый цикл обновления пула прокси."""
        while self._running:
            time.sleep(self.config.PROXY_REFRESH_INTERVAL)
            try:
                self.refresh_pool()
            except Exception as e:
                logger.error(f"Ошибка в _refresh_loop: {e}", exc_info=True)

    def stop(self):
        """Останавливает менеджер прокси."""
        self._running = False

    def get_proxy(self, wait_seconds: float = 0) -> Optional[Dict[str, Any]]:
        """
        Возвращает SOCKS5 прокси в формате Pyrogram.
        При необходимости ожидает появления прокси до wait_seconds.
        """
        if not self.config.USE_PROXY_POOL:
            return self._static_proxy

        if wait_seconds > 0:
            self._proxy_ready.wait(timeout=wait_seconds)

        with self._lock:
            if not self._pool:
                logger.warning("🔄 Пул SOCKS5 прокси пуст, возвращаем статический прокси или None")
                return self._static_proxy

            proxy = self._pool[self._current_index % len(self._pool)]
            self._current_index += 1
            return proxy

    def report_failure(self, proxy_dict: Dict[str, Any]):
        """Удаляет нерабочий прокси из пула и запускает обновление."""
        if not self.config.USE_PROXY_POOL:
            return

        with self._lock:
            if not self._pool:
                return

            # Находим и удаляем нерабочий прокси
            proxy_str = f"{proxy_dict.get('hostname')}:{proxy_dict.get('port')}"
            for i, p in enumerate(self._pool):
                if p.get('hostname') == proxy_dict.get('hostname') and p.get('port') == proxy_dict.get('port'):
                    logger.warning(f"⚠️ Удаляю нерабочий SOCKS5 прокси: {proxy_str}")
                    del self._pool[i]
                    if i <= self._current_index and self._current_index > 0:
                        self._current_index -= 1
                    break

            if not self._pool:
                logger.warning("🔄 Пул SOCKS5 прокси пуст, запускаю экстренное обновление")
                self._proxy_ready.clear()
                threading.Thread(target=self._safe_refresh_pool, daemon=True).start()

    def _safe_refresh_pool(self):
        """Безопасное обновление пула в отдельном потоке."""
        try:
            self.refresh_pool()
        except Exception as e:
            logger.error(f"Ошибка при экстренном обновлении пула: {e}")

    def get_stats(self) -> Dict[str, Any]:
        """Возвращает статистику менеджера прокси."""
        with self._lock:
            return {
                'use_proxy_pool': self.config.USE_PROXY_POOL,
                'pool_size': len(self._pool),
                'static_proxy': self._static_proxy is not None,
                'last_refresh': self._last_refresh.isoformat(),
            }


# Глобальный экземпляр
PROXY_MANAGER: Optional[ProxyManager] = None


def get_proxy_manager() -> ProxyManager:
    """Возвращает (или создаёт) глобальный экземпляр ProxyManager."""
    global PROXY_MANAGER
    if PROXY_MANAGER is None:
        PROXY_MANAGER = ProxyManager()
    return PROXY_MANAGER