"""فاز ۵: «شارژ محصول موجود» — پیدا کردن در فروشگاه، دیف، اعمال (plan §فاز ۵).

Three layers, tested where each decision is made:

* :mod:`bot.services.product_match` — what the shop is asked, and what happens when it answers
  something unfriendly (404 on a SKU, 400 on ``status=any``, a body that is not a list);
* :mod:`bot.services.restock_plan` — the grammar of one compact line, and what it refuses to
  guess;
* :mod:`bot.services.restock_apply` + :mod:`bot.modules.restock_flow` — the write, the
  verification of it, and the buttons around it.

The flow tests drive the **real dry-run store** for the read path (a rehearsal that reads
nothing proves nothing) and a scripted transport for the write path, because a shop that
answers 400 has to be shaped on purpose.

Run with ``python -m pytest tests/test_restock.py`` or
``python3 -m unittest discover -s tests``; like the other flow tests, the module skips itself
when python-telegram-bot is not installed — that is what keeps a bare host deployable.
"""
from __future__ import annotations

import asyncio
import json
import os
import unittest

os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("SUDO_IDS", "1234567")

import _flow_harness as h

try:
    from telegram.ext import ConversationHandler

    from bot.constants import CB
    from bot.modules import product_flow as PF
    from bot.modules import restock_flow as RF
    from bot.services import product_match, products_ledger, restock_apply, restock_plan
    from bot.services.woo_client import Audit

    HAS_FLOW = True
except Exception:                          # pragma: no cover - PTB missing
    CB = PF = RF = ConversationHandler = Audit = None            # type: ignore[assignment]
    product_match = products_ledger = restock_apply = restock_plan = None  # type: ignore[assignment]
    HAS_FLOW = False

needs_flow = unittest.skipUnless(HAS_FLOW, "python-telegram-bot is not installed")

USER = 7
CHAT = 9
END = -1 if not HAS_FLOW else ConversationHandler.END


def product_row(**over: object) -> dict:
    row: dict = {
        "id": 1201, "name": "قاب سیلیکونی آیفون", "sku": "BO7", "type": "variable",
        "status": "publish", "regular_price": "698000", "sale_price": "", "price": "698000",
        "manage_stock": False, "stock_quantity": None, "stock_status": "instock",
        "attributes": [
            {"name": "مدل", "variation": True, "options": ["iPhone 13 Pro Max", "S24 Ultra"]},
            {"name": "رنگ", "variation": True, "options": ["مشکی", "سفید", "سبز"]},
        ],
    }
    row.update(over)
    return row


def variation_row(index: int, model: str, color: str, *, stock: int | None = 0,
                  status: str = "instock", price: str = "698000", sale: str = "") -> dict:
    return {
        "id": 9000 + index, "sku": "", "type": "variation", "status": "publish",
        "attributes": [{"variation": "مدل", "option": model},
                       {"variation": "رنگ", "option": color}],
        "regular_price": price, "sale_price": sale, "price": sale or price,
        "manage_stock": True, "stock_quantity": stock, "stock_status": status, "visible": True,
    }


def shop(text: str | None = None) -> product_match.ShopProduct | restock_plan.RestockPlan:
    """The three-variation product most of these tests argue about (or its plan)."""
    prod = product_match.ShopProduct.from_row(product_row())
    prod.variations = [
        product_match.ShopVariation.from_row(variation_row(1, "iPhone 13 Pro Max", "مشکی")),
        product_match.ShopVariation.from_row(variation_row(2, "iPhone 13 Pro Max", "سفید")),
        product_match.ShopVariation.from_row(variation_row(3, "S24 Ultra", "سبز", stock=7)),
    ]
    return restock_plan.plan_for(prod, text) if text is not None else prod


def query_update(data: str, **kwargs: object):
    """Alias: the harness default is already user 7 / chat 9, the ids these tests use."""
    return h.query_update(data, **kwargs)


def message_update(text: str = "", **kwargs: object):
    return h.message_update(text, **kwargs)


def buttons_of(record: tuple) -> list:
    _kind, _text, kwargs = record
    markup = kwargs.get("reply_markup")
    return [button for row in getattr(markup, "inline_keyboard", []) or [] for button in row]


@needs_flow
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


@needs_flow
class PlanTest(unittest.TestCase):
    """The grammar of one line — and what it refuses to guess."""

    def test_a_colour_and_a_bare_number(self) -> None:
        plan = shop("مشکی ۵")
        assert isinstance(plan, restock_plan.RestockPlan)
        self.assertEqual([change.variation_id for change in plan.changes], [9001])
        self.assertEqual((plan.changes[0].stock_from, plan.changes[0].stock_to), (0, 5))
        self.assertEqual(plan.unmatched, [])

    def test_suffixed_and_labelled_counts_are_the_same_request(self) -> None:
        for text in ("مشکی ۵ عدد", "مشکی موجودی: ۵", "مشکی 5 تا", "مشکی ۵ دانه"):
            plan = shop(text)
            assert isinstance(plan, restock_plan.RestockPlan)
            self.assertEqual(plan.changes[0].stock_to, 5, text)

    def test_two_clauses_in_one_line_target_two_variations(self) -> None:
        plan = shop("مشکی ۵، سفید ۲")
        assert isinstance(plan, restock_plan.RestockPlan)
        self.assertEqual({change.variation_id: change.stock_to for change in plan.changes},
                         {9001: 5, 9002: 2})

    def test_a_model_word_narrows_it_further(self) -> None:
        plan = shop("13promax مشکی ۹")
        assert isinstance(plan, restock_plan.RestockPlan)
        self.assertEqual([change.variation_id for change in plan.changes], [9001])

    def test_zero_is_an_answer_not_an_omission(self) -> None:
        plan = shop("سبز ۰")        # the shop has 7 of it; «۰» must not be read as «no number»
        assert isinstance(plan, restock_plan.RestockPlan)
        self.assertEqual(plan.changes[0].stock_to, 0)
        self.assertEqual(plan.variation_payloads()[0]["stock_quantity"], 0)

    def test_a_zero_the_shop_already_has_is_left_alone(self) -> None:
        plan = shop("مشکی ۰")
        assert isinstance(plan, restock_plan.RestockPlan)
        self.assertEqual(plan.changes, [])
        self.assertEqual(plan.unchanged, 1)

    def test_out_of_stock_writes_the_status_and_no_count(self) -> None:
        plan = shop("⛔ سفید ناموجود")
        assert isinstance(plan, restock_plan.RestockPlan)
        self.assertEqual(plan.changes[0].status_to, "outofstock")
        self.assertIsNone(plan.changes[0].stock_to)
        self.assertNotIn("stock_quantity", plan.variation_payloads()[0])

    def test_backorder_is_its_own_status(self) -> None:
        plan = shop("سفید پیش‌فروش")
        assert isinstance(plan, restock_plan.RestockPlan)
        self.assertEqual(plan.changes[0].status_to, "onbackorder")

    def test_a_price_is_never_mistaken_for_a_count(self) -> None:
        plan = shop("مشکی قیمت 498000")
        assert isinstance(plan, restock_plan.RestockPlan)
        self.assertEqual(plan.changes[0].price_to, 498000)
        self.assertIsNone(plan.changes[0].stock_to, "قیمت نباید موجودی شود")
        self.assertNotIn("stock_quantity", plan.variation_payloads()[0])

    def test_sale_and_count_in_one_line(self) -> None:
        plan = shop("مشکی موجودی ۱۲، قیمت ویژه 498000")
        assert isinstance(plan, restock_plan.RestockPlan)
        change = plan.changes[0]
        self.assertEqual((change.stock_to, change.sale_to), (12, 498000))
        self.assertEqual(plan.variation_payloads()[0],
                        {"id": 9001, "manage_stock": True, "stock_quantity": 12,
                         "sale_price": "498000"})

    def test_a_sale_that_is_not_below_the_price_is_refused(self) -> None:
        plan = shop("مشکی قیمت ویژه 798000")
        assert isinstance(plan, restock_plan.RestockPlan)
        self.assertTrue(plan.blocking, "ووکامرس این را تخفیف نمی‌شمارد")
        self.assertIn("کمتر نیست", plan.errors[0])

    def test_a_colour_this_product_does_not_have_is_shown_not_guessed(self) -> None:
        plan = shop("قرمز ۴")
        assert isinstance(plan, restock_plan.RestockPlan)
        self.assertEqual(plan.changes, [])
        self.assertIn("قرمز", plan.unmatched[0])

    def test_a_bare_count_on_a_variable_product_needs_a_target(self) -> None:
        plan = shop("۵")
        assert isinstance(plan, restock_plan.RestockPlan)
        self.assertTrue(plan.blocking, "یک عدد بی‌نام روی هر ۳ واریژن نوشته نمی‌شود")
        self.assertIn("همه", plan.errors[0], "راه فرار باید گفته شود، نه حدس زده شود")

    def test_every_variation_is_said_out_loud(self) -> None:
        plan = shop("همه ۲")
        assert isinstance(plan, restock_plan.RestockPlan)
        self.assertEqual(sorted(change.variation_id for change in plan.changes), [9001, 9002, 9003])

    def test_a_variation_that_already_has_it_is_left_out(self) -> None:
        plan = shop("سبز ۷")
        assert isinstance(plan, restock_plan.RestockPlan)
        self.assertEqual(plan.changes, [])
        self.assertEqual(plan.unchanged, 1)
        self.assertIn("دست‌نخورده", plan.render())

    def test_an_absurd_count_is_warned_about_not_blocked(self) -> None:
        plan = shop("مشکی ۹۰۰۰۰۰")
        assert isinstance(plan, restock_plan.RestockPlan)
        self.assertFalse(plan.blocking, "سقف برای نگه‌داشتن نیست، برای گفتن است")
        self.assertEqual(plan.changes[0].stock_to, 900000)
        self.assertTrue(any("بیشتر" in note for note in plan.notes))

    def test_a_simple_product_takes_the_line_on_itself(self) -> None:
        prod = product_match.ShopProduct.from_row(product_row(id=7, type="simple",
                                                              manage_stock=True, stock_quantity=1))
        plan = restock_plan.plan_for(prod, "۹")
        self.assertEqual(plan.product_change.get("stock_quantity"), 9)
        self.assertEqual(plan.product_change.get("stock_status"), "instock")
        self.assertEqual(plan.variation_payloads(), [])

    def test_render_shows_the_numbers_the_shop_has_in_the_seller_words(self) -> None:
        plan = shop("مشکی ۵، سفید ناموجود")
        assert isinstance(plan, restock_plan.RestockPlan)
        text = plan.render()
        self.assertIn("موجودی 0 → 5", text)
        self.assertIn("موجود → ناموجود", text)
        self.assertNotIn("outofstock", text, "صفحهٔ تأیید زبانِ فروشنده را دارد، نه زبانِ REST")

    def test_payload_carries_only_what_changes(self) -> None:
        plan = shop("مشکی ۵")
        assert isinstance(plan, restock_plan.RestockPlan)
        self.assertEqual(sorted(plan.variation_payloads()[0]),
                         ["id", "manage_stock", "stock_quantity"],
                         "وضعیت که عوض نشده نباید فرستاده شود")


@needs_flow
class ApplyTest(unittest.TestCase):
    """The write: batch when the shop takes it, per-row when it does not, verified either way."""

    def setUp(self) -> None:
        self.enterContext(h.patched_settings(h.settings_with()))
        self.enterContext(h.no_sleep())

    @staticmethod
    def _body(request) -> dict:
        return json.loads(request.content.decode("utf-8"))

    def test_batch_is_verified_from_what_the_shop_sends_back(self) -> None:
        plan = shop("مشکی ۵، سفید ناموجود")
        script = h.TransportScript(h.respond(200, {"update": [
            variation_row(1, "iPhone 13 Pro Max", "مشکی", stock=5),
            variation_row(2, "iPhone 13 Pro Max", "سفید", status="outofstock"),
        ]}))
        result = asyncio.run(restock_apply.apply_plan(plan, shop(), audit=Audit(),
                                                     transport=script.transport()))
        self.assertEqual(sorted(result.confirmed), [9001, 9002])
        self.assertEqual(result.failed, [])
        self.assertEqual(script.methods, ["POST /wp-json/wc/v3/products/1201/variations/batch"])
        sent = {row["id"]: row for row in self._body(script.requests[0])["update"]}
        self.assertEqual(sent[9001]["stock_quantity"], 5)
        self.assertEqual(sent[9002]["stock_status"], "outofstock")
        self.assertNotIn("regular_price", sent[9002], "چیزی که عوض نشده نباید نوشته شود")

    def test_a_host_without_batch_support_falls_back_to_per_row_puts(self) -> None:
        plan = shop("مشکی ۵")
        script = h.TransportScript(
            h.respond(404, {"message": "Not found"}),
            h.respond(200, variation_row(1, "iPhone 13 Pro Max", "مشکی", stock=5)),
        )
        result = asyncio.run(restock_apply.apply_plan(plan, shop(), transport=script.transport()))
        self.assertEqual(result.confirmed, [9001])
        self.assertEqual(script.methods, [
            "POST /wp-json/wc/v3/products/1201/variations/batch",
            "PUT /wp-json/wc/v3/products/1201/variations/9001"],
            "ربات خودش ادامه می‌دهد؛ از کاربر نمی‌خواهد دوباره کلیک کند")

    def test_a_row_the_batch_swallowed_is_written_again(self) -> None:
        plan = shop("مشکی ۵، سفید ناموجود")
        script = h.TransportScript(
            h.respond(200, {"update": [variation_row(1, "iPhone 13 Pro Max", "مشکی", stock=5)]}),
            h.respond(200, variation_row(2, "iPhone 13 Pro Max", "سفید", status="outofstock")),
        )
        result = asyncio.run(restock_apply.apply_plan(plan, shop(), transport=script.transport()))
        self.assertEqual(sorted(result.confirmed), [9001, 9002])
        self.assertEqual(len(script.methods), 2)

    def test_an_echo_that_disagrees_is_read_once_more_and_then_told(self) -> None:
        plan = shop("مشکی ۵")
        script = h.TransportScript(
            h.respond(200, {"update": [variation_row(1, "iPhone 13 Pro Max", "مشکی", stock=0)]}),
            h.respond(200, [variation_row(1, "iPhone 13 Pro Max", "مشکی", stock=0)]),
        )
        result = asyncio.run(restock_apply.apply_plan(plan, shop(), transport=script.transport()))
        self.assertEqual(result.confirmed, [])
        self.assertEqual(result.unconfirmed, [9001])
        self.assertEqual(script.methods[-1], "GET /wp-json/wc/v3/products/1201/variations",
                         "یک خواندن برای اطمینان؛ نه بیشتر")
        self.assertIn("برنگرداند", result.summary(plan))
        self.assertFalse(result.ok, "«تأیید نشد» موفقیت نیست")

    def test_a_refused_row_is_named_as_refused(self) -> None:
        plan = shop("مشکی ۵")
        script = h.TransportScript(h.respond(200, {"update": []}),
                                   h.respond(400, {"message": "Invalid stock"}))
        result = asyncio.run(restock_apply.apply_plan(plan, shop(), transport=script.transport()))
        self.assertEqual(result.failed, [9001])
        self.assertIn("Invalid stock", result.errors[0])

    def test_a_refused_parent_stops_the_whole_apply(self) -> None:
        prod = product_match.ShopProduct.from_row(product_row(id=7, type="simple"))
        plan = restock_plan.plan_for(prod, "۹")
        script = h.TransportScript(h.respond(400, {"message": "نمی‌شود"}))
        result = asyncio.run(restock_apply.apply_plan(plan, prod, transport=script.transport()))
        self.assertFalse(result.product_updated)
        self.assertIn("نمی‌شود", result.errors[0])
        self.assertEqual(script.methods, ["PUT /wp-json/wc/v3/products/7"],
                         "وقتی خود محصول رد شد، چیزی دیگر فرستاده نمی‌شود")

    def test_prices_are_sent_as_strings_so_the_verification_can_match(self) -> None:
        prod = product_match.ShopProduct.from_row(product_row(id=7, type="simple"))
        plan = restock_plan.plan_for(prod, "قیمت 700000")
        script = h.TransportScript(h.respond(200, product_row(id=7, type="simple",
                                                              regular_price="700000")))
        result = asyncio.run(restock_apply.apply_plan(plan, prod, transport=script.transport()))
        self.assertTrue(result.product_updated)
        self.assertEqual(self._body(script.requests[0])["regular_price"], "700000")

    def test_a_plan_with_errors_is_never_sent(self) -> None:
        plan = shop("مشکی قیمت ویژه 900000")
        with self.assertRaises(ValueError):
            asyncio.run(restock_apply.apply_plan(plan, shop(),
                                                 transport=h.TransportScript().transport()))


@needs_flow
class DryRunStoreTest(unittest.TestCase):
    """The rehearsal has to read something, or it is theatre."""

    def setUp(self) -> None:
        self.enterContext(h.patched_settings(h.settings_with()))

    def test_the_demo_catalog_is_searchable_readable_and_writable(self) -> None:
        found = asyncio.run(product_match.find("دمو", dry_run=True))
        self.assertTrue(found)
        prod = asyncio.run(product_match.read(found[0].product_id, dry_run=True))
        self.assertEqual(len(prod.variations), 2)
        plan = restock_plan.plan_for(prod, "مشکی ۴")
        self.assertTrue(plan.can_apply)
        result = asyncio.run(restock_apply.apply_plan(plan, prod, dry_run=True))
        self.assertEqual(len(result.confirmed), 1)
        self.assertTrue(result.dry_run)
        self.assertIn("dry-run", result.summary(plan))

    def test_an_unrelated_search_does_not_invent_a_product(self) -> None:
        # The SKU machinery searches by candidate code. If the demo store answered that, every
        # dry run would report «this SKU is taken» and the rehearsal would start lying.
        self.assertEqual(asyncio.run(product_match.find("BO12", dry_run=True)), [])


@needs_flow
class FlowTest(unittest.TestCase):
    """Every screen has to say what the next one will do."""

    def setUp(self) -> None:
        self.enterContext(h.temp_ledger())
        self.enterContext(h.patched_settings(h.settings_with(woo_dry_run=True)))
        RF.sessions.clear()
        PF.sessions.clear()
        self.addCleanup(RF.sessions.clear)
        self.addCleanup(PF.sessions.clear)
        # Only the builder binds the permission check (the restock path runs through its entry
        # point), so that is the one name that has to answer "yes" here.
        saved = PF.feature_allowed
        PF.feature_allowed = lambda user_id, key: True
        self.addCleanup(setattr, PF, "feature_allowed", saved)

    def start(self) -> list:
        update, seen = query_update(CB.PHONE_RESTOCK, chat_id=CHAT)
        self.assertEqual(asyncio.run(PF.entry(update, h.context())), RF.RESTOCK_MATCH)
        return seen

    def to_line(self) -> list:
        self.start()
        update, seen = query_update(f"{CB.RESTOCK_PICK}:850001", chat_id=CHAT)
        self.assertEqual(asyncio.run(RF.pick(update, h.context())), RF.RESTOCK_LINE)
        return seen

    def test_the_button_opens_the_lookup_not_the_zip_builder(self) -> None:
        seen = self.start()
        prompt = str(seen[-1][1])
        self.assertIn("SKU", prompt)
        self.assertNotIn("ZIP", prompt, "اول پیدا کردن؛ فایل فقط وقتی که لازم شد")
        self.assertEqual(PF.sessions.get(USER), None, "شارژ، محصول‌سازی را باز نمی‌کند")

    def test_a_search_then_a_pick_leads_to_the_line_prompt(self) -> None:
        self.start()
        update, seen = message_update("دمو")
        self.assertEqual(asyncio.run(RF.handle_search(update, h.context())), RF.RESTOCK_MATCH)
        self.assertIn("محصول آزمایشی", str(seen[-1][1]))
        seen = self.to_line()
        body = str(seen[-1][1])
        self.assertIn("مشکی", body, "فروشنده باید ببیند چه چیزی می‌تواند بنویسد")
        self.assertIn("همه ۲", body, "راه «روی همه بنویس» باید روی صفحه باشد")
        self.assertIn("iPhone 13 Pro Max", body)

    def test_a_line_becomes_a_diff_with_one_apply_button(self) -> None:
        self.to_line()
        update, seen = message_update("مشکی ۴")
        self.assertEqual(asyncio.run(RF.handle_line(update, h.context())), RF.RESTOCK_DIFF)
        self.assertIn("موجودی 0 → 4", str(seen[-1][1]))
        self.assertIn(CB.RESTOCK_APPLY, [button.callback_data for button in buttons_of(seen[-1])])
        self.assertTrue(any("اجرای آزمایشی" in str(button.text) for button in buttons_of(seen[-1])),
                        "در حالت dry-run دکمه نباید بگوید اعمال می‌کند")

    def test_a_blocked_plan_has_no_apply_button(self) -> None:
        self.to_line()
        update, seen = message_update("مشکی قیمت ویژه 900000")
        self.assertEqual(asyncio.run(RF.handle_line(update, h.context())), RF.RESTOCK_DIFF)
        self.assertNotIn(CB.RESTOCK_APPLY, [button.callback_data for button in buttons_of(seen[-1])])
        self.assertIn("کمتر نیست", str(seen[-1][1]))

    def test_an_empty_line_is_asked_for_a_real_one(self) -> None:
        self.to_line()
        update, seen = message_update("   \n  ")
        self.assertEqual(asyncio.run(RF.handle_line(update, h.context())), RF.RESTOCK_LINE)
        self.assertIn("یک خط بنویس", str(seen[-1][1]))

    def test_a_line_the_shop_does_not_have_still_gets_an_answer(self) -> None:
        # «هیچ تغییری نداریم» باید *دلیلش* را بگوید؛ سکوت یعنی «ربات خط را گم کرد».
        self.to_line()
        update, seen = message_update("قرمز ۴")
        self.assertEqual(asyncio.run(RF.handle_line(update, h.context())), RF.RESTOCK_DIFF)
        body = str(seen[-1][1])
        self.assertIn("اعمال نشد", body)
        self.assertIn("قرمز", body)

    def test_applying_writes_a_card_and_closes_the_flow(self) -> None:
        self.to_line()
        update, _seen = message_update("مشکی ۴")
        asyncio.run(RF.handle_line(update, h.context()))
        update, seen = query_update(CB.RESTOCK_APPLY, chat_id=CHAT)
        self.assertEqual(asyncio.run(RF.apply(update, h.context())), END)
        card = products_ledger.recent(1)[0]
        self.assertEqual(card["status"], "dry",
                         "در rehearsal نباید کارت بگوید چیزی در سایت نوشته شد")
        self.assertEqual(card["mode"], "restock")
        self.assertEqual(card["product_id"], 850001)
        self.assertEqual(card["variations"], 1)
        self.assertIn("موجودی 0 → 4", card["report"])
        self.assertIn("شارژ شد", str(seen[-1][1]))
        self.assertIsNone(RF.sessions.get(USER), "یک شارژِ اعمال‌شده نباید دوباره اعمال شود")

    def test_a_real_apply_writes_a_restocked_card(self) -> None:
        # The same screen with a shop that confirmed the write: the card has to say «written»,
        # and say it only because the shop said so. (The read side stays on the rehearsal store:
        # no test here is allowed to reach a network.)
        calls: list = []

        async def fake_apply(plan, product, **kwargs):
            calls.append((plan, product))
            return restock_apply.ApplyResult(confirmed=[860001], dry_run=False)

        saved = RF.restock_apply.apply_plan
        RF.restock_apply.apply_plan = fake_apply
        self.addCleanup(setattr, RF.restock_apply, "apply_plan", saved)
        self.to_line()
        update, _seen = message_update("مشکی ۴")
        asyncio.run(RF.handle_line(update, h.context()))
        update, seen = query_update(CB.RESTOCK_APPLY, chat_id=CHAT)
        self.assertEqual(asyncio.run(RF.apply(update, h.context())), END)
        self.assertEqual(len(calls), 1)
        card = products_ledger.recent(1)[0]
        self.assertEqual(card["status"], "restocked")
        self.assertEqual(card["variations"], 1)
        self.assertIn("شارژ شد", str(seen[-1][1]))

    def test_a_restock_card_reads_as_a_restock(self) -> None:
        from bot.keyboards import cards
        card = products_ledger.record(user_id=USER, status="restocked", product_id=1201,
                                      mode="restock", title="قاب", variations=3)
        text = cards.result_card(card)
        self.assertIn("شارژ موجودی به‌روز شد", text)
        self.assertNotIn("پیش‌نویس ساخته شد", text, "این کارت محصول تازه نساخته است")
        self.assertIn("🔄", products_ledger.summary(card))

    def test_a_file_here_is_answered_not_swallowed(self) -> None:
        self.start()
        update, seen = message_update("")
        state = asyncio.run(RF.ignore_media(update, h.context(), stay=RF.RESTOCK_MATCH))
        self.assertEqual(state, RF.RESTOCK_MATCH)
        self.assertIn("متن", str(seen[-1][1]))
        self.assertIn(CB.RESTOCK_ZIP, [button.callback_data for button in buttons_of(seen[-1])])

    def test_the_zip_escape_hands_the_chat_to_the_builder(self) -> None:
        self.to_line()
        update, _seen = query_update(CB.RESTOCK_ZIP, chat_id=CHAT)
        self.assertEqual(asyncio.run(RF.offer_zip(update, h.context())), PF.COLLECT,
                         "دکمه باید واقعی باشد: خودِ جریان سازنده باز می‌شود")
        self.assertEqual(PF.sessions[USER].mode, "update")
        self.assertIsNone(RF.sessions.get(USER), "دو جریان هم‌زمان باز نمی‌ماند")
        answers = [item for item in _seen if item[0] == "answer"]
        self.assertEqual(len(answers), 1,
                         "دو answer روی یک callback یعنی BadRequest؛ دست‌دادنی که می‌شکند")

    def test_starting_a_product_closes_an_open_diff(self) -> None:
        self.to_line()
        update, _seen = query_update(CB.PHONE_NEW, chat_id=CHAT)
        self.assertEqual(asyncio.run(PF.entry(update, h.context())), PF.COLLECT)
        self.assertIsNone(RF.sessions.get(USER), "دیفِ باز نمی‌تواند بعد از آن اعمال شود")

    def test_cancel_says_nothing_was_written(self) -> None:
        self.to_line()
        update, seen = query_update(CB.RESTOCK_CANCEL, chat_id=CHAT)
        self.assertEqual(asyncio.run(RF.cancel(update, h.context())), END)
        self.assertIn("عوض نشد", str(seen[-1][1]))
        self.assertIsNone(RF.sessions.get(USER))

    def test_every_restock_button_is_a_real_conversation_handler(self) -> None:
        wired = "|".join(h.conversation_patterns())
        for name in ("RESTOCK_PICK", "RESTOCK_APPLY", "RESTOCK_LINE", "RESTOCK_DIFF",
                     "RESTOCK_REFRESH", "RESTOCK_ZIP", "RESTOCK_CANCEL", "RESTOCK_RETRY_SEARCH"):
            value = getattr(CB, name).replace(":", r"\:")
            self.assertRegex(wired, value, f"{name} هندلرِ ConversationHandler ندارد")
        self.assertRegex(wired, CB.PHONE_RESTOCK.replace(":", r"\:"),
                         "دکمهٔ منو باید entry point باشد، نه یک هندلرِ بی‌حالت")


if __name__ == "__main__":
    unittest.main()
