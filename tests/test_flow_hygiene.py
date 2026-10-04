"""Flow-level safety: temp workspaces, cancelled album tasks, double publishing.

These were the operational half of the phase-0 review:

* ``_cleanup`` deleted only ``<root>/compressed`` and left every original
  download on disk — a few abandoned flows filled /tmp on the shared host;
* an album that flushed after the user pressed «❌ لغو» raised ``KeyError`` and
  reported it to the user as «خطا در پردازش عکس‌ها»;
* nothing marked a session as busy while a product was being published, so a
  second tap created a second draft with a second SKU.

Run with ``python3 -m unittest discover -s tests`` — the whole module skips
itself when python-telegram-bot is not installed, and needs a writable temp dir.
"""
from __future__ import annotations

import asyncio
import os
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("SUDO_IDS", "1")

try:
    from bot.modules import product_flow as PF

    HAS_FLOW = True
except Exception:  # pragma: no cover - python-telegram-bot missing
    HAS_FLOW = False

needs_flow = unittest.skipUnless(HAS_FLOW, "python-telegram-bot is not installed")


def _make_workspace(root: Path) -> tuple[Path, Path]:
    """Recreate what ``_prepare_files`` leaves behind: originals + compressed."""
    root.mkdir(parents=True, exist_ok=True)
    original = root / "01_photo.jpg"
    original.write_bytes(b"x" * 4096)
    compressed = root / "compressed"
    compressed.mkdir(exist_ok=True)
    out = compressed / "01_photo_compressed.jpg"
    out.write_bytes(b"y" * 512)
    return original, out


@needs_flow
class TestWorkspaceCleanup(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))
        # Restore it: a leaked module global used to be harmless, but it is still a leak
        # (and now that TEMP_DIR comes from settings, other tests can see it).
        self._temp_dir = PF.TEMP_DIR
        PF.TEMP_DIR = self.tmp
        self.addCleanup(setattr, PF, "TEMP_DIR", self._temp_dir)
        PF.sessions.clear()
        PF.album_buffers.clear()
        PF.album_tasks.clear()

    def test_cleanup_removes_originals_and_compressed_files(self):
        root = self.tmp / "7_1"
        original, compressed = _make_workspace(root)
        PF.sessions[7] = PF.ProductSession(workspace=root, files=[compressed])

        PF._cleanup(7)

        self.assertFalse(compressed.exists())
        self.assertFalse(original.exists(), "original downloads must not survive")
        self.assertFalse(root.exists())

    def test_cleanup_cancels_pending_album_work(self):
        async def scenario():
            root = self.tmp / "7_2"
            _make_workspace(root)
            PF.sessions[7] = PF.ProductSession(workspace=root)

            async def pending():
                await asyncio.sleep(30)

            task = asyncio.create_task(pending())
            PF.album_buffers[(7, "media-group")] = [object()]
            PF.album_tasks[(7, "media-group")] = task
            PF._cleanup(7)
            await asyncio.sleep(0)
            return task

        task = asyncio.run(scenario())
        self.assertTrue(task.cancelled() or task.done())
        self.assertEqual(PF.album_buffers, {})
        self.assertEqual(PF.album_tasks, {})

    def test_album_flush_after_cancel_is_ignored_not_raised(self):
        # old: KeyError, shown to the user as «خطا در پردازش عکس‌ها»
        asyncio.run(PF._prepare_files(4242, [], SimpleNamespace(bot=None)))
        self.assertNotIn(4242, PF.sessions)
        self.assertEqual(PF.album_buffers, {})
        self.assertEqual(PF.album_tasks, {})

    def test_sweep_removes_only_stale_workspaces(self):
        stale = self.tmp / "1_stale"
        fresh = self.tmp / "2_fresh"
        for path in (stale, fresh):
            _make_workspace(path)
            os.utime(path, (time.time() - 90 * 60, time.time() - 90 * 60))
        os.utime(fresh, (time.time(), time.time()))

        removed = PF.sweep_temp_dir(max_age_hours=1)

        self.assertEqual(removed, 1)
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())


@needs_flow
class TestPublishingIsSingleShot(unittest.IsolatedAsyncioTestCase):
    """A second «✅ تأیید» while the first is still running must do nothing."""

    def setUp(self):
        import tempfile
        from bot.services.product_extractor import ProductData

        from _flow_harness import temp_ledger

        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        # ♻️ Two things must be the *test's* state, never the repo's: the publish gate reads
        # the ledger, and the button gate reads roles.json. temp_ledger() gives an empty
        # history plus an admin (user 7) in a throwaway roles file, so the flow really runs.
        self.enterContext(temp_ledger())
        self.user_id = 7
        image = self.tmp / "01_one.jpg"
        image.write_bytes(b"z" * 64)
        session = PF.ProductSession(
            mode="new",
            workspace=self.tmp,
            files=[image],
            data=ProductData(
                title="قاب سیلیکونی",
                price=698_000,
                sku_prefix="BO",
                models=["iPhone 17 Pro Max", "iPhone 17 Pro"],
                attributes={"رنگ": ["سفید", "مشکی"]},
            ),
        )
        PF.sessions[self.user_id] = session

        def query_stub():
            answered = []

            async def answer(text=None, **kwargs):
                answered.append(text)

            async def edit_message_text(text=None, **kwargs):
                return None

            message = SimpleNamespace(chat_id=self.user_id, reply_text=answer)
            cq = SimpleNamespace(
                data="product:confirm",
                answer=answer,
                edit_message_text=edit_message_text,
                message=message,
            )
            update = SimpleNamespace(
                callback_query=cq,
                effective_user=SimpleNamespace(id=self.user_id, username="t", first_name="t"),
                effective_message=message,
            )
            return update, answered

        self.query_stub = query_stub
        self.calls: list = []

        async def fake_create_draft(data, files, *, dry_run=False, report=None, batch_id="", meta=(), resume_existing=True):
            self.calls.append({"data": data, "dry_run": dry_run, "batch_id": batch_id, "meta": meta})
            await asyncio.sleep(0.05)     # the window a double tap used to hit
            return 1234, "https://example.test/edit"

        self._real_create_draft = PF.create_draft
        PF.create_draft = fake_create_draft
        self.addCleanup(lambda: setattr(PF, "create_draft", self._real_create_draft))
        self.addCleanup(PF.sessions.pop, self.user_id, None)

    async def _confirm(self):
        update, answered = self.query_stub()
        context = SimpleNamespace(bot=SimpleNamespace(send_message=lambda *a, **k: asyncio.sleep(0)))
        return await PF.confirm(update, context), answered

    async def test_second_tap_is_rejected_while_publishing(self):
        first = asyncio.create_task(self._confirm())
        await asyncio.sleep(0.01)          # let it get inside the slow section
        second, answered = self.query_stub()
        context = SimpleNamespace(bot=SimpleNamespace(send_message=lambda *a, **k: asyncio.sleep(0)))
        await PF.confirm(second, context)  # must be refused
        await first
        self.assertEqual(len(self.calls), 1, "the product was published twice")
        self.assertIs(False, self.calls[0]["dry_run"], "بدون TISA_DRY_RUN باید انتشار واقعی بماند")
        self.assertRegex(self.calls[0]["batch_id"], r"^[0-9a-f]{24}$",
                         "جریان باید شناسهٔ تلاش (۲۴ رقم هگز) را به سرویس بدهد")
        self.assertEqual("tisa_batch_id", self.calls[0]["meta"][0]["key"],
                         "محصول روی سایت باید برچسب تلاش را داشته باشد تا جستجو شدنی باشد")
        self.assertTrue(any("در جریان است" in (text or "") for text in answered))


@needs_flow
class TestConfirmGate(unittest.IsolatedAsyncioTestCase):
    """The gate is wired into the flow, not only into the service."""

    async def test_missing_models_are_allowed_and_publishable_as_simple_product(self):
        import tempfile
        from _flow_harness import patched_settings, settings_with, temp_ledger
        from bot.services import products_ledger
        from bot.services.product_extractor import ProductData

        workspace = Path(tempfile.mkdtemp(prefix="tisa-no-models-"))
        self.addCleanup(__import__("shutil").rmtree, workspace, True)
        image = workspace / "item.jpg"
        image.write_bytes(b"image")
        data = ProductData(title="قاب ساده", price=698_000, sku_prefix="BO", models=[], attributes={})
        session = PF.ProductSession(
            mode="new", files=[image], data=data, workspace=workspace, chat_id=7
        )
        PF.sessions[7] = session
        self.addCleanup(PF.sessions.pop, 7, None)

        answered: list[str | None] = []
        sent: list[dict] = []

        async def answer(text=None, **kwargs):
            answered.append(text)

        async def edit_message_text(text=None, **kwargs):
            return None

        class Bot:
            async def send_message(self, *args, text="", **kwargs):
                sent.append({"text": text, **kwargs})
                return SimpleNamespace(message_id=101)

            async def edit_message_text(self, *args, **kwargs):
                return None

        update = SimpleNamespace(
            callback_query=SimpleNamespace(
                data="product:confirm", answer=answer, edit_message_text=edit_message_text,
                message=SimpleNamespace(chat_id=101),
            ),
            effective_user=SimpleNamespace(id=7, username="t", first_name="t"),
            effective_message=None,
        )
        calls: list[dict] = []

        async def fake_create_draft(data, files, *, report=None, **kwargs):
            calls.append(data)
            if report is not None:
                report.append("[product] simple product created")
            return 1234, "https://shop.example/wp-admin/post.php?post=1234&action=edit"

        real = PF.create_draft
        PF.create_draft = fake_create_draft
        self.addCleanup(setattr, PF, "create_draft", real)

        context = SimpleNamespace(bot=Bot(), chat_data={})
        with temp_ledger(), patched_settings(settings_with(require_models=False)):
            state = await PF.confirm(update, context)
            entries = products_ledger.recent(1)

        self.assertEqual(PF.ConversationHandler.END, state)
        self.assertEqual(1, len(calls), "missing a model must not prevent the publish attempt")
        self.assertEqual("created", entries[0]["status"])
        self.assertEqual([], calls[0]["models"])
        self.assertTrue(any("مدلی تشخیص داده نشد" in str(item["text"]) for item in sent),
                        "the preview/result can explain the simple-product path without blocking")
        self.assertFalse(any(str(text or "").startswith("⛔") for text in answered))

    async def test_message_not_modified_is_an_idempotent_edit_noop(self):
        from telegram.error import BadRequest

        async def unchanged(*args, **kwargs):
            raise BadRequest("Message is not modified: specified new message content and reply markup are exactly the same")

        target = SimpleNamespace(edit_message_text=unchanged)
        self.assertFalse(await PF._edit_message_if_changed(target, "same preview"))


if __name__ == "__main__":
    unittest.main()
