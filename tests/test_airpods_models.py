"""AirPods parsing, family-safe AI prompts and actual variation-axis routing."""
from __future__ import annotations

import asyncio
import json
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("SUDO_IDS", "1234567")

from bot.services.airpods_parser import (
    AIRPODS_ATTRIBUTE,
    airpods_category_leaves,
    canonical_airpods_model,
    extract_airpods_models,
    split_device_axes,
)
from bot.services.color_matrix import parse_color_matrix
from bot.services.phone_parser import extract_phone_models, normalize_product_models
from bot.services.plan import build_plan, plan_from_dict
from bot.services import postmodel

try:
    import httpx

    import _flow_harness as h
    from bot.modules import product_flow as PF
    from bot.services import ai_normalizer, draft_edits, product_extractor, update_plan
    from bot.services.woocommerce_direct import create_draft

    HAS_FLOW = True
except ImportError:
    HAS_FLOW = False

needs_flow = unittest.skipUnless(HAS_FLOW, "Telegram/httpx dependencies are not installed")

AIRPODS_CAPTION = "AirPod 1/2\nAirPod pro2"
AIRPODS_MODELS = ["AirPods 1/2", "AirPods Pro 2"]
PHONE_MODELS = ["iPhone 17", "iPhone 17 Pro"]
MIXED_CAPTION = "iPhone 17\niPhone 17Pro\n" + AIRPODS_CAPTION
INFO = "قاب طرح ماسا\nLP\n728t"


class TestAirPodsParser(unittest.TestCase):
    def test_singular_plural_persian_digits_and_compact_pro_suffix(self):
        for text, expected in (
            ("AirPod pro2", "AirPods Pro 2"),
            ("AirPods Pro 2", "AirPods Pro 2"),
            ("Air Pods PRO2", "AirPods Pro 2"),
            ("ایرپاد پرو ۲", "AirPods Pro 2"),
            ("ايرپاد پرو٢", "AirPods Pro 2"),
            ("ایرپادز پرو۳", "AirPods Pro 3"),
            ("Apple AirPods Pro", "AirPods Pro"),
            ("AirPod Pro1", "AirPods Pro"),
            ("AirPods Max", "AirPods Max"),
            ("ایرپاد مکس", "AirPods Max"),
            ("AirPod 1/2", "AirPods 1/2"),
        ):
            with self.subTest(text=text):
                self.assertEqual(expected, canonical_airpods_model(text))
                self.assertEqual([expected], extract_airpods_models(text))

    def test_the_users_two_models_are_both_recognized(self):
        self.assertEqual(AIRPODS_MODELS, extract_airpods_models(AIRPODS_CAPTION))

    def test_groups_repeated_brands_and_duplicates(self):
        text = "AirPod Pro/AirPod Pro2\nAirPods Pro/Pro 2\nAirPod pro2\nایرپاد پرو۲"
        self.assertEqual(["AirPods Pro/Pro 2", "AirPods Pro 2"], extract_airpods_models(text))

    def test_bare_section_rows_and_colors_do_not_lose_models(self):
        self.assertEqual(
            AIRPODS_MODELS,
            extract_airpods_models("🎧 ایرپاد:\n۱/۲:\nمشکی\nپرو۲ (سفید)\nLP\n728t"),
        )

    def test_brand_only_numbers_and_prices_are_not_models(self):
        for text in ("AirPods", "AirPod 1098", "AirPod 20", "AirPods Pro20", "1/2\nPro2", "728t", "موجودی ۲"):
            with self.subTest(text=text):
                self.assertEqual([], extract_airpods_models(text))
        self.assertEqual([], extract_airpods_models("AirPods:\nقیمت 1000\nLP\n728t"))

    def test_airpods_end_the_iphone_section_without_inventing_iphone_one_or_two(self):
        text = "Apple\n17pro\nAirPod:\n1/2\nPro2\niPhone 16"
        self.assertEqual(["iPhone 16", "iPhone 17 Pro"], [m.label for m in extract_phone_models(text)])
        self.assertEqual(AIRPODS_MODELS, extract_airpods_models(text))
        self.assertEqual(
            ["iPhone 17"],
            [m.label for m in extract_phone_models("iPhone 17 | AirPod 1/2 | AirPod pro2")],
        )

    def test_uppercase_bare_pro_and_max_are_not_mistaken_for_sku_codes(self):
        self.assertEqual(["AirPods Pro", "AirPods Max"], extract_airpods_models("AirPods:\nPRO\nMAX"))
        self.assertEqual([], extract_airpods_models("AirPods:\nپروانه\nمکسی"))

    def test_unsupported_spaced_suffix_does_not_become_a_bare_pro_model(self):
        for text in ("AirPods Pro 4", "AirPods Pro 20", "AirPods Pro Ultra", "AirPods Pro Max"):
            with self.subTest(text=text):
                self.assertIsNone(canonical_airpods_model(text))
                self.assertEqual([], extract_airpods_models(text))

    def test_an_explicit_model_in_a_price_line_is_not_lost(self):
        self.assertEqual(["AirPods Pro 2"], extract_airpods_models("قیمت AirPod pro2: 728t"))

    def test_inline_mixed_families_work_in_both_orders(self):
        for text in ("iPhone 17 + AirPod 1/2", "AirPod 1/2 | iPhone 17"):
            with self.subTest(text=text):
                self.assertEqual("iPhone 17 | AirPods 1/2", normalize_product_models(text))

    def test_canonical_mixed_list_is_idempotent(self):
        normalized = " | ".join([*PHONE_MODELS, *AIRPODS_MODELS])
        self.assertEqual(normalized, normalize_product_models(MIXED_CAPTION))
        self.assertEqual(normalized, normalize_product_models(normalized))

    def test_airpod_headers_are_brand_blocks_and_model_lines_are_not_prices(self):
        for text in ("AirPod:", "AirPods", "ایرپاد:"):
            self.assertEqual((postmodel.ROLE_BRAND,), postmodel.classify_line(text))
        roles = postmodel.classify_line("ایرپاد پرو ۲")
        self.assertIn(postmodel.ROLE_MODEL, roles)
        self.assertNotIn(postmodel.ROLE_PRICE, roles)

    def test_category_mapping_uses_exact_variants(self):
        self.assertEqual(["Airpods Pro 2"], airpods_category_leaves(["AirPod pro2"]))
        self.assertEqual(["Airpods Pro 3"], airpods_category_leaves(["AirPods Pro3"]))
        self.assertEqual(["Airpods 1/2", "Airpods Pro 2"], airpods_category_leaves(AIRPODS_MODELS))


class TestDeviceAxisPolicy(unittest.TestCase):
    def test_airpods_only_use_the_main_model_axis(self):
        built = build_plan(AIRPODS_MODELS, {})
        self.assertEqual([("مدل", AIRPODS_MODELS)], built.axes)
        self.assertEqual(2, built.count)
        self.assertTrue(built.is_variable)

    def test_mixed_models_get_two_independent_axes(self):
        built = build_plan([*PHONE_MODELS, *AIRPODS_MODELS], {})
        self.assertEqual([("مدل", PHONE_MODELS), (AIRPODS_ATTRIBUTE, AIRPODS_MODELS)], built.axes)
        self.assertEqual(4, built.count)
        self.assertEqual(
            {(phone, airpods) for phone in PHONE_MODELS for airpods in AIRPODS_MODELS},
            {(combo["مدل"], combo[AIRPODS_ATTRIBUTE]) for combo in built.combos},
        )

    def test_singleton_mixed_device_axes_are_not_dropped(self):
        built = build_plan(["iPhone 17", "AirPod pro2"], {})
        self.assertEqual([("مدل", ["iPhone 17"]), (AIRPODS_ATTRIBUTE, ["AirPods Pro 2"])], built.axes)
        self.assertTrue(built.is_variable)
        self.assertEqual(1, built.count)
        self.assertEqual([], built.dropped)

    def test_ordinary_singleton_attributes_still_are_not_axes(self):
        built = build_plan(["iPhone 17"], {"رنگ": ["مشکی"]})
        self.assertFalse(built.is_variable)

    def test_attribute_aliases_are_canonical_and_routing_does_not_mutate_inputs(self):
        attributes = {"ایرپاد:": ["AirPod 1/2", "AirPod pro2"], "طرح": ["A", "B"]}
        primary, attrs = split_device_axes(PHONE_MODELS, attributes)
        self.assertEqual(PHONE_MODELS, primary)
        self.assertEqual(AIRPODS_MODELS, attrs[AIRPODS_ATTRIBUTE])
        self.assertNotIn("ایرپاد:", attrs)
        self.assertIn("ایرپاد:", attributes)
        self.assertEqual((primary, attrs), split_device_axes(primary, attrs))

    def test_no_phone_means_airpods_attribute_moves_back_to_model(self):
        primary, attrs = split_device_axes([], {AIRPODS_ATTRIBUTE: AIRPODS_MODELS})
        self.assertEqual(AIRPODS_MODELS, primary)
        self.assertEqual({}, attrs)

    def test_airpods_color_section_is_not_the_last_phones_color(self):
        text = "iPhone 17: مشکی/سفید\nAirPod 1/2: سفید\nAirPod pro2: مشکی"
        matrix = parse_color_matrix(text)
        self.assertEqual(["مشکی", "سفید"], matrix.by_model["iPhone 17"])
        self.assertEqual(["سفید"], matrix.by_model["AirPods 1/2"])
        self.assertEqual(["مشکی"], matrix.by_model["AirPods Pro 2"])
        built = build_plan(
            ["iPhone 17", *AIRPODS_MODELS], {"رنگ": matrix.colors}, matrix.by_model
        )
        self.assertEqual(2, built.count)
        self.assertEqual(
            {("AirPods 1/2", "سفید"), ("AirPods Pro 2", "مشکی")},
            {(combo[AIRPODS_ATTRIBUTE], combo["رنگ"]) for combo in built.combos},
        )


@needs_flow
class TestAirPodsExtraction(unittest.TestCase):
    def setUp(self):
        self.enterContext(h.patched_settings(h.settings_with(ai_base_url="", ai_token="", ai_model="")))
        for name in ("AI_BASE_URL", "AI_TOKEN", "AI_MODEL"):
            self.enterContext(patch.object(ai_normalizer, name, ""))

    def extract(self, caption, info=INFO, **over):
        session = PF.ProductSession(model_text=caption, info_text=info, **over)
        data = asyncio.run(PF._extract(session, learn=False))
        return session, data

    def test_airpods_only_product_has_two_model_variations(self):
        session, data = self.extract(AIRPODS_CAPTION, "قاب ایرپاد طرح ماسا\nLP\n728t")
        self.assertEqual(AIRPODS_MODELS, data.models)
        self.assertEqual(AIRPODS_MODELS, session.models)
        self.assertNotIn(AIRPODS_ATTRIBUTE, data.attributes)
        self.assertEqual(728_000, data.price)
        self.assertEqual(2, plan_from_dict(data.to_dict()).count)
        self.assertEqual(
            ["لوازم جانبی ایرپاد > Apple AirPods > Airpods 1/2",
             "لوازم جانبی ایرپاد > Apple AirPods > Airpods Pro 2"],
            data.categories,
        )

    def test_mixed_product_keeps_phone_models_and_both_category_families(self):
        session, data = self.extract(MIXED_CAPTION)
        self.assertEqual(PHONE_MODELS, data.models)
        self.assertEqual(PHONE_MODELS, session.models)
        self.assertEqual(AIRPODS_MODELS, data.attributes[AIRPODS_ATTRIBUTE])
        self.assertEqual("قاب طرح ماسا", data.title)
        self.assertEqual("LP", data.sku_prefix)
        self.assertEqual(728_000, data.price)
        self.assertEqual(4, plan_from_dict(data.to_dict()).count)
        self.assertIn("قاب و کاور گوشی و تبلت > آیفون iphone", data.categories)
        self.assertIn("لوازم جانبی ایرپاد > Apple AirPods > Airpods Pro 2", data.categories)
        self.assertNotIn("لوازم جانبی ایرپاد > Apple AirPods > Airpods Pro", data.categories)

    def test_collecting_caption_already_separates_device_axes(self):
        _session, data = self.extract(MIXED_CAPTION, info="", defer_details=True)
        self.assertEqual(PHONE_MODELS, data.models)
        self.assertEqual(AIRPODS_MODELS, data.attributes[AIRPODS_ATTRIBUTE])

    def test_direct_extractor_fallback_obeys_the_same_policy(self):
        data = asyncio.run(product_extractor.extract_product(
            MIXED_CAPTION + "\n" + INFO, PHONE_MODELS, "", caption=MIXED_CAPTION, info_text=INFO
        ))
        self.assertEqual(PHONE_MODELS, data.models)
        self.assertEqual(AIRPODS_MODELS, data.attributes[AIRPODS_ATTRIBUTE])

    def test_manual_model_editor_preserves_slashes_and_canonicalizes_airpods(self):
        self.assertEqual(AIRPODS_MODELS, draft_edits.parse_models(AIRPODS_CAPTION))
        self.assertEqual(["iPhone 13/14"], draft_edits.parse_models("iPhone 13/14"))
        self.assertEqual(AIRPODS_MODELS, draft_edits.parse_airpods_attribute(AIRPODS_CAPTION))

    def test_manual_airpods_axis_supports_one_option_and_survives_reextraction(self):
        session, data = self.extract(MIXED_CAPTION)
        self.assertIsNone(draft_edits.apply_edit(data, "attr:ایرپاد", "AirPod pro2"))
        data = asyncio.run(PF._extract(session, learn=False))
        self.assertEqual(["AirPods Pro 2"], data.attributes[AIRPODS_ATTRIBUTE])
        self.assertEqual(PHONE_MODELS, data.models)
        self.assertEqual(2, plan_from_dict(data.to_dict()).count)

    def test_update_draft_and_structure_support_singleton_mixed_axes(self):
        from _update_shop import shop_product

        product = shop_product(models=["iPhone 17"], colors=["مشکی", "سفید"])
        result = update_plan.build(product, {
            "models": ["iPhone 17", "AirPod pro2"], "price": 728_000
        }, image_count=0)
        self.assertTrue(result.can_apply, result.errors)
        self.assertFalse(result.regenerate, "ترکیب‌های مشترک با ID قبلی باقی می‌مانند")
        self.assertTrue(all(dict(row.combo)[AIRPODS_ATTRIBUTE] == "AirPods Pro 2" for row in result.creates))
        self.assertTrue(all(dict(row.combo)["مدل"] == "iPhone 17" for row in result.creates))


@needs_flow
class TestAirPodsAiContracts(unittest.TestCase):
    def setUp(self):
        self.enterContext(h.temp_metrics())

    def response(self, content):
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]},
            request=httpx.Request("POST", "https://ai.example/v1/chat/completions"),
        )

    def test_empty_ai_answer_preserves_phone_and_airpods_candidates(self):
        async def post(_client, _url, **_kwargs):
            return self.response({"models": []})

        with (
            patch.object(ai_normalizer, "AI_BASE_URL", "https://ai.example/v1"),
            patch.object(ai_normalizer, "AI_TOKEN", "test-token"),
            patch.object(ai_normalizer, "AI_MODEL", "test-model"),
            patch("httpx.AsyncClient.post", new=post),
        ):
            output = asyncio.run(ai_normalizer.ai_normalize(MIXED_CAPTION, normalize_product_models(MIXED_CAPTION)))
        self.assertEqual([*PHONE_MODELS, *AIRPODS_MODELS], output.split(" | "))

    def test_ai_airpods_output_is_canonical_and_other_accessories_still_are_rejected(self):
        self.assertEqual(
            AIRPODS_MODELS,
            ai_normalizer._clean_model_list(["AirPod 1/2", "AirPod pro2", "ایرپاد پرو۲", "AirPods", "case", "watch"]),
        )

    def details_request(self, caption, answer):
        seen = []

        async def post(_client, _url, **kwargs):
            seen.append(kwargs["json"])
            return self.response(answer)

        with (
            h.patched_settings(h.settings_with(
                ai_base_url="https://ai.example/v1", ai_token="test-token", ai_model="test-model"
            )),
            patch("httpx.AsyncClient.post", new=post),
        ):
            data = asyncio.run(product_extractor.extract_product(
                caption + "\n" + INFO, normalize_product_models(caption).split(" | "), "",
                caption=caption, info_text=INFO,
            ))
        return data, seen[0]

    def test_ai_cannot_omit_or_invent_the_mixed_airpods_axis(self):
        data, payload = self.details_request(MIXED_CAPTION, {
            "title": "عنوان تبلیغاتی", "price": 1,
            "attributes": {"AirPods": ["AirPods 3", "AirPods 4"]}, "categories": [],
        })
        self.assertEqual(PHONE_MODELS, data.models)
        self.assertEqual(AIRPODS_MODELS, data.attributes[AIRPODS_ATTRIBUTE])
        self.assertEqual("قاب طرح ماسا", data.title)
        self.assertEqual(728_000, data.price)
        self.assertNotIn("AirPods", data.attributes)
        user = payload["messages"][1]["content"]
        self.assertIn("AIRPODS MODELS:\n" + json.dumps(AIRPODS_MODELS, ensure_ascii=False), user)
        self.assertIn("MODEL OPTIONS:\n" + json.dumps(PHONE_MODELS, ensure_ascii=False), user)
        self.assertIn("AIRPODS AXIS POLICY", payload["messages"][0]["content"])

    def test_ai_airpods_attribute_is_removed_when_only_airpods_are_present(self):
        data, _payload = self.details_request(AIRPODS_CAPTION, {
            "attributes": {"ایرپاد": AIRPODS_MODELS}, "categories": [],
        })
        self.assertEqual(AIRPODS_MODELS, data.models)
        self.assertNotIn(AIRPODS_ATTRIBUTE, data.attributes)

    def test_ai_cannot_add_airpods_to_phone_only_product(self):
        data, _payload = self.details_request("iPhone 17\niPhone 17Pro", {
            "attributes": {"ایرپاد:": AIRPODS_MODELS}, "categories": [],
        })
        self.assertEqual(PHONE_MODELS, data.models)
        self.assertNotIn(AIRPODS_ATTRIBUTE, data.attributes)


@needs_flow
class TestAirPodsWooPayload(unittest.IsolatedAsyncioTestCase):
    async def publish(self, models):
        store = h.FakeStore()
        with h.patched_settings(h.settings_with()), h.temp_ledger(), h.temp_metrics(), h.no_sleep():
            await create_draft({
                "title": "قاب طرح ماسا", "sku_prefix": "LP", "price": 728_000,
                "models": models, "attributes": {},
            }, [], transport=store.transport)
        return store.product_create_body(), store.variation_items()

    async def test_mixed_product_and_variations_have_both_device_attributes(self):
        payload, variations = await self.publish([*PHONE_MODELS, *AIRPODS_MODELS])
        self.assertEqual("variable", payload["type"])
        self.assertEqual(["مدل", AIRPODS_ATTRIBUTE], [a["name"] for a in payload["attributes"]])
        self.assertEqual(4, len(variations))
        for variation in variations:
            attrs = {a["name"]: a["option"] for a in variation["attributes"]}
            self.assertIn(attrs["مدل"], PHONE_MODELS)
            self.assertIn(attrs[AIRPODS_ATTRIBUTE], AIRPODS_MODELS)
            self.assertEqual("728000", variation["regular_price"])

    async def test_airpods_only_product_has_only_the_model_attribute(self):
        payload, variations = await self.publish(AIRPODS_MODELS)
        self.assertEqual("variable", payload["type"])
        self.assertEqual(["مدل"], [a["name"] for a in payload["attributes"]])
        self.assertEqual(2, len(variations))

    async def test_singleton_mixed_product_is_variable_in_woocommerce_too(self):
        payload, variations = await self.publish(["iPhone 17", "AirPod pro2"])
        self.assertEqual("variable", payload["type"])
        self.assertEqual(["مدل", AIRPODS_ATTRIBUTE], [a["name"] for a in payload["attributes"]])
        self.assertEqual(1, len(variations))


if __name__ == "__main__":
    unittest.main()
