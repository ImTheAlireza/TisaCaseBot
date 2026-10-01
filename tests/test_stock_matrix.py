"""Regression tests for explicit per-design × phone-category inventory tables."""
from __future__ import annotations

import asyncio
import os
import unittest
from dataclasses import replace
from unittest.mock import patch

os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("SUDO_IDS", "1234567")

from bot.services import postmodel as evidence
from bot.services import product_extractor
from bot.services.stock_matrix import (
    parse_stock_matrix,
    parse_stock_matrix_sources,
    strip_stock_matrix_sections,
)


EXAMPLE = """قاب لنز طرح‌دار
موجودی ماتریسی:
طرح | iPhone 13 | iPhone 13 Pro/13 Pro Max
پروانه آبی | ۷ | ۰
پاپیون صورتی | - | ۳

متن بعد از جدول
"""


class TestStockMatrixParser(unittest.TestCase):
    def test_reads_persian_digits_zero_and_unavailable_cells(self) -> None:
        matrix = parse_stock_matrix(EXAMPLE, source="info")
        self.assertTrue(matrix.valid, matrix.errors)
        self.assertEqual(["iPhone 13", "iPhone 13 Pro/13 Pro Max"], matrix.models)
        self.assertEqual(["پروانه آبی", "پاپیون صورتی"], matrix.designs)
        self.assertEqual(7, matrix.quantities["پروانه آبی"]["iPhone 13"])
        self.assertEqual(0, matrix.quantities["پروانه آبی"]["iPhone 13 Pro/13 Pro Max"])
        self.assertIsNone(matrix.quantities["پاپیون صورتی"]["iPhone 13"])
        self.assertEqual(3, matrix.sellable_cells)
        self.assertEqual(10, matrix.total_stock)

    def test_product_info_matrix_beats_caption_matrix(self) -> None:
        info = "موجودی ماتریسی:\nطرح | iPhone 13\nA | 7"
        caption = "موجودی ماتریسی:\nطرح | iPhone 13\nA | 99"
        matrix = parse_stock_matrix_sources([("info", info), ("caption", caption)])
        self.assertEqual("info", matrix.source)
        self.assertEqual(7, matrix.quantities["A"]["iPhone 13"])

    def test_latest_matrix_in_one_source_is_a_correction(self) -> None:
        text = "موجودی ماتریسی:\nطرح | iPhone 13\nA | 7\n\nموجودی ماتریسی:\nطرح | iPhone 13\nA | 5"
        matrix = parse_stock_matrix(text)
        self.assertEqual(5, matrix.quantities["A"]["iPhone 13"])

    def test_malformed_cell_is_reported_and_not_silently_coerced(self) -> None:
        matrix = parse_stock_matrix("موجودی ماتریسی:\nطرح | iPhone 13 | iPhone 14\nA | 7")
        self.assertTrue(matrix.errors)
        self.assertIn("دقیقاً 2", matrix.errors[0])

    def test_matrix_section_is_removed_from_free_text_extraction(self) -> None:
        cleaned = strip_stock_matrix_sections(EXAMPLE)
        self.assertIn("قاب لنز طرح‌دار", cleaned)
        self.assertIn("متن بعد از جدول", cleaned)
        self.assertNotIn("پروانه آبی", cleaned)
        self.assertNotIn("iPhone 13 Pro/13 Pro Max", cleaned)


class TestStockMatrixExtraction(unittest.TestCase):
    def test_matrix_header_is_authoritative_in_the_product_flow(self) -> None:
        from bot.modules import product_flow

        info = """عنوان: قاب لنز طرح‌دار
SKU: PT45
قیمت 100000

موجودی ماتریسی:
طرح | iPhone 13 | iPhone 14
پروانه آبی | 7 | 0
پاپیون صورتی | - | 3
"""
        offline = replace(
            product_extractor.settings,
            ai_base_url="",
            ai_token="",
            ai_model="",
        )
        session = product_flow.ProductSession(model_text="iPhone 12", info_text=info)
        with (
            patch.object(product_extractor, "settings", offline),
            patch.object(product_flow, "settings", offline),
        ):
            data = asyncio.run(product_flow._extract(session, learn=False))
        self.assertEqual(["iPhone 13", "iPhone 14"], session.models)
        self.assertEqual(["iPhone 13", "iPhone 14"], data.models)
        self.assertEqual(["پروانه آبی", "پاپیون صورتی"], data.attributes["طرح"])
        self.assertNotIn("رنگ", data.attributes, "رنگ داخل نام طرح محور جدا نمی‌سازد")
        self.assertFalse(any("فهرست مدل‌ها با برداشت هوش مصنوعی" in note for note in data.notes))

    def test_matrix_defines_variation_axes_and_does_not_become_global_stock(self) -> None:
        info = """عنوان: قاب لنز طرح‌دار
SKU: PT45
قیمت 100000

موجودی ماتریسی:
طرح | iPhone 13 | iPhone 14
A | 7 | ۰
B | - | 3
"""
        offline = replace(
            product_extractor.settings,
            ai_base_url="",
            ai_token="",
            ai_model="",
        )
        with patch.object(product_extractor, "settings", offline):
            data = asyncio.run(product_extractor.extract_product(
                info,
                ["iPhone 12"],
                "",
                caption="",
                info_text=info,
            ))
        self.assertEqual(["iPhone 13", "iPhone 14"], data.models)
        self.assertEqual(["A", "B"], data.attributes["طرح"])
        self.assertIsNone(data.stock)
        self.assertEqual(
            {"A": {"iPhone 13": 7, "iPhone 14": 0}, "B": {"iPhone 13": None, "iPhone 14": 3}},
            data.stock_matrix,
        )
        self.assertEqual([], data.stock_matrix_errors)
        self.assertEqual(evidence.INFO, data.evidence["stock_matrix"].source)


if __name__ == "__main__":
    unittest.main()
