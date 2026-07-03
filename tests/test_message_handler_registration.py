import logging
import unittest

from config import BotConfig
from telethon_client import TelethonKeywordBot


class DummyClient:
    def __init__(self):
        self.added = []
        self.removed = []

    def add_event_handler(self, callback, event=None):
        self.added.append((callback, event))

    def remove_event_handler(self, callback, event=None):
        self.removed.append((callback, event))


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
        bot = TelethonKeywordBot(config, logging.getLogger("test"))
        client = DummyClient()
        bot._client = client

        bot._register_message_handler([1, 2])
        first_handler = bot._message_handler
        self.assertIsNotNone(first_handler)

        bot._register_message_handler([3, 4])

        self.assertEqual(len(client.removed), 1)
        self.assertEqual(client.removed[0][0], first_handler)
        self.assertEqual(len(client.added), 2)
        self.assertIsNotNone(bot._message_handler)
        self.assertIsNot(bot._message_handler, first_handler)

    def test_no_handler_registered_when_no_channels(self):
        config = BotConfig(
            api_id="1",
            api_hash="hash",
            admin_id=123,
            channel_id="-100123",
            channels=["first"],
            keywords=["keyword"],
            dedup_window_hours=24,
        )
        bot = TelethonKeywordBot(config, logging.getLogger("test"))
        client = DummyClient()
        bot._client = client

        bot._register_message_handler([])

        self.assertEqual(len(client.added), 0)
        self.assertIsNone(bot._message_handler)


if __name__ == "__main__":
    unittest.main()