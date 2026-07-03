import asyncio
import unittest

from notifier import send_notification


class DummyClient:
    def __init__(self):
        self.calls = []

    async def send_message(self, entity, message, parse_mode=None, link_preview=None):
        self.calls.append({
            "entity": entity,
            "message": message,
            "parse_mode": parse_mode,
            "link_preview": link_preview,
        })


class NotifierTests(unittest.TestCase):
    def test_send_notification_uses_html_parse_mode(self):
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
        self.assertEqual(client.calls[0]["parse_mode"], "html")
        self.assertTrue(client.calls[0]["link_preview"])


if __name__ == "__main__":
    unittest.main()