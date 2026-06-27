import asyncio
import unittest

from pyrogram.enums import ParseMode

from notifier import send_notification


class DummyClient:
    def __init__(self):
        self.calls = []

    async def send_message(self, chat_id, text, parse_mode=None, disable_web_page_preview=None):
        self.calls.append({
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_web_page_preview,
        })


class NotifierTests(unittest.TestCase):
    def test_send_notification_uses_pyrogram_html_parse_mode(self):
        client = DummyClient()

        asyncio.run(send_notification(
            client,
            123,
            "Test Channel",
            ["keyword"],
            "hello world",
            "https://t.me/test/1",
        ))

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["parse_mode"], ParseMode.HTML)
        self.assertFalse(client.calls[0]["disable_web_page_preview"])


if __name__ == "__main__":
    unittest.main()
