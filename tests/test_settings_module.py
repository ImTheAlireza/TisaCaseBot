"""تست‌های صفحه تنظیمات (⚙️) — فقط سودو، تاگل دکمه‌ها، قفل سودو."""
from __future__ import annotations

import os
import unittest
from unittest import mock

os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("SUDO_IDS", "1234567")

from bot.constants import CB
from bot.modules import settings as settings_mod


class _FakeButton:
    def __init__(self, key, label, admin_eligible=True):
        self.key = key
        self.label = label
        self.admin_eligible = admin_eligible


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeQuery:
    def __init__(self, user, data=""):
        self.effective_user = user
        self.data = data
        self.answers = []
        self.edits = []

    async def answer(self, text="", show_alert=False, **kw):
        self.answers.append((text, show_alert))


async def _fake_answer_and_edit(query, text, **kw):
    """جای answer_and_edit را می‌گیرد و ویرایش را ضبط می‌کند."""
    query.edits.append((text, kw))


class _FakeUpdate:
    def __init__(self, user, query):
        self.effective_user = user
        self.callback_query = query


_FAKE_BUTTONS = [
    _FakeButton("ping", "پینگ"),
    _FakeButton("restart", "ری‌استارت", admin_eligible=False),
]


class TestSettingsModule(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.user = _FakeUser(1234567)  # sudo (SUDO_IDS=1234567)
        self.other = _FakeUser(111)
        # Visibility map برای کنترل در تست
        self._visible = {"ping": True}
        self._set_calls = []

        p1 = mock.patch.object(settings_mod, "BUTTONS", _FAKE_BUTTONS)
        p2 = mock.patch("bot.modules.settings.answer_and_edit", _fake_answer_and_edit)
        p3 = mock.patch.object(settings_mod, "rbac")
        p4 = mock.patch.object(settings_mod, "preferences")
        for p in (p1, p2, p3, p4):
            p.start()
            self.addCleanup(p.stop)
        # behavior rbac
        rbac = settings_mod.rbac
        rbac.is_sudo.side_effect = lambda uid: uid == self.user.id
        # behavior preferences
        def vis(k):
            return self._visible.get(k, False)
        def setvis(k, v):
            self._visible[k] = v
            self._set_calls.append((k, v))
        settings_mod.preferences.button_visible.side_effect = vis
        settings_mod.preferences.set_button_visible.side_effect = setvis

    def _query(self, user=None, data=""):
        return _FakeQuery(user or self.user, data=data)

    async def test_cb_settings_denies_non_sudo(self):
        q = self._query(self.other)
        upd = _FakeUpdate(self.other, q)
        await settings_mod.cb_settings(upd, None)
        self.assertEqual(len(q.answers), 1)
        self.assertIn("دسترسی", q.answers[0][0])
        self.assertTrue(q.answers[0][1])
        self.assertEqual(q.edits, [])

    async def test_cb_settings_renders_for_sudo(self):
        q = self._query()
        upd = _FakeUpdate(self.user, q)
        await settings_mod.cb_settings(upd, None)
        self.assertEqual(q.answers, [])
        self.assertEqual(len(q.edits), 1)
        text, kw = q.edits[0]
        self.assertIn("تنظیمات", text)
        self.assertIn("پینگ", text)
        self.assertIn("👑", text)  # restart sudo-only badge
        # پینگ باید قابل تغییر باشد (admin_eligible) پس در کیبورد هست
        kb = kw["reply_markup"]
        all_data = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertIn(f"{CB.SETTINGS_TOGGLE_PREFIX}ping", all_data)
        self.assertIn(CB.SETTINGS_LOCKED, all_data)  # restart button
        self.assertIn(CB.MAIN_MENU, all_data)

    async def test_cb_locked_shows_explanation_to_sudo(self):
        q = self._query()
        upd = _FakeUpdate(self.user, q)
        await settings_mod.cb_locked(upd, None)
        self.assertEqual(len(q.answers), 1)
        self.assertIn("سودو", q.answers[0][0])
        self.assertTrue(q.answers[0][1])

    async def test_cb_toggle_flips_value_and_rerenders(self):
        self._visible["ping"] = True
        q = self._query(data=f"{CB.SETTINGS_TOGGLE_PREFIX}ping")
        upd = _FakeUpdate(self.user, q)
        await settings_mod.cb_toggle(upd, None)
        # یک set با مقدار معکوس
        self.assertEqual(self._set_calls, [("ping", False)])
        self.assertEqual(len(q.edits), 1)
        text, kw = q.edits[0]
        self.assertIn("تنظیمات", text)
        self.assertEqual(kw.get("parse_mode"), "HTML")

    async def test_cb_toggle_with_invalid_key_is_silent(self):
        q = self._query(data=f"{CB.SETTINGS_TOGGLE_PREFIX}garbage!!")
        upd = _FakeUpdate(self.user, q)
        await settings_mod.cb_toggle(upd, None)
        # نه set صدا زده می‌شود نه ویرایش
        self.assertEqual(self._set_calls, [])
        self.assertEqual(q.edits, [])
        self.assertEqual(len(q.answers), 1)


if __name__ == "__main__":
    unittest.main()
