import logging
import unittest

from pyrogram.enums import ChatType

from config import BotConfig
from pyrogram_client import PyrogramKeywordBot


class PyrogramKeywordBotTests(unittest.TestCase):
    def setUp(self):
        config = BotConfig(
            api_id="1",
            api_hash="hash",
            admin_id=123,
            channel_id="-100123",
            channels=["test_channel"],
            keywords=["keyword"],
            dedup_window_hours=24,
        )
        self.bot = PyrogramKeywordBot(config, logging.getLogger("test"))

    def test_supports_pyrogram_chat_types(self):
        self.assertTrue(self.bot._is_supported_chat_type(ChatType.CHANNEL))
        self.assertTrue(self.bot._is_supported_chat_type(ChatType.SUPERGROUP))
        self.assertFalse(self.bot._is_supported_chat_type(ChatType.PRIVATE))

    def test_supports_string_chat_types(self):
        self.assertTrue(self.bot._is_supported_chat_type("channel"))
        self.assertTrue(self.bot._is_supported_chat_type("supergroup"))
        self.assertFalse(self.bot._is_supported_chat_type("private"))


if __name__ == "__main__":
    unittest.main()
