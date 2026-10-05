"""تست‌های ماژول پینگ — رد سودو، مسیر dry-run، و پیام‌های نتایج."""
from __future__ import annotations

import os
import unittest
from unittest import mock

os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("SUDO_IDS", "1234567")

from bot.modules import ping as ping_mod


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeQuery:
    def __init__(self, user, data=""):
        self.effective_user = user
        self.data = data
        self.answers = []
        self.edits = []

    async def answer(self, text="", **kw):
        self.answers.append((text, kw))

    async def edit_message_text(self, text, reply_markup=None, **kw):
        self.edits.append((text, reply_markup, kw))


class _FakeUpdate:
    def __init__(self, user, query):
        self.effective_user = user
        self.callback_query = query


class TestPingModule(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sudo = _FakeUser(1234567)
        self.other = _FakeUser(111)
        p1 = mock.patch.object(ping_mod, "rbac")
        p2 = mock.patch.object(ping_mod, "settings")
        for p in (p1, p2):
            p.start()
            self.addCleanup(p.stop)
        ping_mod.rbac.is_sudo.side_effect = lambda uid: uid == self.sudo.id
        ping_mod.settings.configure_mock(
            woo_dry_run=False,
            woocommerce_url="", woocommerce_key="", woocommerce_secret="",
            woocommerce_version="wc/v3",
        )

    def _q(self, user=None):
        return _FakeQuery(user or self.sudo)

    async def test_ping_denies_non_sudo(self):
        q = self._q(self.other)
        upd = _FakeUpdate(self.other, q)
        await ping_mod.cb_ping(upd, None)
        self.assertEqual(len(q.answers), 1)
        self.assertIn("دسترسی", q.answers[0][0])
        self.assertTrue(q.answers[0][1].get("show_alert"))
        self.assertEqual(q.edits, [])

    async def test_ping_replies_pong(self):
        q = self._q()
        upd = _FakeUpdate(self.sudo, q)
        await ping_mod.cb_ping(upd, None)
        # answer("Pong") یک بار صدا زده شده
        self.assertTrue(any("Pong" in a[0] for a in q.answers))
        self.assertEqual(len(q.edits), 1)
        text, rm, kw = q.edits[0]
        self.assertIn("Pong", text)
        self.assertIn("round-trip", text)
        self.assertIsNotNone(rm)  # کیبورد بازگشت

    async def test_woo_ping_missing_config_shows_env_vars(self):
        ping_mod.settings.woocommerce_url = ""
        q = self._q()
        upd = _FakeUpdate(self.sudo, q)
        await ping_mod.cb_woocommerce_ping(upd, None)
        text, _, _ = q.edits[0]
        self.assertIn("کامل نیست", text)
        self.assertIn("WOOCOMMERCE_URL", text)

    async def test_media_ping_blocked_by_dry_run(self):
        ping_mod.settings.woo_dry_run = True
        q = self._q()
        upd = _FakeUpdate(self.sudo, q)
        await ping_mod.cb_media_ping(upd, None)
        self.assertTrue(any("آزمایشی" in a[0] for a in q.answers))
        self.assertEqual(len(q.edits), 1)
        text, _, _ = q.edits[0]
        self.assertIn("TISA_DRY_RUN", text)
        self.assertIn("واقعاً روی سایت می‌نویسد", text)

    async def test_product_ping_blocked_by_dry_run(self):
        ping_mod.settings.woo_dry_run = True
        q = self._q()
        upd = _FakeUpdate(self.sudo, q)
        await ping_mod.cb_product_ping(upd, None)
        text, _, _ = q.edits[0]
        self.assertIn("TISA_DRY_RUN", text)

    async def test_back_keyboard_has_all_entries(self):
        from bot.constants import CB
        kb = ping_mod._back_keyboard()
        data = {b.callback_data for row in kb.inline_keyboard for b in row}
        for must in (CB.PING, CB.WOO_PING, CB.WP_MEDIA_PING, CB.WOO_PRODUCT_PING, CB.MAIN_MENU):
            self.assertIn(must, data, f"گم شده در کیبورد: {must}")


if __name__ == "__main__":
    unittest.main()
