"""Log-chat transport failures are visible and never break the bot flow."""
from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("SUDO_IDS", "1234567")

from bot.services import product_journal


class TestLogChatDelivery(unittest.IsolatedAsyncioTestCase):
    async def test_sends_to_configured_log_chat(self) -> None:
        bot = SimpleNamespace(send_message=AsyncMock())
        with patch.object(product_journal, "settings", SimpleNamespace(log_chat_id=-1001234567890)):
            sent = await product_journal.send_log_message(bot, "compression trace")

        self.assertTrue(sent)
        bot.send_message.assert_awaited_once_with(
            chat_id=-1001234567890,
            text="compression trace",
        )

    async def test_failed_group_send_returns_false_instead_of_raising(self) -> None:
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("bot is not a member")))
        with patch.object(product_journal, "settings", SimpleNamespace(log_chat_id=-1001234567890)):
            sent = await product_journal.send_log_message(bot, "compression trace")

        self.assertFalse(sent)
        bot.send_message.assert_awaited_once()

    async def test_empty_log_chat_is_reported_as_not_sent(self) -> None:
        bot = SimpleNamespace(send_message=AsyncMock())
        with patch.object(product_journal, "settings", SimpleNamespace(log_chat_id=None)):
            sent = await product_journal.send_log_message(bot, "compression trace")

        self.assertFalse(sent)
        bot.send_message.assert_not_awaited()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
