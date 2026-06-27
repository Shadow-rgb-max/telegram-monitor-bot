import logging
import unittest

from config import BotConfig
from pyrogram_client import PyrogramKeywordBot


class DummyClient:
    def __init__(self):
        self.added = []
        self.removed = []

    def add_handler(self, handler, group=0):
        self.added.append((handler, group))

    def remove_handler(self, handler, group=0):
        self.removed.append((handler, group))

    def on_message(self, *args, **kwargs):
        def decorator(func):
            self.added.append((func, 0))
            return func

        return decorator


class MessageHandlerRegistrationTests(unittest.TestCase):
    def test_re_registers_handler_when_channels_change(self):
        config = BotConfig(
            api_id="1",
            api_hash="hash",
            admin_id=123,
            channel_id="-100123",
            channels=["first"],
            keywords=["keyword"],
            dedup_window_hours=24,
        )
        bot = PyrogramKeywordBot(config, logging.getLogger("test"))
        client = DummyClient()
        bot._client = client

        bot._register_message_handler([1, 2])
        first_handler = bot._message_handler
        bot._register_message_handler([3, 4])

        self.assertEqual(len(client.removed), 1)
        self.assertEqual(client.removed[0][0], first_handler)
        self.assertEqual(len(client.added), 2)
        self.assertIsNotNone(bot._message_handler)


if __name__ == "__main__":
    unittest.main()
