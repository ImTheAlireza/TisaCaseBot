"""فاز ۴ (۵ و ۶): موجودی، قیمت ویژه، تصویر هر رنگ و ترتیب واریژن‌ها.

سه چیز که قبل از این نبود و هر سه «ساکت» خراب می‌شدند:

* **موجودی**: عدد موجودی یا اصلاً ارسال نمی‌شد، یا (بدتر) از هر عددی در کپشن ساخته
  می‌شد. قانون اینجاست: عددی «موجودی» است که *صدا* زده باشد — برچسب «موجودی ۲۰» یا
  پسوند «۲۰ عدد». وزن، تاریخ و مدل عددِ موجودی نیستند.
* **قیمت ویژه**: اگر ارسال نشود، تخفیف فروشنده در سایت نیست؛ اگر به‌جای قیمت اصلی
  نوشته شود، قیمت اصلی برای همیشه می‌سوزد. پس sale_price اضافه می‌شود، نه جانشین.
* **تصویر و ترتیب هر واریژن**: رنگی که تصویر ندارد در فروشگاه سفید می‌ماند، و
  واریژن‌هایی که بی‌ترتیب ساخته شوند با «اولین رنگی که دیدم» قاطی می‌شوند.

اجرا: ``python3 -m unittest discover -s tests``
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("SUDO_IDS", "1234567")

from _flow_harness import FakeStore, patched_settings, settings_with, temp_ledger, write_image

try:
    from bot.services import draft_edits, products_ledger
    from bot.services.product_extractor import ProductData, _fallback, scan_stock_and_sale
    from bot.services.validation import validate_draft

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


def _scan(*lines: str) -> dict:
    return scan_stock_and_sale(list(lines))


@needs_flow
class TestScanStock(unittest.TestCase):
    """عدد موجودی باید *خوانده* شود، نه حدس زده."""

    def test_labelled_stock(self) -> None:
        self.assertEqual(20, _scan("قیمت 698000", "موجودی ۲۰")["stock"])

    def test_stock_with_a_label_and_a_colon_and_latin_digits(self) -> None:
        self.assertEqual(7, _scan("موجودی: 7")["stock"])

    def test_count_suffix_is_stock_even_without_a_label(self) -> None:
        self.assertEqual(15, _scan("۱۵ عدد")["stock"])

    def test_a_bare_number_is_not_stock(self) -> None:
        # «۲۰» تنها در کپشن می‌تواند مدل، وزن، تاریخ یا تعداد روز گارانتی باشد.
        self.assertIsNone(_scan("۲۰")["stock"])
        self.assertIsNone(_scan("وزن 250 گرم")["stock"])
        self.assertIsNone(_scan("تاریخ 1403/01/01")["stock"])

    def test_zero_is_not_a_trusted_reading(self) -> None:
        # «صفر عدد» یا شمارهٔ ردیف؟ موجودی صفر را فقط از خودِ عددِ صریح نمی‌سازیم.
        self.assertIsNone(_scan("۰ عدد")["stock"])

    def test_out_of_stock_sets_the_status_not_a_zero_number(self) -> None:
        result = _scan("این رنگ تمام شده است")
        self.assertEqual("outofstock", result["stock_status"])
        self.assertIsNone(result["stock"], "«ناموجود» یعنی «تعداد را نمی‌دانیم»، نه صفر")

    def test_presale_reads_as_backorder(self) -> None:
        self.assertEqual("onbackorder", _scan("پیش‌فروش — ارسال از هفته آینده")["stock_status"])

    def test_stock_with_the_number_on_the_next_line(self) -> None:
        self.assertEqual(12, _scan("موجودی:", "۱۲")["stock"])

    def test_sale_price_from_its_own_label(self) -> None:
        self.assertEqual(498000, _scan("قیمت ویژه 498000")["sale_price"])

    def test_sale_price_in_toman_shorthand(self) -> None:
        self.assertEqual(498000, _scan("قیمت فروش ویژه: 498t")["sale_price"])

    def test_a_price_line_never_becomes_a_sale_price(self) -> None:
        self.assertEqual(0, _scan("قیمت 698000")["sale_price"])

    def test_quotes_are_kept_for_the_evidence_card(self) -> None:
        result = _scan("موجودی ۲۰")
        self.assertIn("موجودی", result["stock_quote"])


@needs_flow
class TestFallbackCarriesTheNumbers(unittest.TestCase):
    """مقادیر باید تا روی ProductData و کارتِ «از کجا آمد» پیش بروند."""

    TEXT = "\n".join([
        "قاب سیلیکونی آیفون 13",
        "قیمت 698000",
        "قیمت ویژه 498000",
        "موجودی ۲۰",
    ])

    def test_fields_and_evidence(self) -> None:
        data = _fallback(self.TEXT, ["iPhone 13"])
        self.assertEqual(20, data.stock)
        self.assertEqual(498000, data.sale_price)
        from bot.services import postmodel as proof

        self.assertEqual(proof.CAPTION, str(getattr(data.evidence.get("stock"), "source", "")))
        self.assertEqual(proof.CAPTION, str(getattr(data.evidence.get("sale_price"), "source", "")))

    def test_sale_price_not_cheaper_is_said_out_loud(self) -> None:
        data = _fallback("قاب سیلیکونی\nقیمت 100000\nقیمت ویژه 498000", [])
        self.assertTrue(any("کمتر نیست" in note for note in data.notes),
                        f"یک تخفیف بی‌معنی باید گفته شود: {data.notes}")

    def test_silence_stays_silence(self) -> None:
        data = _fallback("قاب سیلیکونی آیفون 13\nقیمت 698000", ["iPhone 13"])
        self.assertIsNone(data.stock)
        self.assertEqual("", data.stock_status)
        self.assertEqual(0, data.sale_price)


@needs_flow
class TestValidation(unittest.TestCase):
    """درِ انتشار: عددِ بی‌سر‌و‌ته نباید به فروشگاه برسد."""

    def _report(self, **over: object):
        data = {"title": "قاب سیلیکونی آیفون 13", "price": 698000, "models": ["iPhone 13"],
                "sku_prefix": "BO", "images": ["a.jpg"], **over}
        return validate_draft(data, mode="new", image_count=1, require_models=False)

    def test_clean_numbers_pass(self) -> None:
        self.assertFalse(self._report(stock=20, sale_price=498000).blocking)

    def test_negative_stock_is_blocked(self) -> None:
        report = self._report(stock=-5)
        self.assertIn("E_STOCK_NEGATIVE", [issue.code for issue in report.errors])

    def test_absurd_stock_is_only_a_warning(self) -> None:
        report = self._report(stock=2_000_000)
        self.assertFalse(report.blocking, "غیرعادی بودن، دروغ بودن نیست")
        self.assertIn("W_STOCK_HUGE", [issue.code for issue in report.warnings])

    def test_unknown_status_is_blocked_because_the_shop_would_reject_it(self) -> None:
        report = self._report(stock_status="maybe")
        self.assertIn("E_STOCK_STATUS", [issue.code for issue in report.errors])

    def test_sale_must_be_below_the_price(self) -> None:
        report = self._report(sale_price=698000)
        codes = [issue.code for issue in report.errors]
        self.assertIn("E_SALE_NOT_CHEAPER", codes)

    def test_sale_below_the_base_but_above_a_group_price_is_still_blocked(self) -> None:
        report = self._report(prices={"iphone": 500000}, sale_price=600000)
        errors = [issue for issue in report.errors if issue.code == "E_SALE_NOT_CHEAPER"]
        self.assertEqual(1, len(errors))
        self.assertIn("iphone", errors[0].message, "باید بگوید کدام گروه")

    def test_sale_out_of_the_accepted_range_is_named_as_sale(self) -> None:
        report = self._report(sale_price=10)
        messages = " ".join(issue.message for issue in report.errors)
        self.assertIn("قیمت ویژه", messages)

    def test_out_of_stock_with_a_number_is_a_contradiction(self) -> None:
        report = self._report(stock=30, stock_status="outofstock")
        self.assertIn("W_STOCK_CONTRADICTION", [issue.code for issue in report.warnings])


@needs_flow
class TestManualEditing(unittest.TestCase):
    """«✏️ ویرایش» باید این دو فیلد را هم بشناسد، ویرایش هم زنده بماند."""

    def _data(self) -> ProductData:
        return ProductData(title="قاب سیلیکونی آیفون 13", price=698000, models=["iPhone 13"])

    def test_the_two_fields_are_offered_in_a_useful_order(self) -> None:
        keys = [key for key, _label, _hint in draft_edits.editable_fields(self._data())]
        self.assertIn("sale_price", keys)
        self.assertIn("stock", keys)
        self.assertLess(keys.index("price"), keys.index("sale_price"), "کنار هم ویرایش می‌شوند")
        self.assertLess(keys.index("sale_price"), keys.index("prices"))
        self.assertLess(keys.index("prices"), keys.index("stock"))

    def test_parsers(self) -> None:
        self.assertEqual(498000, draft_edits.parse_sale_price("قیمت ویژه 498"))
        self.assertEqual(0, draft_edits.parse_sale_price("حذف"))
        self.assertEqual(20, draft_edits.parse_stock("موجودی ۲۰ عدد"))
        self.assertIsNone(draft_edits.parse_stock("حذف"), "«حذف» یعنی هیچ موجودی‌ای ارسال نشود")
        with self.assertRaises(ValueError):
            draft_edits.parse_stock("هیچ عددی اینجا نیست")
        with self.assertRaises(ValueError):
            draft_edits.parse_sale_price("ویژه")

    def test_the_prompt_says_where_the_number_lands(self) -> None:
        variable = self._data()
        variable.models = ["iPhone 15", "S24 Ultra"]
        variable.attributes = {"رنگ": ["مشکی", "سفید"]}
        self.assertIn("روی هر 4 واریژن", draft_edits.prompt_for("stock", variable))
        simple = self._data()
        self.assertIn("محصول ساده", draft_edits.prompt_for("stock", simple))
        self.assertNotIn("واریژن", draft_edits.prompt_for("title", simple))

    def test_apply_edit_writes_value_and_display(self) -> None:
        data = self._data()
        self.assertIsNone(draft_edits.apply_edit(data, "stock", "۲۰"))
        self.assertEqual(20, data.stock)
        self.assertIn("20", draft_edits.display_value(data, "stock"))
        self.assertIsNone(draft_edits.apply_edit(data, "sale_price", "498"))
        self.assertEqual(498000, data.sale_price)
        self.assertIn("498,000", draft_edits.display_value(data, "sale_price"))

    def test_the_preview_does_not_repeat_a_private_note(self) -> None:
        data = self._data()
        draft_edits.apply_edit(data, "stock", "۲۰")
        self.assertEqual([], [n for n in data.notes if "دستی نوشتی" in n],
                         "«دستی نوشتی» در خودِ مقدار دیده می‌شود؛ دو بار گفتن یعنی شلوغی")

    def test_an_edit_survives_the_next_extraction(self) -> None:
        data = self._data()
        draft_edits.apply_edit(data, "stock", "۵")
        fresh = _fallback("قاب سیلیکونی آیفون 13\nقیمت 698000\nموجودی ۹۰", ["iPhone 13"])
        fresh.user_edits = dict(data.user_edits)
        draft_edits.apply_locks(fresh)
        self.assertEqual(5, fresh.stock, "تعدادی که مدیر گفت از استخراج بعدی رد نمی‌شود")
        self.assertEqual(5, _fallback_stock_after_lock(fresh))

    def test_removing_stock_means_sending_nothing(self) -> None:
        data = self._data()
        draft_edits.apply_edit(data, "stock", "۲۰")
        draft_edits.apply_edit(data, "stock", "حذف")
        self.assertIsNone(data.stock)
        self.assertNotIn("stock_quantity", json.dumps(data.to_dict()), "هیچ عددی نباید بماند")


def _fallback_stock_after_lock(data: ProductData) -> int | None:
    return data.stock


@needs_flow
class TestLedgerKnowsTheNumbers(unittest.TestCase):
    """کارت نتیجه فقط چیزی را می‌گوید که واقعاً رفته — پس باید ذخیره‌اش کند."""

    def test_recorded_values(self) -> None:
        with temp_ledger():
            entry = products_ledger.record(
                user_id=7, title="قاب", sale_price=498000, stock=20, stock_status="outofstock",
            )
            self.assertEqual(498000, entry["sale_price"])
            self.assertEqual(20, entry["stock"])
            self.assertEqual("outofstock", entry["stock_status"])
            self.assertEqual(20, products_ledger.recent(1)[0]["stock"])

    def test_absent_stock_is_not_zero(self) -> None:
        with temp_ledger():
            entry = products_ledger.record(user_id=7, title="قاب")
            self.assertIsNone(entry["stock"], "«متن چیزی نگفت» با «صفر تا» یکی نیست")
            self.assertEqual(0, entry["sale_price"])


def _draft(**over: object) -> ProductData:
    kwargs: dict[str, object] = {
        "title": "قاب گوشی اپل",
        "price": 100_000,
        "sku_prefix": "IP15",
        "models": ["iPhone 15", "S24 Ultra"],
        "attributes": {"رنگ": ["مشکی", "سفید"]},
        "stock": 20,
        "sale_price": 49_000,
    }
    kwargs.update(over)
    return ProductData(**kwargs)  # type: ignore[arg-type]


@needs_flow
class TestWhatGoesToTheShop(unittest.IsolatedAsyncioTestCase):
    """بدنهٔ درخواست‌ها: دقیقاً همان عددی که در پیش‌نمایش وعاده داده شد."""

    def setUp(self) -> None:
        self._settings = patched_settings(settings_with(**SHARED))
        self._settings.__enter__()
        self.tmp = Path(tempfile.mkdtemp(prefix="tisa-stock-sale-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(self._settings.__exit__, None, None, None)

    async def _publish(self, data: ProductData, images: list[Path] | None = None):
        store = FakeStore()
        report: list[str] = []
        with temp_ledger():
            await __import__("bot.services.woocommerce_direct", fromlist=["create_draft"]).create_draft(
                data.to_dict(), images or [], report=report, transport=store.transport,
            )
        return store, report

    async def test_simple_product_gets_the_numbers_itself(self) -> None:
        data = _draft(models=[], attributes={}, stock=20, sale_price=49_000)
        store, _report = await self._publish(data)
        body = store.product_create_body()
        self.assertEqual("49000", body["sale_price"], "قیمت اصلی نباید عوض شود")
        self.assertEqual("100000", body["regular_price"])
        self.assertIs(True, body["manage_stock"])
        self.assertEqual(20, body["stock_quantity"])
        self.assertEqual("instock", body["stock_status"])

    async def test_out_of_stock_sends_the_status_without_inventing_a_number(self) -> None:
        data = _draft(models=[], attributes={}, stock=None, stock_status="outofstock")
        store, _report = await self._publish(data)
        body = store.product_create_body()
        self.assertEqual("outofstock", body["stock_status"])
        self.assertNotIn("stock_quantity", body, "«نمی‌دانیم» نباید صفر شود")

    async def test_variable_product_puts_stock_on_every_variation(self) -> None:
        data = _draft()
        store, report = await self._publish(data)
        product = store.product_create_body()
        self.assertNotIn("stock_quantity", product, "محصول متغیر موجودیِ خودش را ندارد")
        self.assertEqual("variable", product["type"])
        variations = store.variation_items()
        self.assertEqual(4, len(variations), f"۲ مدل × ۲ رنگ: {len(variations)}")
        for variation in variations:
            self.assertEqual(20, variation["stock_quantity"])
            self.assertIs(True, variation["manage_stock"])
            self.assertEqual("49000", variation["sale_price"])
            self.assertEqual("instock", variation["stock_status"])
        self.assertTrue(any("موجودی 20" in line for line in report),
                        "کارت باید بگوید عدد روی چند واریژن نوشته می‌شود")

    async def test_variations_keep_the_order_the_seller_listed(self) -> None:
        store, _report = await self._publish(_draft())
        self.assertEqual([0, 1, 2, 3], [v["menu_order"] for v in store.variation_items()])
        self.assertTrue(all(v["visible"] is True for v in store.variation_items()))

    async def test_a_picture_named_after_a_color_is_attached_to_that_color(self) -> None:
        images = [
            write_image(self.tmp, "01_مشکی.jpg"),
            write_image(self.tmp, "02_سفید.jpg"),
            write_image(self.tmp, "03_عمومی.jpg"),
        ]
        store, report = await self._publish(_draft(), images)
        by_color = {v["attributes"][1]["option"]: v for v in store.variation_items()}
        self.assertEqual(900_001, by_color["مشکی"]["image"]["id"])
        self.assertEqual(900_002, by_color["سفید"]["image"]["id"])
        for variation in store.variation_items():
            self.assertNotIn("general", json.dumps(variation.get("image", {}), ensure_ascii=False))
        self.assertIn("2 از 2 رنگ تصویر هم‌نام داشت", "\n".join(report))
        self.assertEqual(3, store.count("POST", "/media"), "هر سه تصویر برای گالری محصول می‌روند")

    async def test_a_color_without_its_picture_says_so_instead_of_guessing(self) -> None:
        images = [write_image(self.tmp, "01_عکس_محصول.jpg")]
        store, report = await self._publish(_draft(), images)
        variations = store.variation_items()
        self.assertTrue(all("image" not in v for v in variations))
        joined = "\n".join(report)
        self.assertNotIn("تصویر رنگ:", joined, "وقتی هیچ رنگی تصویر ندارد نباید ادعا شد")

    def test_the_zip_carries_the_same_facts(self) -> None:
        # دو شکل برای یک محصول یعنی دو فروشگاهِ متفاوت؛ پس product.json هم همین اعداد را
        # می‌برد (افزونهٔ فعلی stock را نادیده می‌گیرد — docs/IMPORTER-CONTRACT.md می‌گوید کدام
        # کلیدها خوانده می‌شوند، ولی نبودنِ کلید یعنی ویژگی‌ای که فقط یک مسیر دارد).
        from bot.modules.product_flow import _zip_manifest

        data = _draft()
        manifest = _zip_manifest(data, usable_attributes={}, image_mode="keep", batch="abc123")
        self.assertEqual(20, manifest["stock"])
        self.assertEqual(49_000, manifest["sale_price"])
        self.assertEqual("", manifest["stock_status"])
        self.assertEqual("simple", manifest["product_type"])
        self.assertIn('"stock": 20', json.dumps(manifest, ensure_ascii=False))


@needs_flow
@needs_flow
class TestWorkspaceKeepsTheName(unittest.TestCase):
    """«اسم فایل = رنگ» تنها وقتی معنا دارد که اسمِ فایل در فضای کاری سالم بماند.

    اگر نام‌گذاری، حروف فارسی را به ``_`` تبدیل کند، نه تصویری به رنگ می‌رسد و نه چیزی در
    گزارش — پس این قاعده باید آزموده شود، نه فرض.
    """

    def test_persian_letters_survive(self) -> None:
        from bot.modules.product_flow import _safe

        self.assertEqual("01_مشکی.jpg", _safe("01_مشکی.jpg", "image_1.jpg"))
        self.assertEqual("عکس.jpeg", _safe("عکس.jpeg", "image_1.jpg"))

    def test_a_path_and_a_blank_name_still_come_out_safe(self) -> None:
        from bot.modules.product_flow import _safe

        self.assertEqual("passwd.jpg", _safe("../../etc/passwd.jpg", "image_1.jpg"))
        self.assertEqual("image_1.jpg", _safe("   ", "image_1.jpg"))
        self.assertEqual(84, len(_safe("ا" * 200 + ".jpg", "image_1.jpg")), "سقف طول، نه کوتاه‌کردن بی‌قاعده")

    def test_a_persian_name_does_not_break_the_upload_header(self) -> None:
        # httpx headers are ASCII: a raw Persian filename used to raise UnicodeEncodeError in
        # the middle of publishing, and the red card said nothing about why.
        import asyncio

        from _flow_harness import TransportScript, respond
        from bot.services import woocommerce_direct as writer
        from bot.services.woo_client import Audit, WooClient

        tmp = Path(tempfile.mkdtemp(prefix="tisa-name-"))
        try:
            path = write_image(tmp, "01_مشکی.jpg")
            script = TransportScript(respond(201, {"id": 900_001}))
            with patched_settings(settings_with(**SHARED)):
                async def go() -> int:
                    async with WooClient(audit=Audit(), transport=script.transport()) as client:
                        return await writer._upload_media(client, path, Audit())
                media_id = asyncio.run(go())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(900_001, media_id)
        header = script.requests[0].headers["content-disposition"]
        self.assertIn('filename="01_.jpg"', header, "نامِ ASCII برای سرویس‌هایی که RFC 5987 را نمی‌فهمند")
        self.assertIn("filename*=UTF-8''", header, "و نام اصلیِ درصدکدگذاری‌شده، تا اسم رنگ گم نشود")


@needs_flow
class TestCategoryLookupReuse(unittest.TestCase):
    def test_shared_parent_category_is_looked_up_once_per_publish(self) -> None:
        import asyncio
        import httpx

        from bot.services.woocommerce_direct import _resolve_categories, clear_category_cache
        from bot.services.woo_client import Audit

        clear_category_cache()
        parent = "قاب و کاور گوشی و تبلت"
        catalog = {
            parent: [{"id": 1, "name": parent, "parent": 0}],
            "آیفون iphone": [{"id": 2, "name": "آیفون iphone", "parent": 1}],
            "سامسونگ samsung": [{"id": 3, "name": "سامسونگ samsung", "parent": 1}],
        }

        class Client:
            def __init__(self, *, dry_run: bool = False) -> None:
                self.dry_run = dry_run
                self.searched: list[str] = []

            async def get(self, _url, *, params):
                name = str(params["search"])
                self.searched.append(name)
                return httpx.Response(200, json=catalog.get(name, []))

        client = Client()
        result = asyncio.run(_resolve_categories(
            client,
            "https://shop.example/wp-json/wc/v3/products",
            [f"{parent} > آیفون iphone", f"{parent} > سامسونگ samsung"],
            Audit(),
        ))
        self.assertEqual([parent, "آیفون iphone", "سامسونگ samsung"], client.searched)
        self.assertEqual([{"id": 1}, {"id": 2}, {"id": 3}], result)

        again = asyncio.run(_resolve_categories(
            client,
            "https://shop.example/wp-json/wc/v3/products",
            [f"{parent} > آیفون iphone", f"{parent} > سامسونگ samsung"],
            Audit(),
        ))
        self.assertEqual(result, again)
        self.assertEqual(3, len(client.searched), "انتشار بعدی دسته‌های یکسان را دوباره نمی‌خواند")

        clear_category_cache()
        dry_client = Client(dry_run=True)
        for _ in range(2):
            asyncio.run(_resolve_categories(
                dry_client,
                "https://shop.example/wp-json/wc/v3/products",
                [f"{parent} > آیفون iphone"],
                Audit(),
            ))
        self.assertEqual(4, len(dry_client.searched), "شناسه‌های ساختگی dry-run نباید cache شوند")


@needs_flow
class TestProductPreview(unittest.TestCase):
    """پیش‌نمایش فقط فیلدهای اصلی را با مدل کامل و دسته‌بندی درختی نشان می‌دهد."""

    def _preview(self, data: ProductData) -> str:
        from bot.modules import product_flow as PF

        session = PF.ProductSession(data=data, chat_id=9)
        return PF._preview(session)

    def test_preview_uses_the_requested_essential_fields(self) -> None:
        text = self._preview(_draft())
        self.assertIn("<b>عنوان:</b> قاب گوشی اپل", text)
        self.assertIn("<b>قیمت:</b> 100,000 تومان", text)
        self.assertIn("<b>شناسه:</b> IP15", text)
        self.assertIn("<b>مدل:</b> iPhone 15 | S24 Ultra", text)
        self.assertIn("<b>رنگ:</b> مشکی | سفید", text)
        self.assertIn("<b>نوع محصول:</b> متغیر", text)
        self.assertIn("<b>موجودی:</b> 20 عدد", text)
        self.assertIn("<b>قیمت ویژه:</b> 49,000 تومان", text)
        self.assertNotIn("واریژن", text)

    def test_empty_stock_and_sale_are_omitted(self) -> None:
        text = self._preview(_draft(stock=None, sale_price=0, stock_status=""))
        self.assertNotIn("موجودی:", text)
        self.assertNotIn("قیمت ویژه:", text)

    def test_out_of_stock_status_is_kept(self) -> None:
        text = self._preview(_draft(stock=None, stock_status="outofstock", sale_price=0))
        self.assertIn("<b>موجودی:</b> ناموجود", text)

    def test_simple_product_says_simple(self) -> None:
        text = self._preview(_draft(models=[], attributes={}))
        self.assertIn("<b>نوع محصول:</b> ساده", text)

    def test_model_list_is_not_truncated(self) -> None:
        models = [f"iPhone {index}" for index in range(36)]
        text = self._preview(_draft(models=models, attributes={"رنگ": ["مشکی", "سفید"]}))
        self.assertIn("iPhone 35", text)
        self.assertNotIn("… +", text)

    def test_categories_are_nested_and_parent_is_not_repeated(self) -> None:
        parent = "قاب و کاور گوشی و تبلت"
        text = self._preview(_draft(categories=[
            f"{parent} > آیفون iphone",
            f"{parent} > سامسونگ samsung",
            "چاپی",
        ]))
        self.assertEqual(1, text.count(parent))
        self.assertIn(f"- {parent}", text)
        self.assertIn("  - آیفون iphone", text)
        self.assertIn("  - سامسونگ samsung", text)
        self.assertIn("  - چاپی", text)

    def test_nonessential_parser_notes_do_not_clutter_preview(self) -> None:
        data = _draft(
            models=["iPhone 13"],
            notes=["برند «PROMAX» در کاتالوگ ربات نیست؛ ممکن است مدلی از جا بماند"],
        )
        text = self._preview(data)
        self.assertNotIn("PROMAX", text)
        self.assertNotIn("حدسی", text)


@needs_flow
class TestTheAiPathDoesNotOverrideATypedNumber(unittest.TestCase):
    """متنی که مدیر خودش نوشته از حدسِ AI محکم‌تر است — در هر دو مسیر."""

    TEXT = "\n".join(["قاب سیلیکونی آیفون 13", "قیمت 698000", "موجودی ۲۰", "قیمت ویژه 498000"])

    def test_local_reading_is_used_when_ai_agrees(self) -> None:
        data = _fallback(self.TEXT, ["iPhone 13"])
        self.assertEqual(20, data.stock)
        self.assertEqual(498000, data.sale_price)

    def test_ai_merge_helper_keeps_the_local_value(self) -> None:
        # مسیر AI با _merge_stock_and_sale همان قاعده را دارد؛ اینجا مستقیم آزموده می‌شود
        # تا «متن صریح محکم‌تر از حدس است» به یک تستِ شبکهٔ ساختگی بند نشود.
        from bot.services.product_extractor import _merge_stock_and_sale

        fallback = ProductData(title="t", stock=20, sale_price=498000, stock_status="outofstock")
        merged = _merge_stock_and_sale(fallback, {"stock": 3, "sale_price": 1, "stock_status": "instock"})
        self.assertEqual(20, merged["stock"])
        self.assertEqual(498000, merged["sale_price"])
        self.assertEqual("outofstock", merged["stock_status"])

    def test_ai_value_is_used_but_flagged_as_unverified(self) -> None:
        from bot.services.product_extractor import _merge_stock_and_sale

        fallback = ProductData(title="t")
        merged = _merge_stock_and_sale(fallback, {"stock": 3, "sale_price": 0, "stock_status": "outofstock"})
        self.assertEqual(3, merged["stock"])
        self.assertEqual("outofstock", merged["stock_status"])
        self.assertTrue(merged["ai_used"], "باید گفته شود این عدد را آدمک خوانده، نه متن")


@needs_flow
class TestSaleAndStatusLinesAreNotData(unittest.TestCase):
    """فاز ۹ (corpus): دو خطی که پارسرِ قیمت/عنوان آن‌ها را با هم قاطی می‌کرد.

    هر دو از همین‌جا می‌آیند که قاعده «آخرین مقدارِ اعلام‌شده برنده است» بدون نگاه به
    *نوع* خط کار می‌کرد: «قیمت ویژه ۴۲۰٬۰۰۰» یک قیمت اعلام‌شده بود و قیمت اصلی را
    عوض می‌کرد، و «ناموجود» یک خطِ prose بود و نام محصول می‌شد.
    """

    def test_a_sale_line_never_replaces_the_regular_price(self) -> None:
        data = _fallback("قاب ضدضربه\nقیمت 500000\nقیمت ویژه 420000", [])
        self.assertEqual(500_000, data.price, "تخفیف جای قیمت اصلی را گرفت؛ والد ۴۲۰٬۰۰۰ می‌شد")
        self.assertEqual(420_000, data.sale_price, "قیمت ویژه هم گم شد")

    def test_the_order_of_the_two_lines_does_not_matter(self) -> None:
        data = _fallback("قاب ضدضربه\nقیمت فروش ویژه: 420000\nقیمت 500000", [])
        self.assertEqual(500_000, data.price)
        self.assertEqual(420_000, data.sale_price)

    def test_a_sale_line_does_not_steal_a_group_price_either(self) -> None:
        data = _fallback("قاب\nقیمت ایفون 698 سامسونگ 598\nقیمت ویژه 420ت", ["iPhone 15"])
        self.assertEqual(698_000, data.price)
        self.assertEqual({"android": 598_000, "iphone": 698_000}, data.prices)
        self.assertEqual(420_000, data.sale_price)

    def test_a_sale_price_that_is_not_cheaper_is_still_said_out_loud(self) -> None:
        # این تست قبل از اصلاح هم سبز بود؛ نگهش می‌داریم تا «تخفیفِ بی‌معنی» با
        # «قیمتِ دزدیده‌شده» اشتباه گرفته نشود.
        data = _fallback("قاب سیلیکونی\nقیمت 100000\nقیمت ویژه 498000", [])
        self.assertEqual(100_000, data.price)
        self.assertTrue(any("کمتر نیست" in note for note in data.notes), str(data.notes))

    def test_an_availability_line_is_never_the_product_title(self) -> None:
        data = _fallback("MT\nناموجود\nقیمت 320000", ["iPhone 14"])
        self.assertNotEqual("ناموجود", data.title)
        self.assertEqual("", data.title, "هیچ خط توصیفی نبود؛ باید خالی بماند و بپرسد")
        self.assertEqual("outofstock", data.stock_status, "وضعیت باز هم خوانده شود")

    def test_availability_words_do_not_eclipse_a_real_title(self) -> None:
        for line in ("ناموجود", "⛔ تمام شده", "پیش‌فروش"):
            with self.subTest(line=line):
                data = _fallback(f"قاب مات آیفون 14\n{line}\nقیمت 320000", ["iPhone 14"])
                self.assertEqual("قاب مات آیفون 14", data.title)


if __name__ == "__main__":
    unittest.main()
