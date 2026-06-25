import re
import time
import hashlib
from typing import List, Dict, Tuple, Set


class KeywordMonitor:
    def __init__(self, keywords: List[str], throttle_seconds: int = 60, dedup_window_hours: int = 24):
        self.keywords = keywords
        self.throttle_seconds = throttle_seconds
        self.dedup_window_hours = dedup_window_hours
        # (channel_id, keyword) -> last_notification_time
        self.last_notifications: Dict[Tuple[int, str], float] = {}
        # message_hash -> timestamp для дедупликации
        self.sent_messages: Dict[str, float] = {}

    def _normalize_text(self, text: str) -> str:
        """Нормализует текст для более точного сравнения."""
        if not text:
            return ""
        # Приводим к нижнему регистру
        text = text.lower()
        # Убираем лишние пробелы
        text = re.sub(r'\s+', ' ', text)
        # Убираем переносы строк
        text = text.replace('\n', ' ').replace('\r', ' ')
        # Убираем пунктуацию (опционально, можно закомментировать)
        # text = re.sub(r'[^\w\s]', '', text)
        return text.strip()

    def _get_message_hash(self, text: str) -> str:
        """Создает хеш нормализованного текста сообщения."""
        normalized_text = self._normalize_text(text)
        return hashlib.md5(normalized_text.encode('utf-8')).hexdigest()

    def match_keywords(self, text: str) -> List[str]:
        """Возвращает список совпавших ключевых слов в тексте."""
        text = text.lower() if text else ""
        matched = []
        # Флаг для отслеживания, найдено ли уже "100%", чтобы не добавлять оба варианта
        found_100_percent = False
        
        for kw in self.keywords:
            kw_lower = kw.lower().strip()
            
            # Специальная обработка для вариантов "100%" и "100 %"
            if kw_lower in ["100%", "100 %"]:
                if not found_100_percent:
                    # Ищем "100" с опциональным пробелом и символом процента
                    # Убираем требование границы слова, так как может быть эмодзи или другие символы
                    if re.search(r"100\s*%", text):
                        matched.append(kw)
                        found_100_percent = True
            # Для обычных ключевых слов используем более гибкий поиск
            else:
                # Экранируем специальные символы регулярных выражений
                kw_escaped = re.escape(kw_lower)
                # Ищем ключевое слово, допуская не-буквенные символы вокруг (эмодзи, пунктуация)
                # Но не внутри самого слова
                pattern = r"(?:^|[^\w])" + kw_escaped + r"(?:[^\w]|$)"
                if re.search(pattern, text):
                    matched.append(kw)
        
        return matched

    def should_notify(self, channel_id: int, keyword: str, message_text: str = "") -> bool:
        """
        Проверяет, следует ли отправлять уведомление.
        Учитывает throttling и дедупликацию сообщений.
        """
        now = time.time()
        
        # Проверка throttling
        key = (channel_id, keyword)
        last_time = self.last_notifications.get(key, 0)
        if now - last_time < self.throttle_seconds:
            return False
        
        # Проверка дедупликации сообщений
        if message_text:
            message_hash = self._get_message_hash(message_text)
            last_sent_time = self.sent_messages.get(message_hash, 0)
            if now - last_sent_time < (self.dedup_window_hours * 3600):
                return False
        
        # Обновляем временные метки
        self.last_notifications[key] = now
        if message_text:
            self.sent_messages[message_hash] = now
        
        return True

    def cleanup_old_entries(self, max_age_hours: int = 24):
        """Очищает старые записи для экономии памяти."""
        now = time.time()
        max_age_seconds = max_age_hours * 3600
        
        # Очистка старых уведомлений
        keys_to_remove = []
        for key, timestamp in self.last_notifications.items():
            if now - timestamp > max_age_seconds:
                keys_to_remove.append(key)
        
        for key in keys_to_remove:
            del self.last_notifications[key]
        
        # Очистка старых сообщений
        hashes_to_remove = []
        for msg_hash, timestamp in self.sent_messages.items():
            if now - timestamp > max_age_seconds:
                hashes_to_remove.append(msg_hash)
        
        for msg_hash in hashes_to_remove:
            del self.sent_messages[msg_hash]

    def get_stats(self) -> Dict[str, int]:
        """Возвращает статистику мониторинга."""
        return {
            'active_throttles': len(self.last_notifications),
            'sent_messages': len(self.sent_messages),
            'keywords_count': len(self.keywords)
        }
