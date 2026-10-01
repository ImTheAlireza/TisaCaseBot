"""قیمت مدل‌محور و قیمت همکاری: «قیمت سری ۱۷ ۵۹۸، بقیه ۴۹۸».

پیام واقعی فروشنده قیمت را سری‌محور می‌نویسد، نه مدل‌محور:

    قیمت سری ۱۷ میشه ۵۹۸
    قیمت سری های دیگه ۴۹۸
    قیمت همکاری
    سری ۱۷ میشه ۴۴۸
    باقی سری ها ۳۹۸

دو خطر اینجا هست و تست هر دو را می‌گیرد:

* نگاشت «سری → مدل» کار زبان است (هوش مصنوعی)، پس اگر آن نگاشت نیاید یا ناقص باشد،
  یک عدد روی همهٔ واریژن‌ها می‌نشیند و قیمت اشتباه منتشر می‌شود. در آن حالت ساخت **قبل
  از اولین درخواست** متوقف می‌شود.
* «قیمت همکاری» قیمت ویژه نیست؛ اگر با sale_price قاطی شود، تخفیف واقعی خراب می‌شود.

اجرا: ``python3 -m unittest discover -s tests``
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

from _flow_harness import FakeStore, patched_settings, settings_with, temp_ledger

try:
    from bot.services import draft_edits, pricing, product_extractor, products_ledger
    from bot.services.product_extractor import ProductData, extract_product
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

#: نام دقیق مدل‌ها همان‌طور که از کپشن/کاتالوگ می‌آید.
MODELS = ["iPhone 16 Pro", "iPhone 17", "iPhone 17 Pro"]

#: همان پیامی که فروشنده می‌فرستد (قیمت سری‌محور + قیمت همکاری).
SELLER_TEXT = "\n".join([
    "قاب سیلیکونی مگنتی آیفون",
    "قیمت سری ۱۷ میشه ۵۹۸",
    "قیمت سری های دیگه ۴۹۸",
    "قیمت همکاری",
    "سری ۱۷ میشه ۴۴۸",
    "باقی سری ها ۳۹۸",
])


def _online_settings():
    return replace(
        product_extractor.settings,
        ai_base_url="https://ai.example/v1",
        ai_token="test-token",
        ai_model="test-model",
    )


def _ai_reply(payload: dict) -> object:
    """A fake ``httpx.AsyncClient.post`` that answers with ``payload`` as the AI JSON."""
    content = json.dumps(payload, ensure_ascii=False)

    async def fake_post(_client, url, **_kwargs):
        request = product_extractor.httpx.Request("POST", url)
        return product_extractor.httpx.Response(
            200, json={"choices": [{"message": {"content": content}}]}, request=request
        )

    return fake_post


def _extract_with_ai(payload: dict) -> ProductData:
    with (
        patch.object(product_extractor, "settings", _online_settings()),
        patch("httpx.AsyncClient.post", new=_ai_reply(payload)),
    ):
        return asyncio.run(
            extract_product(SELLER_TEXT, list(MODELS), "", caption="", info_text=SELLER_TEXT)
        )


def _draft(**over: object) -> ProductData:
    kwargs: dict[str, object] = {
        "title": "قاب گوشی اپل",
        "price": 498_000,
        "sku_prefix": "IP17",
        "models": ["iPhone 17", "iPhone 17 Pro"],
        "attributes": {"رنگ": ["مشکی", "سفید"]},
    }
    kwargs.update(over)
    return ProductData(**kwargs)  # type: ignore[arg-type]


@needs_flow
class TestResolver(unittest.TestCase):
    """قیمت هر واریژن: اول قیمت همان مدل، بعد گروه، آخر قیمت پایه."""

    def test_the_model_override_wins_over_group_and_base(self) -> None:
        self.assertEqual(
            598_000,
            pricing.price_for_model(
                "iPhone 17 Pro", 498_000, {"iphone": 528_000}, {"iPhone 17 Pro": 598_000}
            ),
        )

    def test_other_models_keep_the_group_then_base_price(self) -> None:
        overrides = {"iPhone 17 Pro": 598_000}
        self.assertEqual(528_000, pricing.price_for_model("iPhone 17", 498_000, {"iphone": 528_000}, overrides))
        self.assertEqual(498_000, pricing.price_for_model("Samsung S24", 498_000, {"iphone": 528_000}, overrides))

    def test_a_persian_model_spelling_matches_the_override(self) -> None:
        self.assertEqual(
            598_000,
            pricing.price_for_model("17 پرو", 498_000, {}, {"iPhone 17 Pro": 598_000}),
        )

    def test_unresolved_models_are_named(self) -> None:
        self.assertEqual(
            ["iPhone 17 Pro"],
            pricing.unresolved_models(["iPhone 17 Pro"], 0, {}, {"iPhone 17": 598_000}),
        )
        self.assertEqual(
            ["iPhone 17 Pro"],
            pricing.unresolved_models(["iPhone 17 Pro"], 0, {}, {}),
            "بدون قیمت پایه و بدون override، هیچ عددی برای این مدل نیست",
        )
        self.assertEqual(
            [],
            pricing.unresolved_models(["iPhone 17 Pro"], 498_000, {}, {}),
        )


@needs_flow
class TestTheSellerMessageIsUnderstood(unittest.TestCase):
    """AI سری را باز می‌کند و هر مدل قیمت خودش را می‌گیرد."""

    AI = {
        "title": "قاب سیلیکونی مگنتی آیفون",
        "price": 498_000,
        "model_prices": {"iPhone 17": 598_000, "iPhone 17 Pro": 598_000},
        "wholesale_price": 398_000,
        "wholesale_model_prices": {"iPhone 17": 448_000, "iPhone 17 Pro": 448_000},
        "attributes": {},
        "model_colors": {},
        "categories": [],
    }

    def test_series_prices_land_on_exact_models(self) -> None:
        data = _extract_with_ai(self.AI)
        self.assertEqual({"iPhone 17": 598_000, "iPhone 17 Pro": 598_000}, data.model_prices)
        self.assertEqual(498_000, data.price, "«بقیه سری‌ها» قیمت پایه است")
        self.assertEqual([], data.pricing_errors)

    def test_the_wholesale_tier_stays_out_of_the_regular_price(self) -> None:
        data = _extract_with_ai(self.AI)
        self.assertEqual(398_000, data.wholesale_price)
        self.assertEqual({"iPhone 17": 448_000, "iPhone 17 Pro": 448_000}, data.wholesale_model_prices)
        self.assertEqual(0, data.sale_price, "قیمت همکاری قیمت ویژه نیست")
        self.assertEqual(498_000, data.price)
        self.assertTrue(any("همکاری" in note for note in data.notes), data.notes)

    def test_the_mapping_is_marked_as_a_model_guess(self) -> None:
        data = _extract_with_ai(self.AI)
        self.assertEqual("ai", data.evidence["model_prices"].source)
        self.assertIn("model_prices", product_extractor.ev.inferred_fields(data.evidence))

    def test_a_series_word_that_matches_no_model_is_reported_not_guessed(self) -> None:
        payload = dict(self.AI, model_prices={"سری ۱۷": 598_000})
        data = _extract_with_ai(payload)
        self.assertEqual({}, data.model_prices)
        self.assertTrue(any("جور نشد" in error for error in data.pricing_errors), data.pricing_errors)

    def test_an_ai_that_forgets_the_mapping_blocks_instead_of_one_price_for_all(self) -> None:
        data = _extract_with_ai({"title": "قاب", "price": 598_000, "attributes": {}, "model_colors": {}, "categories": []})
        self.assertEqual({}, data.model_prices)
        self.assertTrue(data.pricing_errors, "نباید همه یک قیمت بگیرند")
        self.assertTrue(
            any("نگاشت" in error or "سری" in error for error in data.pricing_errors),
            data.pricing_errors,
        )


@needs_flow
class TestWithoutAiTheTextIsStillNotGuessed(unittest.TestCase):
    """بدون هوش مصنوعی هم هیچ قیمتی حدس زده نمی‌شود — فقط گزارش می‌شود."""

    def _fallback(self, text: str):
        from bot.services.product_extractor import _fallback

        return _fallback(text, list(MODELS))

    def test_the_tiered_text_blocks_instead_of_taking_one_price(self) -> None:
        data = self._fallback(SELLER_TEXT)
        self.assertEqual(498_000, data.price, "«بقیه سری‌ها» تنها عدد قابل‌اتکاست")
        self.assertTrue(data.pricing_errors)
        report = validate_draft(
            {**data.to_dict(), "sku_prefix": "IP17", "images": ["a.jpg"]},
            mode="new", image_count=1, require_models=False,
        )
        self.assertIn("E_MODEL_PRICES_UNRESOLVED", [issue.code for issue in report.errors])

    def test_a_wholesale_line_never_becomes_the_retail_price(self) -> None:
        data = self._fallback("قاب سیلیکونی\nقیمت همکاری سری ۱۷ ۴۴۸")
        self.assertEqual(0, data.price, "قیمت عمده نباید جای قیمت فروش بنشیند")

    def test_a_series_scoped_sale_price_is_refused_not_applied_to_everyone(self) -> None:
        data = self._fallback("قاب سیلیکونی آیفون\nقیمت ۶۹۸\nقیمت ویژه سری ۱۷ ۵۹۸")
        self.assertTrue(
            any("قیمت ویژه" in error for error in data.pricing_errors), data.pricing_errors
        )
        report = validate_draft(
            {**data.to_dict(), "sku_prefix": "IP17", "images": ["a.jpg"]},
            mode="new", image_count=1, require_models=False,
        )
        self.assertIn("E_MODEL_PRICES_UNRESOLVED", [issue.code for issue in report.errors])

    def test_a_single_sale_price_for_everyone_is_still_fine(self) -> None:
        data = self._fallback("قاب سیلیکونی آیفون 17\nقیمت ۶۹۸\nقیمت ویژه ۵۹۸")
        self.assertEqual([], data.pricing_errors)
        self.assertEqual(598_000, data.sale_price)


@needs_flow
class TestTheGateBeforeWooCommerce(unittest.TestCase):
    """ورودی مبهم باید قبل از اولین درخواست روشن شود، نه با یک قیمت حدسی."""

    def _report(self, **over: object):
        data = {
            "title": "قاب گوشی اپل",
            "price": 498_000,
            "models": ["iPhone 17", "iPhone 17 Pro"],
            "sku_prefix": "IP17",
            "images": ["a.jpg"],
            **over,
        }
        return validate_draft(data, mode="new", image_count=1, require_models=False)

    def test_a_complete_mapping_passes(self) -> None:
        report = self._report(model_prices={"iPhone 17": 598_000})
        self.assertFalse(report.blocking, [issue.message for issue in report.errors])

    def test_unmapped_series_prices_block_with_a_hint(self) -> None:
        report = self._report(pricing_errors=["قیمت‌ها سری/مدل‌محور است"])
        errors = [issue for issue in report.errors if issue.code == "E_MODEL_PRICES_UNRESOLVED"]
        self.assertEqual(1, len(errors))
        self.assertTrue(errors[0].hint)

    def test_a_lone_wholesale_price_is_never_published_as_the_retail_price(self) -> None:
        from bot.services.product_extractor import _fallback

        text = "قاب سیلیکونی\nقیمت همکاری ۳۹۸"
        with (
            patch.object(product_extractor, "settings", _online_settings()),
            patch(
                "httpx.AsyncClient.post",
                new=_ai_reply({"title": "قاب سیلیکونی", "price": 398_000, "wholesale_price": 398_000}),
            ),
        ):
            data = asyncio.run(extract_product(text, list(MODELS), "", caption="", info_text=text))
        self.assertEqual(0, data.price, "قیمت عمده نباید قیمت فروش شود")
        self.assertTrue(data.pricing_errors)
        self.assertEqual(0, _fallback(text, list(MODELS)).price)

    def test_a_label_that_matches_no_model_blocks(self) -> None:
        report = self._report(model_prices={"سری ۱۷": 598_000})
        self.assertIn("E_MODEL_PRICES_LABEL", [issue.code for issue in report.errors])

    def test_a_model_without_any_price_blocks_by_name(self) -> None:
        report = self._report(price=0, model_prices={"iPhone 17": 598_000})
        errors = [issue for issue in report.errors if issue.code == "E_MODEL_PRICE_MISSING"]
        self.assertEqual(1, len(errors))
        self.assertIn("iPhone 17 Pro", errors[0].message)

    def test_model_prices_are_refused_on_the_zip_route(self) -> None:
        report = validate_draft(
            {
                "title": "قاب گوشی اپل",
                "price": 498_000,
                "models": ["iPhone 17", "iPhone 17 Pro"],
                "sku_prefix": "IP17",
                "model_prices": {"iPhone 17": 598_000},
            },
            mode="update",
            image_count=1,
            require_models=False,
        )
        self.assertIn("E_MODEL_PRICES_MODE", [issue.code for issue in report.errors])

    def test_a_sale_price_above_a_model_price_blocks_for_that_model(self) -> None:
        # قیمت پایه 598 است و مدل iPhone 17 ارزان‌تر (498)؛ تخفیف 520 زیر پایه است
        # ولی روی واریژن آن مدل گران‌تر از خودش می‌نشیند و تخفیف دیده نمی‌شود.
        report = self._report(
            price=598_000, model_prices={"iPhone 17": 498_000}, sale_price=520_000
        )
        errors = [issue for issue in report.errors if issue.code == "E_SALE_NOT_CHEAPER"]
        self.assertEqual(1, len(errors), [issue.message for issue in errors])
        self.assertIn("iPhone 17", errors[0].message)

    def test_a_wholesale_price_above_the_retail_price_is_flagged(self) -> None:
        report = self._report(model_prices={"iPhone 17": 598_000}, wholesale_price=600_000)
        self.assertFalse(report.blocking)
        self.assertIn("W_WHOLESALE_ABOVE_RETAIL", [issue.code for issue in report.warnings])


@needs_flow
class TestWhatGoesToTheShop(unittest.IsolatedAsyncioTestCase):
    """بدنهٔ درخواست‌ها: هر واریژن قیمت مدل خودش را می‌گیرد."""

    def setUp(self) -> None:
        self._settings = patched_settings(settings_with(**SHARED))
        self._settings.__enter__()
        self.addCleanup(self._settings.__exit__, None, None, None)

    async def _publish(self, data: ProductData):
        from bot.services.woocommerce_direct import create_draft

        store = FakeStore()
        report: list[str] = []
        with temp_ledger():
            await create_draft(data.to_dict(), [], report=report, transport=store.transport)
        return store, report

    async def test_each_model_gets_its_own_regular_price(self) -> None:
        data = _draft(model_prices={"iPhone 17": 598_000})
        store, _report = await self._publish(data)
        by_model = {
            variation["attributes"][0]["option"]: variation["regular_price"]
            for variation in store.variation_items()
        }
        self.assertEqual("598000", by_model["iPhone 17"])
        self.assertEqual("498000", by_model["iPhone 17 Pro"], "بقیه مدل‌ها قیمت پایه را می‌گیرند")

    async def test_unmapped_tiers_are_refused_before_any_request(self) -> None:
        from bot.services.woocommerce_direct import create_draft

        data = _draft(pricing_errors=["قیمت‌ها سری/مدل‌محور نوشته شده‌اند اما نگاشت نشدند"])
        store = FakeStore()
        with self.assertRaisesRegex(ValueError, "قابل‌اعمال نیستند"):
            await create_draft(data.to_dict(), [], report=[], transport=store.transport)
        self.assertEqual([], store.requests, "ورودی مبهم نباید هیچ درخواستی بفرستد")


@needs_flow
class TestManualEditing(unittest.TestCase):
    """«✏️ اصلاح اطلاعات» هم باید قیمت مدل‌ها را بشناسد و خطا را بگوید."""

    def test_parse_lines_of_model_and_amount(self) -> None:
        self.assertEqual(
            {"iPhone 17": 598_000, "iPhone 17 Pro": 648_000},
            draft_edits.parse_model_prices("iPhone 17 598\niPhone 17 Pro 648"),
        )
        self.assertEqual({}, draft_edits.parse_model_prices("حذف"))

    def test_apply_matches_the_exact_model_options(self) -> None:
        data = _draft()
        self.assertIsNone(draft_edits.apply_edit(data, "model_prices", "iPhone 17 598"))
        self.assertEqual({"iPhone 17": 598_000}, data.model_prices)
        self.assertIn("598,000", draft_edits.display_value(data, "model_prices"))

    def test_an_unknown_model_name_is_an_error_not_a_silent_drop(self) -> None:
        data = _draft()
        message = draft_edits.apply_edit(data, "model_prices", "سری 17 598")
        self.assertIsNotNone(message)
        self.assertEqual({}, data.model_prices)

    def test_wholesale_price_is_editable_and_clearable(self) -> None:
        data = _draft()
        self.assertIsNone(draft_edits.apply_edit(data, "wholesale_price", "448"))
        self.assertEqual(448_000, data.wholesale_price)
        self.assertEqual(0, draft_edits.parse_wholesale_price("حذف"))

    def test_a_typed_price_clears_the_unmapped_stop(self) -> None:
        # وگرنه فروشنده‌ای که متنش سری‌محور بوده هیچ‌جوره نمی‌توانست از ⛔ رد شود.
        data = _draft(price=0, pricing_errors=["قیمت‌ها سری‌محور است و نگاشت نشد"])
        self.assertIsNone(draft_edits.apply_edit(data, "price", "598"))
        self.assertEqual([], data.pricing_errors)
        self.assertEqual(598_000, data.price)

    def test_a_manual_model_price_survives_the_next_extraction(self) -> None:
        data = _draft(price=0)
        self.assertIsNone(draft_edits.apply_edit(data, "model_prices", "iPhone 17 598"))
        # استخراج تازه شکِ متن را برمی‌گرداند؛ قفل دستی باید همان لحظه پاکش کند.
        data.pricing_errors = ["قیمت‌ها سری‌محور است و نگاشت نشد"]
        draft_edits.apply_locks(data)
        self.assertEqual({"iPhone 17": 598_000}, data.model_prices)
        self.assertEqual([], data.pricing_errors)

    def test_the_field_is_offered_in_the_picker(self) -> None:
        keys = [key for key, _label, _value in draft_edits.editable_fields(_draft())]
        self.assertIn("model_prices", keys)
        self.assertLess(keys.index("prices"), keys.index("model_prices"))
        self.assertLess(keys.index("model_prices"), keys.index("stock"))


@needs_flow
class TestWhatTheOwnerSees(unittest.TestCase):
    """پیش‌نمایش و کارت نتیجه باید همان اعداد واقعی را نشان دهند."""

    def _preview(self, data: ProductData) -> str:
        from bot.modules import product_flow as PF

        return PF._preview(PF.ProductSession(data=data, chat_id=9))

    def test_the_preview_names_the_model_prices(self) -> None:
        text = self._preview(_draft(model_prices={"iPhone 17": 598_000}))
        self.assertIn("قیمت مدل‌های خاص", text)
        self.assertIn("iPhone 17: 598,000", text)

    def test_the_preview_says_wholesale_is_not_applied(self) -> None:
        text = self._preview(_draft(wholesale_price=398_000, wholesale_model_prices={"iPhone 17": 448_000}))
        self.assertIn("قیمت همکاری", text)
        self.assertIn("در سایت اعمال نمی‌شود", text)
        self.assertIn("448,000", text)

    def test_the_result_card_shows_the_model_range(self) -> None:
        with temp_ledger():
            entry = products_ledger.record(
                user_id=7, title="قاب", price=498_000, model_prices={"iPhone 17": 598_000}
            )
        from bot.keyboards.cards import result_card

        card = result_card(entry)
        self.assertIn("iPhone 17: 598,000", card)
        self.assertIn("پایه: 498,000", card)


@needs_flow
class TestBatchIdentity(unittest.TestCase):
    """دو قیمت متفاوت، دو بستهٔ متفاوت: batch id باید عوض شود."""

    def test_model_prices_change_the_batch_id(self) -> None:
        from bot.services import publish_batch

        plain = _draft().to_dict()
        priced = _draft(model_prices={"iPhone 17": 598_000}).to_dict()
        self.assertNotEqual(
            publish_batch.batch_id(plain, [], chat_id=1),
            publish_batch.batch_id(priced, [], chat_id=1),
        )

    def test_the_zip_manifest_carries_the_new_keys(self) -> None:
        from bot.modules import product_flow as PF

        data = _draft(
            model_prices={"iPhone 17": 598_000},
            wholesale_price=398_000,
            wholesale_model_prices={"iPhone 17": 448_000},
        )
        manifest = PF._zip_manifest(
            data, usable_attributes={}, image_mode="keep", batch="b1", mode="new"
        )
        self.assertEqual({"iPhone 17": 598_000}, manifest["model_prices"])
        self.assertEqual(398_000, manifest["wholesale_price"])
        self.assertEqual({"iPhone 17": 448_000}, manifest["wholesale_model_prices"])
        self.assertEqual(data.stock_matrix, manifest["stock_matrix"])

    def test_the_manifest_round_trips_through_json(self) -> None:
        from bot.modules import product_flow as PF

        manifest = PF._zip_manifest(
            _draft(model_prices={"iPhone 17": 598_000}),
            usable_attributes={}, image_mode="keep", batch="b1", mode="new",
        )
        self.assertIn("model_prices", json.loads(json.dumps(manifest, ensure_ascii=False)))


@needs_flow
class TestThePriceLineOfAPublishedCard(unittest.TestCase):
    """کارت قدیمی بدون کلید تازه هم باید کار کند (سازگاری عقب‌رو)."""

    def test_an_old_entry_without_model_prices_still_renders(self) -> None:
        self.assertEqual("—", products_ledger.price_range({"price": 0, "price_groups": {}}))
        self.assertEqual("498,000 تومان", products_ledger.price_range({"price": 498_000}))
        self.assertEqual(
            "iphone: 698,000",
            products_ledger.price_range({"price": 0, "price_groups": {"iphone": 698_000}}),
        )
