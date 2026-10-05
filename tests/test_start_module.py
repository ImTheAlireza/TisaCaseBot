"""تست‌های ماژول /start: رد دسترسی، منوی اصلی، کد دعوت."""
from __future__ import annotations

import os
import unittest
from unittest import mock

os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("SUDO_IDS", "1234567")

from bot.modules import start


class _FakeUser:
    def __init__(self, uid, username="tester"):
        self.id = uid
        self.username = username

    @property
    def first_name(self):
        return "Test"

    @property
    def last_name(self):
        return ""


class _FakeMessage:
    def __init__(self, text="/start", user=None):
        self.text = text
        self.effective_user = user
        self.reply_html_calls = []
        self.reply_text_calls = []

    async def reply_html(self, text, **kwargs):
        self.reply_html_calls.append((text, kwargs))

    async def reply_text(self, text, **kwargs):
        self.reply_text_calls.append((text, kwargs))


class _FakeQuery:
    def __init__(self, user=None):
        self.effective_user = user
        self.answers = []
        self.message = _FakeMessage(user=user)

    async def answer(self, text, show_alert=False):
        self.answers.append((text, show_alert))


class _FakeUpdate:
    def __init__(self, *, user=None, message=None, query=None):
        self.effective_user = user
        self.effective_message = message
        self.callback_query = query


def _user(uid):
    return _FakeUser(uid)


class TestStartModule(unittest.IsolatedAsyncioTestCase):
    async def test_cmd_start_denies_unknown_user_and_shows_their_id(self):
        user = _user(999)
        msg = _FakeMessage(user=user, text="/start")
        update = _FakeUpdate(user=user, message=msg)
        with mock.patch("bot.modules.start.rbac.is_allowed", return_value=False):
            await start.cmd_start(update, None)
        self.assertEqual(len(msg.reply_text_calls), 1)
        text = msg.reply_text_calls[0][0]
        self.assertIn("SUDO_IDS", text)
        self.assertIn("999", text)

    async def test_cb_main_menu_denies_callback_user_with_alert(self):
        user = _user(999)
        query = _FakeQuery(user=user)
        update = _FakeUpdate(user=user, message=None, query=query)
        with mock.patch("bot.modules.start.rbac.is_allowed", return_value=False):
            await start.cb_main_menu(update, None)
        self.assertEqual(len(query.answers), 1)
        self.assertIn("دسترسی", query.answers[0][0])
        self.assertTrue(query.answers[0][1])  # show_alert

    async def test_cmd_start_redeems_invite_when_code_matches(self):
        user = _user(555)
        msg = _FakeMessage(user=user, text="/start SECRETCODE")
        update = _FakeUpdate(user=user, message=msg)
        with mock.patch("bot.modules.start.rbac.confirm_invite", return_value=True) as m, \
             mock.patch("bot.modules.start.rbac.is_allowed", return_value=True):
            await start.cmd_start(update, None)
        m.assert_called_once_with(555, "SECRETCODE")
        self.assertEqual(len(msg.reply_html_calls), 1)
        self.assertIn("ادمین", msg.reply_html_calls[0][0])

    async def test_cmd_start_shows_menu_for_allowed_user(self):
        user = _user(7)
        msg = _FakeMessage(user=user, text="/start")
        update = _FakeUpdate(user=user, message=msg)
        with mock.patch("bot.modules.start.rbac.is_allowed", return_value=True), \
             mock.patch("bot.modules.start.main_menu_text", return_value="MENU"), \
             mock.patch("bot.modules.start.main_menu_keyboard", return_value="KB"), \
             mock.patch("bot.modules.start.flow_guard.close_others") as co:
            await start.cmd_start(update, None)
        co.assert_called_once()
        self.assertEqual(len(msg.reply_html_calls), 1)
        self.assertEqual(msg.reply_html_calls[0][0], "MENU")


if __name__ == "__main__":
    unittest.main()
