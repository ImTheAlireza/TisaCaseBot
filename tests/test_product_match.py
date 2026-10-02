"""Finding a product in the shop and reading what the shop says today (``product_match``).

The lookup is the first step of «🔄 اپدیت محصول»: the seller types a SKU or a few words of the
title and the shop's own answer comes back as buttons. What matters here is what the shop is
asked — and in which order — and what happens when it answers something unfriendly (a 404 on a
SKU, a 400 on ``status=any``, a body that is not a list). The reading of variations (paging,
completeness) is tested next to the diff that depends on it, in ``test_update_apply``.

Run with ``python -m pytest tests/test_product_match.py`` or
``python3 -m unittest discover -s tests``.
"""
from __future__ import annotations

import asyncio
import os
import unittest

os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("SUDO_IDS", "1234567")

import _flow_harness as h
from _update_shop import product_row

try:
    from bot.services import product_match

    HAS_SERVICES = True
except Exception:                          # pragma: no cover - httpx missing
    product_match = None                   # type: ignore[assignment]
    HAS_SERVICES = False

needs_services = unittest.skipUnless(HAS_SERVICES, "httpx is not installed")


@needs_services
class FindTest(unittest.TestCase):
    """What the shop is asked, and in which order."""

    def setUp(self) -> None:
        self.enterContext(h.patched_settings(h.settings_with()))
        self.enterContext(h.no_sleep())

    def test_an_sku_is_looked_up_exactly_and_nothing_else(self) -> None:
        script = h.TransportScript(h.respond(200, product_row(id=42)))
        found = asyncio.run(product_match.find("BO7", transport=script.transport()))
        self.assertEqual([item.product_id for item in found], [42])
        self.assertEqual(script.methods, ["GET /wp-json/wc/v3/products/sku/BO7"],
                         "با SKU دقیق، جستجوی عنوان نباید صدا زده شود")

    def test_a_sku_the_shop_does_not_have_falls_back_to_the_title(self) -> None:
        script = h.TransportScript(
            h.respond(404, {"message": "No route found"}),
            h.respond(200, [product_row(id=42), product_row(id=43, name="قاب دیگر")]),
        )
        found = asyncio.run(product_match.find("BO7", transport=script.transport()))
        self.assertEqual([item.product_id for item in found], [42, 43])
        self.assertEqual(len(script.methods), 2, "اول SKU، بعد عنوان — نه برعکس")

    def test_a_host_that_refuses_status_any_is_asked_again_without_it(self) -> None:
        script = h.TransportScript(
            h.respond(400, {"message": "status is not one of the allowed values"}),
            h.respond(200, [product_row(id=42)]),
        )
        found = asyncio.run(product_match.find("قاب", transport=script.transport()))
        self.assertEqual(len(found), 1)
        self.assertIn("status=any", str(script.requests[0].url))
        self.assertNotIn("status=", str(script.requests[1].url),
                         "تلاش دوم باید فیلتر را بردارد، نه اینکه همان را تکرار کند")

    def test_a_response_that_is_not_a_list_is_an_error_not_zero_results(self) -> None:
        script = h.TransportScript(h.respond(200, {"message": "توضیحی"}))
        with self.assertRaises(product_match.WooCommerceAPIError):
            asyncio.run(product_match.find("قاب", transport=script.transport()))

    def test_a_maintenance_page_is_an_error_not_a_crash(self) -> None:
        # The shop answers 200 with HTML: every reader must say «JSON نبود», not die in a parser.
        script = h.TransportScript(default=(200, None))
        with self.assertRaises(product_match.WooCommerceAPIError) as caught:
            asyncio.run(product_match.find("قاب", transport=script.transport()))
        self.assertIn("JSON نبود", str(caught.exception))
        with self.assertRaises(product_match.WooCommerceAPIError):
            asyncio.run(product_match.read(42, transport=h.TransportScript(default=(200, None)).transport()))
        script = h.TransportScript(h.respond(200, product_row(id=42)), default=(200, None))
        prod = asyncio.run(product_match.read(42, transport=script.transport()))
        self.assertFalse(prod.variations_complete, "واریژن‌هایی که JSON نبودند، «خالی» نیستند")
        self.assertIn("JSON نبود", prod.notes[-1])

    def test_the_button_label_says_what_we_know_and_what_we_do_not(self) -> None:
        label = product_match.Candidate.from_row(product_row(id=42, sku="", status="draft")).label()
        self.assertIn("#42", label)
        self.assertIn("SKU ندارد", label)
        self.assertIn("پیش‌نویس", label)

    def test_read_keeps_a_variations_failure_in_notes(self) -> None:
        script = h.TransportScript(h.respond(200, product_row(id=42)), h.respond(500, {"message": "boom"}))
        prod = asyncio.run(product_match.read(42, transport=script.transport()))
        self.assertEqual(prod.product_id, 42)
        self.assertEqual(prod.variations, [])
        self.assertTrue(prod.notes and "500" in prod.notes[0],
                        "«واریژنی نیست» با «واریژن‌ها خوانده نشد» یکی نیست")
        self.assertFalse(prod.variations_complete)


@needs_services
class DryRunStoreTest(unittest.TestCase):
    """The rehearsal has to read something, or it is theatre."""

    def setUp(self) -> None:
        self.enterContext(h.patched_settings(h.settings_with()))

    def test_the_demo_catalog_is_searchable_and_readable(self) -> None:
        found = asyncio.run(product_match.find("دمو", dry_run=True))
        self.assertTrue(found)
        prod = asyncio.run(product_match.read(found[0].product_id, dry_run=True))
        self.assertEqual(len(prod.variations), 2)
        self.assertEqual(len(prod.image_ids), 2, "برای تمرینِ «جای ۲ تصویر فعلی»")
        self.assertEqual([item["name"] for item in prod.attributes], ["مدل", "رنگ"])
        self.assertTrue(prod.variations_complete)

    def test_an_unrelated_search_does_not_invent_a_product(self) -> None:
        # The SKU machinery searches by candidate code. If the demo store answered that, every
        # dry run would report «this SKU is taken» and the rehearsal would start lying.
        self.assertEqual(asyncio.run(product_match.find("BO12", dry_run=True)), [])


if __name__ == "__main__":
    unittest.main()
