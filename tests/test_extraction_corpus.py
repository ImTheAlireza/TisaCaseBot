"""فاز ۹: corpusِ رگرسیونِ استخراج — پیام‌های واقعی، خروجیِ کامل، مقایسهٔ کامل.

اتفاقی که فازهای ۲ تا ۴ افتاد این بود: هر تست یک *برش* را می‌گرفت (فقط قیمت، فقط
موجودی) و قاعدهٔ دیگری سرِ جایش خراب می‌شد. corpus یعنی عکسِ آن: پستِ کامل بده،
**همهٔ فیلدها** را با هم انتظار کن. هر تغییر در مسیر استخراج که یکی از این خانه‌ها را
تکان بدهد، این‌جا باید آگاهانه به‌روز شود — همان چیزی که در DoDِ هر PR آمده:
«برای تغییر مسیر استخراج، corpus را اجرا کن». (اجرای corpus در `python main.py`
نیست: این فایل خودِ replay است، در CI، بدون AI و بدون شبکه.)

خروجیِ هر مورد با `extract_product` حساب می‌شود — همان تابعی که `product_flow` صدا
می‌زند، فقط بدون درخواست AI (هیچ `AI_*` در محیط تست تنظیم نیست، پس مسیر deterministic
تنها مسیر است). `models` هم همان‌طور که فلو می‌سازد محاسبه می‌شود: کاتالوگِ آیفون/سامسونگ/
شیائومی + خانوادۀ لوازم جانبی.
"""

from __future__ import annotations

import asyncio
import json
import os
import unittest
from dataclasses import replace
from unittest.mock import patch

os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("SUDO_IDS", "1234567")

try:
    from bot.services import plan, phone_parser, product_extractor
    from bot.services.product_extractor import extract_accessory_models, extract_product

    HAS_EXTRACTOR = True
except Exception:  # pragma: no cover - httpx نصب نیست؛ مسیر AI/شبکه بدون آن معنا ندارد
    HAS_EXTRACTOR = False

#: فیلدهایی که روی سایت می‌نشینند؛ خانه‌ای که این‌جا نیست یا کارخانهٔ استخراج
#: هرگز به آن دست نمی‌زند (عکس/توضیح/`menu_order` در تست‌های فاز ۴/۵ می‌آیند).
FIELDS = (
    "title",
    "sku_prefix",
    "price",
    "prices",
    "models",
    "attributes",
    "model_colors",
    "categories",
    "stock",
    "stock_status",
    "sale_price",
)


def models_for(text: str) -> list[str]:
    """همان کاری که `product_flow._extract` می‌کند، منهای درخواست AI."""
    labels = [model.label for model in phone_parser.extract_phone_models(text)]
    for accessory in extract_accessory_models(text):
        if accessory.casefold() not in {label.casefold() for label in labels}:
            labels.append(accessory)
    return labels


def run(caption: str, info: str) -> dict:
    """یک پست را از سرِ فلو رد کن و خروجیِ قابل‌مقایسه بده."""
    source = "\n".join(part for part in (info, caption) if part.strip())
    data = asyncio.run(extract_product(source, models_for(source), "", caption=caption, info_text=info))
    result = {field: getattr(data, field) for field in FIELDS}
    result["variation_count"] = plan.plan_from_dict(data.to_dict()).count
    return result


@unittest.skipUnless(HAS_EXTRACTOR, "bot.services.product_extractor قابل import نبود")
class TestExtractionCorpus(unittest.TestCase):
    """هر مورد یک درسِ پس‌گرفته‌شده است؛ توضیحش همین‌جا نوشته شده، نه فقط در CHANGELOG."""

    def check(self, caption: str, info: str, **expected: object) -> None:
        got = run(caption, info)
        wanted = dict(expected)
        missing = set(got) - set(wanted)
        self.assertFalse(missing, f"corpus باید همهٔ خانه‌ها را بگوید: {sorted(missing)}")
        for key, value in wanted.items():
            self.assertEqual(value, got[key], f"«{key}» در این پست فرق کرد: {got!r}")

    def test_print_category_is_controlled_by_sku_not_marketing_copy(self) -> None:
        """«چاپ IMD» توصیف طرح است؛ فقط شناسه‌های CH/SB مجاز به دستهٔ چاپی‌اند."""
        self.assertIn("controlled ONLY by the product SKU prefix", product_extractor.SYSTEM_PROMPT)
        self.assertIn("Never include «چاپی» for other SKU prefixes", product_extractor.SYSTEM_PROMPT)
        cases = (
            ("AS", "چاپ IMD باکیفیت", []),
            ("CH", "قاب ساده بدون اشاره به چاپ", ["چاپی"]),
            ("SB", "قاب ساده بدون اشاره به چاپ", ["چاپی"]),
        )
        offline_settings = replace(
            product_extractor.settings, ai_base_url="", ai_token="", ai_model=""
        )
        with patch.object(product_extractor, "settings", offline_settings):
            for sku, description, categories in cases:
                with self.subTest(sku=sku):
                    info = f"{sku}\nقاب IMD طرح پاپیونی\n{description}\nقیمت 498000"
                    data = asyncio.run(
                        extract_product(info, [], "", caption="", info_text=info)
                    )
                    self.assertEqual(sku, data.sku_prefix)
                    self.assertEqual(categories, data.categories)

    def test_ai_cannot_turn_print_copy_into_a_print_category_or_change_the_sku(self) -> None:
        content = json.dumps({
            "title": "قاب IMD طرح پاپیونی",
            "price": 498000,
            "sku_prefix": "CH",  # deliberately disagrees with the actual AS code
            "attributes": {},
            "model_colors": {},
            "categories": ["قاب و کاور گوشی و تبلت > چاپی"],
        }, ensure_ascii=False)

        async def fake_post(_client, url, **_kwargs):
            request = product_extractor.httpx.Request("POST", url)
            return product_extractor.httpx.Response(
                200, json={"choices": [{"message": {"content": content}}]}, request=request
            )

        online_settings = replace(
            product_extractor.settings,
            ai_base_url="https://ai.example/v1",
            ai_token="test-token",
            ai_model="test-model",
        )
        info = "AS\nقاب IMD طرح پاپیونی\nچاپ IMD باکیفیت\nقیمت 498000"
        with (
            patch.object(product_extractor, "settings", online_settings),
            patch("httpx.AsyncClient.post", new=fake_post),
        ):
            data = asyncio.run(extract_product(info, [], "", caption="", info_text=info))
        self.assertEqual("AS", data.sku_prefix, "the explicit deterministic SKU beats the AI guess")
        self.assertFalse(any("چاپی" in category for category in data.categories))

    def test_group_prices_survive_the_noise_lines(self) -> None:
        """P0-2/P0-3/P0-4: وزن، تاریخ و کد ملی قیمت نیستند؛ دو گروه در یک خط، دو قیمت."""
        self.check(
            "قاب سیلیکونی مگنتی آیفون 13 پرو",
            "BO\nوزن 250 گرم\nتاریخ 1403/05/20\nکد ملی 0012345678\nقیمت ایفون 698 سامسونگ 598",
            title="قاب سیلیکونی مگنتی آیفون 13 پرو",
            sku_prefix="BO",
            price=698_000,
            prices={"android": 598_000, "iphone": 698_000},
            models=["iPhone 13 Pro"],
            attributes={},
            model_colors={},
            categories=[],
            stock=None,
            stock_status="",
            sale_price=0,
            variation_count=1,
        )

    def test_a_sale_line_never_steals_the_regular_price(self) -> None:
        """خطای فاز ۹: «قیمت ویژه» در پیام دوم، قیمت اصلی را می‌بلکید (هر دو ترتیب)."""
        for info in ("DS\nقیمت 500000\nقیمت ویژه 420000", "DS\nقیمت فروش ویژه: 420000\nقیمت 500000"):
            with self.subTest(info=info):
                got = run("قاب ضدضربه آیفون 15", info)
                self.assertEqual(500_000, got["price"], "قیمت اصلی باید همان ۵۰۰٬۰۰۰ بماند")
                self.assertEqual(420_000, got["sale_price"], "قیمت ویژه گم شد")
                self.assertEqual("DS", got["sku_prefix"])

    def test_persian_digits_and_compact_units(self) -> None:
        self.check(
            "قاب چرمی آیفون ۱۵ پرومکس",
            "AP\nقیمت: ۶۹۸ت",
            title="قاب چرمی آیفون ۱۵ پرومکس",
            sku_prefix="AP",
            price=698_000,
            prices={},
            models=["iPhone 15 Pro Max"],
            attributes={},
            model_colors={},
            categories=[],
            stock=None,
            stock_status="",
            sale_price=0,
            variation_count=1,
        )

    def test_grouped_thousands_and_a_product_code(self) -> None:
        """«1.098.000 تومان» یک قیمت است؛ «کد محصول: A-1234» نه."""
        self.check(
            "قاب شفاف ضدزرد آیفون 13",
            "SH\nکد محصول: A-1234\nقیمت 1.098.000 تومان",
            title="قاب شفاف ضدزرد آیفون 13",
            sku_prefix="SH",
            price=1_098_000,
            prices={},
            models=["iPhone 13"],
            attributes={},
            model_colors={},
            categories=[],
            stock=None,
            stock_status="",
            sale_price=0,
            variation_count=1,
        )

    def test_three_variant_spellings_make_three_models(self) -> None:
        """P0-5: «۱۳ پرو مکس»، «۱۳ پرو» و «۱۳» سه مدل‌اند و رنگ‌ها هر سه را می‌بینند."""
        self.check(
            "قاب آیفون",
            "MO\nApple\n13 پرو مکس\n13 پرو\n13\nرنگ: مشکی | سفید | قرمز\nقیمت 700000 تومان",
            title="قاب آیفون",
            sku_prefix="MO",
            price=700_000,
            prices={},
            models=["iPhone 13", "iPhone 13 Pro", "iPhone 13 Pro Max"],
            attributes={"رنگ": ["مشکی", "سفید", "قرمز"]},
            model_colors={},
            categories=[],
            stock=None,
            stock_status="",
            sale_price=0,
            variation_count=9,
        )

    def test_zero_stock_is_not_a_number_and_out_of_stock_is_a_status(self) -> None:
        """«موجودی ۰ عدد» عمداً None است (شارژِ صفر کارِ فاز ۵ است) و «ناموجود» وضعیت."""
        zero = run("قاب مات آیفون 14", "MT\nموجودی ۰ عدد\nقیمت 320000")
        self.assertIsNone(zero["stock"])
        self.assertEqual("", zero["stock_status"])
        self.assertEqual(320_000, zero["price"])

        counted = run("قاب مات آیفون 14", "MT\nموجودی ۲۰\nقیمت 320000")
        self.assertEqual(20, counted["stock"])

        gone = run("قاب مات آیفون 14", "MT\nناموجود\nقیمت 320000")
        self.assertIsNone(gone["stock"], "«ناموجود» نباید موجودی را صفر کند")
        self.assertEqual("outofstock", gone["stock_status"])
        # «ناموجود» نام محصول نمی‌شود؛ عنوان از همان کپشن می‌آید.
        self.assertEqual("قاب مات آیفون 14", gone["title"])

        pre = run("قاب طرح‌دار آیفون 13", "PR\nپیش‌فروش\nقیمت 380000")
        self.assertEqual("onbackorder", pre["stock_status"])
        self.assertIsNone(pre["stock"])

    def test_accessory_family_lines_are_all_kept(self) -> None:
        """بلوکِ «Airpods:» و مقادیر خط‌به‌خط — هر کدام یک مدل (فاز ۹: قبلاً اولی می‌ماند)."""
        self.check(
            "Airpods:\nPro3\n1/2",
            "AP\nقاب ایرپاد\nقیمت ۴۵۰۰۰۰ تومان",
            title="قاب ایرپاد",
            sku_prefix="AP",
            price=450_000,
            prices={},
            models=["Airpods Pro3", "Airpods 1/2"],
            attributes={},
            model_colors={},
            categories=[],
            stock=None,
            stock_status="",
            sale_price=0,
            variation_count=2,
        )

    def test_an_absurd_price_is_dropped_not_published(self) -> None:
        """زیر PRICE_MIN هیچ قیمتی ثبت نمی‌شود؛ بعدش «هیچ قیمتی پیدا نشد» می‌پرسد."""
        self.check(
            "قاب سه‌لایه آیفون 12",
            "OL\nقیمت ۹ تومان",
            title="قاب سه‌لایه آیفون 12",
            sku_prefix="OL",
            price=0,
            prices={},
            models=["iPhone 12"],
            attributes={},
            model_colors={},
            categories=[],
            stock=None,
            stock_status="",
            sale_price=0,
            variation_count=1,
        )


@unittest.skipUnless(HAS_EXTRACTOR, "bot.services.product_extractor قابل import نبود")
class TestColourMatrixCorpus(unittest.TestCase):
    """ماتریس رنگِ هر مدل — همان «کدام رنگ برای کدام گوشی» که در پست نوشته می‌شود."""

    MODELS = ["iPhone 13 Pro Max", "iPhone 13 Pro", "iPhone 13"]

    def test_persian_and_latin_variant_words_agree(self) -> None:
        """خطای فاز ۹: با املای فارسی، رنگ‌ها به مدلِ *دیگر* می‌چسبیدند."""
        from bot.services import color_matrix

        persian = color_matrix.parse_color_matrix(
            "Apple\n13 پرو مکس: مشکی، سفید\n13 پرو: قرمز\n13\nقیمت 700000 تومان"
        )
        latin = color_matrix.parse_color_matrix(
            "Apple\n13 Pro Max: مشکی/سفید\n13 Pro: قرمز\n13\nقیمت 700000 تومان"
        )
        self.assertEqual(
            {"iPhone 13 Pro Max": ["مشکی", "سفید"], "iPhone 13 Pro": ["قرمز"]},
            persian.restrictions_for(self.MODELS),
            "محدودیتِ رنگِ نوشتۀ فارسی به مدلِ اشتباه چسبید",
        )
        self.assertEqual(set(persian.colors), set(latin.colors))
        self.assertEqual(
            latin.restrictions_for(self.MODELS).keys(), persian.restrictions_for(self.MODELS).keys()
        )
        # مدلِ بی‌رنگ‌لیست حذف نمی‌شود: فقط آزاد می‌ماند.
        self.assertEqual(["iPhone 13"], persian.unresolved)
        built = plan.build_plan(self.MODELS, {"رنگ": persian.colors}, persian.restrictions_for(self.MODELS))
        self.assertEqual(6, built.count, "۲+۱ رنگ برای دو مدل + ۳ رنگ آزاد برای ۱۳")
        self.assertEqual(9, built.naive_count)



@unittest.skipUnless(HAS_EXTRACTOR, "bot.services.product_extractor قابل import نبود")
class TestAccessoryBlockLines(unittest.TestCase):
    """بلوکِ «Airpods:» — قالبی که خودِ docstring تابع وعده می‌دهد."""

    def test_every_bare_value_line_is_kept(self) -> None:
        self.assertEqual(
            ["Airpods Pro3", "Airpods 1/2"],
            extract_accessory_models("Airpods:\nPro3\n1/2\nقیمت 450000"),
            r"چند مدل زیر یک سرصفحه: قبلاً فقط اولی می‌ماند (\s* خطِ بعد را می‌بلعید)",
        )

    def test_a_value_on_the_same_line_still_works(self) -> None:
        self.assertEqual(["Airpods Pro3"], extract_accessory_models("Airpods: Pro3\nقیمت 450000"))

    def test_the_block_stops_at_the_first_non_value_line(self) -> None:
        self.assertEqual(
            ["Airpods 1/2", "Airpods Pro/Pro2"],
            extract_accessory_models("Airpods:\n1/2\nPro/Pro2\nرنگ: مشکی"),
        )

    def test_inline_notation_is_read_directly(self) -> None:
        self.assertEqual(["airpods 1/2"], extract_accessory_models("airpods 1/2"))
        self.assertEqual(["Airpods Pro3"], extract_accessory_models("Airpods Pro3"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
