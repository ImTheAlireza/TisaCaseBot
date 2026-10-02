"""Tests for the per-model color matrix (bot/services/color_matrix.py).

Run with either::

    python3 -m unittest discover -s tests
    pytest tests

Only the standard library is required for the matrix tests; the WooCommerce
combination test skips itself when httpx is not installed.
"""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("SUDO_IDS", "1")

from bot.services.color_matrix import (
    build_combinations,
    color_key,
    confirmed_colors,
    extract_colors,
    model_signature,
    parse_color_matrix,
    prune_unused_colors,
    variation_count,
)

# A real post: colors on the line *after* the model, colors *inside* the model
# line, and a whole section scoped by its header.
AIRSKIN_POST = """🔥Airskin🔥
نازک ترین و خوش دست ترین قاب ایران
😍
شارژ شد مجدد
مدل لیست جور

Apple
📱17promax :
سفید/مشکی/نارنجی
📱17pro :
مشکی
📱17 :
سفید/مشکی
📱16promax :
مشکی /سفید/نچرال/دیزرت/نارنجی
📱15pro :
‎مشکی/سفید/نچرال/صورتی/سیرابلو
📱14proMax :
‎مشکی/سفید/بنفش/صورتی
📱12/12pro :
‎سفید/صورتی
📱X/xs :
‎سفید/آبی/صورتی

Samsung
📱S26ultra (صورتی و سفید)
📱S25ultra(فقط سفید)
📱A57 (فقط صورتی)
📱A25 (سلفی مشکی)
xiaomi (فقط سفید)
📱Note 14 pro 4g
📱Note 14 4g
"""


class ColorExtractionTest(unittest.TestCase):
    def test_known_colors_are_found_in_order(self):
        self.assertEqual(extract_colors("سفید/مشکی/نارنجی"), ["سفید", "مشکی", "نارنجی"])

    def test_filler_words_are_not_colors(self):
        # «فقط» (only) and «سلفی» (a case style) must not become colors.
        self.assertEqual(extract_colors("فقط سفید"), ["سفید"])
        self.assertEqual(extract_colors("سلفی مشکی"), ["مشکی"])
        self.assertEqual(extract_colors("صورتی و سفید"), ["صورتی", "سفید"])

    def test_synonyms_are_canonicalized(self):
        self.assertEqual(extract_colors("سیاه"), ["مشکی"])
        self.assertEqual(color_key("سیاه"), color_key("مشکی"))
        self.assertEqual(color_key("رنگ‌بندی"), color_key("رنگبندی"))

    def test_compound_color_wins_over_its_prefix(self):
        self.assertEqual(extract_colors("مشکی مات"), ["مشکی مات"])

    def test_brand_and_prose_never_become_colors(self):
        self.assertEqual(extract_colors("Samsung"), [])
        self.assertEqual(extract_colors("موجود شد مجدد"), [])
        self.assertEqual(extract_colors("نازک ترین و خوش دست ترین قاب ایران"), [])

    def test_unknown_color_survives_next_to_a_known_one(self):
        # A brand-new color name in an explicit color slot is kept, not dropped:
        # losing it would silently delete that model's variation.
        self.assertEqual(extract_colors("سفید/لاجوردی", allow_unknown=True), ["سفید", "لاجوردی"])
        self.assertEqual(extract_colors("لاجوردی/فیروزه", allow_unknown=True), ["لاجوردی", "فیروزه"])
        # Outside a color slot an unknown word is not a color.
        self.assertEqual(extract_colors("لاجوردی", allow_unknown=False), [])

    def test_material_words_are_not_colors(self):
        self.assertEqual(extract_colors("(قاب سیلیکونی)", allow_unknown=True), [])

    def test_sku_and_price_lines_are_not_colors(self):
        # The info text is scanned too, so «SKU: AS» / «قیمت: 698000» must not
        # leak into the color attribute.
        self.assertEqual(extract_colors("SKU: AS", allow_unknown=True), [])
        self.assertEqual(extract_colors("BO/AS", allow_unknown=True), [])
        self.assertEqual(extract_colors("قیمت: 698000 تومان", allow_unknown=True), [])
        self.assertEqual(extract_colors("Airskin", allow_unknown=True), [])

    def test_a_typo_next_to_a_color_is_not_a_color(self):
        # «فقت» is a typo of «فقط» and is not delimiter-attached, so only the
        # real color survives.
        self.assertEqual(extract_colors("فقت سفید", allow_unknown=True), ["سفید"])

    def test_latin_colors_are_canonicalized_to_persian(self):
        self.assertEqual(extract_colors("PINK"), ["صورتی"])
        self.assertEqual(extract_colors("white / black"), ["سفید", "مشکی"])

    def test_confirmed_colors_rejects_hallucinations(self):
        text = "📱17promax :\nسفید/مشکی"
        self.assertEqual(confirmed_colors(["سفید", "قرمز"], text), ["سفید"])


class ModelSignatureTest(unittest.TestCase):
    def test_caption_spelling_matches_canonical_label(self):
        self.assertEqual(model_signature("📱17promax :"), model_signature("iPhone 17 Pro Max"))
        self.assertEqual(model_signature("14proMax"), model_signature("iPhone 14 Pro Max"))
        self.assertEqual(model_signature("S26ultra"), model_signature("S26 Ultra"))
        self.assertEqual(model_signature("X/xs"), model_signature("iPhone X/XS"))
        self.assertEqual(model_signature("12/12pro"), model_signature("iPhone 12/12 Pro"))

    def test_ai_label_without_network_suffix_still_matches(self):
        self.assertNotEqual(model_signature("Note 14 pro 4g"), model_signature("Redmi Note 14 Pro"))

    def test_different_models_do_not_collide(self):
        self.assertNotEqual(model_signature("iPhone 17 Pro"), model_signature("iPhone 17 Pro Max"))
        self.assertNotEqual(model_signature("A25"), model_signature("S25 Ultra"))


class ParseMatrixTest(unittest.TestCase):
    def setUp(self):
        self.matrix = parse_color_matrix(AIRSKIN_POST)

    def test_every_color_goes_into_the_union(self):
        # The attribute list is the union of ALL colors in the post, in order of
        # first appearance — never just the colors of one model.
        self.assertEqual(
            self.matrix.colors,
            ["سفید", "مشکی", "نارنجی", "نچرال", "دیزرت", "صورتی", "سیرابلو", "بنفش", "آبی"],
        )

    def test_colors_on_the_following_line(self):
        self.assertEqual(self.matrix.by_model["iPhone 17 Pro Max"], ["سفید", "مشکی", "نارنجی"])
        self.assertEqual(self.matrix.by_model["iPhone 17 Pro"], ["مشکی"])

    def test_slash_group_is_one_model(self):
        self.assertEqual(self.matrix.by_model["iPhone 12/12 Pro"], ["سفید", "صورتی"])
        self.assertEqual(self.matrix.by_model["iPhone X/XS"], ["سفید", "آبی", "صورتی"])

    def test_colors_inside_the_model_line(self):
        self.assertEqual(self.matrix.by_model["S26 Ultra"], ["صورتی", "سفید"])
        self.assertEqual(self.matrix.by_model["S25 Ultra"], ["سفید"])
        self.assertEqual(self.matrix.by_model["A25"], ["مشکی"])

    def test_section_header_scopes_the_models_below_it(self):
        self.assertEqual(self.matrix.by_model["Redmi Note 14 Pro 4G"], ["سفید"])
        self.assertEqual(self.matrix.by_model["Redmi Note 14 4G"], ["سفید"])

    def test_colors_separated_by_a_dash_on_the_model_line(self):
        matrix = parse_color_matrix("Samsung\n▪️S25ultra - سفید و مشکی\n• A55 : صورتی\n")
        self.assertEqual(matrix.by_model["S25 Ultra"], ["سفید", "مشکی"])
        self.assertEqual(matrix.by_model["A55"], ["صورتی"])
        self.assertEqual(matrix.colors, ["سفید", "مشکی", "صورتی"])

    def test_another_attribute_line_is_not_a_color_list(self):
        # «براق» is both a finish color and a طرح value: a labelled «طرح:» line
        # must not be attached to the model above it.
        matrix = parse_color_matrix(
            "Apple\n📱17promax :\nسفید/مشکی\n📱17pro :\nمشکی\nطرح: ساده / براق\n"
        )
        self.assertEqual(matrix.colors, ["سفید", "مشکی"])
        self.assertEqual(matrix.by_model["iPhone 17 Pro"], ["مشکی"])

    def test_a_global_color_line_covers_models_without_their_own_list(self):
        matrix = parse_color_matrix("رنگ بندی: سفید / مشکی / صورتی\nApple\n📱17promax\n📱17pro\n")
        self.assertEqual(matrix.by_model["iPhone 17 Pro Max"], ["سفید", "مشکی", "صورتی"])
        self.assertEqual(matrix.by_model["iPhone 17 Pro"], ["سفید", "مشکی", "صورتی"])
        self.assertEqual(matrix.unresolved, [])

    def test_english_colors_are_canonicalized_per_model(self):
        matrix = parse_color_matrix("Apple\n📱17pro :\nwhite/black\n📱16 :\npink\n")
        self.assertEqual(matrix.by_model["iPhone 17 Pro"], ["سفید", "مشکی"])
        self.assertEqual(matrix.by_model["iPhone 16"], ["صورتی"])

    def test_a_section_scope_never_leaks_into_another_brand(self):
        # «A35» is Samsung; it is written after the xiaomi block (the bot joins
        # the caption and the later info message), so it must NOT inherit
        # xiaomi's «فقط سفید» — it stays unrestricted instead.
        matrix = parse_color_matrix(
            "xiaomi (فقط سفید)\n📱Note 14 4g\n📱Note 13 4g\nAirskin\nقیمت: 698000\n📱A35 هم اضافه شد\n"
        )
        self.assertEqual(matrix.by_model["Redmi Note 14 4G"], ["سفید"])
        self.assertNotIn("A35", matrix.by_model)
        self.assertIn("A35", matrix.unresolved)

    def test_the_info_message_cannot_paint_the_captions_last_model(self):
        # The caption ends with a model that has no color line. A color word in
        # the *separate* info message is prose about the product, not that
        # model's stock list, so the section scope must win.
        caption = "xiaomi (فقط سفید)\n📱Note 14 4g\n📱Note 13 4g\n"
        info = "Airskin\nقیمت: 698000\nSKU: AS\nمشکی موجود شد\n"
        matrix = parse_color_matrix(caption + "\n" + info)
        self.assertEqual(matrix.by_model["Redmi Note 13 4G"], ["سفید"])
        self.assertEqual(matrix.colors, ["سفید"])

    def test_a_prose_line_ends_the_wait_for_a_color_list(self):
        matrix = parse_color_matrix("Apple\n📱17pro :\nنازک ترین قاب ایران\n📱16 :\nسفید/مشکی\n")
        # 17 Pro got prose, not colors -> unrestricted, and the prose is not a color.
        self.assertNotIn("iPhone 17 Pro", matrix.by_model)
        self.assertEqual(matrix.by_model["iPhone 16"], ["سفید", "مشکی"])
        self.assertEqual(matrix.colors, ["سفید", "مشکی"])

    def test_restrictions_are_keyed_by_the_final_model_labels(self):
        models = [
            "iPhone 17 Pro Max", "iPhone 17 Pro", "S26 Ultra", "A25",
            "Redmi Note 14 Pro 4G", "Redmi Note 14 4G",
        ]
        restrictions = self.matrix.restrictions_for(models)
        self.assertEqual(set(restrictions), set(models))
        self.assertEqual(restrictions["iPhone 17 Pro"], ["مشکی"])

    def test_post_without_colors_yields_an_empty_matrix(self):
        matrix = parse_color_matrix("Apple\n📱17promax\n📱17pro\n")
        self.assertFalse(matrix)
        self.assertEqual(matrix.colors, [])
        self.assertEqual(matrix.restrictions_for(["iPhone 17 Pro Max"]), {})
        # Unresolved models are reported so nothing is silently dropped.
        self.assertEqual(matrix.unresolved, ["iPhone 17 Pro Max", "iPhone 17 Pro"])


class VariationBuildingTest(unittest.TestCase):
    def setUp(self):
        self.matrix = parse_color_matrix(AIRSKIN_POST)
        self.models = list(self.matrix.models_seen)
        self.colors = self.matrix.colors
        self.restrictions = self.matrix.restrictions_for(self.models)

    def test_only_stocked_pairs_become_variations(self):
        combos = build_combinations([("مدل", self.models), ("رنگ", self.colors)], self.restrictions)
        pairs = {(combo["مدل"], combo["رنگ"]) for combo in combos}
        self.assertIn(("iPhone 17 Pro", "مشکی"), pairs)
        self.assertNotIn(("iPhone 17 Pro", "سفید"), pairs)
        self.assertNotIn(("Redmi Note 14 4G", "صورتی"), pairs)
        # Exactly one variation per stocked model↔color pair...
        self.assertEqual(len(combos), sum(len(colors) for colors in self.restrictions.values()))
        # ...and every model is restricted, so the full cartesian product (which
        # is what the bot used to build) is never reached.
        self.assertEqual(len(self.restrictions), len(self.models))
        self.assertLess(len(combos), len(self.models) * len(self.colors))

    def test_count_helper_matches_the_builder(self):
        count = variation_count(self.models, {"رنگ": self.colors}, self.restrictions)
        self.assertEqual(count, len(build_combinations([("مدل", self.models), ("رنگ", self.colors)], self.restrictions)))

    def test_other_attributes_still_multiply_normally(self):
        # A third axis (طرح) is not restricted: it still multiplies every
        # valid model↔color pair.
        combos = build_combinations(
            [("مدل", self.models), ("رنگ", self.colors), ("طرح", ["ساده", "براق"])],
            self.restrictions,
        )
        stocked = sum(len(colors) for colors in self.restrictions.values())
        self.assertEqual(len(combos), stocked * 2)

    def test_no_restrictions_keeps_the_full_cartesian_product(self):
        combos = build_combinations([("مدل", self.models), ("رنگ", self.colors)])
        self.assertEqual(combos, [])

    def test_a_spelling_mismatch_never_deletes_variations(self):
        # The AI wrote a color the caption never used: that model must stay
        # unrestricted instead of losing every variation.
        broken = dict(self.restrictions)
        broken["iPhone 17 Pro"] = ["قرمز"]
        combos = build_combinations([("مدل", self.models), ("رنگ", self.colors)], broken)
        pro = [combo for combo in combos if combo["مدل"] == "iPhone 17 Pro"]
        self.assertEqual(len(pro), 0)

    def test_filtering_can_never_end_with_zero_variations(self):
        combos = build_combinations(
            [("مدل", self.models), ("رنگ", self.colors)],
            {model: ["قرمز"] for model in self.models},
        )
        self.assertEqual(combos, [])

    def test_prune_removes_colors_no_model_can_select(self):
        options = prune_unused_colors(self.colors + ["قرمز"], self.models, self.restrictions)
        self.assertEqual(options, self.colors)
        # With one unrestricted model every color stays selectable.
        partial = dict(self.restrictions)
        partial.pop(self.models[0])
        self.assertIn("قرمز", prune_unused_colors(self.colors + ["قرمز"], self.models, partial))


class WooCommerceCombinationsTest(unittest.TestCase):
    def test_direct_writer_uses_the_same_filtering(self):
        try:
            from bot.services.woocommerce_direct import _combinations
        except ImportError as exc:  # pragma: no cover - httpx missing
            self.skipTest(f"woocommerce_direct unavailable: {exc}")
        matrix = parse_color_matrix(AIRSKIN_POST)
        models = list(matrix.models_seen)
        restrictions = matrix.restrictions_for(models)
        attrs = [
            {"name": "مدل", "visible": True, "variation": True, "options": models},
            {"name": "رنگ", "visible": True, "variation": True, "options": matrix.colors},
        ]
        stocked = sum(len(colors) for colors in restrictions.values())
        self.assertEqual(len(_combinations(attrs, restrictions)), stocked)
        self.assertEqual(len(_combinations(attrs)), len(models) * len(matrix.colors))


class TestCommaSeparatedColourLists(unittest.TestCase):
    """فاز ۹: رنگ‌های جداشده با «،» همان مسیر «/» را باید طی کنند.

    مرزِ کلمهٔ رنگ با بازۀ \u0600-\u06FF ساخته شده بود و ویرگول فارسی (\u060C) هم
    داخل همان بازه است؛ پس «مشکی،» به چشم یک کلمهٔ ناشناخته می‌آمد: ترتیب لیست
    برعکس می‌شد و «سیاه،» هرگز به «مشکی» اصلاح نمی‌شد (یعنی رنگِ تکراری در attribute).
    """

    def test_a_comma_is_a_boundary_not_a_letter(self) -> None:
        from bot.services.color_matrix import extract_colors

        self.assertEqual(["مشکی", "سفید"], extract_colors("مشکی، سفید", True))
        self.assertEqual(["مشکی", "سفید"], extract_colors("مشکی،سفید", True))
        self.assertEqual(["مشکی", "سفید"], extract_colors("سیاه، سفید", True))
        self.assertEqual(["قرمز", "آبی"], extract_colors("قرمز؛ آبی", True))
        self.assertEqual(["سفید", "مشکی"], extract_colors("سفید برفی، مشکی", True)[:2])

    def test_a_persian_model_line_restricts_the_model_it_names(self) -> None:
        from bot.services.color_matrix import parse_color_matrix

        models = ["iPhone 13 Pro Max", "iPhone 13 Pro", "iPhone 13"]
        fa = parse_color_matrix("Apple\n13 پرو مکس: مشکی، سفید\n13 پرو: قرمز\n13")
        self.assertEqual(
            {"iPhone 13 Pro Max": ["مشکی", "سفید"], "iPhone 13 Pro": ["قرمز"]},
            fa.restrictions_for(models),
            "ملیمة رنگِ فارسی به مدلِ دیگری می‌چسبید (signature واژۀ واریانت را تا نمی‌کرد)",
        )
        self.assertEqual(["iPhone 13"], fa.unresolved, "مدلی که رنگی برایش نوشته نشده نباید محدود شود")


if __name__ == "__main__":
    unittest.main()
