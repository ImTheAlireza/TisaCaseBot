"""Phase 0 regression tests: the anti-silent-corruption guard rails.

Every test here reproduces a bug that existed in production and was confirmed by
running the old code — the numbers in the comments are what the OLD parser
returned. They are the contract now: a price must not come from a weight line, a
destroyed barcode must not reach ``tracking.csv``, and the preview must count
exactly what the store will receive.

Run with either::

    python3 -m unittest discover -s tests
    pytest tests

Only the standard library (plus pandas/openpyxl for the order-file test) is
required; the flow tests skip themselves when python-telegram-bot is missing.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("SUDO_IDS", "1")

try:  # these need python-dotenv (through bot.config), like every other module
    from bot.config import Settings
    from bot.services import money
    from bot.services.plan import build_plan, clean_values
    from bot.services.validation import validate_draft

    HAS_BOT = True
except Exception:  # pragma: no cover
    HAS_BOT = False

needs_bot = unittest.skipUnless(HAS_BOT, "python-dotenv / bot package not importable")

try:  # pandas is optional (only the order-file converter needs it)
    from bot.services import processor

    HAS_PROCESSOR = True
except Exception:  # pragma: no cover
    HAS_PROCESSOR = False

try:
    from bot.services.product_extractor import _fallback, _scan_prices

    HAS_EXTRACTOR = True
except Exception:  # pragma: no cover - httpx missing
    HAS_EXTRACTOR = False

needs_processor = unittest.skipUnless(HAS_PROCESSOR, "pandas is not installed")
needs_extractor = unittest.skipUnless(HAS_EXTRACTOR, "httpx is not installed")


# ---------------------------------------------------------------------------
# P0-2 / P0-3 / P0-4: prices
# ---------------------------------------------------------------------------

@needs_bot
class TestAmountParser(unittest.TestCase):
    def test_weight_line_is_not_a_price(self):
        # old: 250,000
        self.assertFalse(money.looks_like_price_line("وزن 250 گرم"))

    def test_date_is_not_a_price(self):
        # old: 1,403 (and 6.1e23 for a tracking code)
        self.assertFalse(money.looks_like_price_line("1403/01/01"))
        self.assertFalse(money.looks_like_price_line("تاریخ: 1403-02-15"))

    def test_code_and_sku_lines_are_not_prices(self):
        # old: 147,000 from «SKU: BO147»; 1,234,567,890 from a national id
        self.assertFalse(money.looks_like_price_line("SKU: BO147"))
        self.assertFalse(money.looks_like_price_line("کد ملی 1234567890"))
        self.assertFalse(money.looks_like_price_line("کد رهگیری 610001573845123456789012"))

    def test_a_bare_amount_is_still_a_price(self):
        self.assertTrue(money.looks_like_price_line("1098"))
        self.assertTrue(money.looks_like_price_line("قیمت 698000 تومان"))
        self.assertEqual(money.parse_line_amount("1098"), 1098)

    def test_the_amount_next_to_the_unit_wins_not_the_first_number(self):
        # old: 24 (the model number!), because the first number in the line won
        self.assertEqual(money.parse_line_amount("S24 اولترا 768t"), 768_000)
        self.assertEqual(money.parse_line_amount("A25 مشکی 598k"), 598_000)

    def test_group_amounts_read_every_group_on_the_line(self):
        # old: only iPhone survived, so every Android variation got the
        # iPhone price through the common-price fallback.
        self.assertEqual(
            money.group_amounts("قیمت ایفون 698 اندروید 598"),
            {"iphone": 698_000, "android": 598_000},
        )
        self.assertEqual(money.group_amounts("698000 ایفون"), {"iphone": 698_000})

    def test_out_of_range_amounts_are_rejected(self):
        self.assertFalse(money.in_accepted_range(24))
        self.assertFalse(money.in_accepted_range(10**15))
        self.assertTrue(money.in_accepted_range(698_000))

    def test_toman_number_keeps_working(self):
        self.assertEqual(money.parse_line_amount("۱ میلیون و ۹۸ هزار تومان"), 1_098_000)
        self.assertEqual(money.parse_line_amount("1.5 میلیون تومان"), 1_500_000)
        self.assertEqual(money.parse_line_amount("3 هزار و 500"), 3_500)


@needs_extractor
class TestPriceScanning(unittest.TestCase):
    def test_weight_cannot_overwrite_a_stated_price(self):
        # old: 250,000 — the weight line came last and won
        lines = "قیمت ایفون 698\nاندروید 598\nوزن 250 گرم".splitlines()
        scan = _scan_prices(lines)
        price, prices = scan.price, scan.prices
        self.assertEqual(prices, {"iphone": 698_000, "android": 598_000})
        self.assertNotEqual(price, 250_000)

    def test_fallback_title_and_price_on_a_messy_post(self):
        text = "قاب سیلیکونی مگنتی\nBO\n1403/02/15\nSKU: BO147\nقیمت ایفون 698\nاندروید 598\nوزن 250 گرم"
        data = _fallback(text, ["iPhone 17 Pro Max", "Galaxy S24"])
        self.assertEqual(data.title, "قاب سیلیکونی مگنتی")
        self.assertEqual(data.sku_prefix, "BO")
        self.assertEqual(data.prices["android"], 598_000)
        self.assertNotEqual(data.price, 250_000)


# ---------------------------------------------------------------------------
# P0-6: the preview and the WooCommerce payload must agree
# ---------------------------------------------------------------------------

@needs_bot
class TestVariationPlan(unittest.TestCase):
    def test_duplicate_values_cannot_inflate_the_count(self):
        # old: the preview said 4 while WooCommerce received one axis at all.
        plan = build_plan(["iPhone 17 Pro Max", "iPhone 17 Pro"], {"رنگ": ["سفید", "سفید"]})
        self.assertEqual([name for name, _ in plan.axes], ["مدل"])
        self.assertEqual(plan.count, 2)
        self.assertEqual(len(plan.woo_attributes()[0]["options"]), 2)

    def test_whitespace_duplicates_are_deduped_once_for_everyone(self):
        self.assertEqual(clean_values([" مشکی ", "مشکی", "سفید"]), ["مشکی", "سفید"])
        plan = build_plan(["A", "B"], {"رنگ": ["مشکی", "مشکی ", "سفید"]})
        self.assertEqual(plan.count, 4)  # 2 models × 2 colours, for preview AND payload
        self.assertEqual(len(plan.combos), 4)

    def test_axis_collapsing_to_one_value_is_reported_not_hidden(self):
        # A duplicate pair is one value, not an axis: it must not become a
        # variation, and the owner must be told it was dropped.
        plan = build_plan(["A", "B"], {"جنس": ["سیلیکون", "سیلیکون"]})
        self.assertIn("جنس", [name for name, _old, _new in plan.dropped])
        self.assertNotIn("جنس", plan.attribute_names())
        self.assertEqual(plan.count, 2)

    def test_colours_restrict_only_the_variations(self):
        plan = build_plan(
            ["iPhone 17 Pro Max", "iPhone 17 Pro"],
            {"رنگ": ["سفید", "مشکی", "نارنجی"]},
            {"iPhone 17 Pro": ["مشکی"]},
        )
        self.assertEqual(len(plan.woo_attributes()[1]["options"]), 3)
        self.assertEqual(plan.count, 4)  # 3 for the Max + 1 for the Pro
        self.assertTrue(plan.restricted)


# ---------------------------------------------------------------------------
# P0-7: one shared gate for both output paths
# ---------------------------------------------------------------------------

@needs_bot
class TestValidationGate(unittest.TestCase):
    BASE = {
        "title": "قاب سیلیکونی",
        "price": 698_000,
        "prices": {},
        "sku_prefix": "BO",
        "models": ["iPhone 17 Pro Max", "iPhone 17 Pro"],
        "attributes": {"رنگ": ["سفید", "مشکی"]},
        "model_colors": {},
        "categories": [],
    }

    def _data(self, **overrides):
        data = dict(self.BASE)
        data.update(overrides)
        return data

    def test_missing_models_are_a_warning_by_default(self):
        report = validate_draft(self._data(models=[]), mode="new", image_count=3)
        self.assertFalse(report.blocking, "model-less simple products are valid")
        self.assertIn("W_NO_MODELS", [issue.code for issue in report.warnings])

    def test_missing_models_can_still_be_required_explicitly(self):
        report = validate_draft(
            self._data(models=[]), mode="new", image_count=3, require_models=True
        )
        self.assertTrue(report.blocking)
        self.assertIn("E_NO_MODELS", [issue.code for issue in report.errors])

    def test_out_of_range_price_blocks(self):
        report = validate_draft(self._data(price=250), mode="new", image_count=3)
        self.assertIn("E_PRICE_RANGE", [issue.code for issue in report.errors])

    def test_restock_needs_at_least_one_change(self):
        report = validate_draft(
            self._data(price=0, models=[], attributes={}), mode="update", image_count=0
        )
        self.assertIn("E_NOTHING_TO_APPLY", [issue.code for issue in report.errors])

    def test_a_clean_draft_passes(self):
        report = validate_draft(self._data(variation_count=4), mode="new", image_count=2)
        self.assertFalse(report.blocking, report.as_html())

    def test_unapplied_model_word_is_a_warning_not_a_crash(self):
        report = validate_draft(
            self._data(),
            mode="new",
            image_count=2,
            unapplied_model_words=[("13 pro plus", ["plus"])],
        )
        self.assertIn("W_MODEL_WORD_UNAPPLIED", [issue.code for issue in report.warnings])


# ---------------------------------------------------------------------------
# P0-1: a destroyed barcode must never enter the import file
# ---------------------------------------------------------------------------

@needs_bot
@needs_processor
class TestOrderFileSafety(unittest.TestCase):
    def _write_xlsx(self, rows):
        import pandas as pd

        path = Path(self.tmp.name) / "orders.xlsx"
        pd.DataFrame(rows).to_excel(path, index=False, header=False)
        return str(path)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_float_destroyed_and_invalid_barcodes_are_excluded(self):
        path = self._write_xlsx([
            ["ردیف", "بارکد", "تاریخ ثبت", "نام گیرنده", "کد سفارش", "مقصد"],
            [1, 1.93e23, "1403-01-01", "امیرحسین عاشوری ۳۰۶۱۷۶", "", "تهران"],
            [2, "610001573845123456789012", "1403-01-01", "رضا ۱۲۳۴۵", "654321", "مشهد"],
            [3, "6100015738451234567", "1403-01-01", "هدیه", "778899", "تبریز"],
            ["جمع کل", "", "", "", "", ""],
        ])
        report = processor.process_file(path, "orders.xlsx")
        lines = report.csv_text.strip().splitlines()
        self.assertEqual(lines[0], "order_id,tracking_code")
        self.assertEqual(lines[1:], ["654321,610001573845123456789012"])
        self.assertNotIn("192999999999999989514240", report.csv_text)
        self.assertIn("needs-review.csv", report.summary)
        self.assertTrue(report.review_csv and "بارکد نامعتبر" in report.review_csv)
        self.assertTrue(report.problems_csv and "دقتش از بین رفته" in report.problems_csv)
        # the report is a row map too: the file-level counts are what the chat says
        self.assertEqual((report.rows, report.usable, report.errors), (3, 1, 2))
        # the fix workbook is the round trip: same headers, so re-sending it works
        self.assertIsNotNone(report.review_xlsx)

    def test_duplicated_valid_barcode_is_only_a_warning(self):
        # One parcel, two orders is legitimate: dropping both rows would have
        # lost real data, so duplicates stay in the CSV and warn instead.
        path = self._write_xlsx([
            ["ردیف", "بارکد", "کد سفارش"],
            [1, "610001573845123456789012", "111111"],
            [2, "610001573845123456789012", "222222"],
        ])
        report = processor.process_file(path, "orders.xlsx")
        self.assertEqual(len(report.csv_text.strip().splitlines()), 3)
        self.assertIn("بارکد تکراری", report.problems_csv or "")
        self.assertIsNone(report.review_csv)
        self.assertIsNone(report.review_xlsx)
        # duplicate *valid* barcodes keep their rows, and each problem names where
        self.assertIn("Sheet1!B2", report.problems_csv or "")


# ---------------------------------------------------------------------------
# P0-9 / P1-12: configuration can no longer crash or leak
# ---------------------------------------------------------------------------

@needs_bot
class TestSettings(unittest.TestCase):
    KEYS = (
        "BOT_TOKEN", "SUDO_IDS", "LOG_CHAT_ID", "ALBUM_WAIT_SECONDS", "IMAGE_QUALITY",
        "PRICE_MIN", "PRICE_MAX", "REQUIRE_MODELS", "BARCODE_LENGTHS", "FLOW_TIMEOUT_SECONDS",
        "LOG_LEVEL",
    )

    def setUp(self):
        self.saved = {key: os.environ.get(key) for key in self.KEYS}
        for key in self.KEYS:
            os.environ.pop(key, None)
        os.environ["BOT_TOKEN"] = "123:TEST"

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_log_chat_has_no_hardcoded_default(self):
        # old: -5061365940 even when the variable was empty
        self.assertIsNone(Settings.from_env().log_chat_id)
        os.environ["LOG_CHAT_ID"] = ""
        self.assertIsNone(Settings.from_env().log_chat_id)
        os.environ["LOG_CHAT_ID"] = "-1001234567890"
        self.assertEqual(Settings.from_env().log_chat_id, -1001234567890)

    def test_bad_numbers_fall_back_instead_of_crashing(self):
        # old: ValueError at import → supervisor crash-loop, no readable reason
        os.environ.update({
            "ALBUM_WAIT_SECONDS": "abc",
            "IMAGE_QUALITY": "88,",
            "PRICE_MIN": "خیلی",
            "FLOW_TIMEOUT_SECONDS": "-5",
            "LOG_LEVEL": "verbose",
        })
        settings = Settings.from_env()
        self.assertEqual(settings.album_wait_seconds, 1.8)
        self.assertEqual(settings.image_quality, 88)
        self.assertEqual(settings.price_min, 1_000)
        self.assertEqual(settings.flow_timeout_seconds, 60)
        self.assertEqual(settings.log_level, "INFO")
        self.assertTrue(settings.problems, "the bad values must be reported")

    def test_models_are_not_required_by_default(self):
        settings = Settings.from_env()
        self.assertFalse(settings.require_models)

    def test_price_range_and_model_requirement_are_configurable(self):
        os.environ["REQUIRE_MODELS"] = "no"
        os.environ["PRICE_MAX"] = "1000000"
        os.environ["BARCODE_LENGTHS"] = "13,24"
        os.environ["MAX_ROWS"] = "500"
        os.environ["MAX_FILE_MB"] = "7.5"
        os.environ["PROCESS_TIMEOUT_SECONDS"] = "30"
        settings = Settings.from_env()
        self.assertFalse(settings.require_models)
        self.assertEqual(settings.price_max, 1_000_000)
        self.assertEqual(settings.barcode_lengths, frozenset({13, 24}))
        # سقف‌های فایل ردیابی هم از همان‌جا می‌آیند (تا فاز ۷ `MAX_ROWS` فقط یک عدد در
        # config بود و هیچ کد نمی‌خواندش)
        self.assertEqual(settings.max_rows, 500)
        self.assertEqual(settings.max_file_mb, 7.5)
        self.assertEqual(settings.process_timeout_seconds, 30.0)

    def test_every_limit_the_readme_configures_is_in_env_example(self):
        """.env.example فایلی است که ادمین کپی می‌کند؛ کلیدی که فقط در README باشد،
        سقفی است که هیچ‌کس نمی‌تواند عوضش کند — و پیامِ ردِّ ربات همان کلید را صدا می‌زند."""
        root = Path(__file__).resolve().parents[1]
        readme = (root / "README.md").read_text(encoding="utf-8")
        example = (root / ".env.example").read_text(encoding="utf-8")
        documented = {
            line.split("=", 1)[0].strip()
            for line in example.splitlines()
            if "=" in line and not line.startswith("#")
        }
        listed = set(re.findall(r"^\| `([A-Z][A-Z0-9_]+)`", readme, re.M))
        self.assertGreater(len(listed), 10, "README's config table was not read at all")
        self.assertEqual(
            sorted(listed - documented), [], "README می‌گوید قابل‌تنظیم است، .env.example نمی‌شناسدش"
        )
    def test_config_boots_without_python_dotenv(self):
        # A shared host where pip did not run must still start: dying on an
        # optional convenience import is a crash-loop with an unreadable reason.
        code = (
            "import sys; sys.modules['dotenv'] = None;"
            "import bot.config; print('imported-ok')"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(Path(__file__).resolve().parents[1]),
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "BOT_TOKEN": "123:TEST"},
        )
        self.assertIn("imported-ok", result.stdout, result.stderr)

    def test_sudo_ids_are_never_silently_dropped(self):
        # A shape rule of «3 to 20 digits» used to reject a short id outright,
        # so the owner booted with no sudo access and only a log line to say why.
        os.environ["SUDO_IDS"] = "1"
        settings = Settings.from_env()
        self.assertIn(1, settings.sudo_ids)
        self.assertTrue(settings.is_sudo(1))
        self.assertTrue(any("کوتاه" in problem for problem in settings.problems))

    def test_sudo_ids_reject_garbage_but_keep_the_rest(self):
        os.environ["SUDO_IDS"] = "123456789, abc, 0"
        settings = Settings.from_env()
        self.assertEqual(settings.sudo_ids, frozenset({123456789}))
        self.assertTrue(any("abc" in problem for problem in settings.problems))


if __name__ == "__main__":
    unittest.main()
