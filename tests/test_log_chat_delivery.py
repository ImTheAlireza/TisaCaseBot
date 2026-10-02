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

    async def test_publish_trace_is_truthful_and_sent_only_to_the_log_group(self) -> None:
        bot = SimpleNamespace(send_message=AsyncMock())
        with patch.object(product_journal, "settings", SimpleNamespace(log_chat_id=-1001234567890)):
            sent = await product_journal.send_publish_trace(
                bot,
                ["[http:start] #1 POST /wp-json/wc/v3/products"],
                dry_run=False,
            )

        self.assertTrue(sent)
        bot.send_message.assert_awaited_once()
        kwargs = bot.send_message.await_args.kwargs
        self.assertEqual(-1001234567890, kwargs["chat_id"])
        self.assertIn("درخواست‌ها به سایت ارسال شدند", kwargs["text"])
        self.assertNotIn("هیچ‌کدام به سایت نرفتند", kwargs["text"])
        self.assertIn("[http:start]", kwargs["text"])

    async def test_dry_run_trace_says_no_shop_request_was_sent(self) -> None:
        text = product_journal.publish_trace_report(
            ["[dry-run] POST /wp-json/wc/v3/products"], dry_run=True
        )
        self.assertIn("هیچ‌کدام به سایت نرفتند", text)

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

    async def test_product_card_send_failure_is_visible_at_info_deployments(self) -> None:
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("bot is not a member")))
        context = SimpleNamespace(bot=bot, chat_data={})
        settings = SimpleNamespace(log_chat_id=-5061365940, verbose_log=False)
        with patch.object(product_journal, "settings", settings), self.assertLogs(
            "bot.services.product_journal", level="WARNING"
        ) as logs:
            product_journal.journal_for(context).line("publish started")
            await product_journal.flush(context, status="failed")

        self.assertIn("LOG_CHAT_ID=-5061365940", "\\n".join(logs.output))
        self.assertIn("RuntimeError", "\\n".join(logs.output))
        self.assertIn("bot is not a member", "\\n".join(logs.output))

    async def test_unhandled_callback_error_is_sent_to_the_log_chat(self) -> None:
        from bot.modules import fallback

        bot = SimpleNamespace()
        reply_text = AsyncMock()
        update = SimpleNamespace(
            update_id=812,
            effective_user=SimpleNamespace(id=77),
            callback_query=SimpleNamespace(data="product:confirm"),
            effective_message=SimpleNamespace(reply_text=reply_text),
        )
        context = SimpleNamespace(error=RuntimeError("callback query timed out"), bot=bot)
        sender = AsyncMock(return_value=True)
        with patch.object(fallback.product_journal, "send_log_message", new=sender):
            await fallback.on_error(update, context)

        sender.assert_awaited_once()
        text = sender.await_args.args[1]
        self.assertIn("update_id=812", text)
        self.assertIn("callback=product:confirm", text)
        self.assertIn("RuntimeError: callback query timed out", text)
        reply_text.assert_awaited_once()
        self.assertIn("ثبت شد", reply_text.await_args.args[0])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
