"""تست‌های مدیریت هم‌زمانی گفتگوها (FlowConversationHandler و close_for_user).

چیزی که واقعاً مهم است:
* FlowConversationHandler الزام می‌کند per_user=True / per_message=False (تا
  هر کاربر قفل خودش را داشته باشد) — اگر کسی یک روز هندلرِ جدید با پیش‌فرض غلط
  اضافه کرد باید تست بالا بیاورد.
* end_for_user گفتگوی همان کاربر را می‌بندد و timeout job را حذف می‌کند (تا
  یک timeout قدیمی یک گفتگوی تازه را از زیر در نبرد).
* close_for_user گفتگوهای *غیر* از except_flow را می‌بندد (جایگزین flow_guard).
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("SUDO_IDS", "1234567")

from telegram.ext import CommandHandler

from bot.services import conversations as conv_mod


async def _h(*a, **k):
    return None


def _cmd(name):
    return CommandHandler(name, _h)


class TestFlowConversationHandler(unittest.TestCase):
    def test_rejects_bad_configuration(self):
        with self.assertRaises(ValueError):
            conv_mod.FlowConversationHandler(
                entry_points=[_cmd("a")],
                states={},
                fallbacks=[],
                flow="bad",
                per_message=True,  # غلط
            )
        with self.assertRaises(ValueError):
            conv_mod.FlowConversationHandler(
                entry_points=[_cmd("a")],
                states={},
                fallbacks=[],
                flow="bad",
                per_user=False,  # غلط
            )

    def test_registers_and_closes_one_user(self):
        conv_mod._registry.clear()
        h = conv_mod.FlowConversationHandler(
            entry_points=[_cmd("a")],
            states={0: [_cmd("b")]},
            fallbacks=[],
            flow="ping",
        )
        # PTB با per_chat=True و per_user=False کلید را (chat, user) می‌سازد
        key = (100, 7)
        h._conversations[key] = 0
        closed = h.end_for_user(7)
        self.assertTrue(closed)
        self.assertNotIn(key, h._conversations)
        # کاربر دیگری دست‌نخورده
        key2 = (100, 42)
        h._conversations[key2] = 0
        self.assertFalse(h.end_for_user(7))  # قبلاً بسته شده
        self.assertIn(key2, h._conversations)
        conv_mod._registry.clear()

    def test_end_for_user_cancels_timeout_job(self):
        conv_mod._registry.clear()
        h = conv_mod.FlowConversationHandler(
            entry_points=[_cmd("a")],
            states={},
            fallbacks=[],
            flow="x",
        )
        key = (100, 7)
        h._conversations[key] = 0
        job = mock.Mock()
        h.timeout_jobs[key] = job
        self.assertTrue(h.end_for_user(7))
        job.schedule_removal.assert_called_once()
        conv_mod._registry.clear()


class TestCloseForUser(unittest.TestCase):
    def test_closes_all_except_given_flow(self):
        conv_mod._registry.clear()
        # دو هندلر flow را جدا می‌سازیم
        a = conv_mod.FlowConversationHandler(
            entry_points=[_cmd("a")],
            states={}, fallbacks=[], flow="flow_a",
        )
        b = conv_mod.FlowConversationHandler(
            entry_points=[_cmd("b")],
            states={}, fallbacks=[], flow="flow_b",
        )
        a._conversations[(100, 7)] = 0
        b._conversations[(100, 7)] = 0
        closed = conv_mod.close_for_user(7, except_flow="flow_a")
        self.assertEqual(closed, {"flow_b"})
        self.assertIn((100, 7), a._conversations)  # flow_a دست‌نخورده
        self.assertNotIn((100, 7), b._conversations)  # flow_b بسته شده
        conv_mod._registry.clear()


if __name__ == "__main__":
    unittest.main()
