"""Менеджер SOCKS5 прокси с автоматическим подбором рабочих прокси.
Версия 3.0 — принудительный режим прокси (без прямого соединения), детальное логирование.
"""

import os
import time
import json
import threading
import concurrent.futures
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from urllib.parse import urlparse
import logging
import logging.handlers

import requests

# ==================== НАСТРОЙКА ЛОГИРОВАНИЯ ====================

# Отдельный логгер для процесса подбора прокси
_proxy_logger = None

def _get_proxy_logger() -> logging.Logger:
    """Возвращает логгер для детального логирования подбора прокси."""
    global _proxy_logger
    if _proxy_logger is not None:
        return _proxy_logger

    _proxy_logger = logging.getLogger("proxy_manager.detailed")
    _proxy_logger.setLevel(logging.DEBUG)
    _proxy_logger.propagate = False  # Не дублируем в root logger

    # Формат с микросекундами для точного тайминга
    formatter = logging.Formatter(
        "%(asctime)s.%(msecs)03d | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Ротация по 5MB, храним 5 файлов
    file_handler = logging.handlers.RotatingFileHandler(
        "proxy_manager.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)

    # Также выводим в stdout для Docker
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler.setLevel(logging.INFO)

    _proxy_logger.addHandler(file_handler)
    _proxy_logger.addHandler(stream_handler)

    return _proxy_logger


# Основной логгер (краткий, для основного bot.log)
logger = logging.getLogger(__name__)


# ==================== КОНФИГУРАЦИЯ ====================

class ProxyConfig:
    """Конфигурация прокси из переменных окружения."""
    # ВСЕГДА используем пул прокси — прямое соединение запрещено
    USE_PROXY_POOL = True  # Принудительно True

    # Статический прокси игнорируется в режиме принудительного пула
    TELEGRAM_PROXY = os.getenv('TELEGRAM_PROXY') or os.getenv('TELEGRAM_PROXY_URL')

    # Основной источник — ProxyScrape API
    PROXY_POOL_URL = os.getenv(
        'PROXY_POOL_URL',
        'https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&proxy_format=protocolipport&format=text&protocol=socks5&timeout=10000'
    )

    # Резервные источники (GitHub mirrors + доп. API)
    BACKUP_SOURCES = [
        'https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&proxy_format=protocolipport&format=text&protocol=socks5',
        'https://cdn.jsdelivr.net/gh/proxyscrape/free-proxy-list@main/proxies/protocols/socks5/data.txt',
        'https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt',
        'https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt',
        'https://raw.githubusercontent.com/zevtyardt/proxy-list/main/socks5.txt',
        'https://raw.githubusercontent.com/ALIILAPRO/Proxy/main/socks5.txt',
        'https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS5_RAW.txt',
    ]

    PROXY_REFRESH_INTERVAL = int(os.getenv('PROXY_REFRESH_INTERVAL', '300'))
    PROXY_POOL_SIZE = int(os.getenv('PROXY_POOL_SIZE', '10'))
    PROXY_TIMEOUT = int(os.getenv('PROXY_TIMEOUT', '15'))
    PROXY_TEST_TIMEOUT = int(os.getenv('PROXY_TEST_TIMEOUT', '10'))

    # Максимальное количество прокси для тестирования за один цикл
    MAX_PROXIES_TO_TEST = int(os.getenv('MAX_PROXIES_TO_TEST', '100'))

    # Количество воркеров для параллельного тестирования
    TEST_WORKERS = int(os.getenv('PROXY_TEST_WORKERS', '20'))


# ==================== МЕНЕДЖЕР ПРОКСИ ====================

class ProxyManager:
    """
    Менеджер SOCKS5 прокси с автоматическим подбором.
    Версия 3.0:
    - Принудительный режим: ТОЛЬКО прокси, прямое соединение запрещено
    - Детальное логирование всего процесса подбора в proxy_manager.log
    - Множественные источники с fallback
    - Graceful degradation: если пул пуст — блокируем get_proxy() до появления рабочих
    """

    def __init__(self):
        self.config = ProxyConfig()
        self._pool: List[Dict[str, Any]] = []
        self._current_index = 0
        self._lock = threading.Lock()
        self._last_refresh = datetime.now() - timedelta(hours=1)
        self._running = True
        self._proxy_ready = threading.Event()
        self._session = requests.Session()
        self._session.headers.update({'User-Agent': 'Mozilla/5.0'})

        # Статистика
        self._stats = {
            'total_fetched': 0,
            'total_tested': 0,
            'total_working': 0,
            'failed_sources': [],
            'last_successful_source': None,
            'refresh_count': 0,
        }

        self._plog = _get_proxy_logger()
        self._plog.info("=" * 60)
        self._plog.info("ProxyManager v3.0 инициализирован")
        self._plog.info(f"Режим: ПРИНУДИТЕЛЬНЫЙ (только прокси, без прямого соединения)")
        self._plog.info(f"Целевой размер пула: {self.config.PROXY_POOL_SIZE}")
        self._plog.info(f"Макс. прокси для теста: {self.config.MAX_PROXIES_TO_TEST}")
        self._plog.info(f"Воркеры тестирования: {self.config.TEST_WORKERS}")
        self._plog.info(f"Таймаут теста: {self.config.PROXY_TEST_TIMEOUT}с")
        self._plog.info(f"Интервал обновления: {self.config.PROXY_REFRESH_INTERVAL}с")
        self._plog.info("=" * 60)

        # Запускаем фоновый поток обновления
        self._refresh_thread = threading.Thread(target=self._refresh_loop, daemon=True)
        self._refresh_thread.start()

        # Первоначальное заполнение пула (блокирующее)
        self._plog.info("[INIT] Запуск первоначального подбора прокси...")
        self.refresh_pool(blocking=True)

        if not self._pool:
            self._plog.error("[INIT] КРИТИЧЕСКАЯ ОШИБКА: Не удалось найти ни одного рабочего прокси при старте!")
            self._plog.error("[INIT] Бот не сможет подключиться к Telegram без прокси.")
            logger.error("❌ Ни одного рабочего SOCKS5 прокси не найдено. Проверьте интернет-соединение.")

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
            self._plog.warning(f"[PARSE] Ошибка парсинга прокси URL '{url[:50]}...': {e}")
            return None

    def _fetch_from_source(self, url: str, source_name: str) -> List[str]:
        """Загружает прокси из одного источника."""
        self._plog.info(f"[FETCH] Источник: {source_name}")
        self._plog.info(f"[FETCH] URL: {url[:80]}...")

        start_time = time.time()
        try:
            resp = self._session.get(url, timeout=20)
            elapsed = time.time() - start_time

            if resp.status_code != 200:
                self._plog.warning(f"[FETCH] {source_name} — HTTP {resp.status_code} (за {elapsed:.2f}с)")
                return []

            proxies = []
            lines = resp.text.strip().split('\n')
            self._plog.info(f"[FETCH] {source_name} — получено {len(lines)} строк (за {elapsed:.2f}с)")

            for line in lines:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue

                # Формат protocol://host:port или host:port
                if line.startswith(('socks5://', 'socks4://', 'http://', 'https://')):
                    proxies.append(line)
                elif ':' in line:
                    # Предполагаем SOCKS5 если схема не указана
                    proxies.append(f"socks5://{line}")

            socks5_only = [p for p in proxies if 'socks5' in p.lower()]
            self._plog.info(f"[FETCH] {source_name} — распарсено {len(proxies)} прокси, из них SOCKS5: {len(socks5_only)}")
            return socks5_only

        except requests.exceptions.Timeout:
            self._plog.warning(f"[FETCH] {source_name} — ТАЙМАУТ (>20с)")
            return []
        except requests.exceptions.ConnectionError as e:
            self._plog.warning(f"[FETCH] {source_name} — ОШИБКА СОЕДИНЕНИЯ: {e}")
            return []
        except Exception as e:
            self._plog.warning(f"[FETCH] {source_name} — ОШИБКА: {type(e).__name__}: {e}")
            return []

    def _fetch_proxies(self) -> List[str]:
        """Загружает список SOCKS5 прокси из всех источников с fallback."""
        self._plog.info("=" * 60)
        self._plog.info("[FETCH_ALL] Начинаю загрузку прокси из всех источников")
        self._plog.info("=" * 60)

        all_proxies: List[str] = []
        failed = []

        # Основной источник
        primary = self._fetch_from_source(self.config.PROXY_POOL_URL, "ProxyScrape API (primary)")
        if primary:
            all_proxies.extend(primary)
            self._stats['last_successful_source'] = "ProxyScrape API"
            self._plog.info(f"[FETCH_ALL] Основной источник успешен: {len(primary)} прокси")
        else:
            failed.append("ProxyScrape API")
            self._plog.warning("[FETCH_ALL] Основной источник недоступен, пробую резервные...")

        # Резервные источники (пробуем пока не наберём достаточно)
        for idx, source_url in enumerate(self.config.BACKUP_SOURCES, 1):
            if len(all_proxies) >= self.config.MAX_PROXIES_TO_TEST:
                self._plog.info(f"[FETCH_ALL] Достаточно прокси для теста ({len(all_proxies)}), останавливаю загрузку")
                break

            source_name = f"Backup-{idx} ({source_url.split('/')[-1][:30]})"
            proxies = self._fetch_from_source(source_url, source_name)
            if proxies:
                all_proxies.extend(proxies)
                if not self._stats['last_successful_source']:
                    self._stats['last_successful_source'] = source_name
                self._plog.info(f"[FETCH_ALL] {source_name}: +{len(proxies)} прокси (всего: {len(all_proxies)})")
            else:
                failed.append(source_name)

        # Удаляем дубликаты сохраняя порядок
        seen = set()
        unique = []
        for p in all_proxies:
            if p not in seen:
                seen.add(p)
                unique.append(p)

        self._stats['total_fetched'] += len(unique)
        self._stats['failed_sources'] = failed

        self._plog.info("=" * 60)
        self._plog.info(f"[FETCH_ALL] ИТОГО: уникальных SOCKS5 прокси: {len(unique)}")
        self._plog.info(f"[FETCH_ALL] Успешных источников: {len(self._stats['failed_sources']) - len(failed) + 1}")
        self._plog.info(f"[FETCH_ALL] Неудачных источников: {len(failed)}")
        if failed:
            self._plog.info(f"[FETCH_ALL] Список неудачных: {', '.join(failed[:5])}")
        self._plog.info("=" * 60)

        return unique

    def _test_single_proxy(self, proxy_url: str, proxy_id: int) -> Optional[Dict[str, Any]]:
        """Тестирует один прокси. Возвращает dict или None."""
        proxy_dict = self._parse_proxy_url(proxy_url)
        if not proxy_dict:
            self._plog.debug(f"[TEST #{proxy_id}] Пропуск — не удалось распарсить URL")
            return None

        host = proxy_dict.get('hostname', '?')
        port = proxy_dict.get('port', 0)
        proxy_str = f"{host}:{port}"

        start_time = time.time()

        try:
            scheme = proxy_dict.get('scheme', 'socks5')
            user = proxy_dict.get('username', '')
            passwd = proxy_dict.get('password', '')

            if user and passwd:
                proxy_str_full = f"{scheme}://{user}:{passwd}@{host}:{port}"
            else:
                proxy_str_full = f"{scheme}://{host}:{port}"

            proxies = {'http': proxy_str_full, 'https': proxy_str_full}

            resp = self._session.get(
                "https://api.telegram.org",
                proxies=proxies,
                timeout=self.config.PROXY_TEST_TIMEOUT,
                verify=False
            )
            elapsed = time.time() - start_time

            if resp.status_code in (200, 401, 404):
                self._plog.info(f"[TEST #{proxy_id}] ✅ РАБОТАЕТ {proxy_str} (ответ {resp.status_code}, {elapsed:.2f}с)")
                return proxy_dict
            else:
                self._plog.debug(f"[TEST #{proxy_id}] ❌ НЕРАБОТАЕТ {proxy_str} (HTTP {resp.status_code}, {elapsed:.2f}с)")
                return None

        except requests.exceptions.ProxyError as e:
            self._plog.debug(f"[TEST #{proxy_id}] ❌ ПРОКСИ-ОШИБКА {proxy_str}: {type(e).__name__}")
            return None
        except requests.exceptions.Timeout:
            self._plog.debug(f"[TEST #{proxy_id}] ❌ ТАЙМАУТ {proxy_str} (>{self.config.PROXY_TEST_TIMEOUT}с)")
            return None
        except requests.exceptions.ConnectionError as e:
            self._plog.debug(f"[TEST #{proxy_id}] ❌ СОЕДИНЕНИЕ {proxy_str}: {type(e).__name__}")
            return None
        except Exception as e:
            self._plog.debug(f"[TEST #{proxy_id}] ❌ ОШИБКА {proxy_str}: {type(e).__name__}: {str(e)[:50]}")
            return None

    def refresh_pool(self, blocking: bool = False):
        """
        Обновляет пул рабочих SOCKS5 прокси.

        Args:
            blocking: Если True, ждёт завершения обновления (для старта).
        """
        self._stats['refresh_count'] += 1
        refresh_id = self._stats['refresh_count']

        self._plog.info("")
        self._plog.info("=" * 60)
        self._plog.info(f"[REFRESH #{refresh_id}] Начинаю обновление пула прокси")
        self._plog.info(f"[REFRESH #{refresh_id}] Текущий размер пула: {len(self._pool)}")
        self._plog.info("=" * 60)

        def _do_refresh():
            try:
                with self._lock:
                    # 1. Загружаем прокси
                    all_proxies = self._fetch_proxies()

                    if not all_proxies:
                        self._plog.error(f"[REFRESH #{refresh_id}] Не удалось загрузить ни одного прокси!")
                        if not self._pool:
                            self._proxy_ready.clear()
                        self._last_refresh = datetime.now()
                        return

                    # 2. Ограничиваем количество для теста
                    to_test = all_proxies[:self.config.MAX_PROXIES_TO_TEST]
                    self._plog.info(f"[REFRESH #{refresh_id}] Буду тестировать {len(to_test)} прокси (из {len(all_proxies)} загруженных)")

                    # 3. Параллельное тестирование
                    working = []
                    tested_count = 0

                    self._plog.info(f"[REFRESH #{refresh_id}] Запускаю тестирование ({self.config.TEST_WORKERS} воркеров)...")
                    test_start = time.time()

                    with concurrent.futures.ThreadPoolExecutor(max_workers=self.config.TEST_WORKERS) as executor:
                        futures = {
                            executor.submit(self._test_single_proxy, p, i+1): p 
                            for i, p in enumerate(to_test)
                        }

                        for future in concurrent.futures.as_completed(futures):
                            tested_count += 1
                            proxy_url = futures[future]

                            try:
                                result = future.result(timeout=self.config.PROXY_TEST_TIMEOUT + 5)
                                if result:
                                    working.append(result)
                                    self._plog.info(
                                        f"[REFRESH #{refresh_id}] Прогресс: {tested_count}/{len(to_test)} тестов, "
                                        f"{len(working)}/{self.config.PROXY_POOL_SIZE} рабочих найдено"
                                    )

                                    # Досрочная остановка если набрали нужное количество
                                    if len(working) >= self.config.PROXY_POOL_SIZE:
                                        self._plog.info(f"[REFRESH #{refresh_id}] Целевой размер пула достигнут ({self.config.PROXY_POOL_SIZE}), отменяю оставшиеся тесты...")
                                        for f in futures:
                                            f.cancel()
                                        break
                            except concurrent.futures.CancelledError:
                                pass
                            except concurrent.futures.TimeoutError:
                                self._plog.debug(f"[REFRESH #{refresh_id}] Тест прокси превысил таймаут выполнения")
                            except Exception as e:
                                self._plog.debug(f"[REFRESH #{refresh_id}] Ошибка future: {e}")

                    test_elapsed = time.time() - test_start
                    self._stats['total_tested'] += tested_count

                    self._plog.info("=" * 60)
                    self._plog.info(f"[REFRESH #{refresh_id}] ТЕСТИРОВАНИЕ ЗАВЕРШЕНО")
                    self._plog.info(f"[REFRESH #{refresh_id}] Протестировано: {tested_count}")
                    self._plog.info(f"[REFRESH #{refresh_id}] Рабочих найдено: {len(working)}")
                    self._plog.info(f"[REFRESH #{refresh_id}] Успешность: {len(working)/max(tested_count,1)*100:.1f}%")
                    self._plog.info(f"[REFRESH #{refresh_id}] Время тестирования: {test_elapsed:.2f}с")
                    self._plog.info("=" * 60)

                    # 4. Обновляем пул
                    if working:
                        self._pool = working
                        self._current_index = 0
                        self._stats['total_working'] += len(working)
                        self._proxy_ready.set()

                        logger.info(f"✅ Пул SOCKS5 прокси обновлён: {len(self._pool)} рабочих (refresh #{refresh_id})")
                        self._plog.info(f"[REFRESH #{refresh_id}] ПУЛ ОБНОВЛЁН: {len(self._pool)} рабочих прокси")

                        for i, p in enumerate(self._pool[:5], 1):
                            self._plog.info(f"[REFRESH #{refresh_id}]   {i}. {p['hostname']}:{p['port']}")
                        if len(self._pool) > 5:
                            self._plog.info(f"[REFRESH #{refresh_id}]   ... и ещё {len(self._pool)-5}")
                    else:
                        self._plog.error(f"[REFRESH #{refresh_id}] НЕ НАЙДЕНО НИ ОДНОГО РАБОЧЕГО ПРОКСИ!")
                        if not self._pool:
                            self._proxy_ready.clear()
                            logger.error("❌ Пул прокси пуст! Бот не сможет подключиться.")
                        else:
                            self._plog.warning(f"[REFRESH #{refresh_id}] Оставляю старый пул ({len(self._pool)} прокси)")

                    self._last_refresh = datetime.now()

            except Exception as e:
                self._plog.error(f"[REFRESH #{refresh_id}] НЕОЖИДАННАЯ ОШИБКА: {e}", exc_info=True)
                logger.error(f"Ошибка в refresh_pool: {e}")

        if blocking:
            _do_refresh()
        else:
            threading.Thread(target=_do_refresh, daemon=True).start()

    def _refresh_loop(self):
        """Фоновый цикл обновления пула прокси."""
        self._plog.info("[LOOP] Фоновый цикл обновления запущен")

        while self._running:
            time.sleep(self.config.PROXY_REFRESH_INTERVAL)
            if not self._running:
                break
            try:
                self.refresh_pool(blocking=False)
            except Exception as e:
                self._plog.error(f"[LOOP] Ошибка в цикле обновления: {e}", exc_info=True)

    def stop(self):
        """Останавливает менеджер прокси."""
        self._running = False
        self._plog.info("[STOP] Менеджер прокси остановлен")
        logger.info("🛑 Менеджер прокси остановлен")

    def get_proxy(self, wait_seconds: float = 30) -> Optional[Dict[str, Any]]:
        """
        Возвращает SOCKS5 прокси в формате Pyrogram.
        В режиме принудительного пула: если пул пуст — ждём до wait_seconds.
        Прямое соединение НИКОГДА не возвращается.

        Returns:
            Dict с прокси или None если пул пуст и wait_seconds истёк.
        """
        self._plog.debug(f"[GET_PROXY] Запрос прокси (wait={wait_seconds}с)")

        # Ждём появления прокси в пуле
        if not self._pool:
            self._plog.info(f"[GET_PROXY] Пул пуст, ожидаю появления прокси (до {wait_seconds}с)...")
            ready = self._proxy_ready.wait(timeout=wait_seconds)
            if not ready:
                self._plog.error(f"[GET_PROXY] ТАЙМАУТ: прокси так и не появились за {wait_seconds}с!")
                logger.error(f"❌ Таймаут ожидания прокси ({wait_seconds}с). Пул пуст.")
                return None

        with self._lock:
            if not self._pool:
                self._plog.error("[GET_PROXY] Пул всё ещё пуст после ожидания!")
                return None

            proxy = self._pool[self._current_index % len(self._pool)]
            self._current_index += 1
            self._plog.info(
                f"[GET_PROXY] Выдан прокси: {proxy['hostname']}:{proxy['port']} "
                f"(индекс {self._current_index % len(self._pool)}/{len(self._pool)})"
            )
            return proxy

    def report_failure(self, proxy_dict: Dict[str, Any]):
        """Удаляет нерабочий прокси из пула и запускает обновление."""
        if not proxy_dict:
            return

        host = proxy_dict.get('hostname', '?')
        port = proxy_dict.get('port', 0)
        proxy_str = f"{host}:{port}"

        with self._lock:
            if not self._pool:
                return

            for i, p in enumerate(self._pool):
                if p.get('hostname') == host and p.get('port') == port:
                    self._plog.warning(f"[REPORT_FAIL] Удаляю нерабочий прокси: {proxy_str}")
                    del self._pool[i]
                    if i <= self._current_index and self._current_index > 0:
                        self._current_index -= 1

                    logger.warning(f"⚠️ Удалён нерабочий прокси: {proxy_str}. Осталось: {len(self._pool)}")
                    break

            if not self._pool:
                self._plog.error("[REPORT_FAIL] Пул опустел после удаления! Запускаю экстренное обновление...")
                logger.error("🔄 Пул SOCKS5 прокси пуст, запускаю экстренное обновление")
                self._proxy_ready.clear()
                threading.Thread(target=self._safe_refresh_pool, daemon=True).start()

    def _safe_refresh_pool(self):
        """Безопасное обновление пула в отдельном потоке."""
        try:
            self.refresh_pool(blocking=False)
        except Exception as e:
            self._plog.error(f"[SAFE_REFRESH] Ошибка: {e}")

    def get_stats(self) -> Dict[str, Any]:
        """Возвращает статистику менеджера прокси."""
        with self._lock:
            return {
                'mode': 'FORCED_PROXY_ONLY',
                'pool_size': len(self._pool),
                'last_refresh': self._last_refresh.isoformat(),
                'total_fetched': self._stats['total_fetched'],
                'total_tested': self._stats['total_tested'],
                'total_working': self._stats['total_working'],
                'refresh_count': self._stats['refresh_count'],
                'last_successful_source': self._stats['last_successful_source'],
                'failed_sources_count': len(self._stats['failed_sources']),
            }


# Глобальный экземпляр
PROXY_MANAGER: Optional[ProxyManager] = None


def get_proxy_manager() -> ProxyManager:
    """Возвращает (или создаёт) глобальный экземпляр ProxyManager."""
    global PROXY_MANAGER
    if PROXY_MANAGER is None:
        PROXY_MANAGER = ProxyManager()
    return PROXY_MANAGER