"""Менеджер SOCKS5 прокси с автоматическим подбором рабочих прокси.
Версия 4.0 — принудительный режим прокси, socket-level SOCKS5 тестирование.
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
import socket
import struct
import ssl

import requests

# ==================== НАСТРОЙКА ЛОГИРОВАНИЯ ====================

_proxy_logger = None

def _get_proxy_logger() -> logging.Logger:
    global _proxy_logger
    if _proxy_logger is not None:
        return _proxy_logger

    _proxy_logger = logging.getLogger("proxy_manager.detailed")
    _proxy_logger.setLevel(logging.DEBUG)
    _proxy_logger.propagate = False

    formatter = logging.Formatter(
        "%(asctime)s.%(msecs)03d | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    file_handler = logging.handlers.RotatingFileHandler(
        "proxy_manager.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler.setLevel(logging.INFO)

    _proxy_logger.addHandler(file_handler)
    _proxy_logger.addHandler(stream_handler)

    return _proxy_logger


logger = logging.getLogger(__name__)


# ==================== КОНФИГУРАЦИЯ ====================

class ProxyConfig:
    USE_PROXY_POOL = True
    TELEGRAM_PROXY = os.getenv('TELEGRAM_PROXY') or os.getenv('TELEGRAM_PROXY_URL')
    PROXY_POOL_URL = os.getenv(
        'PROXY_POOL_URL',
        'https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&proxy_format=protocolipport&format=text&protocol=socks5&timeout=10000'
    )
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
    MAX_PROXIES_TO_TEST = int(os.getenv('MAX_PROXIES_TO_TEST', '500'))
    TEST_WORKERS = int(os.getenv('PROXY_TEST_WORKERS', '50'))


# ==================== SOCKET-LEVEL SOCKS5 TEST ====================

def _test_proxy_socks5_handshake(proxy_host: str, proxy_port: int,
                                   target_host: str = "api.telegram.org",
                                   target_port: int = 443, timeout: int = 8) -> tuple[bool, str, float]:
    """
    Быстрый тест: только SOCKS5 handshake + CONNECT.
    Не делает TLS — это отдельная фаза.
    """
    sock = None
    start = time.time()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((proxy_host, proxy_port))

        # SOCKS5 greeting
        sock.sendall(struct.pack("!BBB", 0x05, 0x01, 0x00))
        resp = sock.recv(2)
        if len(resp) < 2:
            return False, "handshake_truncated", 0
        version, method = struct.unpack("!BB", resp)
        if version != 0x05:
            return False, f"wrong_version_{version}", 0
        if method == 0xFF:
            return False, "no_auth_method", 0

        # CONNECT request
        target_bytes = target_host.encode('utf-8')
        req = struct.pack("!BBBB", 0x05, 0x01, 0x00, 0x03)
        req += struct.pack("!B", len(target_bytes)) + target_bytes
        req += struct.pack("!H", target_port)
        sock.sendall(req)

        resp = sock.recv(4)
        if len(resp) < 4:
            return False, "connect_truncated", 0
        ver, rep, rsv, atyp = struct.unpack("!BBBB", resp)
        if ver != 0x05:
            return False, f"connect_version_{ver}", 0
        if rep != 0x00:
            error_codes = {
                0x01: "general_fail", 0x02: "not_allowed", 0x03: "net_unreachable",
                0x04: "host_unreachable", 0x05: "conn_refused", 0x06: "ttl_expired",
                0x07: "cmd_not_supported", 0x08: "addr_not_supported",
            }
            return False, error_codes.get(rep, f"code_{rep}"), 0

        # Read bind address
        if atyp == 0x01:
            sock.recv(6)
        elif atyp == 0x03:
            l = sock.recv(1)
            if l:
                sock.recv(struct.unpack("!B", l)[0] + 2)
        elif atyp == 0x04:
            sock.recv(18)

        elapsed = (time.time() - start) * 1000
        return True, "handshake_ok", elapsed

    except socket.timeout:
        return False, "timeout", 0
    except socket.error:
        return False, "socket_error", 0
    except struct.error:
        return False, "protocol_error", 0
    except Exception as e:
        return False, f"unexpected_{type(e).__name__}", 0
    finally:
        if sock:
            try:
                sock.close()
            except:
                pass


def _test_proxy_full_tls(proxy_host: str, proxy_port: int,
                         target_host: str = "api.telegram.org",
                         target_port: int = 443, timeout: int = 10) -> tuple[bool, str, float]:
    """
    Полный тест: SOCKS5 handshake + CONNECT + TLS + HTTP request.
    Используется только для прокси, прошедших handshake-тест.
    """
    sock = None
    start = time.time()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((proxy_host, proxy_port))

        # SOCKS5 greeting
        sock.sendall(struct.pack("!BBB", 0x05, 0x01, 0x00))
        resp = sock.recv(2)
        if len(resp) < 2 or resp[0] != 0x05 or resp[1] == 0xFF:
            return False, "handshake_fail", 0

        # CONNECT
        target_bytes = target_host.encode('utf-8')
        req = struct.pack("!BBBB", 0x05, 0x01, 0x00, 0x03)
        req += struct.pack("!B", len(target_bytes)) + target_bytes
        req += struct.pack("!H", target_port)
        sock.sendall(req)

        resp = sock.recv(4)
        if len(resp) < 4 or resp[0] != 0x05 or resp[1] != 0x00:
            return False, "connect_fail", 0

        atyp = resp[3]
        if atyp == 0x01:
            sock.recv(6)
        elif atyp == 0x03:
            l = sock.recv(1)
            if l:
                sock.recv(struct.unpack("!B", l)[0] + 2)
        elif atyp == 0x04:
            sock.recv(18)

        # TLS wrap
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

        tls_sock = context.wrap_socket(sock, server_hostname=target_host)

        # HTTP request to Telegram API
        tls_sock.sendall(
            b"GET /bot HTTP/1.1\r\n"
            b"Host: api.telegram.org\r\n"
            b"Connection: close\r\n\r\n"
        )
        http_resp = tls_sock.recv(1024)
        tls_sock.close()

        elapsed = (time.time() - start) * 1000

        if b"HTTP/1.1" in http_resp:
            return True, "tls_ok", elapsed
        else:
            return False, "http_no_response", elapsed

    except socket.timeout:
        return False, "timeout", 0
    except socket.error:
        return False, "socket_error", 0
    except ssl.SSLError:
        return False, "tls_error", 0
    except Exception as e:
        return False, f"unexpected_{type(e).__name__}", 0
    finally:
        if sock:
            try:
                sock.close()
            except:
                pass


# ==================== МЕНЕДЖЕР ПРОКСИ ====================

class ProxyManager:
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
        self._plog.info("ProxyManager v4.0 инициализирован")
        self._plog.info(f"Режим: ПРИНУДИТЕЛЬНЫЙ (только прокси)")
        self._plog.info(f"Тестирование: socket-level SOCKS5 (двухфазное)")
        self._plog.info(f"Целевой размер пула: {self.config.PROXY_POOL_SIZE}")
        self._plog.info(f"Макс. прокси для теста: {self.config.MAX_PROXIES_TO_TEST}")
        self._plog.info(f"Воркеры: {self.config.TEST_WORKERS}")
        self._plog.info(f"Таймаут handshake: 8с, TLS: 10с")
        self._plog.info("=" * 60)

        self._refresh_thread = threading.Thread(target=self._refresh_loop, daemon=True)
        self._refresh_thread.start()

        self._plog.info("[INIT] Запуск первоначального подбора прокси...")
        self.refresh_pool(blocking=True)

        if not self._pool:
            self._plog.error("[INIT] КРИТИЧЕСКАЯ ОШИБКА: Не удалось найти ни одного рабочего прокси!")
            logger.error("❌ Ни одного рабочего SOCKS5 прокси не найдено.")

    def _parse_proxy_url(self, url: str) -> Optional[Dict[str, Any]]:
        url = url.strip()
        if not url:
            return None
        if '://' not in url:
            url = f"socks5://{url}"
        try:
            parsed = urlparse(url)
            scheme = parsed.scheme.lower()
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
            self._plog.warning(f"[PARSE] Ошибка '{url[:50]}...': {e}")
            return None

    def _fetch_from_source(self, url: str, source_name: str) -> List[str]:
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
                if line.startswith(('socks5://', 'socks4://', 'http://', 'https://')):
                    proxies.append(line)
                elif ':' in line:
                    proxies.append(f"socks5://{line}")

            socks5_only = [p for p in proxies if 'socks5' in p.lower()]
            self._plog.info(f"[FETCH] {source_name} — распарсено {len(proxies)} прокси, SOCKS5: {len(socks5_only)}")
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
        self._plog.info("=" * 60)
        self._plog.info("[FETCH_ALL] Начинаю загрузку прокси")
        self._plog.info("=" * 60)

        all_proxies: List[str] = []
        failed = []

        primary = self._fetch_from_source(self.config.PROXY_POOL_URL, "ProxyScrape API")
        if primary:
            all_proxies.extend(primary)
            self._stats['last_successful_source'] = "ProxyScrape API"
            self._plog.info(f"[FETCH_ALL] Основной источник: {len(primary)} прокси")
        else:
            failed.append("ProxyScrape API")
            self._plog.warning("[FETCH_ALL] Основной источник недоступен, пробую резервные...")

        for idx, source_url in enumerate(self.config.BACKUP_SOURCES, 1):
            if len(all_proxies) >= self.config.MAX_PROXIES_TO_TEST:
                self._plog.info(f"[FETCH_ALL] Достаточно прокси ({len(all_proxies)})")
                break

            source_name = f"Backup-{idx}"
            proxies = self._fetch_from_source(source_url, source_name)
            if proxies:
                all_proxies.extend(proxies)
                if not self._stats['last_successful_source']:
                    self._stats['last_successful_source'] = source_name
            else:
                failed.append(source_name)

        seen = set()
        unique = []
        for p in all_proxies:
            if p not in seen:
                seen.add(p)
                unique.append(p)

        self._stats['total_fetched'] += len(unique)
        self._stats['failed_sources'] = failed

        self._plog.info("=" * 60)
        self._plog.info(f"[FETCH_ALL] ИТОГО: уникальных SOCKS5: {len(unique)}")
        self._plog.info(f"[FETCH_ALL] Неудачных источников: {len(failed)}")
        self._plog.info("=" * 60)

        return unique

    def _test_single_proxy(self, proxy_url: str, proxy_id: int) -> Optional[Dict[str, Any]]:
        """Двухфазное тестирование: handshake → TLS"""
        proxy_dict = self._parse_proxy_url(proxy_url)
        if not proxy_dict:
            return None

        host = proxy_dict.get('hostname', '?')
        port = proxy_dict.get('port', 0)
        proxy_str = f"{host}:{port}"

        # Phase 1: Handshake (быстро)
        ok1, err1, ms1 = _test_proxy_socks5_handshake(
            host, port,
            target_host="api.telegram.org",
            target_port=443,
            timeout=8
        )

        if not ok1:
            self._plog.debug(f"[TEST #{proxy_id}] ❌ HANDSHAKE {proxy_str} — {err1}")
            return None

        # Phase 2: TLS (только для прошедших handshake)
        ok2, err2, ms2 = _test_proxy_full_tls(
            host, port,
            target_host="api.telegram.org",
            target_port=443,
            timeout=10
        )

        if ok2:
            self._plog.info(f"[TEST #{proxy_id}] ✅ РАБОТАЕТ {proxy_str} (handshake {ms1:.0f}ms, TLS {ms2:.0f}ms)")
            return proxy_dict
        else:
            self._plog.debug(f"[TEST #{proxy_id}] ❌ TLS_FAIL {proxy_str} — {err2}")
            return None

    def refresh_pool(self, blocking: bool = False):
        self._stats['refresh_count'] += 1
        refresh_id = self._stats['refresh_count']

        self._plog.info("")
        self._plog.info("=" * 60)
        self._plog.info(f"[REFRESH #{refresh_id}] Начинаю обновление пула")
        self._plog.info(f"[REFRESH #{refresh_id}] Текущий размер пула: {len(self._pool)}")
        self._plog.info("=" * 60)

        def _do_refresh():
            try:
                with self._lock:
                    all_proxies = self._fetch_proxies()

                    if not all_proxies:
                        self._plog.error(f"[REFRESH #{refresh_id}] Не удалось загрузить прокси!")
                        if not self._pool:
                            self._proxy_ready.clear()
                        self._last_refresh = datetime.now()
                        return

                    to_test = all_proxies[:self.config.MAX_PROXIES_TO_TEST]
                    self._plog.info(f"[REFRESH #{refresh_id}] Буду тестировать {len(to_test)} прокси")

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
                                result = future.result(timeout=20)
                                if result:
                                    working.append(result)
                                    self._plog.info(
                                        f"[REFRESH #{refresh_id}] Прогресс: {tested_count}/{len(to_test)} тестов, "
                                        f"{len(working)}/{self.config.PROXY_POOL_SIZE} рабочих"
                                    )

                                    if len(working) >= self.config.PROXY_POOL_SIZE:
                                        self._plog.info(f"[REFRESH #{refresh_id}] Целевой размер достигнут, отменяю...")
                                        for f in futures:
                                            f.cancel()
                                        break
                            except concurrent.futures.CancelledError:
                                pass
                            except concurrent.futures.TimeoutError:
                                self._plog.debug(f"[REFRESH #{refresh_id}] Тест прокси превысил таймаут")
                            except Exception as e:
                                self._plog.debug(f"[REFRESH #{refresh_id}] Ошибка future: {e}")

                    test_elapsed = time.time() - test_start
                    self._stats['total_tested'] += tested_count

                    self._plog.info("=" * 60)
                    self._plog.info(f"[REFRESH #{refresh_id}] ТЕСТИРОВАНИЕ ЗАВЕРШЕНО")
                    self._plog.info(f"[REFRESH #{refresh_id}] Протестировано: {tested_count}")
                    self._plog.info(f"[REFRESH #{refresh_id}] Рабочих найдено: {len(working)}")
                    self._plog.info(f"[REFRESH #{refresh_id}] Успешность: {len(working)/max(tested_count,1)*100:.1f}%")
                    self._plog.info(f"[REFRESH #{refresh_id}] Время: {test_elapsed:.2f}с")
                    self._plog.info("=" * 60)

                    if working:
                        self._pool = working
                        self._current_index = 0
                        self._stats['total_working'] += len(working)
                        self._proxy_ready.set()

                        logger.info(f"✅ Пул обновлён: {len(self._pool)} рабочих (refresh #{refresh_id})")
                        self._plog.info(f"[REFRESH #{refresh_id}] ПУЛ ОБНОВЛЁН: {len(self._pool)} рабочих")

                        for i, p in enumerate(self._pool[:5], 1):
                            self._plog.info(f"[REFRESH #{refresh_id}]   {i}. {p['hostname']}:{p['port']}")
                        if len(self._pool) > 5:
                            self._plog.info(f"[REFRESH #{refresh_id}]   ... и ещё {len(self._pool)-5}")
                    else:
                        self._plog.error(f"[REFRESH #{refresh_id}] НЕ НАЙДЕНО РАБОЧИХ ПРОКСИ!")
                        if not self._pool:
                            self._proxy_ready.clear()
                            logger.error("❌ Пул прокси пуст!")
                        else:
                            self._plog.warning(f"[REFRESH #{refresh_id}] Оставляю старый пул ({len(self._pool)})")

                    self._last_refresh = datetime.now()

            except Exception as e:
                self._plog.error(f"[REFRESH #{refresh_id}] НЕОЖИДАННАЯ ОШИБКА: {e}", exc_info=True)
                logger.error(f"Ошибка в refresh_pool: {e}")

        if blocking:
            _do_refresh()
        else:
            threading.Thread(target=_do_refresh, daemon=True).start()

    def _refresh_loop(self):
        self._plog.info("[LOOP] Фоновый цикл обновления запущен")
        while self._running:
            time.sleep(self.config.PROXY_REFRESH_INTERVAL)
            if not self._running:
                break
            try:
                self.refresh_pool(blocking=False)
            except Exception as e:
                self._plog.error(f"[LOOP] Ошибка: {e}", exc_info=True)

    def stop(self):
        self._running = False
        self._plog.info("[STOP] Менеджер прокси остановлен")
        logger.info("🛑 Менеджер прокси остановлен")

    def get_proxy(self, wait_seconds: float = 30) -> Optional[Dict[str, Any]]:
        self._plog.debug(f"[GET_PROXY] Запрос (wait={wait_seconds}с)")

        if not self._pool:
            self._plog.info(f"[GET_PROXY] Пул пуст, ожидаю (до {wait_seconds}с)...")
            ready = self._proxy_ready.wait(timeout=wait_seconds)
            if not ready:
                self._plog.error(f"[GET_PROXY] ТАЙМАУТ за {wait_seconds}с!")
                logger.error(f"❌ Таймаут ожидания прокси ({wait_seconds}с)")
                return None

        with self._lock:
            if not self._pool:
                self._plog.error("[GET_PROXY] Пул всё ещё пуст!")
                return None

            proxy = self._pool[self._current_index % len(self._pool)]
            self._current_index += 1
            self._plog.info(
                f"[GET_PROXY] Выдан: {proxy['hostname']}:{proxy['port']} "
                f"({self._current_index % len(self._pool)}/{len(self._pool)})"
            )
            return proxy

    def report_failure(self, proxy_dict: Dict[str, Any]):
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
                    self._plog.warning(f"[REPORT_FAIL] Удаляю: {proxy_str}")
                    del self._pool[i]
                    if i <= self._current_index and self._current_index > 0:
                        self._current_index -= 1

                    logger.warning(f"⚠️ Удалён: {proxy_str}. Осталось: {len(self._pool)}")
                    break

            if not self._pool:
                self._plog.error("[REPORT_FAIL] Пул опустел! Экстренное обновление...")
                logger.error("🔄 Пул пуст, запускаю экстренное обновление")
                self._proxy_ready.clear()
                threading.Thread(target=self._safe_refresh_pool, daemon=True).start()

    def _safe_refresh_pool(self):
        try:
            self.refresh_pool(blocking=False)
        except Exception as e:
            self._plog.error(f"[SAFE_REFRESH] Ошибка: {e}")

    def get_stats(self) -> Dict[str, Any]:
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
    global PROXY_MANAGER
    if PROXY_MANAGER is None:
        PROXY_MANAGER = ProxyManager()
    return PROXY_MANAGER