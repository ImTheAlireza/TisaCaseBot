"""«🔄 اپدیت محصول» — the write: what is sent, in which order, and what the shop confirmed.

Every test here runs the real reader, the real planner and the real writer against
:class:`_update_shop.UpdateShop` — a shop that *remembers* — and then asks the shop what it holds.
That is the only honest way to test «delete the old models, add the new ones, change the price»:
a mock that answers «fine» to everything would pass while the product ended up half-built.

The order is part of the contract (pictures → product → create/update → delete), because a
failure half-way must leave something sellable; so is verification from the shop's own answer.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

import httpx

os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("SUDO_IDS", "1234567")

import _flow_harness as h
from _update_shop import UpdateShop, product_row, variation_row

try:
    from bot.services import product_match, update_apply, update_plan
    from bot.services.woo_client import Audit

    HAS_SERVICES = True
except Exception:                         # pragma: no cover - httpx missing
    product_match = update_apply = update_plan = Audit = None  # type: ignore[assignment]
    HAS_SERVICES = False

needs_services = unittest.skipUnless(HAS_SERVICES, "httpx is not installed")

PRODUCT_ID = 1201
NEW_MODELS = ["iPhone 13 Pro Max", "iPhone 15", "iPhone 15 Pro"]


class ShopTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(h.patched_settings(h.settings_with()))
        self.enterContext(h.no_sleep())
        self.tmp = Path(tempfile.mkdtemp(prefix="tisa-update-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.shop = UpdateShop()

    def photos(self, *names: str) -> list[Path]:
        return [h.write_image(self.tmp, name) for name in (names or ("01_a.jpg",))]

    def update(self, data: dict, files: list[Path] | None = None, *, shop: UpdateShop | None = None,
               audit: Audit | None = None, baseline=None):
        """read → plan → apply, all against the fake shop; returns ``(plan, result)``."""
        shop = shop or self.shop

        async def go():
            product = await product_match.read(PRODUCT_ID, transport=shop.transport)
            plan = update_plan.build(product, data, image_count=len(files or []), baseline=baseline)
            result = await update_apply.apply(plan, files or [], audit=audit, transport=shop.transport)
            return plan, result

        return asyncio.run(go())


@needs_services
class WhatTheShopHoldsAfterwards(ShopTestCase):
    def test_new_models_replace_old_ones_and_the_price_and_stock_land_everywhere(self) -> None:
        files = self.photos("01_a.jpg", "02_b.jpg", "03_c.jpg")
        plan, result = self.update(
            {"models": NEW_MODELS, "price": 720_000, "stock": 12}, files)
        self.assertTrue(result.ok, result.summary())
        self.assertEqual(self.shop.options("مدل"), NEW_MODELS)
        self.assertEqual(self.shop.options("رنگ"), ["مشکی", "سفید", "سبز"], "رنگ‌ها نگفتی؛ دست نخورد")
        models_in_shop = {values[0] for values in self.shop.grid()}
        self.assertEqual(models_in_shop, set(NEW_MODELS), "S24 Ultra رفته، دو مدل تازه آمده")
        self.assertEqual({row["regular_price"] for row in self.shop.variations.values()}, {"720000"})
        self.assertEqual({row["stock_quantity"] for row in self.shop.variations.values()}, {12})
        self.assertEqual(len(self.shop.product["images"]), 3, "گالری با سه عکس تازه جایگزین شد")
        self.assertEqual(result.created, 8, "کل شبکه از نو: ۲ ترکیبِ iPhone 13 + ۳ + ۳")
        self.assertEqual(result.deleted, 3, "هر سه واریژنِ قبلی پاک شد")
        self.assertEqual(result.updated, [], "چیزی درجا عوض نشد؛ همه از نو ساخته شد")
        self.assertEqual(plan.variations_after, len(self.shop.variations))

    def test_a_changed_list_rebuilds_every_variation_even_where_the_model_stays(self) -> None:
        before = set(self.shop.variations)
        self.update({"models": NEW_MODELS, "price": 720_000})
        self.assertFalse(before & set(self.shop.variations),
                         "هیچ‌کدام از واریژن‌های قبلی (حتی iPhone 13 Pro Max) نمانده؛ همه شناسهٔ تازه دارند")
        self.assertEqual(len(self.shop.variations), 8)
        self.assertEqual(len(self.shop.grid()), 8, "هیچ ترکیبی دوبار نیست")
        self.assertIsNotNone(self.shop.by_values("iPhone 13 Pro Max", "مشکی"))

    def test_a_list_that_only_repeats_the_shop_leaves_the_variations_alone(self) -> None:
        before = set(self.shop.variations)
        _plan, result = self.update({"models": ["iPhone 13 Pro Max", "S24 Ultra"], "price": 720_000})
        self.assertTrue(result.ok, result.summary())
        self.assertEqual(set(self.shop.variations), before, "ساختار عوض نشده؛ فقط قیمت درجا نوشته شد")
        self.assertEqual((result.created, result.deleted, len(result.updated)), (0, 0, 3))

    def test_a_rebuild_loses_nothing_the_seller_did_not_write(self) -> None:
        shop = UpdateShop(variations=[
            {**variation_row(1, "iPhone 13 Pro Max", "مشکی", stock=4, sale="598000"), "image": {"id": 71}},
            {**variation_row(2, "iPhone 13 Pro Max", "سفید", stock=0, status="outofstock"), "image": {"id": 72}},
            variation_row(3, "S24 Ultra", "سبز", stock=7, price="598000"),
        ])
        _plan, result = self.update({"models": NEW_MODELS}, shop=shop)
        self.assertTrue(result.ok, result.summary())
        black = shop.by_values("iPhone 13 Pro Max", "مشکی")
        white = shop.by_values("iPhone 13 Pro Max", "سفید")
        assert black is not None and white is not None
        self.assertEqual((black["regular_price"], black["sale_price"], black["stock_quantity"]),
                         ("698000", "598000", 4), "قیمت، قیمت ویژه و موجودی قبلی")
        self.assertEqual((black["image"], white["image"]), ({"id": 71}, {"id": 72}), "عکس هر واریژن")
        self.assertEqual((white["stock_quantity"], white["stock_status"]), (0, "outofstock"))
        fresh = shop.by_values("iPhone 15", "مشکی")
        assert fresh is not None
        self.assertEqual(fresh["image"], {"id": 71}, "مدل تازه عکسِ رنگِ مشکی را می‌گیرد")
        self.assertFalse(fresh["manage_stock"], "موجودی ننوشتی؛ مدلِ تازه بدون شمارش")

    def test_a_rebuild_does_not_put_a_hidden_variation_back_on_sale(self) -> None:
        shop = UpdateShop(variations=[
            variation_row(1, "iPhone 13 Pro Max", "مشکی"),
            {**variation_row(2, "iPhone 13 Pro Max", "سفید"), "status": "private"}])
        _plan, result = self.update({"models": ["iPhone 13 Pro Max", "iPhone 15"], "price": 698_000}, shop=shop)
        self.assertTrue(result.ok, result.summary())
        white = shop.by_values("iPhone 13 Pro Max", "سفید")
        black = shop.by_values("iPhone 13 Pro Max", "مشکی")
        assert white is not None and black is not None
        self.assertEqual((white["status"], black["status"]), ("private", "publish"))

    def test_the_rows_that_duplicate_an_existing_combination_are_sent_first(self) -> None:
        self.update({"models": ["iPhone 15", "iPhone 13 Pro Max"], "price": 720_000})
        sent = self.shop.body("POST", "/variations/batch").get("create") or []
        models = [row["attributes"][0]["option"] for row in sent]
        self.assertEqual(models[:2], ["iPhone 13 Pro Max", "iPhone 13 Pro Max"],
                         "اول ترکیب‌هایی که از قبل هستند، تا شکستِ وسط کار همیشه یک «همزاد» باقی بگذارد")
        orders = {tuple(item["option"] for item in row["attributes"]): row["menu_order"] for row in sent}
        self.assertLess(orders[("iPhone 15", "مشکی")], orders[("iPhone 13 Pro Max", "مشکی")],
                        "ترتیبِ نمایش همان ترتیبِ فهرستِ فروشنده است، نه ترتیبِ ارسال")

    def test_the_order_is_pictures_then_product_then_variations_then_deletes(self) -> None:
        self.update({"models": NEW_MODELS, "price": 720_000}, self.photos("01_a.jpg", "02_b.jpg"))
        kinds = []
        for method, path, _params, body in self.shop.requests:
            if path.endswith("/media"):
                kinds.append("media")
            elif method == "PUT" and path.endswith(f"/products/{PRODUCT_ID}"):
                kinds.append("product")
            elif path.endswith("/variations/batch"):
                kinds.append("delete" if '"delete"' in body else "variations")
            elif method == "GET":
                continue
            else:
                kinds.append(f"{method} {path}")
        compact = [kind for index, kind in enumerate(kinds) if index == 0 or kind != kinds[index - 1]]
        self.assertEqual(compact, ["media", "product", "variations", "delete"])

    def test_only_what_changed_is_sent(self) -> None:
        self.update({"price": 720_000})
        self.assertEqual(self.shop.calls("PUT", f"/products/{PRODUCT_ID}"), [],
                         "فقط قیمت واریژن‌ها عوض شد؛ خودِ محصول (عنوان/گالری/ویژگی) نباید PUT شود")
        self.assertEqual(self.shop.calls("POST", "/media"), [])
        body = self.shop.body("POST", "/variations/batch")
        self.assertEqual(set(body), {"update"})
        for row in body["update"]:
            self.assertEqual(set(row), {"id", "regular_price", "tisa_fence"},
                             "امضای قفلِ امنیتی (tisa_fence) همراه هر ردیف می‌رود")

    def test_a_title_alone_is_one_product_write_and_no_variation_request(self) -> None:
        plan, result = self.update({"title": "قاب مگنتی"})
        self.assertTrue(result.ok)
        self.assertEqual(self.shop.product["name"], "قاب مگنتی")
        self.assertEqual(self.shop.calls("POST", "/variations"), [])
        self.assertEqual(plan.variations_after, 3)

    def test_asking_again_after_an_update_finds_nothing_left_to_do(self) -> None:
        data = {"models": NEW_MODELS, "price": 720_000, "stock": 12, "title": "قاب مگنتی"}
        self.update(data)
        product = asyncio.run(product_match.read(PRODUCT_ID, transport=self.shop.transport))
        again = update_plan.build(product, data)
        self.assertTrue(again.empty, "تغییر اعمال شده دیگر تفاوت نیست؛ دوباره زدن چیزی نمی‌فرستد: "
                        + "\n".join(again.change_lines()))

    def test_a_simple_product_is_written_on_itself(self) -> None:
        shop = UpdateShop(product_row(type="simple", attributes=[], manage_stock=True,
                                      stock_quantity=3), [])
        plan, result = self.update({"price": 720_000, "sale_price": 650_000, "stock": 9}, shop=shop)
        self.assertTrue(result.ok, result.summary())
        self.assertEqual((shop.product["regular_price"], shop.product["sale_price"],
                          shop.product["stock_quantity"]), ("720000", "650000", 9))
        self.assertEqual(shop.calls("GET", "/variations"), [], "ساده واریژن ندارد؛ نخواستن‌شان یک درخواست کمتر است")
        self.assertEqual(shop.calls("POST", "/variations"), [])
        self.assertTrue(result.product_updated)

    def test_the_shops_own_view_is_the_baseline_not_our_memory(self) -> None:
        self.shop.variations[9001]["stock_quantity"] = 20
        plan, _result = self.update({"stock": 20})
        self.assertNotIn(9001, [row.variation_id for row in plan.updates])


@needs_services
class TheShopIsAskedToConfirm(ShopTestCase):
    def test_a_row_the_batch_swallowed_is_written_again_on_its_own(self) -> None:
        self.shop.swallow_batch_updates = {9001}
        _plan, result = self.update({"price": 720_000})
        self.assertIn(9001, result.updated)
        self.assertEqual(self.shop.variations[9001]["regular_price"], "720000")
        self.assertEqual(len(self.shop.calls("PUT", "/variations/9001")), 1,
                         "فقط همان ردیفِ بلعیده‌شده دوباره می‌رود")
        self.assertEqual(self.shop.calls("PUT", "/variations/9002"), [], "ردیف‌های تأییدشده تکرار نمی‌شوند")

    def test_a_row_every_write_drops_is_reported_as_unconfirmed_not_as_done(self) -> None:
        self.shop.swallow_updates = {9001}
        _plan, result = self.update({"price": 720_000})
        self.assertEqual(result.unconfirmed, [9001])
        self.assertFalse(result.ok)
        self.assertIn("برنگرداند", result.summary())

    def test_an_echo_that_disagrees_is_read_once_more_and_then_believed_or_told(self) -> None:
        self.shop.stale_echo = {9001}
        _plan, result = self.update({"price": 720_000})
        self.assertIn(9001, result.updated, "یک خواندنِ مجدد دید که اعمال شده")
        self.assertEqual(result.unconfirmed, [])
        reads = self.shop.calls("GET", "/variations")
        self.assertGreaterEqual(len(reads), 2, "یکی برای خواندن اولیه، یکی برای اطمینان")

    def test_a_blank_echo_is_confirmed_by_the_reread(self) -> None:
        self.shop.blank_echo = {9002}
        _plan, result = self.update({"stock": 20})
        self.assertTrue(result.ok, result.summary())

    def test_a_refused_parent_stops_everything_after_it(self) -> None:
        self.shop.product_put_status = 400
        _plan, result = self.update({"models": NEW_MODELS, "price": 720_000}, self.photos())
        self.assertFalse(result.product_updated)
        self.assertFalse(result.changed)
        self.assertIn("نمی‌شود", result.errors[0])
        self.assertEqual(self.shop.calls("POST", "/variations"), [],
                         "ویژگی‌ها نشسته‌اند یا نه؟ نه؛ پس واریژن‌ها جایی برای ایستادن ندارند")
        self.assertEqual(len(self.shop.variations), 3, "هیچ واریژنی دست نخورد")

    def test_a_parent_that_accepts_but_does_not_keep_the_title_is_not_called_success(self) -> None:
        self.shop.product_put_ignores = {"name"}
        _plan, result = self.update({"title": "قاب مگنتی", "price": 720_000})
        self.assertFalse(result.product_updated)
        self.assertTrue(any("عنوان" in error for error in result.errors))
        self.assertEqual(self.shop.calls("POST", "/variations"), [])

    def test_a_failed_upload_writes_nothing(self) -> None:
        self.shop.media_status = 500
        _plan, result = self.update({"price": 720_000}, self.photos())
        self.assertFalse(result.changed)
        self.assertTrue(any("آپلود" in error for error in result.errors))
        self.assertEqual(self.shop.calls("PUT"), [])
        self.assertEqual(self.shop.calls("POST", "/variations"), [])

    def test_a_shop_without_the_batch_route_is_written_row_by_row(self) -> None:
        self.shop.batch_status = 404
        _plan, result = self.update({"models": NEW_MODELS, "price": 720_000})
        self.assertTrue(result.ok, result.summary())
        self.assertEqual({values[0] for values in self.shop.grid()}, set(NEW_MODELS))
        for old in (9001, 9002, 9003):
            self.assertEqual(len(self.shop.calls("DELETE", f"/variations/{old}")), 1)
            self.assertEqual(self.shop.calls("DELETE", f"/variations/{old}")[0][2].get("force"), "true")
        singles = [call for call in self.shop.calls("POST", "/variations") if not call[1].endswith("/batch")]
        self.assertEqual(len(singles), 8, "هر ترکیب با یک POST جدا ساخته شد")
        self.assertEqual(self.shop.calls("PUT", "/variations/"), [], "چیزی درجا عوض نمی‌شود")

    def test_a_batch_that_creates_is_never_retried_after_a_400(self) -> None:
        # A 400 on a creating batch may mean some rows were already made; asking one by one
        # would make them twice.
        self.shop.batch_status = 400
        _plan, result = self.update({"models": NEW_MODELS, "price": 720_000})
        self.assertFalse(result.ok)
        singles = [call for call in self.shop.calls("POST", "/variations") if not call[1].endswith("/batch")]
        self.assertEqual(singles, [])
        self.assertEqual(result.created, 0)

    def test_rows_the_shop_refuses_to_create_are_named_and_the_old_models_stay(self) -> None:
        self.shop.reject_creates = True
        _plan, result = self.update({"models": NEW_MODELS, "price": 720_000})
        self.assertTrue(result.failed)
        self.assertGreater(result.skipped_deletes, 0)
        self.assertEqual(self.shop.calls("POST", "/variations/batch")[-1][3].count('"delete"'), 0,
                         "تا ساخت تأیید نشده، چیزی حذف نمی‌شود")
        self.assertIn(9003, self.shop.variations, "S24 Ultra هنوز هست؛ محصول بی‌مدل نماند")

    def test_a_row_the_shop_names_and_refuses_is_failed_with_its_reason(self) -> None:
        # The product was edited between the card and the write: a variation we meant to update
        # is gone. The shop says so in the batch answer; that row is a failure, not «unconfirmed».
        async def go():
            product = await product_match.read(PRODUCT_ID, transport=self.shop.transport)
            plan = update_plan.build(product, {"price": 720_000})
            self.shop.variations.pop(9001)
            return await update_apply.apply(plan, transport=self.shop.transport)

        result = asyncio.run(go())
        self.assertTrue(any("9001" in line for line in result.failed))
        self.assertEqual(sorted(result.updated), [9002, 9003], "بقیه ردیف‌ها اعمال شدند")
        self.assertFalse(result.ok)

    def test_a_delete_the_shop_did_not_echo_is_checked_with_a_reread(self) -> None:
        self.shop.omit_delete_echo = True
        _plan, result = self.update({"models": NEW_MODELS, "price": 720_000})
        self.assertEqual(result.deleted, 3)
        self.assertTrue(result.ok, result.summary())

    def test_a_delete_the_shop_refuses_is_reported_not_swallowed(self) -> None:
        self.shop.refuse_delete = {9003}
        _plan, result = self.update({"models": NEW_MODELS, "price": 720_000})
        self.assertEqual(result.deleted, 2, "دو تا پاک شد، یکی را فروشگاه نداد")
        self.assertTrue(any("حذف نشد" in line for line in result.failed))
        self.assertFalse(result.ok)

    def test_a_hundred_and_fifty_rows_go_in_two_batches_and_are_read_in_two_pages(self) -> None:
        rows = [variation_row(index, f"Model {index // 3}", ("مشکی", "سفید", "سبز")[index % 3])
                for index in range(150)]
        shop = UpdateShop(product_row(attributes=[
            {"id": 0, "name": "مدل", "position": 0, "visible": True, "variation": True,
             "options": [f"Model {number}" for number in range(50)]},
            {"id": 0, "name": "رنگ", "position": 1, "visible": True, "variation": True,
             "options": ["مشکی", "سفید", "سبز"]}]), rows)
        plan, result = self.update({"price": 720_000}, shop=shop)
        self.assertEqual(len(plan.updates), 150, "هر ۱۵۰ واریژن خوانده شد، نه فقط صفحهٔ اول")
        self.assertEqual(len(shop.calls("POST", "/variations/batch")), 2)
        self.assertEqual(len(shop.calls("GET", "/variations")), 2)
        self.assertTrue(result.ok, result.summary())
        self.assertEqual({row["regular_price"] for row in shop.variations.values()}, {"720000"})


def flaky(shop: UpdateShop, when):
    """The shop, except that every request ``when`` matches dies with a read timeout."""
    def handle(request: httpx.Request) -> httpx.Response:
        if when(request):
            raise httpx.ReadTimeout("slow")
        return shop.handle(request)

    return httpx.MockTransport(handle)


@needs_services
class WhenTheNetworkOrTheHostMisbehaves(ShopTestCase):
    def plan(self, data: dict, files: int = 0):
        product = asyncio.run(product_match.read(PRODUCT_ID, transport=self.shop.transport))
        return update_plan.build(product, data, image_count=files)

    def apply(self, plan, transport, files=()):
        return asyncio.run(update_apply.apply(plan, list(files), transport=transport))

    def test_a_timeout_on_the_product_write_is_reported_and_stops_everything(self) -> None:
        plan = self.plan({"models": NEW_MODELS, "price": 720_000})
        result = self.apply(plan, flaky(self.shop, lambda r: r.method == "PUT"))
        self.assertFalse(result.changed)
        self.assertTrue(any("نوشتن خود محصول" in error for error in result.errors))
        self.assertEqual(self.shop.calls("POST", "/variations"), [])
        self.assertEqual(len(self.shop.variations), 3)

    def test_a_timeout_on_the_variation_batch_names_what_was_not_written(self) -> None:
        plan = self.plan({"price": 720_000})
        result = self.apply(plan, flaky(self.shop, lambda r: r.url.path.endswith("/variations/batch")))
        self.assertFalse(result.changed)
        self.assertTrue(result.failed)
        self.assertIn("3 واریژن به‌روز نشد", " ".join(result.failed))
        self.assertTrue(result.errors)

    def test_a_timeout_on_the_delete_batch_leaves_the_new_models_in_place(self) -> None:
        plan = self.plan({"models": NEW_MODELS, "price": 720_000})
        result = self.apply(plan, flaky(
            self.shop, lambda r: r.url.path.endswith("/variations/batch") and b'"delete"' in r.content))
        self.assertEqual(result.created, 8)
        self.assertEqual(result.deleted, 0)
        self.assertTrue(any("حذف نشد" in line for line in result.failed))
        self.assertFalse(result.ok)
        self.assertEqual({values[0] for values in self.shop.grid()} >= set(NEW_MODELS), True,
                         "تازه‌ها ساخته شده‌اند؛ فقط قدیمی هنوز هست و دوباره زدن حذفش می‌کند")

    def test_a_timeout_while_uploading_a_picture_writes_nothing(self) -> None:
        plan = self.plan({"price": 720_000}, files=1)
        result = self.apply(plan, flaky(self.shop, lambda r: r.url.path.endswith("/media")), self.photos())
        self.assertFalse(result.changed)
        self.assertTrue(any("آپلود" in error for error in result.errors))
        self.assertEqual(self.shop.calls("PUT"), [])

    def test_a_timeout_on_a_single_row_is_that_rows_failure(self) -> None:
        self.shop.batch_status = 404
        plan = self.plan({"price": 720_000})
        result = self.apply(plan, flaky(self.shop, lambda r: r.method == "PUT" and r.url.path.endswith("/9002")))
        self.assertEqual(sorted(result.updated), [9001, 9003])
        self.assertTrue(any("9002" in line for line in result.failed))

    def test_rows_that_fail_one_by_one_are_named(self) -> None:
        self.shop.batch_status = 404
        self.shop.reject_creates = True
        plan = self.plan({"models": NEW_MODELS, "price": 720_000})
        result = self.apply(plan, self.shop.transport)
        self.assertEqual(result.created, 0)
        self.assertTrue(any("ساخت" in line and "ناموفق" in line for line in result.failed))
        self.assertGreater(result.skipped_deletes, 0, "ساخت نشد؛ پس حذفی هم نیست")

    def test_rows_swallowed_one_by_one_are_unconfirmed_not_done(self) -> None:
        self.shop.batch_status = 404
        self.shop.swallow_updates = {9001}
        result = self.apply(self.plan({"price": 720_000}), self.shop.transport)
        self.assertEqual(result.unconfirmed, [9001])
        self.assertEqual(sorted(result.updated), [9002, 9003])

    def test_a_row_that_disappeared_is_a_failure_one_by_one_too(self) -> None:
        self.shop.batch_status = 404
        plan = self.plan({"price": 720_000})
        self.shop.variations.pop(9001)
        result = self.apply(plan, self.shop.transport)
        self.assertTrue(any("9001" in line for line in result.failed))
        self.assertEqual(sorted(result.updated), [9002, 9003])

    def test_deletes_the_shop_refuses_one_by_one_are_named(self) -> None:
        self.shop.batch_status = 404
        self.shop.refuse_delete = {9003}
        result = self.apply(self.plan({"models": NEW_MODELS, "price": 720_000}), self.shop.transport)
        self.assertEqual(result.deleted, 2)
        self.assertTrue(any("9003" in line for line in result.failed))

    def test_a_recheck_the_shop_cannot_answer_leaves_the_row_unconfirmed(self) -> None:
        self.shop.stale_echo = {9001}
        plan = self.plan({"price": 720_000})
        self.shop.variations_status = 500                 # the re-read after the echo fails
        result = self.apply(plan, self.shop.transport)
        self.assertEqual(result.unconfirmed, [9001], "اعتماد به حدس نه؛ تأییدنشده می‌ماند")

    def test_a_response_that_is_not_json_is_not_confirmation(self) -> None:
        # A maintenance page answers 200 to everything and carries no JSON. The fence preflight
        # is the one request that has to be answered properly before anything is written at all.
        shop = UpdateShop()

        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/tisa-health"):
                return shop.handle(request)
            return httpx.Response(200)

        plan = self.plan({"price": 720_000})
        result = self.apply(plan, httpx.MockTransport(handle))
        self.assertFalse(result.ok)
        self.assertEqual(result.updated, [], "صفحهٔ تعمیرات «موفق» نیست")
        self.assertEqual(sorted(result.unconfirmed), [9001, 9002, 9003])


@needs_services
class AskingAgainAfterARebuildStoppedHalfWay(ShopTestCase):
    """The write is not transactional: the lists are written, then variations are made, then the
    old ones go. Whatever stops it, asking again must end on the grid the first try meant — one
    variation per combination, nothing from the old grid left over, nothing missing."""

    DATA = {"models": NEW_MODELS, "price": 720_000, "stock": 12}

    def original(self):
        return asyncio.run(product_match.read(PRODUCT_ID, transport=self.shop.transport))

    def assert_converged(self) -> None:
        self.assertEqual(len(self.shop.variations), 8, sorted(self.shop.grid()))
        self.assertEqual(len(self.shop.grid()), 8, "هیچ ترکیبی دوبار نیست")
        self.assertEqual({values[0] for values in self.shop.grid()}, set(NEW_MODELS))
        self.assertEqual({row["regular_price"] for row in self.shop.variations.values()}, {"720000"})
        self.assertEqual({row["stock_quantity"] for row in self.shop.variations.values()}, {12})
        self.assertIsNone(self.shop.by_values("S24 Ultra", "سبز"))
        self.assertEqual(len(self.shop.options("مدل")), 3)

    def test_creates_that_stopped_half_way_are_completed_without_duplicates(self) -> None:
        baseline = self.original()
        self.shop.create_limit = 4            # the first four rows land, the rest are refused
        _plan, result = self.update(self.DATA, baseline=baseline)
        self.assertFalse(result.ok)
        self.assertEqual(result.created, 4)
        self.assertEqual(len(self.shop.variations), 3 + 4, "قدیمی‌ها هنوز هستند؛ چیزی حذف نشده")
        self.assertEqual(result.deleted, 0)
        self.shop.create_limit = None
        plan, result = self.update(self.DATA, baseline=baseline)
        self.assertTrue(result.ok, result.summary())
        self.assertFalse(plan.regenerate, "فهرست‌ها نشسته‌اند؛ کار نیمه‌مانده کامل می‌شود")
        self.assert_converged()
        self.assertFalse({9001, 9002, 9003} & set(self.shop.variations),
                         "از هر دو همزاد، تازه‌ترین ماند؛ هیچ واریژنِ قدیمی باقی نیست")
        self.assertEqual(result.updated, [], "همزادهای تازه قیمت و موجودیِ درست را از قبل داشتند")
        again = update_plan.build(self.original(), self.DATA, baseline=baseline)
        self.assertTrue(again.empty, "بعد از کامل‌شدن چیزی برای فرستادن نیست: " + "\n".join(again.change_lines()))

    def test_a_new_model_made_in_one_colour_only_gets_its_other_colours_on_the_retry(self) -> None:
        baseline = self.original()
        self.shop.create_limit = 3            # 13 Pro Max × ۲ (twins), then iPhone 15 × مشکی
        self.update(self.DATA, baseline=baseline)
        self.assertIsNotNone(self.shop.by_values("iPhone 15", "مشکی"))
        self.assertIsNone(self.shop.by_values("iPhone 15", "سفید"))
        self.shop.create_limit = None
        self.update(self.DATA, baseline=baseline)
        self.assertIsNotNone(self.shop.by_values("iPhone 15", "سفید"), "نیمه‌ساخته را «فقط مشکی» نخوانده است")
        self.assertIsNotNone(self.shop.by_values("iPhone 15", "سبز"))
        self.assert_converged()

    def test_a_delete_that_was_refused_is_finished_on_the_retry(self) -> None:
        baseline = self.original()
        self.shop.refuse_delete = {9001, 9002, 9003}
        _plan, result = self.update(self.DATA, baseline=baseline)
        self.assertFalse(result.ok)
        self.assertEqual(result.created, 8)
        self.assertEqual(len(self.shop.variations), 3 + 8, "تازه‌ها ساخته شده‌اند؛ قدیمی‌ها هنوز هستند")
        self.shop.refuse_delete = set()
        _plan, result = self.update(self.DATA, baseline=baseline)
        self.assertTrue(result.ok, result.summary())
        self.assert_converged()
        self.assertEqual((result.created, result.deleted), (0, 3),
                         "چیزی دوباره ساخته نشد؛ فقط سه قدیمی پاک شد: دو همزادِ iPhone 13 و S24")
        self.assertFalse({9001, 9002, 9003} & set(self.shop.variations))

    def test_a_batch_that_never_arrived_leaves_the_old_grid_and_asking_again_rebuilds_it(self) -> None:
        baseline = self.original()
        self.shop.reject_creates = True
        _plan, result = self.update(self.DATA, baseline=baseline)
        self.assertFalse(result.ok)
        self.assertEqual(len(self.shop.variations), 3, "محصول بی‌واریژن نماند")
        self.shop.reject_creates = False
        _plan, result = self.update(self.DATA, baseline=baseline)
        self.assertTrue(result.ok, result.summary())
        self.assert_converged()


@needs_services
class NewVariationsGetTheirPictures(ShopTestCase):
    def test_a_photo_named_after_a_new_colour_goes_on_that_colours_variations(self) -> None:
        files = [h.write_image(self.tmp, "01_main.jpg"), h.write_image(self.tmp, "02_آبی.jpg")]
        _plan, result = self.update({"attributes": {"رنگ": ["مشکی", "آبی"]}, "price": 698_000}, files)
        self.assertTrue(result.ok, result.summary())
        blue = [row for row in self.shop.variations.values()
                if any(item["option"] == "آبی" for item in row["attributes"])]
        self.assertTrue(blue)
        media_ids = {image["id"] for image in self.shop.product["images"]}
        for row in blue:
            self.assertIn(row["image"]["id"], media_ids)
        black = self.shop.by_values("iPhone 13 Pro Max", "مشکی")
        assert black is not None
        self.assertFalse(black.get("image"), "عکسی نداشت و رنگ‌اش هم عکسِ هم‌نام ندارد؛ چیزی نمی‌گیرد")

    def test_a_variation_that_had_a_picture_keeps_it_when_the_new_photos_do_not_name_its_colour(self) -> None:
        shop = UpdateShop(variations=[
            {**variation_row(1, "iPhone 13 Pro Max", "مشکی"), "image": {"id": 71}},
            variation_row(2, "iPhone 13 Pro Max", "سفید")])
        files = [h.write_image(self.tmp, "01_main.jpg"), h.write_image(self.tmp, "02_آبی.jpg")]
        _plan, result = self.update({"models": ["iPhone 13 Pro Max", "iPhone 15"], "price": 698_000},
                                    files, shop=shop)
        self.assertTrue(result.ok, result.summary())
        black = shop.by_values("iPhone 13 Pro Max", "مشکی")
        assert black is not None
        self.assertEqual(black["image"], {"id": 71}, "گالری عوض شد، عکسِ خودِ واریژن نه")


@needs_services
class RefusingToSendWhatShouldNotBeSent(ShopTestCase):
    def test_an_empty_plan_is_not_sent(self) -> None:
        product = asyncio.run(product_match.read(PRODUCT_ID, transport=self.shop.transport))
        plan = update_plan.build(product, {})
        with self.assertRaises(ValueError):
            asyncio.run(update_apply.apply(plan, transport=self.shop.transport))

    def test_a_blocked_plan_is_not_sent(self) -> None:
        product = asyncio.run(product_match.read(PRODUCT_ID, transport=self.shop.transport))
        plan = update_plan.build(product, {"sale_price": 900_000})
        self.assertTrue(plan.blocking)
        with self.assertRaises(ValueError):
            asyncio.run(update_apply.apply(plan, transport=self.shop.transport))
        self.assertEqual(self.shop.calls("PUT"), [])

    def test_photos_without_wordpress_credentials_fail_before_any_request(self) -> None:
        with h.patched_settings(h.settings_with(wordpress_app_password="")):
            product = asyncio.run(product_match.read(PRODUCT_ID, transport=self.shop.transport))
            plan = update_plan.build(product, {"price": 720_000}, image_count=1)
            before = len(self.shop.requests)
            with self.assertRaises(RuntimeError):
                asyncio.run(update_apply.apply(plan, self.photos(), transport=self.shop.transport))
            self.assertEqual(len(self.shop.requests), before)

    def test_images_in_the_plan_but_not_in_hand_is_refused(self) -> None:
        product = asyncio.run(product_match.read(PRODUCT_ID, transport=self.shop.transport))
        plan = update_plan.build(product, {}, image_count=2)
        with self.assertRaises(ValueError):
            asyncio.run(update_apply.apply(plan, [], transport=self.shop.transport))


@needs_services
class ReadingTheShop(ShopTestCase):
    def read(self, shop: UpdateShop | None = None):
        shop = shop or self.shop
        return asyncio.run(product_match.read(PRODUCT_ID, transport=shop.transport))

    def test_a_complete_read_says_so(self) -> None:
        product = self.read()
        self.assertTrue(product.variations_complete)
        self.assertEqual(len(product.variations), 3)

    def test_a_page_that_fails_makes_the_whole_read_incomplete(self) -> None:
        rows = [variation_row(index, f"Model {index}", "مشکی") for index in range(150)]
        shop = UpdateShop(product_row(), rows)
        shop.fail_page = 2
        product = self.read(shop)
        self.assertFalse(product.variations_complete)
        self.assertTrue(product.notes)
        plan = update_plan.build(product, {"price": 720_000})
        self.assertFalse(plan.can_apply, "نصفِ لیست را نمی‌شود پایهٔ اپدیت کرد")

    def test_a_host_that_refuses_status_any_is_asked_again_without_it(self) -> None:
        script = h.TransportScript(
            h.respond(200, product_row()),
            h.respond(400, {"code": "rest_invalid_param"}),
            h.respond(200, [variation_row(1, "iPhone 13 Pro Max", "مشکی")]),
        )
        product = asyncio.run(product_match.read(PRODUCT_ID, transport=script.transport()))
        self.assertTrue(product.variations_complete)
        self.assertEqual(len(product.variations), 1)
        self.assertNotIn("status", script.requests[-1].url.params)

    def test_variations_that_were_not_requested_do_not_look_like_an_empty_list(self) -> None:
        product = asyncio.run(product_match.read(PRODUCT_ID, include_variations=False,
                                                 transport=self.shop.transport))
        self.assertFalse(product.variations_complete)

    def test_a_variations_failure_stays_in_the_notes(self) -> None:
        self.shop.variations_status = 500
        product = self.read()
        self.assertFalse(product.variations_complete)
        self.assertIn("500", product.notes[-1])


@needs_services
class TheRehearsal(unittest.TestCase):
    """Dry-run: the same code path against the demo store, nothing leaves the process."""

    def setUp(self) -> None:
        self.enterContext(h.patched_settings(h.settings_with(woo_dry_run=True)))
        self.tmp = Path(tempfile.mkdtemp(prefix="tisa-update-dry-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def test_the_demo_product_can_be_read_planned_and_applied(self) -> None:
        async def go():
            product = await product_match.read(850_001, dry_run=True)
            plan = update_plan.build(
                product, {"models": ["iPhone 13 Pro Max", "iPhone 15"], "price": 720_000, "stock": 5},
                image_count=1)
            audit = Audit()
            result = await update_apply.apply(plan, [h.write_image(self.tmp)], audit=audit, dry_run=True)
            return plan, result, audit

        plan, result, audit = asyncio.run(go())
        self.assertTrue(result.dry_run)
        self.assertTrue(result.ok, result.summary())
        self.assertIn("dry-run", result.summary())
        self.assertEqual(result.created, 4, "iPhone 13 Pro Max × ۲ و iPhone 15 × ۲ از نو")
        self.assertEqual(result.deleted, 2)
        self.assertEqual(result.updated, [])
        self.assertEqual(result.images, 1)
        self.assertTrue(any(line.startswith("[dry-run] PUT") for line in audit.lines))
        self.assertTrue(any(line.startswith("[dry-run] POST") and "variations/batch" in line
                            for line in audit.lines))
        self.assertGreater(len(plan.rows("full")), 3)

    def test_a_rehearsed_delete_is_echoed_like_the_real_one(self) -> None:
        async def go():
            product = await product_match.read(850_001, dry_run=True)
            plan = update_plan.build(product, {"models": ["S24 Ultra", "iPhone 15"], "price": 698_000})
            return plan, await update_apply.apply(plan, dry_run=True)

        plan, result = asyncio.run(go())
        self.assertEqual(len(plan.deletes), 2)
        self.assertEqual(result.deleted, 2)
        self.assertTrue(result.ok, result.summary())


if __name__ == "__main__":
    unittest.main()
