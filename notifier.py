from pyrogram import Client
from pyrogram.enums import ParseMode
from typing import List, Union
import logging


async def send_notification(
    client: Client,
    recipient_id: Union[int, str],
    channel_title: str,
    matched_keywords: List[str],
    message_text: str,
    message_link: str,
) -> None:
    """Отправляет уведомление о найденном ключевом слове."""
    import html
    safe_text = html.escape(message_text[:4000]) if message_text else "Без текста"
    safe_title = html.escape(channel_title)

    notification = (
        f"🔔 <b>Найдено ключевое слово</b>\n"
        f"📢 Канал: <b>{safe_title}</b>\n"
        f"🔍 Ключевые слова: <code>{', '.join(matched_keywords)}</code>\n"
        f"📝 Сообщение: <pre>{safe_text}</pre>\n"
        f"🔗 <a href='{message_link}'>Открыть сообщение</a>"
    )

    await client.send_message(
        recipient_id,
        notification,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=False
    )

    logging.getLogger("telegram_keyword_monitor").info(
        f"✅ Уведомление отправлено: keywords={matched_keywords}, channel={channel_title}"
    )


async def send_error_notification(
    client: Client, recipient_id: Union[int, str], error_text: str
) -> None:
    """Отправляет уведомление об ошибке."""
    try:
        import html
        safe_error = html.escape(str(error_text)[:3000])
        await client.send_message(
            recipient_id,
            f"⚠️ <b>[ОШИБКА]</b>\n<pre>{safe_error}</pre>",
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logging.getLogger("telegram_keyword_monitor").error(
            f"Ошибка при отправке уведомления об ошибке: {e}"
        )