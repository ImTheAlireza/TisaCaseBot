"""فاز ۴ (۸): صفِ بادوامِ ارسال — «سایت جواب نداد» نباید یعنی «دوباره خودت بزن».

انتشار یک درخواست نیست: آپلود تصویر ← ساخت محصول ← واریژن‌ها. وقتی هاست روی ۴۲۹ یا ۵۰۳
می‌رود، سیاست retry داخل `woo_client` همان لحظه سه بار زده و تمام شده؛ تا این فاز، ادامه‌اش
با دستِ مالک چت بود. صفِ ارسال این را به قولِ خودِ ربات تبدیل می‌کند: تلاشِ ردشده با
تصویرهایش و همان `batch_id` در SQLite می‌نشیند و طبق برنامه دوباره امتحان می‌شود تا
موفق شود یا بعد از ۸ تلاش / ۲۴ ساعت رها شود — و در هر دو حالت در تاریخچه نوشته می‌شود.

دو مرز که نگهش می‌دارد: صف هیچ مسیر دومِ انتشاری ندارد (همان `create_draft`، همان دروازهٔ
idempotency)، و در حالت آزمایشی هرگز تخلیه نمی‌شود.

اجرا: ``python3 -m unittest discover -s tests``
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("SUDO_IDS", "1234567")

from _flow_harness import context as make_context, patched_settings, settings_with, temp_ledger

try:
    from bot.modules import outbox_flow, product_flow as PF
    from bot.services import outbox, products_ledger
    from bot.services.product_extractor import ProductData
    from bot.services.woo_client import WooCommerceAPIError

    HAS_FLOW = True
except Exception:                                       # pragma: no cover - PTB missing
    HAS_FLOW = False

needs_flow = unittest.skipUnless(HAS_FLOW, "python-telegram-bot is not installed")

SHARED = {
    "woocommerce_url": "https://shop.example",
    "woocommerce_key": "ck_test",
    "woocommerce_secret": "cs_test",
    "wordpress_url": "https://shop.example",
    "wordpress_username": "admin",
    "wordpress_app_password": "aaaa bbbb",
}


def _payload(**over: object) -> dict:
    base = {"title": "قاب سیلیکونی آیفون 13", "price": 698000, "stock": 20, "sale_price": 498000}
    base.update(over)
    return base


@needs_flow
class QueueTestCase(unittest.IsolatedAsyncioTestCase):
    """یک صفِ تنها در دایرکتوری موقت: نه در ``data/`` مخزن، نه بین تست‌ها قاطی."""

    def setUp(self) -> None:
        self._stack = contextlib.ExitStack()
        self.ledger = self._stack.enter_context(temp_ledger())
        self._settings = patched_settings(settings_with(**SHARED))
        self._settings.__enter__()
        self.tmp = Path(tempfile.mkdtemp(prefix="tisa-outbox-"))
        self.addCleanup(self._cleanup_all)

    def _cleanup_all(self) -> None:
        self._settings.__exit__(None, None, None)
        self._stack.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def row(self, *, expected: int = 1, in_seconds: float = 0.0):
        """ردیفِ صف، فارغ از اینکه موعدش رسیده یا نه (`due` فقطِ رسیده‌ها را می‌دهد)."""
        rows = outbox.due(now=time.time() + in_seconds)
        self.assertEqual(expected, len(rows), f"تعداد ردیف‌های صف: {len(rows)}")
        return rows[0] if expected else None

    def image(self, name: str = "01_مشکی.jpg") -> Path:
        path = self.tmp / name
        path.write_bytes(b"z" * 64)
        return path

    def enqueue(self, *, batch: str = "aaaa1111bbbb", error: str = "HTTP 503: busy",
                images: list[Path] | None = None, delay: float = 0.0, now: float | None = None) -> bool:
        return outbox.enqueue(
            batch_id=batch, chat_id=9, user_id=7, payload=_payload(), images=images if images is not None else [],
            thread_id=3, mode="new", error=error, delay=delay, now=now,
        )


@needs_flow
class TestQueueStorage(QueueTestCase):
    async def test_an_item_waits_for_its_backoff(self) -> None:
        moment = time.time()
        self.assertTrue(self.enqueue(delay=120, now=moment))
        self.assertEqual([], outbox.due(now=moment + 60), "زودتر از موعد نباید تلاش شود")
        self.assertEqual(1, len(outbox.due(now=moment + 121)))

    async def test_the_same_content_updates_one_row(self) -> None:
        moment = time.time()
        self.enqueue(batch="same1234same", now=moment)
        self.enqueue(batch="same1234same", error="HTTP 500: fatal", now=moment + 10)
        with outbox._db() as conn:
            rows = conn.execute("SELECT * FROM outbox").fetchall()
        self.assertEqual(1, len(rows), "شناسهٔ محتوا یکتاست؛ دوبار صف = یک صف")
        self.assertEqual("HTTP 500: fatal", rows[0]["last_error"])
        self.assertEqual(moment, float(rows[0]["created_at"]), "سن از اولین تلاش شمارش می‌شود")

    async def test_images_are_copied_into_the_queue(self) -> None:
        source = self.image()
        self.enqueue(batch="pic1234pic12", images=[source], delay=0.0)
        entry = outbox.due()[0]
        self.assertEqual(1, len(entry.images))
        self.assertNotEqual(source, entry.images[0], "تصویرِ صف نباید به فایلِ در حال حذفِ session وصل باشد")
        source.unlink()
        self.assertTrue(entry.images[0].is_file())
        self.assertEqual(b"z" * 64, entry.images[0].read_bytes())
        self.assertIn("01_مشکی.jpg", entry.images[0].name, "شمارهٔ گالری می‌ماند تا ترتیب عوض نشود")

    async def test_a_lost_spool_file_is_reported_not_hidden(self) -> None:
        source = self.image()
        self.enqueue(batch="lost1234lost", images=[source], delay=0.0)
        entry = outbox.due()[0]
        entry.images[0].unlink()
        again = outbox.due()[0]
        self.assertEqual([], again.images)
        self.assertEqual(1, len(again.missing_images))

    async def test_backoff_grows_and_stops(self) -> None:
        moment = time.time()
        self.enqueue(batch="grow1234grow", delay=0.0, now=moment)
        entry = outbox.due(now=moment)[0]
        delays = []
        for _ in range(7):
            entry = outbox.note_failure(entry, "HTTP 503", now=moment)
            delays.append(round(entry.next_at - moment))
            moment += delays[-1] + 1
        self.assertEqual([120, 240, 480, 960, 1920, 3840, 7680], delays,
                         "دو برابر، از ۱٫۵ دقیقه (تلاش اولِ صف ۶۰ ثانیه بود) تا سقف سه‌ساعتی")
        self.assertEqual(outbox.MAX_DELAY_SECONDS, outbox.backoff_seconds(30), "رشد بی‌نهایت نیست")
        self.assertEqual(outbox.STATUS_DROPPED, entry.status, "تلاش هشتم آخرین شانس است")
        self.assertEqual([], outbox.due(now=moment + 10_000))

    async def test_an_item_older_than_a_day_is_dropped_early(self) -> None:
        moment = time.time()
        self.enqueue(batch="old1234old12", delay=0.0, now=moment - outbox.MAX_AGE_SECONDS - 60)
        entry = outbox.due(now=moment)[0]
        updated = outbox.note_failure(entry, "HTTP 502", now=moment)
        self.assertEqual(outbox.STATUS_DROPPED, updated.status, "۲۴ ساعت، حتی با یک تلاش")

    async def test_success_removes_the_row_and_its_files(self) -> None:
        source = self.image()
        self.enqueue(batch="win1234win12", images=[source], delay=0.0)
        entry = outbox.due()[0]
        outbox.succeed(entry.batch_id)
        self.assertEqual(0, outbox.pending())
        self.assertFalse((outbox.FILES_DIR / entry.batch_id).exists(), "تصویرِ بی‌صاحب نباید بماند")

    async def test_stats_answer_the_only_question_about_the_queue(self) -> None:
        self.enqueue(batch="st1234st1234", delay=600)
        stats = outbox.stats()
        self.assertEqual(1, stats["pending"])
        self.assertEqual(0, stats["ready_now"])
        self.assertGreater(stats["waiting_seconds"], 0)
        self.assertIn("503", stats["last_error"])

    async def test_probe_reports_a_store_it_cannot_write(self) -> None:
        self.assertEqual("", outbox.probe(), "روی دایرکتوری سالم باید ساکت باشد")
        blocked = self.tmp / "not-a-db"
        blocked.write_bytes(b"x")
        saved = outbox.DB_PATH
        try:
            outbox.DB_PATH = blocked / "outbox.sqlite3"
            problem = outbox.probe()
        finally:
            outbox.DB_PATH = saved
        self.assertIn("صفِ ارسال", problem)

    async def test_a_corrupt_payload_is_read_as_nothing_rather_than_a_crash(self) -> None:
        self.enqueue(batch="bad1234bad12", delay=0.0)
        with outbox._db() as conn:
            conn.execute("UPDATE outbox SET payload = '{', images = 'nope'")
        entry = outbox.due()[0]
        self.assertEqual({}, entry.payload)
        self.assertEqual([], entry.images)


@needs_flow
class TestWhatCountsAsTransient(unittest.TestCase):
    """درِ ورود به صف: فقط چیزی که «بعداً» می‌تواند درست شود."""

    def test_busy_store_answers_are_queued(self) -> None:
        for status in (408, 425, 429, 500, 502, 503, 504):
            with self.subTest(status=status):
                self.assertTrue(outbox.is_transient(WooCommerceAPIError(status, "busy")))

    def test_a_wrong_value_is_not(self) -> None:
        for status in (400, 401, 403, 404, 422):
            with self.subTest(status=status):
                self.assertFalse(outbox.is_transient(WooCommerceAPIError(status, "bad")),
                                 "یک ۴۰۰ فردا هم همان جواب را می‌دهد")

    def test_network_problems_are(self) -> None:
        import httpx

        self.assertTrue(outbox.is_transient(httpx.ConnectError("no route")))
        self.assertTrue(outbox.is_transient(httpx.ReadTimeout("slow")))
        self.assertTrue(outbox.is_transient(TimeoutError()))

    def test_a_bug_is_not(self) -> None:
        self.assertFalse(outbox.is_transient(ValueError("bad key")))
        self.assertFalse(outbox.is_transient(KeyError("images")))


@needs_flow
class TestQueueIsCreatedFromTheFlow(QueueTestCase):
    """«تأیید و ساخت» روی ۵۰۳: یک کارتِ 🐇، یک ردیفِ صف، هیچ کارت دوم."""

    def setUp(self) -> None:
        super().setUp()          # صف و تاریخچهٔ تست نباید در data/ واقعی بنویسند
        for store in (PF.sessions, PF.album_buffers, PF.album_tasks):
            store.clear()
        self._real_sudo = PF.rbac.is_sudo
        PF.rbac.is_sudo = lambda user_id: True
        self._real_create_draft = PF.create_draft
        self.addCleanup(self._restore)
        self.data = ProductData(title="قاب سیلیکونی آیفون 13", price=698000, stock=20,
                                sale_price=498000, sku_prefix="BO", models=["iPhone 13"])
        self.workspace = Path(tempfile.mkdtemp(prefix="tisa-outbox-ws-"))
        self.addCleanup(shutil.rmtree, self.workspace, True)
        self.image_file = self.workspace / "01_مشکی.jpg"
        self.image_file.write_bytes(b"z" * 64)
        PF.sessions[7] = PF.ProductSession(
            files=[self.image_file], model_text="قاب", info_text="", models=["iPhone 13"],
            data=self.data, mode="new", chat_id=9, thread_id=3, workspace=self.workspace,
        )

    def _restore(self) -> None:
        PF.rbac.is_sudo = self._real_sudo
        PF.create_draft = self._real_create_draft
        for store in (PF.sessions, PF.album_buffers, PF.album_tasks):
            store.clear()

    async def _confirm(self):
        from _flow_harness import query_update

        update, seen = query_update("product:confirm", user_id=7, chat_id=9, thread_id=3)
        context = make_context()
        result = await PF.confirm(update, context)
        return result, seen, context

    def _fail_with(self, exc: Exception) -> None:
        async def failing(data, files, *, report=None, **kwargs):
            raise exc

        PF.create_draft = failing

    async def test_a_busy_store_goes_to_the_queue_not_to_a_second_card(self) -> None:
        self._fail_with(WooCommerceAPIError(503, "سایت مشغول است"))
        _result, _seen, _context = await self._confirm()
        entries = products_ledger.recent(5)
        self.assertEqual(1, len(entries), f"یک تلاش، یک کارت: {[e['status'] for e in entries]}")
        self.assertEqual("queued", entries[0]["status"])
        self.assertIn("HTTP 503", entries[0]["error"])
        self.assertEqual(1, outbox.pending(), "یک ردیفِ صف، همان محتوای ردشده")
        row = self.row(in_seconds=3600)
        self.assertEqual(20, row.payload["stock"], "موجودی هم باید در صف باشد، نه فقط قیمت")
        self.assertEqual(498000, row.payload["sale_price"])
        self.assertEqual(1, len(row.images), "تصویر هم در صف نسخهٔ خودش را دارد")

    async def test_the_queued_row_keeps_the_ledger_key_so_the_card_closes(self) -> None:
        self._fail_with(WooCommerceAPIError(500, "fatal"))
        await self._confirm()
        row = self.row(in_seconds=3600)
        self.assertEqual(str(products_ledger.recent(1)[0]["key"]), row.ledger_key)
        self.assertGreater(row.next_at, time.time(), "تلاش بعدی باید عقب باشد، نه فوراً")

    async def test_a_rejected_value_is_not_queued(self) -> None:
        self._fail_with(WooCommerceAPIError(400, "عکس مجاز نیست"))
        await self._confirm()
        self.assertEqual(0, outbox.pending())
        self.assertEqual("failed", products_ledger.recent(1)[0]["status"])

    async def test_a_rehearsal_never_queues_anything(self) -> None:
        with patched_settings(settings_with(**SHARED, woo_dry_run=True)):
            self._fail_with(WooCommerceAPIError(503, "سایت مشغول است"))
            await self._confirm()
        self.assertEqual(0, outbox.pending(), "در حالت آزمایشی چیزی برای تلاش مجدد نیست")

    async def test_the_zip_mode_never_queues_anything(self) -> None:
        # حالت ZIP یعنی «فایل را خودت آپلود کن»؛ قولِ تلاشِ خودکار ربات در آن دروغ است.
        PF.sessions[7].mode = "update"
        self._fail_with(WooCommerceAPIError(503, "سایت مشغول است"))
        await self._confirm()
        self.assertEqual(0, outbox.pending())

    async def test_the_message_promises_only_what_the_queue_will_do(self) -> None:
        self._fail_with(WooCommerceAPIError(502, "bad gateway"))
        _result, seen, _context = await self._confirm()
        text = " ".join(str(item[1]) for item in seen)
        self.assertIn("🐇", text)
        self.assertIn("تلاش مجدد", text)
        self.assertNotIn(f"{outbox.REMAINING_TRIES_AFTER_FIRST} بار دیگر", text)
        self.assertNotIn("رها شد", text, "در لحظهٔ صف‌گذاری هنوز رها نشده")


@needs_flow
class TestDrainingTheQueue(QueueTestCase):
    """تخلیهٔ صف: همان مسیر انتشار، با همان پیام‌ها، بدون اینکه کسی دکمه بزند."""

    def setUp(self) -> None:
        super().setUp()          # same reason: never write into the repo's real data/
        from bot.services.woocommerce_direct import create_draft

        self.sent: list[dict] = []
        self._calls: list[dict] = []
        self.error: Exception | None = None
        outbox_flow.create_draft = self._fake_create_draft
        self.addCleanup(setattr, outbox_flow, "create_draft", create_draft)

    async def _fake_create_draft(self, data, files, *, report=None, batch_id="", meta=(), **kwargs):
        self._calls.append({"data": data, "files": list(files), "batch_id": batch_id})
        if report is not None:
            report.append("[product] محصول ساخته شد: id=4321")
        if isinstance(self.error, Exception):
            raise self.error
        return 4321, "https://shop.example/wp-admin/post.php?post=4321&action=edit"

    async def _drain(self) -> int:
        app = type("App", (), {})()
        app.bot = type("Bot", (), {})()

        async def send_message(**kwargs):
            self.sent.append(kwargs)

        app.bot.send_message = send_message
        app.job_queue = None
        return await outbox_flow.drain_once(app)

    async def test_a_queued_product_is_published_and_the_card_closes(self) -> None:
        source = self.image()
        self.enqueue(batch="drain1234dra1", images=[source], delay=0.0)
        row = outbox.due()[0]
        products_ledger.record(user_id=7, status="queued", key=row.ledger_key or "k1", title="قاب")
        self.assertEqual(1, await self._drain())
        self.assertEqual(0, outbox.pending(), "موفق یعنی ردیف از صف برود")
        self.assertEqual(1, len(self._calls))
        self.assertEqual("drain1234dra1", self._calls[0]["batch_id"], "همان شناسهٔ محتوا، پس تکراری ساخته نمی‌شود")
        self.assertEqual(1, len(self._calls[0]["files"]), "تصویرِ صف فرستاده شد")
        entry = products_ledger.recent(1)[0]
        self.assertEqual("created", entry["status"])
        self.assertEqual(4321, entry["product_id"])

    async def test_the_message_never_borrows_another_chats_card(self) -> None:
        """کارتِ موفقیت از همان ردیفِ بسته‌شده ساخته می‌شود، نه از «تازه‌ترین تاریخچه»."""
        self.enqueue(batch="mix1234mix12", delay=0.0)
        row = outbox.due()[0]
        products_ledger.record(user_id=7, status="queued", key=row.ledger_key or "k9", title="قاب اول")
        products_ledger.record(user_id=8, status="created", title="محصولِ چت دیگر", product_id=9999)
        await self._drain()
        text = str(self.sent[0].get("text"))
        self.assertIn("4321", text, "id همین محصول باید در پیام باشد")
        self.assertNotIn("9999", text, "کارتِ چت دیگر نباید قرض گرفته شود")
        self.assertTrue(any("صفِ ارسال انجام شد" in str(item.get("text")) for item in self.sent))
        self.assertEqual(9, self.sent[0]["chat_id"])
        self.assertEqual(3, self.sent[0]["message_thread_id"], "همان فی‌تورِ چت که کار شروع شده بود")

    async def test_a_still_busy_store_waits_longer_and_says_nothing(self) -> None:
        self.error = WooCommerceAPIError(503, "busy")
        self.enqueue(batch="again1234aga1", delay=0.0)
        before = outbox.due()[0].attempts
        await self._drain()
        self.assertEqual(1, outbox.pending(), "هنوز در صف است")
        after = self.row(in_seconds=3600)
        self.assertEqual(before + 1, after.attempts)
        self.assertGreater(after.next_at, time.time(), "با backoff عقب افتاد")
        self.assertEqual([], self.sent, "بین دو تلاش پیام نمی‌فرستیم؛ هر دقیقه یک «هنوز نشد» آدم را دیوانه می‌کند")

    async def test_a_permanent_error_leaves_the_queue_and_says_why(self) -> None:
        self.error = WooCommerceAPIError(400, "تصویر مجاز نیست")
        self.enqueue(batch="perm1234perm", delay=0.0)
        await self._drain()
        self.assertEqual(0, outbox.pending(), "صف برای ۴۰۰ بیدار نمی‌ماند")
        self.assertEqual(1, outbox.stats()["dropped"], "ولی رد شدنش نوشته می‌شود، نه پاک")
        self.assertEqual("failed", products_ledger.recent(1)[0]["status"])
        self.assertIn("تصویر مجاز نیست", str(self.sent[0].get("text")))

    async def test_the_last_attempt_gives_up_in_front_of_the_owner(self) -> None:
        self.error = WooCommerceAPIError(503, "busy")
        self.enqueue(batch="die1234die12", delay=0.0)
        key = "777123"
        with outbox._db() as conn:
            conn.execute(
                "UPDATE outbox SET attempts = ?, payload = ?",
                (outbox.MAX_ATTEMPTS - 1, json.dumps({**_payload(), "ledger_key": key}, ensure_ascii=False)),
            )
        products_ledger.record(user_id=7, status="queued", key=key, title="قاب سیلیکونی آیفون 13")
        await self._drain()
        self.assertEqual(0, outbox.pending(), "رها شده دیگر بیدار نمی‌شود")
        self.assertEqual(1, outbox.stats()["dropped"], "رها کردن یک نتیجه است، نه پاک کردن تاریخچه")
        entry = products_ledger.recent(1)[0]
        self.assertEqual("failed", entry["status"], "کارتِ 🐇 باید با ❌ بسته شود")
        self.assertIn(str(outbox.MAX_ATTEMPTS), str(self.sent[0].get("text")), "تعداد تلاش در پیام هست")
        self.assertIn("رها شد", str(self.sent[0].get("text")))
        self.assertFalse((outbox.FILES_DIR / "die1234die12").exists(), "و فایل‌هایش هم پاک می‌شوند")

    async def test_a_rehearsal_does_not_drain(self) -> None:
        self.enqueue(batch="dry1234dry12", delay=0.0)
        with patched_settings(settings_with(**SHARED, woo_dry_run=True)):
            self.assertEqual(0, await self._drain())
        self.assertEqual(1, outbox.pending(), "در حالت آزمایشی صف دست‌نخورده می‌ماند")

    async def test_start_schedules_and_runs_once_after_a_restart(self) -> None:
        self.enqueue(batch="boot1234boot", delay=0.0)
        jobs: list[dict] = []

        class Queue:
            def run_repeating(self, callback, **kwargs):
                jobs.append({"callback": callback, **kwargs})

        app = type("App", (), {})()
        app.job_queue = Queue()
        app.bot = type("Bot", (), {})()

        async def send_message(**kwargs):
            self.sent.append(kwargs)

        app.bot.send_message = send_message
        await outbox_flow.start(app)
        self.assertEqual(1, len(jobs), "صف باید زمان‌بندی شود")
        self.assertEqual("outbox_drain", jobs[0]["name"])
        self.assertEqual(0, outbox.pending(), "موردِ رسیده در لحظهٔ استارت معطل نمی‌ماند")
        self.assertEqual(1, len(self._calls))


@needs_flow
class TestNothingElseOpensTheQueue(QueueTestCase):
    """صف باید تنها راهِ ورودش را داشته باشد، وگرنه یک انتشار دومِ بی‌صاحب می‌سازد."""

    def test_only_the_queue_module_writes_to_the_queue(self) -> None:
        """هیچ جای دیگری `enqueue/succeed/note_failure` را صدا نزند.

        سه فراخوانِ صف یعنی سه مسیر انتشار: آنِ flow، آنِ drain و آنِ سومی که کسی نمی‌داند
        کی محصول می‌سازد. همین خط فازهای ۴ را از «دوزارهٔ باگ» بیرون آورد — پس نگهبان دارد.
        """
        root = Path(__file__).resolve().parents[1]
        offenders = []
        for path in sorted((root / "bot").rglob("*.py")):
            if path.name in {"outbox.py", "outbox_flow.py"}:
                continue
            text = path.read_text(encoding="utf-8")
            if any(f"outbox.{called}(" in text for called in
                   ("enqueue", "succeed", "note_failure", "clear_for_tests")):
                offenders.append(path.relative_to(root).as_posix())
        self.assertEqual([], offenders, f"صف از جای دیگری نوشته می‌شود: {offenders}")

    def test_the_queue_never_imports_a_module_layer(self) -> None:
        # law of the house: bot/services/* must not import bot/modules/*
        text = (Path(__file__).resolve().parents[1] / "bot/services/outbox.py").read_text(encoding="utf-8")
        self.assertNotIn("bot.modules", text)
        self.assertNotIn("from bot.modules", text)

    def test_the_retry_promise_is_stated_once(self) -> None:
        # README/CHANGELOG the numbers from the constants, not from a copy of them
        docs = Path(__file__).resolve().parents[1] / "README.md"
        if not docs.exists():
            self.skipTest("README missing")
        text = docs.read_text(encoding="utf-8")
        if "صف" in text:
            self.assertIn(str(outbox.MAX_ATTEMPTS), text,
                          "اگر عددِ README با کد فرق کند، README دارد دروغ می‌گوید")


if __name__ == "__main__":
    unittest.main()
