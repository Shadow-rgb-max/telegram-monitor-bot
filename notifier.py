from telethon import TelegramClient
from typing import List, Union
import logging


async def send_notification(
    client: TelegramClient,
    recipient_id: Union[int, str],
    channel_title: str,
    matched_keywords: List[str],
    message_text: str,
    message_link: str,
) -> None:
    notification = (
        f"Найдено сообщение с ключевыми словами в канале {channel_title}:\n"
        f"Ключевые слова: {', '.join(matched_keywords)}\n"
        f"Сообщение: {message_text}\n"
        f"Ссылка: {message_link}"
    )
    await client.send_message(recipient_id, notification)
    logging.getLogger("telegram_keyword_monitor").info(
        f"Отправлено уведомление: {notification}"
    )


async def send_error_notification(
    client: TelegramClient, recipient_id: Union[int, str], error_text: str
) -> None:
    try:
        await client.send_message(recipient_id, f"[ОШИБКА] {error_text}")
    except Exception as e:
        logging.getLogger("telegram_keyword_monitor").error(
            f"Ошибка при отправке уведомления об ошибке: {e}"
        )
