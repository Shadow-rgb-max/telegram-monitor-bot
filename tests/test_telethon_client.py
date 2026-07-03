import logging
import unittest

from telethon.tl.types import Channel, Chat, User

from config import BotConfig
from telethon_client import TelethonKeywordBot


class TelethonKeywordBotTests(unittest.TestCase):
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
        self.bot = TelethonKeywordBot(config, logging.getLogger("test"))

    def _make_channel(self, megagroup=False, broadcast=False):
        # Минимальный набор полей, достаточный для isinstance-проверки и getattr(title/username)
        return Channel(
            id=1,
            title="Test Channel",
            photo=None,
            date=None,
            version=0,
            megagroup=megagroup,
            broadcast=broadcast,
            access_hash=0,
        )

    def test_supports_channel(self):
        self.assertTrue(self.bot._is_supported_chat(self._make_channel(broadcast=True)))

    def test_supports_supergroup(self):
        self.assertTrue(self.bot._is_supported_chat(self._make_channel(megagroup=True)))

    def test_rejects_basic_group(self):
        chat = Chat(
            id=1,
            title="Basic group",
            photo=None,
            participants_count=2,
            date=None,
            version=0,
        )
        self.assertFalse(self.bot._is_supported_chat(chat))

    def test_rejects_private_user(self):
        user = User(id=1, is_self=False, access_hash=0)
        self.assertFalse(self.bot._is_supported_chat(user))


if __name__ == "__main__":
    unittest.main()