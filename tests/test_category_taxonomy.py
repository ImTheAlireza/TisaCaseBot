"""SKU-driven rules for the WooCommerce category taxonomy."""

from __future__ import annotations

import unittest

from bot.services.category_taxonomy import apply_sku_category_policy


class TestPrintedCategoryPolicy(unittest.TestCase):
    def test_print_category_is_removed_for_every_non_ch_sb_sku(self) -> None:
        for sku in ("AS", "BO", "CHILD", "", "123"):
            with self.subTest(sku=sku):
                categories = ["قاب و کاور گوشی و تبلت > چاپی", "قاب تبلت"]
                self.assertEqual(["قاب تبلت"], apply_sku_category_policy(categories, sku))

    def test_ch_and_sb_skus_get_one_canonical_print_category(self) -> None:
        for sku in ("CH", "ch", "CH147", "SB", "sb-02"):
            with self.subTest(sku=sku):
                categories = ["چاپی", "قاب و کاور گوشی و تبلت > چاپی"]
                self.assertEqual(
                    ["چاپی"],
                    apply_sku_category_policy(categories, sku),
                )

    def test_other_categories_are_preserved(self) -> None:
        self.assertEqual(
            ["قاب تبلت", "گلس"],
            apply_sku_category_policy(["قاب تبلت", "گلس"], "AS"),
        )


if __name__ == "__main__":
    unittest.main()
