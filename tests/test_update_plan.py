"""«🔄 اپدیت محصول» — the diff: what a draft changes on the product the shop already has.

These are the seller's own rules, one test each:

* a field the draft says nothing about is never touched (no price ⇒ the old price stays);
* a value the shop already has is not a change (and is not sent);
* a model list is the whole list — models the shop has and the list lacks are deleted, models the
  list has and the shop lacks are created, models in both stay the *same variation*;
* new photos replace the gallery;
* the plan is computed from what the shop returned, and a product it could not read completely is
  never planned on.

The plan is a pure value, so nothing here needs a network, a bot or a clock.
"""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("SUDO_IDS", "1234567")

from _update_shop import product_row, shop_product, variation_row

from bot.services import product_match, update_plan


def shop(*, extra: list[dict] | None = None, **product_over: object) -> product_match.ShopProduct:
    """The three-variation product most of these tests argue about (see ``_update_shop``)."""
    return shop_product(extra=extra, **product_over)


def plan_for(data: dict, *, images: int = 0, product: product_match.ShopProduct | None = None
             ) -> update_plan.UpdatePlan:
    return update_plan.build(product or shop(), data, image_count=images)


class SilenceIsNotAnInstruction(unittest.TestCase):
    """What the draft does not say stays exactly as the shop has it."""

    def test_an_empty_draft_changes_nothing(self) -> None:
        plan = plan_for({})
        self.assertTrue(plan.empty)
        self.assertFalse(plan.can_apply)
        self.assertEqual(plan.product_body(), {})
        self.assertEqual(plan.kept, 3, "هر سه واریژن همان‌طور می‌مانند")

    def test_no_price_means_the_old_prices_stay(self) -> None:
        plan = plan_for({"stock": 20})
        for change in plan.updates:
            self.assertIsNone(change.price, "قیمت نفرستاده، قیمت قبلی می‌ماند")
            self.assertNotIn("regular_price", change.payload())

    def test_only_the_stock_fields_are_written_when_only_stock_is_said(self) -> None:
        plan = plan_for({"stock": 20})
        self.assertEqual(len(plan.updates), 3)
        row = plan.updates[0].payload()
        self.assertEqual(set(row) - {"id"}, {"manage_stock", "stock_quantity"},
                         "وضعیت «موجود» عوض نمی‌شود، پس همراهش نمی‌رود")
        self.assertEqual(row["stock_quantity"], 20)
        self.assertIsNone(plan.attributes, "ساختار دست نمی‌خورد")
        self.assertEqual(plan.product_body(), {}, "خودِ محصول (عنوان/گالری) دست نمی‌خورد")

    def test_photos_alone_are_an_update_and_touch_nothing_else(self) -> None:
        plan = plan_for({}, images=4)
        self.assertTrue(plan.can_apply)
        self.assertEqual(plan.product_body([901, 902, 903, 904]),
                         {"images": [{"id": 901}, {"id": 902}, {"id": 903}, {"id": 904}]})
        self.assertFalse(plan.changes_variations)
        self.assertEqual((plan.images_old, plan.images_new), (3, 4))

    def test_the_sku_is_never_rewritten_and_a_wrong_prefix_is_a_warning(self) -> None:
        plan = plan_for({"sku_prefix": "CH", "price": 720_000})
        self.assertNotIn("sku", plan.product_body())
        self.assertTrue(any("CH" in warning and "BO7" in warning for warning in plan.warnings),
                        "پیشوند ناهماهنگ یعنی شاید محصولِ اشتباه انتخاب شده")
        self.assertFalse(plan_for({"sku_prefix": "BO", "price": 720_000}).warnings)

    def test_wholesale_price_is_noted_and_never_sent(self) -> None:
        plan = plan_for({"stock": 5, "wholesale_price": 500_000})
        self.assertTrue(any("همکاری" in note for note in plan.notes))
        for row in plan.updates:
            self.assertNotIn("wholesale", str(row.payload()))


class EqualIsNotAChange(unittest.TestCase):
    def test_a_price_the_shop_already_has_is_left_out(self) -> None:
        plan = plan_for({"price": 698_000})
        # only the S24 Ultra variation (598,000) differs
        self.assertEqual([row.variation_id for row in plan.updates], [9003])
        self.assertEqual(plan.kept, 2)

    def test_a_draft_that_repeats_the_shop_is_an_empty_plan(self) -> None:
        plan = plan_for({"prices": {"iphone": 698_000, "android": 598_000}})
        self.assertTrue(plan.empty)
        self.assertEqual(plan.kept, 3)

    def test_the_same_title_in_another_spelling_is_not_a_new_title(self) -> None:
        plan = plan_for({"title": "  قاب   سیلیکونی  آیفون "})
        self.assertEqual(plan.new_title, "")
        product = shop(name="قاب &amp; کاور")
        self.assertEqual(plan_for({"title": "قاب & کاور"}, product=product).new_title, "",
                         "ووکامرس عنوان را با entity برمی‌گرداند؛ این تفاوت نیست")

    def test_a_different_title_is_written(self) -> None:
        plan = plan_for({"title": "قاب مگنتی"})
        self.assertEqual(plan.new_title, "قاب مگنتی")
        self.assertEqual(plan.product_body(), {"name": "قاب مگنتی"})

    def test_stock_the_shop_already_has_is_not_rewritten(self) -> None:
        product = shop()
        plan = plan_for({"stock": 7}, product=product)
        self.assertEqual([row.variation_id for row in plan.updates], [9001, 9002],
                         "S24 Ultra همین ۷ تا را دارد و ردیفش نباید برود")


class PricesAndStock(unittest.TestCase):
    def test_one_price_applies_to_every_variation_and_the_card_shows_what_it_flattens(self) -> None:
        plan = plan_for({"price": 720_000})
        self.assertEqual([row.price for row in plan.updates],
                         [(698_000, 720_000), (698_000, 720_000), (598_000, 720_000)])
        text = "\n".join(plan.rows("full"))
        self.assertIn("698,000 ← 720,000", text)
        self.assertIn("598,000 ← 720,000", text, "قیمت اندروید را هم تخت می‌کند؛ باید دیده شود")

    def test_group_prices_land_on_their_own_group(self) -> None:
        plan = plan_for({"prices": {"iphone": 720_000, "android": 620_000}})
        by_id = {row.variation_id: row.price for row in plan.updates}
        self.assertEqual(by_id, {9001: (698_000, 720_000), 9002: (698_000, 720_000),
                                 9003: (598_000, 620_000)})

    def test_a_model_specific_price_lands_on_that_model_only(self) -> None:
        plan = plan_for({"price": 698_000, "model_prices": {"S24 Ultra": 650_000}})
        self.assertEqual([(row.variation_id, row.price) for row in plan.updates],
                         [(9003, (598_000, 650_000))])

    def test_prices_are_sent_as_strings(self) -> None:
        self.assertEqual(plan_for({"price": 720_000}).updates[0].payload()["regular_price"], "720000")

    def test_stock_is_written_and_the_status_follows_only_when_it_moves(self) -> None:
        row = plan_for({"stock": 20}).updates[0].payload()
        self.assertEqual((row["manage_stock"], row["stock_quantity"]), (True, 20))
        self.assertNotIn("stock_status", row)
        sold_out = shop()
        sold_out.variations[0] = product_match.ShopVariation.from_row(
            variation_row(1, "iPhone 13 Pro Max", "مشکی", stock=0, status="outofstock"))
        moved = plan_for({"stock": 20}, product=sold_out).updates[0].payload()
        self.assertEqual(moved["stock_status"], "instock", "ناموجود بود و ۲۰ تا آمد؛ وضعیت هم باید برگردد")

    def test_stock_zero_is_a_real_zero_and_means_out_of_stock(self) -> None:
        plan = plan_for({"stock": 0})
        change = next(row for row in plan.updates if row.variation_id == 9003)
        self.assertEqual(change.stock, (7, 0))
        self.assertEqual(change.status, ("instock", "outofstock"))

    def test_a_status_alone_writes_only_the_status(self) -> None:
        plan = plan_for({"stock_status": "outofstock"})
        for row in plan.updates:
            self.assertEqual(set(row.payload()) - {"id"}, {"stock_status"})
        self.assertEqual(len(plan.updates), 3)

    def test_a_sale_price_is_written_and_must_be_below_the_price(self) -> None:
        plan = plan_for({"sale_price": 498_000})
        self.assertTrue(plan.can_apply)
        self.assertEqual({row.payload()["sale_price"] for row in plan.updates}, {"498000"})
        clash = plan_for({"sale_price": 650_000})
        self.assertFalse(clash.can_apply, "۶۵۰ از قیمت S24 (۵۹۸) بیشتر است")
        self.assertTrue(any("کمتر نیست" in error for error in clash.errors))

    def test_the_sale_is_compared_with_the_price_that_will_exist_after_the_update(self) -> None:
        plan = plan_for({"price": 700_000, "sale_price": 650_000})
        self.assertTrue(plan.can_apply, "قیمتِ بعد از اپدیت ۷۰۰ است، نه ۵۹۸")


class ModelsAreTheWholeList(unittest.TestCase):
    NEW_LIST = ["iPhone 13 Pro Max", "iPhone 15", "iPhone 15 Pro"]

    def test_new_models_are_added_old_ones_removed_and_shared_ones_stay(self) -> None:
        plan = plan_for({"models": self.NEW_LIST})
        self.assertEqual(sorted(variation.variation_id for variation in plan.deletes), [9003])
        created = [dict(row.combo)["مدل"] for row in plan.creates]
        self.assertEqual(sorted(set(created)), ["iPhone 15", "iPhone 15 Pro"])
        self.assertEqual(plan.kept, 2, "iPhone 13 Pro Max همان واریژن‌های قبلی است")
        self.assertEqual(plan.updates, [], "چیزی به‌جز ساختار گفته نشد")
        change = plan.axis_changes[0]
        self.assertEqual((change.added, change.removed, change.kept),
                         (("iPhone 15", "iPhone 15 Pro"), ("S24 Ultra",), 1))

    def test_every_new_model_gets_every_colour_the_product_has(self) -> None:
        plan = plan_for({"models": self.NEW_LIST})
        colours = sorted({dict(row.combo)["رنگ"] for row in plan.creates})
        self.assertEqual(colours, ["سبز", "سفید", "مشکی"])
        self.assertEqual(len(plan.creates), 6)

    def test_the_attribute_lists_are_rewritten_whole_and_in_the_sellers_order(self) -> None:
        plan = plan_for({"models": self.NEW_LIST})
        assert plan.attributes is not None
        model = next(item for item in plan.attributes if item["name"] == "مدل")
        colour = next(item for item in plan.attributes if item["name"] == "رنگ")
        self.assertEqual(model["options"], self.NEW_LIST)
        self.assertEqual(colour["options"], ["مشکی", "سفید", "سبز"], "رنگ‌ها که نگفتی دست نمی‌خورند")
        self.assertEqual({item["name"] for item in plan.attributes}, {"مدل", "رنگ"})
        self.assertTrue(all("id" in item and "visible" in item for item in plan.attributes),
                        "فیلدهای دیگرِ ویژگی باید همراهش برگردند")

    def test_the_same_model_in_another_spelling_is_the_same_model(self) -> None:
        plan = plan_for({"models": ["iPhone 13 ProMax", "S24 Ultra"]})
        self.assertTrue(plan.empty, "«ProMax» و «Pro Max» یک مدل‌اند؛ واریژن نباید ساخته/پاک شود")
        self.assertIsNone(plan.attributes)

    def test_the_shops_own_spelling_wins_when_a_new_spelling_matches(self) -> None:
        plan = plan_for({"models": ["iPhone 13 ProMax", "iPhone 15"]})
        assert plan.attributes is not None
        model = next(item for item in plan.attributes if item["name"] == "مدل")
        self.assertEqual(model["options"], ["iPhone 13 Pro Max", "iPhone 15"])

    def test_the_list_replaces_even_when_nothing_is_added(self) -> None:
        plan = plan_for({"models": ["iPhone 13 Pro Max"]})
        self.assertEqual([variation.variation_id for variation in plan.deletes], [9003])
        self.assertEqual(plan.creates, [])
        assert plan.attributes is not None
        model = next(item for item in plan.attributes if item["name"] == "مدل")
        self.assertEqual(model["options"], ["iPhone 13 Pro Max"],
                         "یک مدل تنها هم محور می‌ماند؛ ساختار محصول موجود عوض نمی‌شود")

    def test_models_that_stay_keep_the_colours_they_had(self) -> None:
        # S24 Ultra only ever came in سبز; iPhone 13 Pro Max in مشکی/سفید. A new list that says
        # nothing about colours must not invent S24 × مشکی.
        plan = plan_for({"models": ["iPhone 13 Pro Max", "S24 Ultra", "S25 Ultra"]})
        made = {tuple(dict(row.combo).values()) for row in plan.creates}
        self.assertNotIn(("S24 Ultra", "مشکی"), made)
        self.assertNotIn(("S24 Ultra", "سفید"), made)
        self.assertEqual({model for model, _ in made}, {"S25 Ultra"})
        self.assertEqual(plan.deletes, [])

    def test_a_colour_list_replaces_the_colours(self) -> None:
        plan = plan_for({"attributes": {"رنگ": ["مشکی", "آبی"]}})
        change = next(item for item in plan.axis_changes if item.kind == update_plan.COLOR)
        self.assertEqual((change.added, sorted(change.removed)), (("آبی",), ["سبز", "سفید"]))
        self.assertEqual(sorted(variation.variation_id for variation in plan.deletes), [9002, 9003])
        # A colour list with no per-model limits is the full grid: both models × both colours,
        # minus the iPhone × مشکی that already exists.
        self.assertEqual(sorted(tuple(dict(row.combo).values()) for row in plan.creates),
                         [("S24 Ultra", "آبی"), ("S24 Ultra", "مشکی"), ("iPhone 13 Pro Max", "آبی")])

    def test_an_explicit_colour_list_per_model_narrows_the_grid(self) -> None:
        plan = plan_for({"models": ["iPhone 13 Pro Max", "S24 Ultra"],
                         "model_colors": {"iPhone 13 Pro Max": ["مشکی"],
                                          "S24 Ultra": ["سبز"]}})
        self.assertEqual([variation.variation_id for variation in plan.deletes], [9002],
                         "سفید برای آیفون گفته نشد؛ همان یک واریژن حذف می‌شود")

    def test_models_given_to_a_product_without_a_model_axis_add_the_axis(self) -> None:
        product = product_match.ShopProduct.from_row(product_row(attributes=[
            {"id": 0, "name": "رنگ", "position": 0, "visible": True, "variation": True,
             "options": ["مشکی", "سفید"]}]))
        product.variations = [
            product_match.ShopVariation.from_row({"id": 1, "attributes": [{"name": "رنگ", "option": "مشکی"}],
                                                  "regular_price": "698000", "manage_stock": False}),
            product_match.ShopVariation.from_row({"id": 2, "attributes": [{"name": "رنگ", "option": "سفید"}],
                                                  "regular_price": "698000", "manage_stock": False}),
        ]
        plan = plan_for({"models": ["iPhone 15", "iPhone 16"]}, product=product)
        self.assertEqual(len(plan.deletes), 2, "ترکیب‌های قدیمی مدل ندارند؛ همه جایگزین می‌شوند")
        self.assertEqual(len(plan.creates), 4)
        assert plan.attributes is not None
        self.assertEqual([item["name"] for item in plan.attributes], ["مدل", "رنگ"], "محور مدل اول می‌آید")
        self.assertTrue(plan.axis_changes[0].new_axis)

    def test_a_single_model_does_not_make_an_axis_on_its_own(self) -> None:
        product = product_match.ShopProduct.from_row(product_row(attributes=[
            {"id": 0, "name": "رنگ", "position": 0, "visible": True, "variation": True,
             "options": ["مشکی", "سفید"]}]))
        product.variations = [product_match.ShopVariation.from_row(
            {"id": 1, "attributes": [{"name": "رنگ", "option": "مشکی"}], "regular_price": "1"})]
        plan = plan_for({"models": ["iPhone 15"]}, product=product)
        self.assertEqual(plan.creates, [])
        self.assertTrue(any("یک مدل" in note for note in plan.notes))

    def test_a_list_equal_to_the_shop_does_not_rebuild_a_restricted_grid(self) -> None:
        plan = plan_for({"models": ["iPhone 13 Pro Max", "S24 Ultra"],
                         "attributes": {"رنگ": ["مشکی", "سفید", "سبز"]}})
        self.assertTrue(plan.empty,
                        "همان لیست؛ نباید ترکیب‌هایی که فروشگاه عمداً ندارد (S24×مشکی) ساخته شود")


class AskingAgainFinishesWhatStoppedHalfWay(unittest.TestCase):
    """A non-transactional write can stop between the attribute lists and the variations."""

    def half_done(self) -> product_match.ShopProduct:
        # The product PUT landed (the model list already says iPhone 15 and no S24 Ultra), but the
        # variation batch did not: no iPhone 15 variation, the S24 Ultra one still there.
        return shop(attributes=[
            {"id": 0, "name": "مدل", "position": 0, "visible": True, "variation": True,
             "options": ["iPhone 13 Pro Max", "iPhone 15"]},
            {"id": 0, "name": "رنگ", "position": 1, "visible": True, "variation": True,
             "options": ["مشکی", "سفید", "سبز"]},
        ])

    def test_the_same_update_completes_the_variations(self) -> None:
        plan = plan_for({"models": ["iPhone 13 Pro Max", "iPhone 15"]}, product=self.half_done())
        self.assertEqual([variation.variation_id for variation in plan.deletes], [9003],
                         "واریژنِ مدلی که دیگر در فهرست نیست حذف می‌شود")
        self.assertEqual(sorted({dict(row.combo)["مدل"] for row in plan.creates}), ["iPhone 15"])
        self.assertIsNone(plan.attributes, "فهرست‌ها از قبل نشسته‌اند؛ دوباره PUT نمی‌شوند")
        self.assertEqual(plan.kept, 2)

    def test_it_rebuilds_the_grid_the_original_update_would_have(self) -> None:
        retry = plan_for({"models": ["iPhone 13 Pro Max", "iPhone 15"]}, product=self.half_done())
        first = plan_for({"models": ["iPhone 13 Pro Max", "iPhone 15"]})
        self.assertEqual(sorted(tuple(dict(row.combo).values()) for row in retry.creates),
                         sorted(tuple(dict(row.combo).values()) for row in first.creates))

    def test_a_consistent_shop_with_a_restricted_grid_is_left_alone(self) -> None:
        plan = plan_for({"models": ["iPhone 13 Pro Max", "S24 Ultra"]})
        self.assertTrue(plan.empty)

    def test_a_listed_model_with_no_variation_gets_its_variations(self) -> None:
        product = shop(attributes=[
            {"id": 0, "name": "مدل", "position": 0, "visible": True, "variation": True,
             "options": ["iPhone 13 Pro Max", "S24 Ultra", "S25 Ultra"]},
            {"id": 0, "name": "رنگ", "position": 1, "visible": True, "variation": True,
             "options": ["مشکی", "سفید", "سبز"]},
        ])
        plan = plan_for({"models": ["iPhone 13 Pro Max", "S24 Ultra", "S25 Ultra"]}, product=product)
        self.assertEqual({dict(row.combo)["مدل"] for row in plan.creates}, {"S25 Ultra"})
        self.assertEqual(plan.deletes, [])


class NewVariations(unittest.TestCase):
    def test_a_new_model_takes_the_price_the_seller_gave_it(self) -> None:
        plan = plan_for({"models": ["iPhone 13 Pro Max", "iPhone 15"], "price": 720_000})
        self.assertEqual({row.price for row in plan.creates}, {720_000})
        self.assertEqual(plan.creates[0].payload()["regular_price"], "720000")

    def test_without_a_price_a_new_model_takes_its_own_groups_current_price(self) -> None:
        plan = plan_for({"models": ["iPhone 13 Pro Max", "iPhone 15", "S25 Ultra"]})
        by_model = {row.model: row.price for row in plan.creates}
        self.assertEqual(by_model["iPhone 15"], 698_000, "قیمت فعلی گروه آیفون")
        self.assertEqual(by_model["S25 Ultra"], 598_000, "قیمت فعلی گروه اندروید")
        self.assertTrue(any("ننوشتی" in warning and "قیمت" in warning for warning in plan.warnings),
                        "این برداشت باید به فروشنده گفته شود")

    def test_a_model_with_no_price_anywhere_blocks_instead_of_inventing_zero(self) -> None:
        product = shop()
        product.regular_price = 0
        product.variations = [
            product_match.ShopVariation.from_row(variation_row(1, "iPhone 13 Pro Max", "مشکی", price="")),
            product_match.ShopVariation.from_row(variation_row(2, "S24 Ultra", "سبز", price="")),
        ]
        plan = plan_for({"models": ["iPhone 13 Pro Max", "iPhone 15"]}, product=product)
        self.assertFalse(plan.can_apply)
        self.assertTrue(any("قیمتی پیدا نکردم" in error for error in plan.errors))

    def test_stock_given_goes_onto_the_new_variations_too(self) -> None:
        plan = plan_for({"models": ["iPhone 13 Pro Max", "iPhone 15"], "stock": 10})
        row = plan.creates[0].payload()
        self.assertEqual((row["manage_stock"], row["stock_quantity"], row["stock_status"]),
                         (True, 10, "instock"))
        self.assertTrue(all(change.stock for change in plan.updates), "واریژن‌های مانده هم ۱۰ می‌شوند")

    def test_without_stock_new_variations_are_unmanaged_and_the_seller_is_told(self) -> None:
        plan = plan_for({"models": ["iPhone 13 Pro Max", "iPhone 15"], "price": 698_000})
        row = plan.creates[0].payload()
        self.assertNotIn("manage_stock", row)
        self.assertTrue(any("موجودی" in warning and "ننوشتی" in warning for warning in plan.warnings))

    def test_new_variations_are_visible_published_and_ordered(self) -> None:
        plan = plan_for({"models": ["iPhone 13 Pro Max", "iPhone 15"], "price": 698_000})
        row = plan.creates[0].payload()
        self.assertEqual((row["status"], row["visible"]), ("publish", True))
        self.assertEqual([create.order for create in plan.creates], sorted(create.order for create in plan.creates))
        self.assertEqual(row["attributes"], [{"name": "مدل", "option": "iPhone 15"},
                                             {"name": "رنگ", "option": "مشکی"}])

    def test_a_big_deletion_is_flagged(self) -> None:
        plan = plan_for({"models": ["iPhone 15"], "price": 698_000})
        self.assertEqual(len(plan.deletes), 3)
        self.assertTrue(any("حذف می‌شود" in warning for warning in plan.warnings),
                        "«دو مدل فرستادم و محصول چهل واریژن را از دست داد» باید دیده شود")


class SimpleProducts(unittest.TestCase):
    def simple(self, **over: object) -> product_match.ShopProduct:
        row = product_row(type="simple", attributes=[], manage_stock=True, stock_quantity=3, **over)
        return product_match.ShopProduct.from_row(row)

    def test_price_sale_and_stock_go_on_the_product_itself(self) -> None:
        plan = plan_for({"price": 720_000, "sale_price": 650_000, "stock": 9}, product=self.simple())
        self.assertEqual(plan.product_fields, {
            "regular_price": "720000", "sale_price": "650000",
            "manage_stock": True, "stock_quantity": 9, "stock_status": "instock"})
        self.assertEqual(plan.updates, [])
        text = "\n".join(plan.rows("full"))
        self.assertIn("698,000 ← 720,000", text)
        self.assertIn("3 ← 9", text)

    def test_what_is_not_said_is_not_in_the_body(self) -> None:
        plan = plan_for({"stock": 9}, product=self.simple())
        self.assertEqual(set(plan.product_fields), {"manage_stock", "stock_quantity", "stock_status"})

    def test_a_simple_product_refuses_models_and_colours(self) -> None:
        plan = plan_for({"models": ["iPhone 15", "iPhone 16"], "price": 720_000}, product=self.simple())
        self.assertFalse(plan.can_apply)
        self.assertTrue(any("ساده" in error for error in plan.errors))
        self.assertFalse(plan_for({"attributes": {"رنگ": ["مشکی", "سفید"]}}, product=self.simple()).can_apply)

    def test_a_single_model_is_ignored_with_a_note(self) -> None:
        plan = plan_for({"models": ["iPhone 15"], "price": 720_000}, product=self.simple())
        self.assertTrue(plan.can_apply)
        self.assertTrue(any("یک مدل" in note for note in plan.notes))

    def test_a_sale_not_below_the_price_is_refused(self) -> None:
        plan = plan_for({"sale_price": 800_000}, product=self.simple())
        self.assertFalse(plan.can_apply)


class RefusalsAreHonest(unittest.TestCase):
    def test_a_variation_list_that_was_not_read_completely_is_never_planned_on(self) -> None:
        product = shop()
        product.variations_complete = False
        product.notes.append("واریژن‌ها خوانده نشدند (HTTP 500)")
        plan = plan_for({"models": ["iPhone 15", "iPhone 16"], "price": 1_000_000}, product=product)
        self.assertFalse(plan.can_apply)
        self.assertTrue(any("کامل خوانده نشد" in error for error in plan.errors))
        self.assertEqual((plan.creates, plan.deletes, plan.updates), ([], [], []),
                         "روی نیمه‌لیست نه می‌سازیم، نه حذف می‌کنیم")

    def test_an_unsupported_product_type_is_refused(self) -> None:
        product = product_match.ShopProduct.from_row(product_row(type="grouped"))
        plan = plan_for({"price": 720_000}, product=product)
        self.assertFalse(plan.can_apply)
        self.assertTrue(any("grouped" in error for error in plan.errors))

    def test_an_attribute_the_product_has_but_not_for_variations_is_refused(self) -> None:
        product = shop(attributes=[
            {"id": 0, "name": "مدل", "position": 0, "visible": True, "variation": True,
             "options": ["iPhone 13 Pro Max", "S24 Ultra"]},
            {"id": 0, "name": "رنگ", "position": 1, "visible": True, "variation": False,
             "options": ["مشکی", "سفید"]},
        ])
        product.variations = [product_match.ShopVariation.from_row(variation_row(1, "iPhone 13 Pro Max", "مشکی"))]
        plan = plan_for({"attributes": {"رنگ": ["مشکی", "آبی"]}}, product=product)
        self.assertFalse(plan.can_apply)
        self.assertTrue(any("فعال نیست" in error for error in plan.errors))


class WhatTheCardShows(unittest.TestCase):
    def test_rows_only_grow_as_the_stages_advance(self) -> None:
        plan = plan_for({"price": 720_000, "stock": 5, "models": ["iPhone 13 Pro Max", "iPhone 15"]},
                        images=2)
        text_rows = plan.rows("text")
        models_rows = plan.rows("models")
        full_rows = plan.rows("full")
        self.assertFalse(any("مدل‌ها" in row for row in text_rows), "مدل‌ها هنوز خوانده نشده‌اند")
        self.assertTrue(any("مدل‌ها" in row for row in models_rows))
        for row in text_rows:
            self.assertIn(row, models_rows)
        for row in models_rows:
            self.assertIn(row, full_rows, "ردیفی که یک بار آمد، جابه‌جا نمی‌شود")

    def test_the_card_says_what_stays(self) -> None:
        text = "\n".join(plan_for({"price": 720_000}).rows("full"))
        self.assertIn("دست‌نخورده", text)
        self.assertIn("SKU", text)
        self.assertIn("موجودی", text.split("دست‌نخورده")[1])
        self.assertNotIn("قیمت،", text.split("دست‌نخورده")[1], "قیمت عوض شد؛ در فهرست مانده‌ها نیست")

    def test_models_rows_name_the_added_and_removed(self) -> None:
        text = "\n".join(plan_for({"models": ["iPhone 13 Pro Max", "iPhone 15"], "price": 698_000}).rows("full"))
        self.assertIn("➕ iPhone 15", text)
        self.assertIn("🗑 S24 Ultra", text)

    def test_an_empty_plan_says_there_is_no_difference(self) -> None:
        lines = plan_for({}).rows("full")
        self.assertTrue(any("هیچ تفاوتی" in row for row in lines))

    def test_html_in_names_cannot_break_the_card(self) -> None:
        product = shop(name="<b>قاب</b>")
        text = "\n".join(plan_for({"title": "قاب <i>تازه</i>"}, product=product).rows("full"))
        self.assertNotIn("<i>", text)
        self.assertNotIn("<b>قاب</b>", text)

    def test_a_huge_title_cannot_push_the_card_over_telegrams_limit(self) -> None:
        plan = plan_for({"title": "قاب " * 2000})
        card = "\n".join(plan.rows("full"))
        self.assertLess(len(card), 600, "عنوان روی کارت یک برچسب است، نه یک سند")
        self.assertIn("…", card)

    def test_the_worst_realistic_card_fits_in_one_message(self) -> None:
        extra = [variation_row(10 + index, f"Model {index}", ("مشکی", "سفید", "سبز")[index % 3],
                               price=str(400_000 + index * 7_000), stock=index)
                 for index in range(60)]
        product = shop(extra=extra)
        plan = update_plan.build(product, {
            "title": "قاب " * 40, "price": 900_000, "sale_price": 800_000, "stock": 3,
            "models": [f"New Model {index}" for index in range(30)], "sku_prefix": "ZZ",
            "wholesale_price": 5, "attributes": {"رنگ": ["آبی", "قرمز", "زرد"]}}, image_count=9)
        card = "\n".join(plan.rows("full"))
        self.assertLess(len(card), 3600, len(card))

    def test_change_lines_are_short_plain_text(self) -> None:
        lines = plan_for({"price": 720_000, "title": "قاب مگنتی"}, images=2).change_lines()
        self.assertTrue(any(line.startswith("✏️ عنوان") for line in lines))
        self.assertTrue(any("720,000" in line for line in lines))
        self.assertTrue(any("2 تصویر" in line for line in lines))
        self.assertFalse(any("<" in line for line in lines))

    def test_many_groups_are_capped(self) -> None:
        extra = [variation_row(10 + index, f"Model {index}", "مشکی", price=str(500_000 + index * 1000))
                 for index in range(9)]
        product = shop(extra=extra)
        text = "\n".join(plan_for({"price": 900_000}, product=product).rows("full"))
        self.assertIn("گروه دیگر", text)


class TheSignature(unittest.TestCase):
    def test_it_is_stable_for_the_same_plan(self) -> None:
        self.assertEqual(plan_for({"price": 720_000}).signature(), plan_for({"price": 720_000}).signature())

    def test_it_changes_when_anything_sent_changes(self) -> None:
        base = plan_for({"price": 720_000}).signature()
        self.assertNotEqual(base, plan_for({"price": 730_000}).signature())
        self.assertNotEqual(base, plan_for({"price": 720_000}, images=1).signature())
        self.assertNotEqual(base, plan_for({"price": 720_000, "title": "x"}).signature())

    def test_it_changes_when_the_shop_changes_what_would_be_sent(self) -> None:
        drifted = shop()
        drifted.variations[0] = product_match.ShopVariation.from_row(
            variation_row(1, "iPhone 13 Pro Max", "مشکی", price="720000"))
        self.assertNotEqual(plan_for({"price": 720_000}).signature(),
                            plan_for({"price": 720_000}, product=drifted).signature(),
                            "یک واریژن همین حالا ۷۲۰ شده؛ چیزی که فرستاده می‌شود فرق کرده")

    def test_a_change_only_in_the_old_values_is_not_a_reason_to_ask_again(self) -> None:
        # Stock moves with every order; «5 → 20» approved, «4 → 20» sent, is the same decision.
        sold = shop()
        sold.variations[0] = product_match.ShopVariation.from_row(
            variation_row(1, "iPhone 13 Pro Max", "مشکی", stock=4))
        base = shop()
        base.variations[0] = product_match.ShopVariation.from_row(
            variation_row(1, "iPhone 13 Pro Max", "مشکی", stock=5))
        self.assertEqual(plan_for({"stock": 20}, product=base).signature(),
                         plan_for({"stock": 20}, product=sold).signature())


class AxesAndAttributesPayload(unittest.TestCase):
    def test_a_product_without_attribute_rows_derives_its_axes_from_the_variations(self) -> None:
        product = shop(attributes=[])
        axes = update_plan.shop_axes(product)
        self.assertEqual([(axis.name, axis.kind) for axis in axes],
                         [("مدل", update_plan.MODEL), ("رنگ", update_plan.COLOR)])
        plan = plan_for({"models": ["iPhone 13 Pro Max", "iPhone 15"]}, product=product)
        assert plan.attributes is not None
        self.assertEqual({item["name"] for item in plan.attributes}, {"مدل", "رنگ"},
                         "ویژگی‌هایی که هاست نفرستاده بود هم باید با PUT برگردند، نه گم شوند")

    def test_attributes_that_are_not_for_variations_travel_unchanged(self) -> None:
        product = shop(attributes=[
            {"id": 0, "name": "جنس", "position": 0, "visible": True, "variation": False, "options": ["سیلیکون"]},
            {"id": 0, "name": "مدل", "position": 1, "visible": True, "variation": True,
             "options": ["iPhone 13 Pro Max", "S24 Ultra"]},
            {"id": 0, "name": "رنگ", "position": 2, "visible": True, "variation": True,
             "options": ["مشکی", "سفید", "سبز"]},
        ])
        plan = plan_for({"models": ["iPhone 13 Pro Max", "iPhone 15"]}, product=product)
        assert plan.attributes is not None
        material = next(item for item in plan.attributes if item["name"] == "جنس")
        self.assertEqual(material["options"], ["سیلیکون"])
        self.assertFalse(material["variation"])

    def test_positions_are_renumbered_after_an_axis_is_added(self) -> None:
        product = product_match.ShopProduct.from_row(product_row(attributes=[
            {"id": 0, "name": "رنگ", "position": 0, "visible": True, "variation": True,
             "options": ["مشکی", "سفید"]}]))
        product.variations = [product_match.ShopVariation.from_row(
            {"id": 1, "attributes": [{"name": "رنگ", "option": "مشکی"}], "regular_price": "1"})]
        plan = plan_for({"models": ["iPhone 15", "iPhone 16"], "price": 1000}, product=product)
        assert plan.attributes is not None
        self.assertEqual([item["position"] for item in plan.attributes], [0, 1])


class ReadingTheShop(unittest.TestCase):
    def test_images_and_attributes_are_kept_as_the_shop_sent_them(self) -> None:
        product = product_match.ShopProduct.from_row(product_row())
        self.assertEqual(product.image_ids, [11, 12, 13])
        self.assertEqual([item["name"] for item in product.attributes], ["مدل", "رنگ"])
        self.assertTrue(product.attributes[0]["visible"])

    def test_a_variations_own_picture_is_read(self) -> None:
        row = variation_row(1, "iPhone 13 Pro Max", "مشکی")
        row["image"] = {"id": 55, "src": "x"}
        self.assertEqual(product_match.ShopVariation.from_row(row).image_id, 55)
        row["image"] = None
        self.assertEqual(product_match.ShopVariation.from_row(row).image_id, 0)


if __name__ == "__main__":
    unittest.main()
