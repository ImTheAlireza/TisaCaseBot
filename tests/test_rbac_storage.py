"""Runtime-state safety: atomic writes, backup recovery, invite-confirmed admins.

Two real incidents these tests prevent:

* the «🔄 ری‌استارت» button firing while ``roles.json`` was being written left a
  truncated file; the reader's ``except ValueError`` then returned an empty store
  and every admin silently lost access;
* «➕ افزودن ادمین» accepted any typed number as a privilege grant, with no proof
  that the person behind that id ever talked to the bot.

Stdlib only — safe for ``python3 -m unittest discover -s tests`` on a bare host.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("SUDO_IDS", "1")

from bot.services import jsonstore

try:  # rbac/preferences import bot.config, which needs python-dotenv
    from bot import rbac as _rbac_module  # noqa: F401
    from bot.services import preferences as _preferences_module  # noqa: F401

    HAS_CONFIG = True
except Exception:  # pragma: no cover
    HAS_CONFIG = False

needs_config = unittest.skipUnless(HAS_CONFIG, "python-dotenv is not installed")


class _TempState(unittest.TestCase):
    """Give each test its own data directory and a clean cache."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        jsonstore.invalidate()


class TestJsonStore(_TempState):
    def test_round_trip(self):
        path = self.tmp / "state.json"
        self.assertTrue(jsonstore.write_json(path, {"a": 1, "persian": "سلام"}))
        self.assertEqual(jsonstore.read_json(path, None), {"a": 1, "persian": "سلام"})

    def test_corrupt_file_is_recovered_from_the_backup(self):
        path = self.tmp / "roles.json"
        jsonstore.write_json(path, {"admins": {"1": {"name": "ali"}}})
        jsonstore.write_json(path, {"admins": {"1": {"name": "ali"}, "2": {"name": "reza"}}})
        # Simulate the kill that used to happen mid-write.
        path.write_text('{"admins": {"1": {"na', encoding="utf-8")
        jsonstore.invalidate()

        data = jsonstore.read_json(path, None)

        self.assertEqual(list(data["admins"]), ["1"], "expected the previous good copy")

    def test_missing_file_returns_the_default(self):
        self.assertIsNone(jsonstore.read_json(self.tmp / "nope.json", None))
        self.assertEqual(jsonstore.read_json(self.tmp / "nope.json", {"x": 1}), {"x": 1})

    def test_no_temp_files_are_left_behind(self):
        path = self.tmp / "prefs.json"
        for index in range(5):
            jsonstore.write_json(path, {"i": index})
        self.assertEqual({p.name for p in self.tmp.iterdir()}, {"prefs.json", "prefs.json.bak"})

    def test_file_is_owner_only(self):
        path = self.tmp / "roles.json"
        jsonstore.write_json(path, {"admins": {}})
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)


@needs_config
class TestRbacInvites(_TempState):
    def setUp(self) -> None:
        super().setUp()
        from bot import rbac

        self.rbac = rbac
        self.addCleanup(setattr, rbac, "ROLES_FILE", rbac.ROLES_FILE)
        self.addCleanup(setattr, rbac, "DATA_DIR", rbac.DATA_DIR)
        rbac.DATA_DIR = self.tmp
        rbac.ROLES_FILE = self.tmp / "roles.json"

    def test_a_typed_id_alone_grants_nothing(self):
        self.rbac.issue_invite(777, name="کاربر ۷۷۷", added_by=1)
        self.assertFalse(self.rbac.is_admin(777))
        self.assertEqual(self.rbac.role(777), self.rbac.USER)
        self.assertIn("777", self.rbac.pending_invites())
        self.assertNotIn("777", self.rbac.admins())

    def test_the_code_activates_the_admin(self):
        code = self.rbac.issue_invite(777, added_by=1)
        self.assertFalse(self.rbac.confirm_invite(777, "WRONG"))
        self.assertTrue(self.rbac.confirm_invite(777, code.lower()))
        self.assertTrue(self.rbac.is_admin(777))
        self.assertEqual(self.rbac.role(777), self.rbac.ADMIN)
        self.assertEqual(self.rbac.pending_invites(), {})

    def test_a_code_only_works_for_its_own_user(self):
        code = self.rbac.issue_invite(777, added_by=1)
        self.assertFalse(self.rbac.confirm_invite(888, code))
        self.assertFalse(self.rbac.is_admin(888))

    def test_revoke_removes_the_pending_record(self):
        self.rbac.issue_invite(777, added_by=1)
        self.assertTrue(self.rbac.cancel_invite(777))
        self.assertEqual(self.rbac.pending_invites(), {})
        self.assertFalse(self.rbac.is_admin(777))

    def test_direct_add_still_works(self):
        self.rbac.add_admin(555, name="ali", added_by=1)
        self.assertTrue(self.rbac.is_admin(555))
        self.assertIn("555", self.rbac.admins())

    def test_sudo_is_never_removed_by_admin_bookkeeping(self):
        # Sudo power comes from .env, so storing/removing the same id in the
        # runtime admin list must not touch it (no locking yourself out).
        from bot.config import settings

        original = settings.sudo_ids
        self.addCleanup(lambda: object.__setattr__(settings, "sudo_ids", original))
        object.__setattr__(settings, "sudo_ids", frozenset({1}))
        self.assertTrue(self.rbac.is_sudo(1))
        self.rbac.add_admin(1)
        self.assertTrue(self.rbac.remove_admin(1))        # only bookkeeping
        self.assertTrue(self.rbac.is_sudo(1))

    def test_state_survives_a_reload(self):
        self.rbac.add_admin(4242, name="پایدار")
        jsonstore.invalidate()
        self.assertIn("4242", self.rbac.admins())
        self.assertEqual(self.rbac.admins()["4242"]["name"], "پایدار")

    def test_the_admin_list_is_not_rebuilt_when_roles_json_did_not_change(self):
        """کش فقط با تغییرِ *واقعیِ* فایل باطل می‌شود — با همان قاعدهٔ jsonstore (امضا)."""
        from unittest import mock

        self.rbac.add_admin(4242, name="کشِ نقش")
        self.assertTrue(self.rbac.is_admin(4242))  # کش را گرم کن
        with mock.patch.object(self.rbac, "_load", wraps=self.rbac._load) as loader:
            self.assertTrue(self.rbac.is_admin(4242))
            self.assertIn("4242", self.rbac.admins())
            self.assertTrue(self.rbac.is_admin(4242))
            self.assertEqual(0, loader.call_count, "بدون تغییرِ فایل، دوباره خوانده نمی‌شود")
            # ...و برعکس: سلبِ نقش باید فوراً دیده شود، نه بعد از ری‌استارت.
            self.rbac.remove_admin(4242)
            self.assertFalse(self.rbac.is_admin(4242))
            self.assertGreater(loader.call_count, 0, "تغییرِ نقش باید کش را باطل کند")


@needs_config
class TestPreferences(_TempState):
    def setUp(self) -> None:
        super().setUp()
        from bot.services import preferences

        self.preferences = preferences
        self.addCleanup(setattr, preferences, "FILE", preferences.FILE)
        self.addCleanup(setattr, preferences, "DATA_DIR", preferences.DATA_DIR)
        preferences.DATA_DIR = self.tmp
        preferences.FILE = self.tmp / "preferences.json"

    def test_default_is_visible_and_toggle_persists(self):
        self.assertTrue(self.preferences.button_visible("tracking"))
        self.preferences.set_button_visible("tracking", False)
        jsonstore.invalidate()
        self.assertFalse(self.preferences.button_visible("tracking"))

    def test_legacy_single_flag_is_migrated(self):
        (self.tmp / "preferences.json").write_text(
            json.dumps({"show_compress_to_admins": False}), encoding="utf-8"
        )
        jsonstore.invalidate()
        self.assertFalse(self.preferences.button_visible("compress"))


if __name__ == "__main__":
    unittest.main()
