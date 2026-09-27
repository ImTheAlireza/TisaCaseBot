"""Phase 3b: the loop closes with a card, a history, and a parser sandbox.

A publish used to end in one line of «✅ ساخته شد», and «چرا این متن این‌طور
خوانده شد؟» had no answer short of reading the code. Both are UI surfaces over a
small JSON ledger here, so the tests are mostly about what a human can actually
get back out of it: the right numbers, the link, the stored report, and no
product created by a screen that promised not to create one.

Run with ``python3 -m unittest discover -s tests``; the Telegram-dependent
classes skip themselves when python-telegram-bot is not installed.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("SUDO_IDS", "1234567")

from bot.services import jsonstore, products_ledger

try:
    from bot.modules import product_tools as PT
    from bot.keyboards import result_card, result_keyboard
    HAS_FLOW = True
except Exception:                                  # pragma: no cover - PTB missing
    PT = None
    HAS_FLOW = False

needs_flow = unittest.skipUnless(HAS_FLOW, "python-telegram-bot is not installed")


class LedgerTestCase(unittest.TestCase):
    """Every case gets an empty store in a temp dir, restored afterwards."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._file = products_ledger.FILE
        products_ledger.FILE = self.tmp / "recent_products.json"
        jsonstore.invalidate()
        self.addCleanup(self._restore)

    def _restore(self):
        products_ledger.FILE = self._file
        jsonstore.invalidate()

    def _raw(self):
        return json.loads(products_ledger.FILE.read_text(encoding="utf-8"))


class TestLedger(LedgerTestCase):
    def test_record_then_find_and_recent(self):
        entry = products_ledger.record(user_id=7, product_id=4321, edit_url="https://x/e",
                                        title="قاب سیلیکونی", variations=79, price=698000,
                                        images=3, sku_prefix="BO147")
        self.assertEqual(products_ledger.recent(5), [entry])
        self.assertEqual(products_ledger.find(entry["key"])["product_id"], 4321)
        self.assertTrue(products_ledger.FILE.exists())
        self.assertEqual(self._raw()["version"], 1)

    def test_newest_first_and_capped(self):
        for index in range(products_ledger.MAX_ENTRIES + 6):
            products_ledger.record(user_id=7, product_id=index, title=f"p{index}")
        entries = products_ledger.recent(100)
        self.assertEqual(len(entries), products_ledger.MAX_ENTRIES)
        self.assertEqual(entries[0]["title"], f"p{products_ledger.MAX_ENTRIES + 5}")

    def test_report_is_stored_but_capped(self):
        huge = "x" * (products_ledger.REPORT_LIMIT + 500)
        entry = products_ledger.record(user_id=7, title="t", report=huge)
        self.assertEqual(len(products_ledger.find(entry["key"])["report"]), products_ledger.REPORT_LIMIT)

    def test_summary_mentions_the_outcome(self):
        created = products_ledger.record(user_id=7, title="قاب", variations=79, product_id=12)
        failed = products_ledger.record(user_id=7, title="کیف", status="failed", error="HTTP 401")
        zip_entry = products_ledger.record(user_id=7, title="ست", status="zip")
        self.assertIn("79 واریژن", products_ledger.summary(created))
        self.assertTrue(products_ledger.summary(failed).startswith("❌"))
        self.assertIn("HTTP 401", products_ledger.summary(failed))
        self.assertTrue(products_ledger.summary(zip_entry).startswith("📦"))

    def test_price_range_prefers_the_groups(self):
        entry = products_ledger.record(user_id=7, price=698000,
                                       price_groups={"iphone": 698000, "android": 598000})
        self.assertIn("iphone: 698,000", products_ledger.price_range(entry))
        self.assertEqual(products_ledger.price_range({"price": 0, "price_groups": {}}), "—")

    def test_corrupt_file_is_survivable(self):
        products_ledger.FILE.write_text("{ not json", encoding="utf-8")
        jsonstore.invalidate()
        self.assertEqual(products_ledger.recent(), [])
        products_ledger.record(user_id=7, title="بعد از خرابی")
        self.assertEqual(len(products_ledger.recent()), 1)

    def test_clear_returns_what_it_dropped(self):
        products_ledger.record(user_id=7, title="a")
        products_ledger.record(user_id=7, title="b")
        self.assertEqual(products_ledger.clear(), 2)
        self.assertEqual(products_ledger.recent(), [])

    def test_find_unknown_key_is_none(self):
        self.assertIsNone(products_ledger.find("no-such-key"))

    def test_queued_summary_shows_only_the_short_error_type(self):
        entry = products_ledger.record(
            user_id=7, status="queued", title="قاب",
            error="ReadTimeout: پاسخ فروشگاه نرسید؛ ممکن است درخواست انجام شده باشد",
        )
        text = products_ledger.summary(entry)
        self.assertIn("ReadTimeout", text)
        self.assertNotIn("پاسخ فروشگاه", text)


@needs_flow
class TestResultCard(LedgerTestCase):
    def _entry(self, **kwargs):
        kwargs.setdefault("title", "قاب سیلیکونی")
        return products_ledger.record(user_id=7, variations=79, images=3,
                                       price=698000, sku_prefix="BO147", **kwargs)

    def test_created_card_shows_id_link_and_counts(self):
        entry = self._entry(product_id=4321, edit_url="https://shop/wp-admin/post.php?post=4321")
        text = result_card(entry)
        for needle in ("4321", "https://shop/wp-admin", "79 واریژن", "698,000", "BO147"):
            self.assertIn(needle, text, needle)
        self.assertIn("پیش‌نویس ساخته شد", text)

    def test_failed_card_shows_the_error_not_a_green_tick(self):
        entry = self._entry(status="failed", error="HTTP 401: unauthorized")
        text = result_card(entry)
        self.assertIn("ساخت ناموفق", text)
        self.assertIn("HTTP 401", text)
        self.assertNotIn("4321", text)

    def test_queued_card_is_short_but_keeps_the_error_type(self):
        entry = self._entry(status="queued", error="ReadTimeout: server did not answer")
        text = result_card(entry)
        self.assertIn("تلاش مجدد خودکار", text)
        self.assertIn("ReadTimeout", text)
        self.assertNotIn("server did not answer", text)
        self.assertNotIn("24 ساعت", text)

    def test_zip_card_offers_the_next_product(self):
        entry = self._entry(status="zip")
        rows = result_keyboard(entry).inline_keyboard
        data = [button.callback_data for row in rows for button in row]
        self.assertIn("product:next:new", data)
        self.assertTrue(any(str(x).startswith("products:open:") for x in data))

    def test_restock_card_keeps_the_restock_mode(self):
        entry = self._entry(mode="update")
        data = [button.callback_data for row in result_keyboard(entry).inline_keyboard for button in row]
        self.assertIn("product:next:update", data)

    def test_warnings_survive_into_the_card(self):
        entry = self._entry(warnings=["رنگ برای ۲ مدل محدود نشد"])
        self.assertIn("محدود نشد", result_card(entry))

    def test_html_in_titles_cannot_break_the_message(self):
        entry = self._entry(title="<b>قاب</b> & بیشتر")
        text = result_card(entry)
        self.assertNotIn("<b>قاب</b>", text)
        self.assertIn("&lt;b&gt;", text)


@needs_flow
class TestProductTools(LedgerTestCase):
    def setUp(self):
        super().setUp()

    def _query(self, data, *, user_id=7):
        sent = []

        async def edit_message_text(text, **kwargs):
            sent.append(("edit", text, kwargs))

        async def reply_text(text, **kwargs):
            sent.append(("reply", text, kwargs))
            return SimpleNamespace(message_id=1)

        async def answer(*a, **k):
            return None

        query = SimpleNamespace(
            data=data, from_user=SimpleNamespace(id=user_id), answer=answer,
            message=SimpleNamespace(reply_text=reply_text, chat_id=user_id),
            edit_message_text=edit_message_text,
        )
        return SimpleNamespace(callback_query=query), sent

    def test_recent_lists_cards_and_links_each_one(self):
        entry = products_ledger.record(user_id=7, title="قاب", product_id=11, variations=4)
        update, sent = self._query("products:recent")
        asyncio.run(PT.cb_recent(update, SimpleNamespace()))
        text = sent[0][1]
        self.assertIn("قاب", text)
        markup = sent[0][2]["reply_markup"]
        buttons = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn(f"products:open:{entry['key']}", buttons)

    def test_recent_without_history_says_so(self):
        update, sent = self._query("products:recent")
        asyncio.run(PT.cb_recent(update, SimpleNamespace()))
        self.assertIn("ساخته نشده", sent[0][1])

    def test_open_shows_a_short_summary_and_keeps_preview_optional(self):
        products_ledger.record(user_id=7, title="قاب", product_id=11,
                               report="<b>پیش‌نمایش</b> قیمت: 698,000")
        entry = products_ledger.recent(1)[0]
        update, sent = self._query(f"products:open:{entry['key']}")
        asyncio.run(PT.cb_open(update, SimpleNamespace()))
        self.assertNotIn("پیش‌نمایش</b>", sent[0][1], "detail is hidden unless requested")
        self.assertEqual(sent[0][0], "reply", "the history list must stay on screen")
        buttons = [b.callback_data for row in sent[0][2]["reply_markup"].inline_keyboard for b in row]
        self.assertIn(f"products:report:{entry['key']}", buttons)

    def test_approved_preview_is_available_on_demand(self):
        products_ledger.record(user_id=7, title="قاب", report="<b>پیش‌نمایش</b> قیمت: 698,000")
        entry = products_ledger.recent(1)[0]
        update, sent = self._query(f"products:report:{entry['key']}")
        asyncio.run(PT.cb_report(update, SimpleNamespace()))
        self.assertIn("پیش‌نمایش", sent[0][1])
        self.assertIn(f"products:summary:{entry['key']}", [
            b.callback_data for row in sent[0][2]["reply_markup"].inline_keyboard for b in row
        ])

    def test_open_with_a_lost_key_is_polite(self):
        update, sent = self._query("products:open:999999")
        asyncio.run(PT.cb_open(update, SimpleNamespace()))
        self.assertIn("پیدا نشد", sent[0][1])

    def test_parser_test_arms_and_disarms(self):
        data = {}
        context = SimpleNamespace(user_data=data)
        update, sent = self._query("tools:parser")
        asyncio.run(PT.cb_parser_test(update, context))
        self.assertGreater(data[PT._PENDING_KEY], time.time())
        self.assertIn("هیچ محصولی ساخته نمی‌شود", sent[0][1])

        update, sent = self._query("tools:parser:cancel")
        asyncio.run(PT.cb_parser_test_cancel(update, context))
        self.assertNotIn(PT._PENDING_KEY, data)

    def test_parser_text_runs_the_flow_pipeline_once(self):
        from bot.modules import product_flow as PF
        calls = []

        async def fake_analyze(text):
            calls.append(text)
            from bot.services.product_extractor import ProductData
            return ProductData(title="قاب", price=698000, models=["iPhone 13 Pro Max"])

        original = PF.analyze
        PF.analyze = fake_analyze
        self.addCleanup(setattr, PF, "analyze", original)

        replies = []

        async def reply_text(text, **kwargs):
            replies.append(text)

        update = SimpleNamespace(
            effective_message=SimpleNamespace(text="قیمت 698000", reply_text=reply_text),
        )
        context = SimpleNamespace(user_data={PT._PENDING_KEY: time.time() + 30})
        asyncio.run(PT.on_parser_text(update, context))
        self.assertEqual(calls, ["قیمت 698000"])
        self.assertIn("iPhone 13 Pro Max", replies[0])
        self.assertIn("698,000", replies[0])
        self.assertNotIn(PT._PENDING_KEY, context.user_data, "one sample per tap")

    def test_expired_flag_leaves_the_message_alone(self):
        called = []

        async def fake_analyze(text):
            called.append(text)

        from bot.modules import product_flow as PF
        original = PF.analyze
        PF.analyze = fake_analyze
        self.addCleanup(setattr, PF, "analyze", original)

        update = SimpleNamespace(effective_message=SimpleNamespace(text="قیمت 698000"))
        context = SimpleNamespace(user_data={PT._PENDING_KEY: time.time() - 1})
        asyncio.run(PT.on_parser_text(update, context))
        self.assertEqual(called, [])

    def test_report_is_clipped_to_telegram_limits(self):
        from bot.services.product_extractor import ProductData
        data = ProductData(title="ق" * 5000, notes=["نکته"] * 200)
        text, mode = PT._data_report(data)
        self.assertLessEqual(len(text), PT._MAX_REPORT + 40)
        self.assertIsNone(mode, "a clipped HTML message could break on a half-written tag")

    def test_long_history_report_degrades_to_plain_text(self):
        products_ledger.record(user_id=7, title="قاب", product_id=1, report="<b>" + "ب" * 9000 + "</b>")
        entry = products_ledger.recent(1)[0]
        update, sent = self._query(f"products:open:{entry['key']}")
        asyncio.run(PT.cb_open(update, SimpleNamespace()))
        _kind, text, kwargs = sent[0]
        self.assertNotIn("parse_mode", kwargs)
        self.assertNotIn("<", text[-200:], "no dangling tag may reach Telegram")


@needs_flow
class TestNextProductEntry(LedgerTestCase):
    def setUp(self):
        super().setUp()
        from bot.modules import product_flow as PF
        self.PF = PF
        PF.sessions.clear()
        PF.album_buffers.clear()
        PF.album_tasks.clear()
        # The flow's RBAC gate reads the admin store; the test user is not in it.
        self._allowed = PF.feature_allowed
        PF.feature_allowed = lambda user_id, key: True
        self.addCleanup(setattr, PF, "feature_allowed", self._allowed)

    def _query(self, data, *, user_id=7, message_id=55):
        actions = []

        async def answer(*a, **k):
            return None

        async def edit_message_text(text, **kwargs):
            actions.append(("edit", text))

        async def reply_text(text, **kwargs):
            actions.append(("reply", text))
            return SimpleNamespace(message_id=message_id + 1)

        # CallbackQuery exposes both: edit_message_text on the query itself (the
        # flow's shortcut) and reply_text on the message it came from.
        query = SimpleNamespace(
            data=data, from_user=SimpleNamespace(id=user_id), answer=answer,
            edit_message_text=edit_message_text,
            message=SimpleNamespace(reply_text=reply_text, edit_message_text=edit_message_text,
                                    chat_id=user_id, message_id=message_id),
        )
        return SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=user_id)), actions

    def test_next_keeps_the_mode_and_starts_clean(self):
        PF = self.PF
        PF.sessions[7] = PF.ProductSession(mode="new", info_text="قبلی")
        update, actions = self._query("product:next:update")
        result = asyncio.run(PF.entry(update, SimpleNamespace()))
        self.assertEqual(result, PF.WAITING)
        self.assertEqual(PF.sessions[7].mode, "update")
        self.assertEqual(PF.sessions[7].info_text, "", "the previous product's text must not leak")
        self.assertEqual(actions[0][0], "reply", "the result card must stay readable")

    def test_next_still_respects_the_permission_gate(self):
        PF = self.PF
        PF.feature_allowed = lambda user_id, key: False
        update, actions = self._query("product:next:update")
        result = asyncio.run(PF.entry(update, SimpleNamespace()))
        self.assertEqual(result, -1, "a stale card must not open a flow for a demoted admin")
        self.assertNotIn(7, PF.sessions)

    def test_menu_entry_still_edits_its_own_message(self):
        PF = self.PF
        update, actions = self._query("phone:new")
        asyncio.run(PF.entry(update, SimpleNamespace()))
        self.assertEqual(actions[0][0], "edit")


@needs_flow
class TestLedgerRecordsPublishes(LedgerTestCase):
    """A publish is only «done» when it is written down."""

    def test_record_result_stores_the_preview_snapshot(self):
        from bot.modules import product_flow as PF
        from bot.services.product_extractor import ProductData

        session = PF.ProductSession(mode="new", files=[Path("/tmp/a.jpg")])
        session.data = ProductData(title="قاب", price=698000, sku_prefix="BO147",
                                   categories=["قاب و کاور گوشی و تبلت > آیفون iphone"])
        session.data.variation_count = 12
        entry = PF._record_result(7, session, session.data, status="created",
                                  product_id=99, edit_url="https://x/99", warnings=["w1"])
        stored = products_ledger.find(entry["key"])
        self.assertEqual(stored["product_id"], 99)
        self.assertEqual(stored["variations"], 12)
        self.assertIn("پیش‌نمایش", stored["report"])
        self.assertEqual(stored["warnings"], ["w1"])

    def test_failures_are_recorded_with_the_error(self):
        from bot.modules import product_flow as PF
        from bot.services.product_extractor import ProductData

        session = PF.ProductSession(mode="new")
        session.data = ProductData(title="قاب")
        entry = PF._record_result(7, session, session.data, status="failed",
                                 error="HTTP 500: boom")
        self.assertEqual(entry["status"], "failed")
        self.assertIn("boom", products_ledger.summary(entry))


@needs_flow
class TestPublishAuditSummary(unittest.TestCase):
    def test_failed_http_request_and_media_context_survive_condensing(self):
        from bot.modules.product_flow import _audit_for_chat

        lines = [
            "[config] تعداد تصاویر: 7",
            "[media:start] آپلود 03_آبی.jpg؛ حجم 123,456 بایت",
            "[http:start] #4 POST /wp-json/wp/v2/media (تلاش 1/1؛ مهلت هر فاز=45s)",
            "[media:error] آپلود 03_آبی.jpg (123,456 بایت): ReadTimeout: timeout",
            "[http:error] #4 POST /wp-json/wp/v2/media پس از 45,000 ms: ReadTimeout: timeout",
            "[attempt 1] POST محصول پاسخ داد HTTP 400",
        ]
        summary = _audit_for_chat(lines)
        self.assertIn("03_آبی.jpg", summary)
        self.assertIn("123,456 بایت", summary)
        self.assertIn("#4 POST /wp-json/wp/v2/media", summary)
        self.assertIn("45,000 ms", summary)


if __name__ == "__main__":
    unittest.main()
