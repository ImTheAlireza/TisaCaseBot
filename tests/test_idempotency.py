"""فاز ۴ (۲): idempotency — یک محتوا، یک محصول (طرح ۴.۳ + ۴.۷).

باگِ واقعی این بود: انتشار چند درخواست است (آپلود تصویر ← ساخت محصول ← واریژن‌ها).
اگر ربات بین «POST ارسال شد» و «جواب رسید» بمیرد، ما نمی‌دانیم محصول ساخته شده یا نه؛
مالک چت را دوباره شروع می‌کند، «تأیید و ساخت» را می‌زند، و ووکامرس — که هیچ‌وقت دو
محصول هم‌عنوان را رد نمی‌کند — یک SKU تازه بهش می‌دهد و محصول دوم ساخته می‌شود.

راه‌حل دو تیکه است: شناسهٔ محتوا (`publish_batch.batch_id`) که بعد از crash هم همان
عدد است، و نوشتن *همان* شناسه روی خودِ محصول در سایت — پس «آیا این انتشار قبلاً
انجام شده؟» یک سؤال قابل‌پاسخ می‌شود، نه یک حدس.

اجرا: ``python3 -m unittest discover -s tests``
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("SUDO_IDS", "1234567")

from bot.services import products_ledger

from _flow_harness import (
    FakeStore as _Store,
    context as make_context,
    patched_settings,
    query_update,
    settings_with,
    temp_ledger,
)

try:
    from bot.modules import product_flow as PF
    from bot.services import publish_batch
    from bot.services.plan import plan_from_dict
    from bot.services.product_extractor import ProductData
    from bot.services.woocommerce_direct import WooCommerceAPIError, create_draft

    HAS_FLOW = True
except Exception:                                      # pragma: no cover - PTB missing
    HAS_FLOW = False

needs_flow = unittest.skipUnless(HAS_FLOW, "python-telegram-bot is not installed")


def _data(**over: object) -> ProductData:
    kwargs: dict[str, object] = {
        "title": "قاب گوشی اپل",
        "price": 100_000,
        "sku_prefix": "IP15",
        "models": ["iPhone 15", "S24 Ultra"],
        "attributes": {"رنگ": ["مشکی", "سفید"]},
        "categories": ["قاب گوشی"],
    }
    kwargs.update(over)
    data = ProductData(**kwargs)  # type: ignore[arg-type]
    data.variation_count = plan_from_dict(data.to_dict()).count
    return data


def _ghost(batch: str, *, product_id: int = 5555) -> dict:
    """محصولی که از تلاش قبلی مانده: همان محتوا، همان برچسب تلاش."""
    return {
        "id": product_id,
        "name": "قاب گوشی اپل",
        "sku": "IP151",
        "status": "draft",
        "meta_data": [{"key": publish_batch.META_BATCH, "value": batch}],
    }


def _variation(model: str, color: str) -> dict:
    return {
        "id": 7000,
        "attributes": [{"name": "مدل", "option": model}, {"name": "رنگ", "option": color}],
    }


@needs_flow
class TestBatchId(unittest.TestCase):
    def test_same_content_same_id(self) -> None:
        first = publish_batch.batch_id(_data().to_dict(), [], chat_id=9)
        second = publish_batch.batch_id(_data().to_dict(), [], chat_id=9)
        self.assertEqual(first, second, "بعد از crash همان draft باید همان شناسه را بدهد")
        self.assertRegex(first, r"^[0-9a-f]{12}$")

    def test_everything_that_matters_changes_the_id(self) -> None:
        base = publish_batch.batch_id(_data().to_dict(), [], chat_id=9)
        changed = {
            "price": _data(price=200_000),
            "title": _data(title="قاب گوشی سامسونگ"),
            "models": _data(models=["iPhone 15"]),
            "colors": _data(attributes={"رنگ": ["مشکی", "سفید", "قرمز"]}),
            "categories": _data(categories=["قاب گوشی", "گلس"]),
        }
        for key, value in changed.items():
            other = publish_batch.batch_id(value.to_dict(), [], chat_id=9)
            self.assertNotEqual(base, other, f"«{key}» نباید شناسه را بی‌تغییر بگذارد")
        other_chat = publish_batch.batch_id(_data().to_dict(), [], chat_id=10)
        self.assertNotEqual(base, other_chat, "چت دیگر یعنی قصدِ دیگر")
        matrix_a = _data().to_dict()
        matrix_a["stock_matrix"] = {"A": {"iPhone 13": 7}}
        matrix_b = _data().to_dict()
        matrix_b["stock_matrix"] = {"A": {"iPhone 13": 8}}
        self.assertNotEqual(
            publish_batch.batch_id(matrix_a, [], chat_id=9),
            publish_batch.batch_id(matrix_b, [], chat_id=9),
            "تغییر موجودی یک خانه باید شناسهٔ تلاش را عوض کند",
        )

    def test_cosmetic_fields_do_not_change_the_id(self) -> None:
        """یادداشت‌ها و شواهدِ پارسر بخشی از محصول نیستند؛ شناسه را عوض نکنند."""
        plain = _data()
        with_notes = _data()
        with_notes.notes = ["یک مبلغ را به‌عنوان قیمت نپذیرفتیم"]
        with_notes.warnings = ["E_X"]
        with_evidence = _data()
        with_evidence.evidence = {"title": SimpleNamespace(source="message", quote="قاب")}
        self.assertEqual(publish_batch.batch_id(plain.to_dict(), [], chat_id=9),
                         publish_batch.batch_id(with_notes.to_dict(), [], chat_id=9))
        self.assertEqual(publish_batch.batch_id(plain.to_dict(), [], chat_id=9),
                         publish_batch.batch_id(with_evidence.to_dict(), [], chat_id=9))

    def test_images_are_part_of_the_identity(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        small = tmp / "1.jpg"
        small.write_bytes(b"a" * 10)
        base = publish_batch.batch_id(_data().to_dict(), [], chat_id=9)
        small.write_bytes(b"a" * 10)
        one = publish_batch.batch_id(_data().to_dict(), [small], chat_id=9)
        small.write_bytes(b"a" * 40)          # همان نام، حجم تازه
        bigger = publish_batch.batch_id(_data().to_dict(), [small], chat_id=9)
        self.assertNotEqual(base, one, "همان متن با یک عکس تازه، محصول دیگری است")
        self.assertNotEqual(one, bigger, "حجم فایل هم بخشی از هویت است (نام تنها کافی نیست)")
        # فایلِ غایب نباید تست را بترکاند (روی هاست هم ممکن است پاک شده باشد)
        self.assertEqual(12, len(publish_batch.batch_id(_data().to_dict(), [tmp / "nope.jpg"], chat_id=9)))

    def test_two_admins_are_two_intentions(self) -> None:
        """دو ادمین با یک متن، دو محصول می‌خواهند؛ بلاک کردن دومی خودش باگ است."""
        self.assertNotEqual(publish_batch.batch_id(_data().to_dict(), [], chat_id=11),
                            publish_batch.batch_id(_data().to_dict(), [], chat_id=22))


@needs_flow
class TestMeta(unittest.TestCase):
    def test_source_meta_shape(self) -> None:
        meta = publish_batch.source_meta("abc", chat_id=9, thread_id=3, images=2,
                                         variations=4, bot_version="0.9.0")
        self.assertEqual(publish_batch.META_BATCH, meta[0]["key"])
        self.assertEqual("abc", meta[0]["value"])
        source = json.loads(meta[1]["value"])
        for key, want in (("chat_id", 9), ("thread_id", 3), ("images", 2),
                          ("variations", 4), ("bot_version", "0.9.0")):
            self.assertEqual(want, source[key], key)
        self.assertRegex(source["captured_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d")

    def test_meta_batch_of_reads_it_back(self) -> None:
        self.assertEqual("xy", publish_batch.meta_batch_of(
            {"meta_data": [{"key": "tisa_batch_id", "value": "xy"}]}))
        self.assertEqual("", publish_batch.meta_batch_of({"meta_data": [{"key": "other", "value": "xy"}]}))
        self.assertEqual("", publish_batch.meta_batch_of({}))

    def test_it_does_not_copy_the_chat_into_the_shop(self) -> None:
        """شناسه باید قابل‌مقایسه باشد، نه یک متن آزاد از چتِ مالک."""
        meta = publish_batch.source_meta("abc", chat_id=9, bot_version="0.9.0")
        self.assertNotIn("قاب", meta[1]["value"])


@needs_flow
class TestLedgerIntents(unittest.TestCase):
    def setUp(self) -> None:
        stack = contextlib.ExitStack()
        stack.enter_context(temp_ledger())
        self.addCleanup(stack.close)

    def test_pending_then_finished_is_one_card(self) -> None:
        key = products_ledger.new_key(7)
        products_ledger.record(user_id=7, status="pending", title="قاب", key=key, batch_id="b1")
        self.assertEqual("pending", products_ledger.recent(1)[0]["status"])
        products_ledger.update(key, status="created", product_id=99, edit_url="https://x/99")
        entries = products_ledger.recent(5)
        self.assertEqual(1, len(entries), "یک تلاش = یک کارت؛ وگرنه تاریخچه پر از روح می‌شود")
        self.assertEqual("created", entries[0]["status"])
        self.assertEqual(99, entries[0]["product_id"])
        self.assertEqual("b1", entries[0]["batch_id"])

    def test_pending_says_what_it_is(self) -> None:
        products_ledger.record(user_id=7, status="pending", title="قاب نیمه‌کاره", batch_id="b2")
        line = products_ledger.summary(products_ledger.recent(1)[0])
        self.assertIn("⏳", line)
        self.assertIn("نیمه‌کاره", line, "کارت باید بگوید معلوم نیست ساخته شده یا نه")

    def test_find_batch_and_unknowns(self) -> None:
        self.assertIsNone(products_ledger.find_batch("nope"))
        self.assertIsNone(products_ledger.find_batch(""), "بدون شناسه نباید اولین کارت را برگرداند")
        products_ledger.record(user_id=7, status="failed", title="قاب", batch_id="b3", error="HTTP 500")
        self.assertEqual("failed", products_ledger.find_batch("b3")["status"])
        self.assertIsNone(products_ledger.update("missing-key", status="created"))

    def test_latest_attempt_wins(self) -> None:
        products_ledger.record(user_id=7, status="failed", title="قاب", batch_id="b4")
        products_ledger.record(user_id=7, status="created", title="قاب", batch_id="b4", product_id=5)
        self.assertEqual("created", products_ledger.find_batch("b4")["status"],
                         "تاریخچه newest-first است؛ پاسخ باید آخرین تلاش باشد")

    def test_update_can_clear_the_error(self) -> None:
        key = products_ledger.record(user_id=7, status="failed", title="قاب", error="HTTP 500")["key"]
        products_ledger.update(key, status="created", error="", product_id=8)
        entry = products_ledger.find(str(key))
        self.assertEqual("", entry["error"], "اگر بعداً موفق شد، «خطا» باید پاک شود")
        self.assertEqual("created", entry["status"])


@needs_flow
class TestResumeOnTheShop(unittest.IsolatedAsyncioTestCase):
    """مسیر واقعیِ «پیدا کن و تمامش کن»، روی یک فروشگاه ساختگی."""

    def setUp(self) -> None:
        from bot.services.woocommerce_direct import clear_category_cache

        stack = contextlib.ExitStack()
        stack.enter_context(temp_ledger())
        clear_category_cache()
        self.addCleanup(stack.close)

    async def _create(self, store: _Store, batch: str, report: list[str] | None = None) -> tuple[int, str]:
        with patched_settings(settings_with()):
            return await create_draft(_data().to_dict(), [], report=report if report is not None else [],
                                      batch_id=batch, transport=store.transport)

    async def test_first_publish_skips_the_recovery_search_and_sku_preflight(self) -> None:
        from bot.services import sku

        store = _Store(products=[])
        batch = publish_batch.batch_id(_data().to_dict(), [], chat_id=9)
        sku.remember("IP15", 16)
        with patched_settings(settings_with()):
            product_id, _edit_url = await create_draft(
                _data().to_dict(), [], report=[], batch_id=batch,
                resume_existing=False, transport=store.transport,
            )
        self.assertEqual(4321, product_id)
        self.assertEqual([], store.product_searches, "تلاش اول نه جستجوی resume دارد نه GET تأیید SKU")
        self.assertEqual(
            [
                ("GET", "/wp-json/wc/v3/products/categories"),
                ("POST", "/wp-json/wc/v3/products"),
                ("POST", "/wp-json/wc/v3/products/4321/variations/batch"),
            ],
            [(method, path) for method, path, _params, _body in store.requests],
        )

    async def test_half_made_product_is_toppped_up_not_duplicated(self) -> None:
        batch = publish_batch.batch_id(_data().to_dict(), [], chat_id=9)
        store = _Store(products=[_ghost(batch)],
                       variations=[_variation("iPhone 15", "مشکی"), _variation("iPhone 15", "سفید")])
        report: list[str] = []
        product_id, edit_url = await self._create(store, batch, report)
        self.assertEqual(5555, product_id, "id محصولِ موجود برگردانده می‌شود، نه یک id تازه")
        self.assertIn("post.php?post=5555", edit_url)
        self.assertEqual(0, store.count_created_products(), "محصول دومی ساخته نشد")
        self.assertEqual(1, store.count("POST", "variations/batch"))
        items = store.variation_items()
        self.assertEqual(2, len(items), f"فقط دو واریژن جاافتاده باید فرستاده می‌شد: {items}")
        sent = {tuple(sorted({a["name"]: a["option"] for a in v["attributes"]}.items())) for v in items}
        self.assertEqual(
            {(("رنگ", "مشکی"), ("مدل", "S24 Ultra")), (("رنگ", "سفید"), ("مدل", "S24 Ultra"))},
            set(sent), "فقط ترکیب‌هایی که نبودند ساخته می‌شوند")
        joined = "\n".join(report)
        self.assertIn("2 واریژن از تلاش قبلی موجود بود", joined)
        self.assertIn("[resume] جمع‌بندی", joined, "کارت نتیجه به همین خط نگاه می‌کند")
        self.assertEqual(0, store.count("DELETE", "/products"), "محصولِ تلاش قبلی پاک نمی‌شود")

    async def test_no_ghost_publishes_normally_with_the_label(self) -> None:
        batch = publish_batch.batch_id(_data().to_dict(), [], chat_id=9)
        store = _Store(products=[])
        report: list[str] = []
        product_id, _url = await self._create(store, batch, report)
        self.assertEqual(4321, product_id)
        self.assertEqual(1, store.count_created_products())
        sent = json.loads(store.body("POST", "/products"))
        meta = {row["key"]: row["value"] for row in sent["meta_data"]}
        self.assertEqual(batch, meta[publish_batch.META_BATCH],
                         "برچسب تلاش باید روی محصولِ سایت باشد، وگرنه جستجوی بعدی چیزی پیدا نمی‌کند")
        self.assertEqual(4, len(store.variation_items()))
        self.assertNotIn("[resume] جمع‌بندی", "\n".join(report))

    async def test_store_without_the_variations_endpoint_still_finishes(self) -> None:
        batch = publish_batch.batch_id(_data().to_dict(), [], chat_id=9)
        store = _Store(products=[_ghost(batch)], variations_status=404)
        report: list[str] = []
        await self._create(store, batch, report)
        self.assertEqual(4, len(store.variation_items()))
        self.assertIn("endpoint واریژن‌ها", "\n".join(report))

    async def test_unreadable_variations_stop_a_resume_instead_of_doubling(self) -> None:
        """اگر نخوانیم، «بساز همه‌چیز» یعنی هشت واریژن برای چهار رنگِ واقعی."""
        batch = publish_batch.batch_id(_data().to_dict(), [], chat_id=9)
        store = _Store(products=[_ghost(batch)], variations_status=500)
        with patched_settings(settings_with()), self.assertRaises(WooCommerceAPIError) as ctx:
            await create_draft(_data().to_dict(), [], report=[], batch_id=batch,
                               transport=store.transport)
        self.assertIn("خواندن واریژن‌های موجود", str(ctx.exception))
        self.assertEqual(0, store.count("POST", "variations/batch"), "هیچ واریژنی اضافه نشد")
        self.assertEqual(0, store.count("DELETE", "/products"), "محصولِ کسیِ دیگر پاک نشد")

    async def test_search_rejection_falls_back_instead_of_failing(self) -> None:
        """بعضی فروشگاه‌ها status=any را روی products نمی‌پذیرند (HTTP 400)."""
        batch = publish_batch.batch_id(_data().to_dict(), [], chat_id=9)
        store = _Store(products=[], search_status=400)
        await self._create(store, batch)
        searches = store.product_searches
        self.assertGreaterEqual(len(searches), 2, "یک بار با status و یک بار بدون آن امتحان شد")
        self.assertIn("status", searches[0])
        self.assertNotIn("status", searches[1])
        self.assertEqual(1, store.count_created_products(), "انتشار به خاطر جستجو متوقف نشد")

    async def test_rollback_still_happens_for_a_product_we_made(self) -> None:
        """رگرسیون: resume نباید پاک‌سازیِ تلاشِ خودش را بسوزاند."""
        batch = publish_batch.batch_id(_data().to_dict(), [], chat_id=9)
        store = _Store(products=[], batch_status=500, variation_create_status=500)
        with self.assertRaises(WooCommerceAPIError):
            await self._create(store, batch)
        self.assertEqual(1, store.count("DELETE", "/products/4321"), "محصول نیمه‌کارهٔ خودش را پاک می‌کند")
        self.assertEqual(1, sum(1 for m, path, *_ in store.requests if m == "DELETE" and "/products/" in path),
                       "فقط همان یک محصول پاک شد، نه چیز دیگری")

    async def test_dry_run_ignores_the_injected_transport(self) -> None:
        """seam فقط متد تست است؛ اگر dry_run به آن افتاد، دیگر همان fake تضمین‌شده نیست."""
        store = _Store(products=[_ghost("x")])
        with patched_settings(settings_with(woo_dry_run=True)):
            product_id, edit_url = await create_draft(_data().to_dict(), [], dry_run=True,
                                                       report=[], batch_id="x", transport=store.transport)
        self.assertEqual([], store.requests, "در حالت آزمایشی هیچ درخواستی به transport تست نمی‌رود")
        self.assertGreaterEqual(product_id, 800_001)
        self.assertEqual("", edit_url)


@needs_flow
class TestFlowGate(unittest.IsolatedAsyncioTestCase):
    """«تأیید و ساخت» روی محتوای قبلاً‌منتشرشده: سؤال می‌پرسد، حدس نمی‌زند."""

    def setUp(self) -> None:
        for store in (PF.sessions, PF.album_buffers, PF.album_tasks):
            store.clear()
        self._real_sudo = PF.rbac.is_sudo
        PF.rbac.is_sudo = lambda user_id: True
        self._real_extract = PF._extract
        self._real_create_draft = PF.create_draft
        # NOTE: _cleanup() removes session.workspace, so the ledger gets its own
        # directory — putting it in the workspace made the history vanish between
        # taps and the gate silently stopped refusing duplicates.
        self.tmp = Path(tempfile.mkdtemp(prefix="tisa-idem-"))
        self.ledger_dir = Path(tempfile.mkdtemp(prefix="tisa-idem-ledger-"))
        self._ledger_file = products_ledger.FILE
        products_ledger.FILE = self.ledger_dir / "recent_products.json"
        from bot.services import jsonstore

        self._jsonstore = jsonstore
        jsonstore.invalidate()
        self.addCleanup(self._restore)
        self.calls: list[dict] = []

        async def fake_create_draft(data, files, *, dry_run=False, report=None, batch_id="", meta=(), resume_existing=True):
            self.calls.append({"batch_id": batch_id, "meta": meta, "data": data,
                               "resume_existing": resume_existing})
            if report is not None:
                report.append("[product] محصول ساخته شد: id=4321")
            return 4321, "https://shop.example/wp-admin/post.php?post=4321&action=edit"

        PF.create_draft = fake_create_draft
        self.data = _data()
        self._new_session()

    def _restore(self) -> None:
        PF.rbac.is_sudo = self._real_sudo
        PF._extract = self._real_extract
        PF.create_draft = self._real_create_draft
        products_ledger.FILE = self._ledger_file
        self._jsonstore.invalidate()
        for store in (PF.sessions, PF.album_buffers, PF.album_tasks):
            store.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.ledger_dir, ignore_errors=True)
        for directory in getattr(self, "workspaces", []):
            shutil.rmtree(directory, ignore_errors=True)

    def _new_session(self, **over: object) -> PF.ProductSession:
        """A fresh session with a fresh workspace, but the same content.

        The image has the same name and size on purpose: the batch id is derived from
        (payload, images, chat), and a retry after a crash must land on the *same* id.
        """
        workspace = Path(tempfile.mkdtemp(prefix="tisa-idem-ws-"))
        self.workspaces = getattr(self, "workspaces", []) + [workspace]
        self.image = workspace / "01.jpg"
        self.image.write_bytes(b"z" * 64)
        kwargs: dict[str, object] = {
            "mode": "new", "data": _data(), "model_text": "قاب",
            "files": [self.image], "chat_id": 9, "workspace": workspace,
        }
        kwargs.update(over)
        session = PF.ProductSession(**kwargs)  # type: ignore[arg-type]
        PF.sessions[7] = session
        return session

    async def _confirm(self, bot: object | None = None):
        update, seen = query_update("product:confirm", user_id=7, chat_id=9)
        ctx = make_context(bot)  # type: ignore[arg-type]
        with patched_settings(settings_with()):
            result = await PF.confirm(update, ctx)
        return result, seen, ctx

    async def _force(self, bot: object | None = None):
        update, seen = query_update("product:force", user_id=7, chat_id=9)
        ctx = make_context(bot)  # type: ignore[arg-type]
        with patched_settings(settings_with()):
            result = await PF.force_publish(update, ctx)
        return result, seen, ctx

    async def test_second_tap_on_the_same_content_is_refused(self) -> None:
        result, _seen, _ctx = await self._confirm()
        self.assertEqual(PF.ConversationHandler.END, result)
        self.assertEqual(1, len(self.calls))
        self.assertFalse(self.calls[0]["resume_existing"], "تلاش اول نباید GET بازیابی بفرستد")

        messages: list[dict] = []

        class Bot:
            async def send_message(self, *args, text="", **kwargs):
                messages.append({"text": text, **kwargs})

            async def edit_message_text(self, *args, **kwargs):
                return None

        # جریان تازه با همان محتوا — دقیقاً همان چیزی که یک crash به‌جا می‌گذارد
        self._new_session()
        result2, seen2, _ = await self._confirm(Bot())
        self.assertEqual(PF.REVIEW, result2, "جریان باز می‌ماند تا تصمیم مالک روشن شود")
        self.assertEqual(1, len(self.calls), "محصول دوم ساخته نشد")
        text = str(messages[-1]["text"])
        self.assertIn("♻️", text)
        self.assertIn("4321", text, "باید بگوید کدام محصول از قبل هست")
        self.assertIn("post.php?post=4321", text, "و لینکش را بدهد تا همان را باز کند")
        self.assertIn("SKU تازه", text, "باید بگوید تکراری یعنی چه: محصول دوم با SKU دوم")
        self.assertEqual(9, messages[-1].get("chat_id"), "پیام به همان چت، نه چت خصوصی")
        buttons = [b.callback_data for row in messages[-1]["reply_markup"].inline_keyboard for b in row]
        self.assertEqual(["product:force"], buttons)
        answers = [item for item in seen2 if item[0] == "answer"]
        self.assertTrue(answers and "پیش‌تر" in str(answers[-1][1]))

    async def test_the_override_button_publishes_once(self) -> None:
        await self._confirm()
        self._new_session()
        await self._confirm()                       # refused, gate still on
        self.assertEqual(1, len(self.calls))

        session = PF.sessions[7]
        result, _seen, _ctx = await self._force()
        self.assertEqual(PF.ConversationHandler.END, result)
        self.assertEqual(2, len(self.calls), "دکمهٔ «با این حال» باید کار کند، نه اینکه فقط ظاهر شود")
        self.assertFalse(session.force_publish, "پرچم یک‌بارمصرف است؛ تپ بعدی دوباره سؤال می‌پرسد")

        self._new_session()
        await self._confirm()
        self.assertEqual(2, len(self.calls), "تپ سوم بدون دکمهٔ تأیید نباید بسازد")

    async def test_force_without_a_session_closes_the_flow(self) -> None:
        PF.sessions.clear()
        update, seen = query_update("product:force", user_id=7)
        result = await PF.force_publish(update, make_context())
        self.assertEqual(PF.ConversationHandler.END, result)
        self.assertIn("بسته شده", str(seen[0][1]))

    async def test_a_rehearsal_never_blocks_the_real_publish(self) -> None:
        """کارت 🧪 نباید جلوی انتشار واقعی را بگیرد — چیزی ساخته نشده که تکراری باشد."""
        batch = publish_batch.batch_id(self.data.to_dict(), [self.image], chat_id=9)
        products_ledger.record(user_id=7, status="dry", title="قاب گوشی اپل", batch_id=batch)
        result, _seen, _ctx = await self._confirm()
        self.assertEqual(PF.ConversationHandler.END, result)
        self.assertEqual(1, len(self.calls))
        self.assertFalse(self.calls[0]["resume_existing"], "فقط dry-run قبلی هیچ محصول نیمه‌کاره‌ای نمی‌سازد")

    async def test_a_dry_card_does_not_hide_an_earlier_live_failure(self) -> None:
        batch = publish_batch.batch_id(self.data.to_dict(), [self.image], chat_id=9)
        products_ledger.record(
            user_id=7, status="failed", title="قاب گوشی اپل", batch_id=batch, error="timeout"
        )
        products_ledger.record(user_id=7, status="dry", title="قاب گوشی اپل", batch_id=batch)
        result, _seen, _ctx = await self._confirm()
        self.assertEqual(PF.ConversationHandler.END, result)
        self.assertTrue(self.calls[0]["resume_existing"], "هر تلاش واقعیِ قبلی باید قابل بازیابی بماند")

    async def test_a_previous_failure_does_not_block_the_retry(self) -> None:
        """❌ یعنی «انجام نشد»؛ retry باید آزاد باشد، وگرنه دروازه به بن‌بست تبدیل می‌شود."""
        batch = publish_batch.batch_id(self.data.to_dict(), [self.image], chat_id=9)
        products_ledger.record(user_id=7, status="failed", title="قاب گوشی اپل", batch_id=batch, error="HTTP 500")
        result, _seen, _ctx = await self._confirm()
        self.assertEqual(PF.ConversationHandler.END, result)
        self.assertEqual(1, len(self.calls))
        self.assertTrue(self.calls[0]["resume_existing"], "تلاش ناموفق می‌تواند محصول نیمه‌کاره داشته باشد")

    async def test_one_card_per_attempt_even_when_it_fails(self) -> None:
        """کارت ⏳ باید به ❌ تبدیل شود، نه اینکه یک کارت دوم اضافه شود.

        ۴۰۰ انتخاب شده چون «ایستا» است: صفِ تلاش مجدد فقط برای خطای موقتی است (طرح ۴.۸) و
        یک ۴۰۰ فردا هم همان جواب را می‌دهد. کارتِ ❌ و ❌ نبودنِ صف، هر دو همین‌جا آزموده
        می‌شود؛ نسخهٔ ۵۰۰ در ``test_outbox.py`` است.
        """
        async def failing_create_draft(data, files, *, report=None, **kwargs):
            raise WooCommerceAPIError(400, "تصویر مجاز نیست")

        PF.create_draft = failing_create_draft
        await self._confirm()
        entries = products_ledger.recent(5)
        self.assertEqual(1, len(entries), f"یک تلاش باید یک کارت باشد: {[e['status'] for e in entries]}")
        self.assertEqual("failed", entries[0]["status"])
        self.assertIn("HTTP 400", entries[0]["error"])
        self.assertEqual(12, len(str(entries[0]["batch_id"])))
        from bot.services import outbox as queue

        self.assertEqual(0, queue.pending(), "خطای تکراری نباید در صف بماند")

    async def test_the_card_is_pending_while_publishing(self) -> None:
        """بین «تأیید» و جواب، تاریخچه باید بداند چیزی در جریان است."""
        seen: list[str] = []

        async def slow_create_draft(data, files, *, report=None, **kwargs):
            seen.append(products_ledger.recent(1)[0]["status"])
            return 4321, "https://shop/x"

        PF.create_draft = slow_create_draft
        await self._confirm()
        self.assertEqual(["pending"], seen, "وسط کار کارت باید pending باشد (همین چیزی که crash پیدا می‌کند)")

    async def test_batch_is_written_on_the_product_and_in_the_history(self) -> None:
        await self._confirm()
        entry = products_ledger.recent(1)[0]
        meta = {row["key"]: row["value"] for row in self.calls[0]["meta"]}
        self.assertEqual(entry["batch_id"], meta[publish_batch.META_BATCH])
        self.assertRegex(entry["batch_id"], r"^[0-9a-f]{12}$")
        self.assertEqual(4, json.loads(meta[publish_batch.META_SOURCE])["variations"])
        self.assertEqual(9, json.loads(meta[publish_batch.META_SOURCE])["chat_id"])

    async def test_the_override_button_is_a_real_conversation_handler(self) -> None:
        """دکمه‌ای که هندلرش در مکالمه نیست، فقط یک متن کلیک‌نشدنی است.

        همین درس را فاز ۳ گرفت (product:next اول یک هندلر معمولی بود و هیچ
        مکالمه‌ای به آن نمی‌رسید)، پس برای هر دکمهٔ تازه همین بررسی تکرار می‌شود.
        """
        from _flow_harness import conversation_patterns

        wired = conversation_patterns()
        self.assertTrue(any("product:force" in pattern for pattern in wired),
                        "product:force باید هندلرِ ConversationHandler باشد")


if __name__ == "__main__":
    unittest.main()
