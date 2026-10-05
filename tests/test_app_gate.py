"""دروازهٔ مشترکِ ``app._dispatch_update`` — سه چیز که هیچ هندلری نباید دور بزند.

گزارش: این تابع همان‌جایی است که «فقط چت خصوصی»، «فقط کاربر مجاز» و «ثبتِ کاربر برای
لاگ» اعمال می‌شود؛ ولی هیچ تستی نداشت، یعنی تنها لایهٔ امنیتیِ مشترک بدون قفلِ رگرسیون
بود. این تست‌ها فقط سه رفتارِ قابل مشاهده را می‌سنجند: آپدیتِ گروهی به هندلر نمی‌رسد،
کاربرِ ناشناس پیش از هر هندلری رد می‌شود (جز `/start` که برای فعال‌سازیِ دعوت لازم است)،
و کاربرِ مجاز رد نمی‌شود. هیچ‌کدام به پیاده‌سازیِ داخلی دست نمی‌زنند.
"""
from __future__ import annotations

import asyncio
import os
import unittest
from unittest import mock

from telegram import Update

os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("SUDO_IDS", "1234567")

from bot.app import build_application

STRANGER = 909090
SUDO = 1234567


def _update(*, user_id: int, chat_type: str = "private", text: str = "سلام") -> Update:
    return Update.de_json(
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 0,
                "chat": {"id": user_id, "type": chat_type},
                "from": {"id": user_id, "is_bot": False, "first_name": "تست"},
                "text": text,
            },
        },
        None,
    )


class TestTheSharedGate(unittest.TestCase):
    def setUp(self) -> None:
        self.app = build_application()
        self.handlers_ran: list[object] = []

        async def record(update, *_args, **_kwargs):
            self.handlers_ran.append(update)

        patcher = mock.patch("telegram.ext.Application.process_update", record)
        patcher.start()
        self.addCleanup(patcher.stop)

        deny = mock.AsyncMock()
        deny_patcher = mock.patch("bot.modules.start._deny", deny)
        deny_patcher.start()
        self.addCleanup(deny_patcher.stop)
        self.deny = deny

        close = mock.Mock(return_value=[])
        close_patcher = mock.patch("bot.services.flow_guard.close_others", close)
        close_patcher.start()
        self.addCleanup(close_patcher.stop)
        self.close = close

    def _dispatch(self, update: Update) -> None:
        asyncio.run(self.app._dispatch_update(update))

    def test_a_group_update_never_reaches_the_handlers(self):
        self._dispatch(_update(user_id=STRANGER, chat_type="supergroup"))
        self.assertEqual([], self.handlers_ran, "ربات در گروه هیچ کاری نمی‌کند")
        self.deny.assert_not_awaited()

    def test_a_stranger_is_denied_before_any_handler_runs(self):
        self._dispatch(_update(user_id=STRANGER))
        self.assertEqual([], self.handlers_ran, "کاربر ناشناس نباید به هندلر برسد")
        self.deny.assert_awaited_once()
        self.close.assert_called_once_with("", STRANGER)

    def test_a_stranger_can_still_send_start(self):
        # /start باید باز بماند، وگرنه کدِ دعوتِ ادمین هرگز فعال نمی‌شود.
        self._dispatch(_update(user_id=STRANGER, text="/start kd-1234"))
        self.assertEqual(1, len(self.handlers_ran), "‏/start باید به هندلر برسد")
        self.deny.assert_not_awaited()

    def test_an_allowed_user_is_not_denied(self):
        self._dispatch(_update(user_id=SUDO))
        self.assertEqual(1, len(self.handlers_ran))
        self.deny.assert_not_awaited()
