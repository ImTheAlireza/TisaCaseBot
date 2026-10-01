"""«🔄 اپدیت محصول» — the conversation: search, pick, send things like a new product, review a diff.

The handlers are the real ones (``PF.entry``, ``RF.handle_search``, ``RF.pick``, ``PF.on_text``,
``PF.confirm`` …) driven through a recording Telegram stub, against :class:`_update_shop.UpdateShop`
— a shop that remembers — or the built-in dry-run store. The text is read by the real
deterministic parser (no AI is configured), so what these tests assert is what a seller would see.

The seller's own rules are the test names: the product is picked like a search; after that it is a
new product's intake whose card shows only what differs; errors are never new messages; the log
group, not the seller's chat, gets the technical trace; and nothing is written until
«✅ اعمال تغییرات».
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("SUDO_IDS", "1234567")

import _flow_harness as h
from _update_shop import UpdateShop, product_row

try:
    from PIL import Image
    from telegram.ext import ConversationHandler

    from bot.constants import CB
    from bot.modules import product_flow as PF
    from bot.modules import restock_flow as RF
    from bot.services import flow_guard, flow_state, product_match, products_ledger, update_apply

    HAS_FLOW = True
except Exception:                          # pragma: no cover - PTB / Pillow missing
    CB = PF = RF = ConversationHandler = Image = None            # type: ignore[assignment]
    flow_guard = flow_state = product_match = products_ledger = update_apply = None  # type: ignore[assignment]
    HAS_FLOW = False

needs_flow = unittest.skipUnless(HAS_FLOW, "python-telegram-bot is not installed")

USER, CHAT, LOG_CHAT = 7, 9, -100777
END = -1 if not HAS_FLOW else ConversationHandler.END


def plain(text: object) -> str:
    return re.sub(r"<[^>]+>", "", str(text or ""))


def labels(markup: object) -> list[str]:
    return [button.text for row in getattr(markup, "inline_keyboard", []) or [] for button in row]


def callbacks(markup: object) -> list[str]:
    return [str(button.callback_data) for row in getattr(markup, "inline_keyboard", []) or []
            for button in row]


class Bot:
    """A recording Telegram bot: every send, edit, delete, reaction and chat action, in order."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int | None, object, dict]] = []
        self.next_id = 100

    async def send_message(self, text=None, **kwargs):
        self.next_id += 1
        self.calls.append(("send", self.next_id, text, kwargs))
        return SimpleNamespace(message_id=self.next_id)

    async def edit_message_text(self, text=None, **kwargs):
        self.calls.append(("edit", kwargs.get("message_id"), text, kwargs))
        return SimpleNamespace(message_id=kwargs.get("message_id"))

    async def delete_message(self, **kwargs):
        self.calls.append(("delete", kwargs.get("message_id"), None, kwargs))
        return True

    async def send_chat_action(self, **kwargs):
        self.calls.append(("action", None, kwargs.get("action"), kwargs))

    async def set_message_reaction(self, **kwargs):
        self.calls.append(("react", kwargs.get("message_id"), kwargs.get("reaction"), kwargs))
        return True

    async def get_file(self, file_id):
        async def download_to_drive(*, custom_path):
            Image.new("RGB", (16, 16), (200, 30, 30)).save(custom_path, "JPEG")
        return SimpleNamespace(file_size=100, download_to_drive=download_to_drive)

    def of(self, kind: str) -> list[tuple[str, int | None, object, dict]]:
        return [call for call in self.calls if call[0] == kind]

    def to_chat(self, chat_id: int) -> list[tuple[str, int | None, object, dict]]:
        return [call for call in self.calls if call[0] in ("send", "edit")
                and call[3].get("chat_id") == chat_id]


@needs_flow
class UpdateFlowCase(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(h.temp_ledger())
        self.enterContext(h.patched_settings(h.settings_with(log_chat_id=LOG_CHAT)))
        self.enterContext(h.no_sleep())
        PF.sessions.clear()
        RF.sessions.clear()
        self.addCleanup(PF.sessions.clear)
        self.addCleanup(RF.sessions.clear)
        self.tmp = Path(tempfile.mkdtemp(prefix="tisa-update-flow-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        saved_dir = PF.TEMP_DIR
        PF.TEMP_DIR = self.tmp
        self.addCleanup(setattr, PF, "TEMP_DIR", saved_dir)
        saved_allowed = PF.feature_allowed
        PF.feature_allowed = lambda user_id, key: True
        self.addCleanup(setattr, PF, "feature_allowed", saved_allowed)
        self.bot = Bot()
        self.ctx = SimpleNamespace(bot=self.bot, chat_data={}, job_queue=None)
        self.shop = UpdateShop()
        self.install(self.shop)
        flow_state.clear(USER)

    # — wiring —
    def install(self, shop: UpdateShop) -> None:
        """Route the flow's shop calls to ``shop`` (the flow itself passes no transport)."""
        real_find, real_read, real_apply = product_match.find, product_match.read, update_apply.apply
        self.read_calls: list[int] = []

        async def find(query, **kwargs):
            kwargs.pop("dry_run", None)
            return await real_find(query, transport=shop.transport, **kwargs)

        async def read(product_id, **kwargs):
            kwargs.pop("dry_run", None)
            self.read_calls.append(product_id)
            return await real_read(product_id, transport=shop.transport, **kwargs)

        async def apply(plan, files=(), **kwargs):
            kwargs.pop("dry_run", None)
            return await real_apply(plan, files, transport=shop.transport, **kwargs)

        product_match.find, product_match.read, update_apply.apply = find, read, apply
        self.addCleanup(setattr, product_match, "find", real_find)
        self.addCleanup(setattr, product_match, "read", real_read)
        self.addCleanup(setattr, update_apply, "apply", real_apply)

    # — driving —
    def tap(self, handler, data: str):
        update, seen = h.query_update(data, user_id=USER, chat_id=CHAT)
        result = asyncio.run(handler(update, self.ctx))
        return result, seen

    def say(self, text: str, *, handler=None, message_id: int = 40):
        update, seen = h.message_update(text, user_id=USER, chat_id=CHAT)
        update.effective_message.message_id = message_id
        update.effective_message.from_user = SimpleNamespace(id=USER)
        result = asyncio.run((handler or PF.on_text)(update, self.ctx))
        return result, seen

    def start(self):
        return self.tap(PF.entry, CB.PHONE_RESTOCK)

    def pick(self, product_id: int = 1201):
        return self.tap(RF.pick, f"{CB.RESTOCK_PICK}:{product_id}")

    def open_update(self):
        """Search prompt → pick → the guide: the chat is now in the builder, against product 1201."""
        self.start()
        result, seen = self.pick()
        self.assertEqual(result, PF.COLLECT)
        return seen

    def send_photos(self, count: int = 2):
        """Photos arrive the way Telegram sends them: download, compress, extract, paint."""
        messages = [SimpleNamespace(
            message_id=70 + index, photo=[SimpleNamespace(file_id=f"f{index}")], document=None,
            caption=None, text=None, media_group_id=None) for index in range(count)]
        asyncio.run(PF._prepare_files(USER, messages, self.ctx))

    @property
    def session(self):
        return PF.sessions[USER]

    def card_text(self) -> str:
        """The text of the live preview card as the seller sees it now."""
        card_id = self.session.status_message_id
        texts = [call[2] for call in self.bot.calls if call[0] in ("send", "edit")
                 and (call[1] == card_id or (call[0] == "send" and call[1] == card_id))]
        return plain(texts[-1]) if texts else ""

    def card_buttons(self) -> list[str]:
        card_id = self.session.status_message_id
        shown = [call[3].get("reply_markup") for call in self.bot.calls
                 if call[0] in ("send", "edit") and call[1] == card_id]
        return labels(shown[-1]) if shown else []

    def grid_models(self) -> set[str]:
        return {values[0] for values in self.shop.grid()}


@needs_flow
class FindingTheProduct(UpdateFlowCase):
    def test_the_button_opens_a_search_not_the_builder(self) -> None:
        result, seen = self.start()
        self.assertEqual(result, RF.RESTOCK_MATCH)
        prompt = plain(seen[-1][1])
        self.assertIn("SKU", prompt)
        self.assertIn("اپدیت", prompt)
        self.assertNotIn("ZIP", prompt, "فایل/ZIP دیگر در این جریان نیست")
        self.assertNotIn(USER, PF.sessions, "تا محصولی انتخاب نشده چیزی ساخته نمی‌شود")
        self.assertIn(USER, RF.sessions)
        self.assertEqual(seen[-1][0], "edit", "تپ دکمه، پیام زیر دست را ویرایش می‌کند")

    def test_the_menu_button_is_called_update_not_restock(self) -> None:
        from bot.buttons import BY_KEY
        self.assertIn("اپدیت", BY_KEY["product_restock"].label)

    def test_next_on_a_result_card_posts_the_search_under_it(self) -> None:
        result, seen = self.tap(PF.entry, "product:next:update")
        self.assertEqual(result, RF.RESTOCK_MATCH)
        self.assertEqual(seen[-1][0], "text", "کارت نتیجه سر جایش می‌ماند؛ جست‌وجو زیرش می‌آید")
        self.assertNotIn("edit", [item[0] for item in seen])

    def test_a_user_without_access_is_turned_away(self) -> None:
        PF.feature_allowed = lambda user_id, key: key != "product_restock"
        result, seen = self.start()
        self.assertEqual(result, END)
        self.assertTrue(any(item[0] == "answer" and "دسترسی" in str(item[1]) for item in seen))

    def test_a_search_shows_candidates_with_a_typing_action_and_no_status_message(self) -> None:
        self.start()
        _result, seen = self.say("BO7", handler=RF.handle_search)
        self.assertEqual([call[2] for call in self.bot.of("action")], ["typing"])
        texts = [str(item[1]) for item in seen if item[0] == "text"]
        self.assertEqual(len(texts), 1, "فقط جوابِ جست‌وجو؛ «دنبال می‌گردم…» پیامِ جدا نیست")
        self.assertNotIn("دنبال می‌گردم", texts[0])
        self.assertIn("قاب سیلیکونی آیفون", texts[0])
        buttons = callbacks(seen[-1][2]["reply_markup"])
        self.assertIn(f"{CB.RESTOCK_PICK}:1201", buttons)

    def test_a_title_fragment_finds_it_too(self) -> None:
        self.start()
        _result, seen = self.say("سیلیکونی", handler=RF.handle_search)
        self.assertIn(f"{CB.RESTOCK_PICK}:1201", callbacks(seen[-1][2]["reply_markup"]))

    def test_a_search_with_no_hit_says_so_in_the_chat(self) -> None:
        self.start()
        result, seen = self.say("هیچی", handler=RF.handle_search)
        self.assertEqual(result, RF.RESTOCK_MATCH)
        self.assertIn("پیدا نکردم", str(seen[-1][1]))

    def test_a_shop_that_is_down_is_a_reaction_and_a_log_never_a_message(self) -> None:
        self.shop.search_status = 503
        self.start()
        result, seen = self.say("BO7", handler=RF.handle_search)
        self.assertEqual(result, RF.RESTOCK_MATCH)
        self.assertEqual([item for item in seen if item[0] == "text"], [], "پیام تازه‌ای برای خطا نیست")
        self.assertEqual([call[2] for call in self.bot.of("react")], [["⚡"]])
        journal = self.ctx.chat_data["tisa_product_journal"]
        self.assertTrue(any("جست‌وجو در فروشگاه ناموفق" in line for line in journal.trace),
                        "دلیل فنی برای گروه لاگ می‌ماند")

    def test_a_photo_before_a_product_is_picked_gets_a_reaction_only(self) -> None:
        self.start()
        update, seen = h.message_update("", user_id=USER, chat_id=CHAT)
        update.effective_message.from_user = SimpleNamespace(id=USER)
        result = asyncio.run(RF.ignore_media(update, self.ctx))
        self.assertEqual(result, RF.RESTOCK_MATCH)
        self.assertEqual(seen, [], "هیچ پیامی")
        self.assertEqual(len(self.bot.of("react")), 1)

    def test_a_seller_s_recent_products_are_one_tap_away(self) -> None:
        products_ledger.record(user_id=USER, status="created", product_id=1201, title="قاب سیلیکونی")
        products_ledger.record(user_id=USER, status="updated", product_id=1300, title="قاب دوم")
        products_ledger.record(user_id=USER, status="failed", product_id=1400, title="خراب")
        products_ledger.record(user_id=99, status="created", product_id=1500, title="مال دیگری")
        _result, seen = self.start()
        buttons = callbacks(seen[-1][2]["reply_markup"])
        self.assertIn(f"{CB.RESTOCK_PICK}:1201", buttons)
        self.assertIn(f"{CB.RESTOCK_PICK}:1300", buttons, "محصولِ اپدیت‌شده هم دوباره قابل انتخاب است")
        self.assertNotIn(f"{CB.RESTOCK_PICK}:1400", buttons)
        self.assertNotIn(f"{CB.RESTOCK_PICK}:1500", buttons)

    def test_the_search_can_be_restarted_and_cancelled(self) -> None:
        self.start()
        _result, seen = self.tap(RF.retry_search, CB.RESTOCK_RETRY_SEARCH)
        self.assertEqual(seen[-1][0], "edit")
        result, seen = self.tap(RF.cancel, CB.RESTOCK_CANCEL)
        self.assertEqual(result, END)
        self.assertIn("چیزی در فروشگاه عوض نشد", str(seen[-1][1]))
        self.assertEqual(seen[-1][0], "edit", "لغو هم پیام زیر دست را ویرایش می‌کند")
        self.assertNotIn(USER, RF.sessions)


@needs_flow
class PickingTheProduct(UpdateFlowCase):
    def test_the_tapped_message_becomes_the_one_guide_and_the_builder_opens(self) -> None:
        self.start()
        result, seen = self.pick()
        self.assertEqual(result, PF.COLLECT)
        guide = seen[-1]
        self.assertEqual(guide[0], "edit", "تپ، ویرایشِ درجاست؛ پیامِ تازه نیست")
        text = str(guide[1])
        self.assertIn("قاب سیلیکونی آیفون", text)
        self.assertIn("BO7", text)
        self.assertIn("3 واریژن", text)
        self.assertIn("iPhone 13 Pro Max", text)
        self.assertIn("هرچه نفرستی دست‌نخورده می‌ماند", text)
        self.assertEqual(callbacks(guide[2]["reply_markup"]), ["product:cancel"],
                         "راهنما فقط دکمهٔ لغو دارد")
        self.assertEqual(self.session.mode, "update")
        self.assertEqual(self.session.target.product_id, 1201)
        self.assertEqual(self.session.guide_message_id, seen[-1][2]["reply_markup"] and self.session.guide_message_id)
        self.assertNotIn(USER, RF.sessions, "جست‌وجو تمام شد؛ دو جریان هم‌زمان باز نمی‌ماند")

    def test_the_guide_says_what_the_product_has_now(self) -> None:
        self.start()
        _result, seen = self.pick()
        text = str(seen[-1][1])
        self.assertIn("598,000 تا 698,000", text)
        self.assertIn("رنگ‌ها:", text)

    def test_the_guide_tells_the_seller_what_a_list_and_a_missing_price_do(self) -> None:
        self.start()
        _result, seen = self.pick()
        text = str(seen[-1][1])
        self.assertIn("قیمت نفرستی ← قیمت فعلی می‌ماند", text)
        self.assertIn("موجودی عوض شد ← موجودی تازه می‌نشیند", text)
        self.assertIn("همهٔ واریژن‌ها پاک و از نو ساخته می‌شوند", text)
        self.assertIn("اگر ننویسی، همان می‌ماند", text)

    def test_a_product_the_shop_cannot_give_is_a_toast_and_the_list_stays(self) -> None:
        self.shop.product_put_status = None
        self.start()

        async def broken(product_id, **kwargs):
            raise product_match.WooCommerceAPIError(500, "سرور خراب است")

        product_match.read = broken
        result, seen = self.pick()
        self.assertEqual(result, RF.RESTOCK_MATCH)
        answers = [item for item in seen if item[0] == "answer"]
        self.assertEqual(len(answers), 1)
        self.assertTrue(answers[0][2].get("show_alert"))
        self.assertIn("خوانده نشد", str(answers[0][1]))
        self.assertNotIn(USER, PF.sessions)
        self.assertIn(USER, RF.sessions, "فهرست سر جایش است؛ کاندید دیگری می‌شود زد")

    def test_an_old_button_is_a_toast(self) -> None:
        self.start()
        result, seen = self.tap(RF.pick, f"{CB.RESTOCK_PICK}:abc")
        self.assertEqual(result, RF.RESTOCK_MATCH)
        self.assertTrue(seen[-1][2].get("show_alert"))

    def test_the_interrupted_flow_is_recorded_as_an_update(self) -> None:
        self.open_update()
        pending = flow_state.pending()
        self.assertEqual(pending[str(USER)]["mode"], "update")

    def test_starting_a_new_product_closes_an_open_update(self) -> None:
        self.open_update()
        _result, _seen = self.tap(PF.entry, CB.PHONE_NEW)
        self.assertEqual(PF.sessions[USER].mode, "new")
        self.assertIsNone(PF.sessions[USER].target)

    def test_starting_an_update_closes_an_open_new_product(self) -> None:
        self.tap(PF.entry, CB.PHONE_NEW)
        self.assertEqual(PF.sessions[USER].mode, "new")
        self.start()
        self.assertNotIn(USER, PF.sessions)
        self.assertIn(USER, RF.sessions)


@needs_flow
class TheDiffCard(UpdateFlowCase):
    def test_text_becomes_a_diff_card_with_the_apply_button(self) -> None:
        self.open_update()
        result, _seen = self.say("قیمت 720000 تومان\nموجودی 12")
        self.assertEqual(result, PF.REVIEW)
        text = self.card_text()
        self.assertIn("پیش‌نمایش اپدیت", text)
        self.assertIn("قاب سیلیکونی آیفون · #1201 · SKU: BO7", text)
        self.assertIn("698,000 ← 720,000", text)
        self.assertIn("0 ← 12", text)
        self.assertIn("دست‌نخورده", text)
        self.assertIn("✅ اعمال تغییرات", self.card_buttons())
        for stale in ("✅ تأیید و ساخت", "جایگزینی تصاویر", "تصاویر فعلی"):
            self.assertFalse(any(stale in label for label in self.card_buttons()),
                             f"دکمهٔ «{stale}» دیگر معنی ندارد")

    def test_nothing_is_written_while_the_card_is_only_a_card(self) -> None:
        self.open_update()
        self.say("قیمت 720000 تومان\nموجودی 12")
        self.assertEqual([call for call in self.shop.requests if call[0] != "GET"], [])

    def test_a_price_that_was_not_sent_is_not_in_the_card_or_the_payload(self) -> None:
        self.open_update()
        self.say("موجودی 12")
        text = self.card_text()
        self.assertNotIn("💰", text)
        self.assertIn("قیمت", text.split("دست‌نخورده")[1], "قیمت در فهرستِ مانده‌هاست")

    def test_models_one_per_line_replace_the_old_list_in_the_preview(self) -> None:
        self.open_update()
        self.say("iPhone 13 Pro Max\niPhone 15\nقیمت 720000 تومان")
        text = self.card_text()
        self.assertIn("➕ iPhone 15", text)
        self.assertIn("🗑 S24 Ultra", text)

    def test_a_changed_list_says_that_every_variation_is_rebuilt_and_what_each_was(self) -> None:
        self.open_update()
        self.say("iPhone 13 Pro Max\niPhone 15\nقیمت 720000 تومان\nموجودی 12")
        text = self.card_text()
        self.assertIn("♻️ واریژن‌ها: همهٔ 3 واریژن فعلی پاک می‌شوند و 5 ترکیب از نو ساخته می‌شود", text)
        self.assertIn("698,000 ← 720,000 تومان (2 واریژن)", text, "قبل ← بعدِ هر ترکیبی که از قبل بود")
        self.assertIn("0 ← 12 عدد (2 واریژن)", text)
        self.assertNotIn("➕ 5 · 🗑 3", text)
        self.assertEqual([call for call in self.shop.requests if call[0] != "GET"], [],
                         "پیش‌نمایش چیزی را عوض نمی‌کند")

    def test_a_list_that_repeats_the_shop_says_nothing_about_a_rebuild(self) -> None:
        self.open_update()
        self.say("iPhone 13 Pro Max\nS24 Ultra\nقیمت 720000 تومان")
        text = self.card_text()
        self.assertNotIn("♻️", text)
        self.assertNotIn("از نو ساخته", text)
        self.assertIn("698,000 ← 720,000", text)

    def test_the_same_text_as_the_shop_has_is_no_difference(self) -> None:
        self.open_update()
        self.say("قیمت 698000 تومان")
        self.assertIn("598,000 ← 698,000", self.card_text(), "فقط S24 با ۵۹۸ فرق دارد")
        self.say("قیمت 698000 تومان\nقیمت آندروید 598000")
        # the builder's grammar for group prices is its own; the point is the empty-diff message
        shop = UpdateShop(product_row(), [])
        shop.variations = {}

    def test_a_title_that_differs_is_a_row_and_one_that_matches_is_not(self) -> None:
        self.open_update()
        self.say("عنوان: قاب مگنتی آیفون\nقیمت 720000 تومان")
        self.assertIn("✏️ عنوان: قاب سیلیکونی آیفون ← قاب مگنتی آیفون", self.card_text())

    def test_a_stray_line_is_never_taken_for_a_new_title(self) -> None:
        self.open_update()
        self.say("سلام\nقیمت 720000 تومان")
        text = self.card_text()
        self.assertNotIn("✏️ عنوان", text, "«سلام» عنوانِ محصول نمی‌شود")
        self.assertIn("«سلام» به‌عنوان عنوان برداشت شد ولی دست نمی‌خورد", text,
                      "فروشنده باید بداند ربات چه خوانده و چرا نگذاشته")
        self.assertIn("عنوان: …", text, "راهِ عوض‌کردن عنوان گفته شده")
        self.assertIn("698,000 ← 720,000", text, "بقیهٔ پیام سر جایش اعمال می‌شود")

    def test_a_title_the_seller_states_or_edits_is_a_title(self) -> None:
        data = PF.ProductData(title="قاب تازه")
        plain_session = PF.ProductSession(mode="update", info_text="قاب تازه")
        self.assertFalse(PF._says_title(plain_session, data))
        stated = PF.ProductSession(mode="update", info_text="قیمت 10\nعنوان: قاب تازه")
        self.assertTrue(PF._says_title(stated, data))
        for label in ("نام محصول: قاب", "اسم محصول: قاب", "title: x"):
            self.assertTrue(PF._says_title(PF.ProductSession(mode="update", model_text=label), data), label)
        self.assertFalse(PF._says_title(PF.ProductSession(mode="update", info_text="عنوان:"), data),
                         "برچسب بدون مقدار چیزی نگفته")
        edited = PF.ProductData(title="قاب تازه", user_edits={"title": "قاب تازه"})
        self.assertTrue(PF._says_title(PF.ProductSession(mode="update"), edited),
                        "ویرایشِ دستیِ عنوان یعنی خودش خواسته")

    def test_a_guessed_title_that_equals_the_shops_says_nothing(self) -> None:
        self.open_update()
        self.say("قاب سیلیکونی آیفون\nقیمت 720000 تومان")
        self.assertNotIn("برداشت شد", self.card_text(), "همان عنوانِ فعلی است؛ حرفی برای گفتن نیست")

    def test_the_card_fills_in_stage_by_stage_on_one_message(self) -> None:
        self.open_update()
        self.say("قیمت 720000 تومان\nموجودی 12")
        card_id = self.session.status_message_id
        steps = [plain(call[2]) for call in self.bot.calls
                 if call[0] in ("send", "edit") and call[1] == card_id]
        self.assertGreaterEqual(len(steps), 2, "کارت مرحله‌به‌مرحله پر می‌شود")
        self.assertIn("قیمت", steps[0])
        self.assertIn("⏳", steps[0], "تا خواندن مدل‌ها تمام نشده کارت همین را می‌گوید")
        self.assertNotIn("⏳", steps[-1])
        self.assertEqual(len([c for c in self.bot.of("send") if c[1] == card_id]), 1,
                         "یک پیام؛ بقیه ویرایش همان است")

    def test_a_new_message_moves_the_card_to_the_bottom_and_a_tap_edits_in_place(self) -> None:
        self.open_update()
        self.say("قیمت 720000 تومان")
        first = self.session.status_message_id
        self.say("موجودی 12", message_id=41)
        second = self.session.status_message_id
        self.assertNotEqual(first, second)
        self.assertIn(("delete", first), [(call[0], call[1]) for call in self.bot.calls],
                      "کارت قبلی پاک می‌شود و تازه پایین چت می‌آید")
        sends_before = len(self.bot.of("send"))
        self.tap(PF.edit, "product:edit")
        self.assertEqual(len(self.bot.of("send")), sends_before, "تپ دکمه پیام تازه نمی‌فرستد")

    def test_the_guide_is_never_touched_by_the_intake(self) -> None:
        self.open_update()
        guide_id = self.session.guide_message_id
        self.say("قیمت 720000 تومان")
        self.say("موجودی 12", message_id=41)
        self.assertNotIn(guide_id, [call[1] for call in self.bot.calls if call[0] in ("edit", "delete")],
                         "پیام راهنما با رسیدن متن ویرایش یا پاک نمی‌شود")

    def test_photos_alone_show_the_gallery_row_and_the_apply_button_at_once(self) -> None:
        self.open_update()
        self.send_photos(2)
        text = self.card_text()
        self.assertIn("2 تصویر جدید جای 3 تصویر فعلی می‌نشیند", text)
        self.assertIn("✅ اعمال تغییرات", self.card_buttons(),
                      "عکس به‌تنهایی یک اپدیت کامل است؛ منتظر متن نمی‌ماند")
        self.assertEqual(len(self.session.files), 2)

    def test_more_text_after_photos_adds_to_the_same_card(self) -> None:
        self.open_update()
        self.send_photos(2)
        self.say("قیمت 720000 تومان")
        text = self.card_text()
        self.assertIn("2 تصویر جدید", text)
        self.assertIn("698,000 ← 720,000", text)

    def test_the_field_picker_hides_what_an_update_never_writes(self) -> None:
        self.open_update()
        self.say("قیمت 720000 تومان")
        self.tap(PF.edit, "product:edit")
        keys = self.session.field_keys
        self.assertIn("price", keys)
        for hidden in ("sku_prefix", "categories", "wholesale_price"):
            self.assertNotIn(hidden, keys)

    def test_undo_walks_the_diff_back(self) -> None:
        self.open_update()
        self.say("قیمت 720000 تومان")
        self.say("موجودی 12", message_id=41)
        self.assertIn("0 ← 12", self.card_text())
        self.tap(PF.undo, "product:undo")
        self.assertNotIn("0 ← 12", self.card_text())
        self.assertIn("698,000 ← 720,000", self.card_text())

    def test_an_update_with_nothing_new_says_there_is_no_difference(self) -> None:
        self.open_update()
        self.say("سلام")
        text = self.card_text()
        self.assertIn("هیچ تفاوتی", text)

    def test_a_sale_price_that_is_not_below_the_price_blocks_with_the_reason_on_the_card(self) -> None:
        self.open_update()
        self.say("قیمت ویژه 900000")
        self.assertIn("کمتر نیست", self.card_text())

    def test_cancel_says_nothing_was_changed_in_the_shop(self) -> None:
        self.open_update()
        self.say("قیمت 720000 تومان")
        result, seen = self.tap(PF.cancel, "product:cancel")
        self.assertEqual(result, END)
        self.assertIn("اپدیت محصول لغو شد", str(seen[-1][1]))
        self.assertIn("چیزی در فروشگاه عوض نشد", str(seen[-1][1]))
        self.assertNotIn(USER, PF.sessions)
        self.assertEqual([call for call in self.shop.requests if call[0] != "GET"], [])

    def test_cancelling_the_search_says_it_was_an_update(self) -> None:
        self.start()
        update, _sent = h.message_update("/cancel", user_id=USER, chat_id=CHAT)
        result = asyncio.run(PF.exit_command(update, self.ctx))
        self.assertEqual(result, END)
        self.assertIn("اپدیت", str(_sent[-1][1]))

    def test_the_timeout_names_the_update(self) -> None:
        self.open_update()
        update, sent = h.message_update("x", user_id=USER, chat_id=CHAT)
        asyncio.run(PF.on_timeout(update, self.ctx))
        self.assertIn("اپدیت محصول", str(sent[-1][1]))
        self.assertNotIn(USER, PF.sessions)


@needs_flow
class ApplyingIt(UpdateFlowCase):
    def confirm(self):
        return self.tap(PF.confirm, "product:confirm")

    def test_confirm_writes_the_diff_and_the_card_becomes_the_result_in_place(self) -> None:
        self.open_update()
        self.say("iPhone 13 Pro Max\niPhone 15\nقیمت 720000 تومان\nموجودی 12")
        card_id = self.session.status_message_id
        sends_before = len(self.bot.to_chat(CHAT))
        result, seen = self.confirm()
        self.assertEqual(result, END)
        self.assertEqual(self.grid_models(), {"iPhone 13 Pro Max", "iPhone 15"})
        self.assertEqual({row["regular_price"] for row in self.shop.variations.values()}, {"720000"})
        edits = [call for call in self.bot.of("edit") if call[1] == card_id]
        self.assertIn("محصول به‌روز شد", plain(edits[-1][2]), "همان کارت به نتیجه تبدیل می‌شود")
        self.assertEqual(len(self.bot.to_chat(CHAT)) - sends_before, 1,
                         "تأیید یک پیامِ تازه به چت فروشنده نمی‌فرستد، فقط همان کارت را ویرایش می‌کند")
        self.assertEqual(len([item for item in seen if item[0] == "answer"]), 1, "فقط یک toast")
        self.assertNotIn(USER, PF.sessions)

    def test_a_new_model_list_rebuilds_the_grid_and_the_result_card_says_so(self) -> None:
        self.open_update()
        self.say("iPhone 13 Pro Max\niPhone 15\nقیمت 720000 تومان\nموجودی 12")
        before = set(self.shop.variations)
        result, _seen = self.confirm()
        self.assertEqual(result, END)
        self.assertFalse(before & set(self.shop.variations), "همهٔ واریژن‌ها شناسهٔ تازه دارند")
        self.assertEqual(self.grid_models(), {"iPhone 13 Pro Max", "iPhone 15"})
        self.assertEqual(len(self.shop.variations), 5)
        text = plain(self.bot.of("edit")[-1][2])
        self.assertIn("♻️ واریژن: همهٔ 3 واریژن پاک و 5 ترکیب از نو ساخته شد", text)
        entry = products_ledger.recent(1)[0]
        self.assertTrue(any("♻️" in line for line in entry["changes"]))

    def test_a_retry_rebuilds_the_grid_the_first_attempt_meant_from_the_product_as_it_was_picked(self) -> None:
        self.open_update()
        self.say("iPhone 13 Pro Max\niPhone 15\nقیمت 720000 تومان\nموجودی 12")
        picked = self.session.baseline
        self.assertIsNotNone(picked)
        self.shop.create_limit = 3            # 13 Pro Max × ۲, then iPhone 15 × مشکی — and the batch stops
        self.confirm()
        self.assertIsNotNone(self.shop.by_values("iPhone 15", "مشکی"))
        self.assertIsNone(self.shop.by_values("iPhone 15", "سفید"))
        self.assertIs(self.session.baseline, picked, "اولین خواندن عوض نمی‌شود، هرچند target دوباره خوانده شد")
        self.assertIsNot(self.session.target, picked)
        self.shop.create_limit = None
        result, _seen = self.confirm()
        self.assertEqual(result, END)
        self.assertEqual(len(self.shop.variations), 5)
        self.assertIsNotNone(self.shop.by_values("iPhone 15", "سفید"), "iPhone 15 نیمه‌ساخته «فقط مشکی» خوانده نشد")
        self.assertIsNotNone(self.shop.by_values("iPhone 15", "سبز"))

    def test_the_result_card_lists_what_changed_and_offers_the_next_update(self) -> None:
        self.open_update()
        self.say("قیمت 720000 تومان")
        self.confirm()
        last_edit = self.bot.of("edit")[-1]
        text = plain(last_edit[2])
        self.assertIn("تغییرها:", text)
        self.assertIn("698,000 ← 720,000", text)
        self.assertIn("🆔 #1201", text)
        buttons = callbacks(last_edit[3]["reply_markup"])
        self.assertIn("product:next:update", buttons)
        self.assertTrue(any(item.startswith(f"{CB.PRODUCTS_OPEN}:") for item in buttons))

    def test_the_ledger_records_an_update_with_its_changes(self) -> None:
        self.open_update()
        self.say("قیمت 720000 تومان")
        self.confirm()
        entry = products_ledger.recent(1)[0]
        self.assertEqual((entry["status"], entry["mode"], entry["product_id"]), ("updated", "update", 1201))
        self.assertTrue(any("720,000" in line for line in entry["changes"]))
        self.assertIn("اپدیت", products_ledger.summary(entry))

    def test_the_technical_trace_goes_to_the_log_group_and_never_to_the_seller(self) -> None:
        self.open_update()
        self.say("قیمت 720000 تومان")
        self.confirm()
        group = [call for call in self.bot.of("send") if call[3].get("chat_id") == LOG_CHAT]
        self.assertGreaterEqual(len(group), 2, "کارت لاگ و ردپای HTTP")
        group_text = " ".join(str(call[2]) for call in group)
        self.assertIn("محصول اپدیت شد", group_text)
        self.assertIn("variations/batch", group_text)
        seller_text = " ".join(plain(call[2]) for call in self.bot.to_chat(CHAT))
        self.assertNotIn("[http", seller_text)
        self.assertNotIn("ردپای", seller_text)

    def test_a_confirm_with_nothing_to_change_is_a_toast(self) -> None:
        self.open_update()
        self.say("سلام")
        result, seen = self.confirm()
        self.assertEqual(result, PF.REVIEW)
        self.assertTrue(seen[-1][2].get("show_alert"))
        self.assertIn("هیچ تفاوتی", str(seen[-1][1]))
        self.assertEqual([call for call in self.shop.requests if call[0] != "GET"], [])
        self.assertIn(USER, PF.sessions, "جریان باز می‌ماند؛ می‌شود چیزی تازه فرستاد")

    def test_a_price_out_of_range_is_a_toast_and_nothing_is_written(self) -> None:
        self.open_update()
        self.say("قیمت 50 تومان")
        result, seen = self.confirm()
        self.assertEqual(result, PF.REVIEW)
        self.assertTrue(any(item[0] == "answer" and item[2].get("show_alert") for item in seen))
        self.assertEqual([call for call in self.shop.requests if call[0] != "GET"], [])

    def test_the_stock_matrix_is_refused_in_an_update_with_a_clear_reason(self) -> None:
        self.open_update()
        self.say("موجودی ماتریسی:\nطرح | iPhone 13 Pro Max\nA | 3")
        result, seen = self.confirm()
        self.assertEqual(result, PF.REVIEW)
        self.assertIn("ماتریس", str(seen[-1][1]))
        self.assertEqual([call for call in self.shop.requests if call[0] != "GET"], [])

    def test_a_double_tap_while_writing_is_blocked(self) -> None:
        self.open_update()
        self.say("قیمت 720000 تومان")
        self.session.submitting = True
        result, seen = self.confirm()
        self.assertEqual(result, PF.REVIEW)
        self.assertTrue(seen[-1][2].get("show_alert"))
        self.assertEqual([call for call in self.shop.requests if call[0] != "GET"], [])

    def test_a_shop_that_changed_since_the_card_is_shown_again_and_nothing_is_written(self) -> None:
        self.open_update()
        self.say("قیمت 720000 تومان")
        self.shop.variations[9001]["regular_price"] = "720000"      # someone beat us to it
        result, _seen = self.confirm()
        self.assertEqual(result, PF.REVIEW)
        self.assertEqual([call for call in self.shop.requests if call[0] != "GET"], [],
                         "تغییری که هیچ‌کس تأیید نکرده نوشته نمی‌شود")
        text = self.card_text()
        self.assertIn("فروشگاه از آخرین بررسی عوض شده", text)
        self.assertIn("598,000 ← 720,000", text)
        self.assertEqual(self.session.target.variations[0].regular_price, 720_000, "پایه تازه شد")
        self.assertFalse(self.session.submitting)
        result, _seen = self.confirm()
        self.assertEqual(result, END, "بار دوم همان کارت تازه نوشته می‌شود")
        self.assertEqual({row["regular_price"] for row in self.shop.variations.values()}, {"720000"})

    def test_a_shop_that_cannot_be_read_at_confirm_is_a_line_on_the_card(self) -> None:
        self.open_update()
        self.say("قیمت 720000 تومان")
        self.shop.variations_status = 500
        result, _seen = self.confirm()
        self.assertEqual(result, PF.REVIEW)
        self.assertEqual([call for call in self.shop.requests if call[0] != "GET"], [])
        self.assertIn("چیزی نوشته نشد" if False else "پیش‌نمایش اپدیت", self.card_text())
        self.assertFalse(self.session.submitting)
        self.assertIn("✅ اعمال تغییرات", self.card_buttons(), "همان کارت، همان دکمه؛ دوباره می‌شود زد")

    def test_a_partial_failure_keeps_the_card_open_with_what_is_left(self) -> None:
        self.open_update()
        self.say("iPhone 13 Pro Max\niPhone 15\nقیمت 720000 تومان\nموجودی 12")
        self.shop.reject_creates = True
        result, _seen = self.confirm()
        self.assertEqual(result, PF.REVIEW)
        text = self.card_text()
        self.assertIn("بخشی اعمال شد", text)
        self.assertIn("«✅ اعمال تغییرات» را دوباره بزن", text)
        self.assertEqual(text.count("دوباره بزن"), 1, "یک جمله، نه تکرارِ چند پیامِ داخلی")
        self.assertNotIn(".؛", text)
        self.assertNotIn("؛.", text)
        self.assertIn(9003, self.shop.variations, "قدیمی‌ها تا ساختنِ تازه‌ها حذف نشدند")
        self.assertFalse(self.session.submitting)
        entry = products_ledger.recent(1)[0]
        self.assertEqual(entry["status"], "failed", "نیمه‌کاره سبز نیست؛ تاریخچه نباید موفق بخواندش")
        self.assertTrue(entry["error"])
        self.assertEqual(entry["changes"], [], "آنچه فقط «قرار بود» عوض شود، «تغییر» ثبت نمی‌شود")
        group = " ".join(str(call[2]) for call in self.bot.of("send") if call[3].get("chat_id") == LOG_CHAT)
        self.assertIn("اپدیت کامل نشد", group, "گروه لاگ باید همین را بخواند")
        # the shop recovers; asking again sends only what is left
        self.shop.reject_creates = False
        self.shop.requests.clear()
        result, _seen = self.confirm()
        self.assertEqual(result, END)
        self.assertEqual(self.grid_models(), {"iPhone 13 Pro Max", "iPhone 15"})
        self.assertEqual(len(self.shop.variations), 5, "۲ ترکیبِ iPhone 13 + ۳ ترکیبِ iPhone 15")
        self.assertEqual(len(self.shop.grid()), 5, "هیچ ترکیبی دوبار نیست")
        self.assertEqual({row["regular_price"] for row in self.shop.variations.values()}, {"720000"})
        self.assertEqual({row["stock_quantity"] for row in self.shop.variations.values()}, {12})

    def test_nothing_applied_is_a_failed_entry_and_the_card_stays(self) -> None:
        self.open_update()
        self.say("قیمت 720000 تومان")
        self.shop.batch_status = 500
        self.shop.product_put_status = None
        result, _seen = self.confirm()
        self.assertEqual(result, PF.REVIEW)
        self.assertEqual(products_ledger.recent(1)[0]["status"], "failed")
        self.assertIn("چیزی اعمال نشد", self.card_text())

    def test_a_gallery_that_already_landed_is_not_uploaded_a_second_time(self) -> None:
        self.open_update()
        self.say("iPhone 13 Pro Max\niPhone 15\nقیمت 720000 تومان")
        self.send_photos(2)
        self.shop.reject_creates = True
        self.confirm()
        self.assertTrue(self.session.gallery_applied)
        uploads_first = len(self.shop.calls("POST", "/media"))
        self.assertEqual(uploads_first, 2)
        self.assertNotIn("تصویر جدید", self.card_text(), "گالری نشسته؛ دیگر تفاوتی نیست")
        self.shop.reject_creates = False
        self.confirm()
        self.assertEqual(len(self.shop.calls("POST", "/media")), uploads_first,
                         "همان دو عکس دوباره آپلود (و یتیم) نمی‌شود")

    def test_a_new_photo_after_a_failed_attempt_is_a_new_gallery(self) -> None:
        self.open_update()
        self.send_photos(1)
        self.session.gallery_applied = True
        self.send_photos(1)
        self.assertFalse(self.session.gallery_applied)

    def test_photos_are_uploaded_and_the_gallery_replaced(self) -> None:
        self.open_update()
        self.send_photos(2)
        result, _seen = self.confirm()
        self.assertEqual(result, END)
        self.assertEqual(len(self.shop.calls("POST", "/media")), 2)
        self.assertEqual(len(self.shop.product["images"]), 2)

    def test_nothing_is_written_to_the_shop_by_search_pick_or_card(self) -> None:
        self.open_update()
        self.say("قیمت 720000 تومان")
        self.assertEqual({call[0] for call in self.shop.requests}, {"GET"})

    def test_confirm_without_a_picked_product_is_a_toast(self) -> None:
        self.session_without_target = PF.ProductSession(mode="update")
        self.session_without_target.data = PF.ProductData(price=720_000)
        PF.sessions[USER] = self.session_without_target
        result, seen = self.confirm()
        self.assertEqual(result, PF.REVIEW)
        self.assertTrue(seen[-1][2].get("show_alert"))


@needs_flow
class TheRehearsal(unittest.TestCase):
    """Dry-run through the real conversation, against the built-in demo store."""

    def setUp(self) -> None:
        self.enterContext(h.temp_ledger())
        self.enterContext(h.patched_settings(h.settings_with(woo_dry_run=True, log_chat_id=LOG_CHAT)))
        self.enterContext(h.no_sleep())
        PF.sessions.clear()
        RF.sessions.clear()
        self.addCleanup(PF.sessions.clear)
        self.addCleanup(RF.sessions.clear)
        saved = PF.feature_allowed
        PF.feature_allowed = lambda user_id, key: True
        self.addCleanup(setattr, PF, "feature_allowed", saved)
        self.bot = Bot()
        self.ctx = SimpleNamespace(bot=self.bot, chat_data={}, job_queue=None)

    def test_the_whole_path_runs_without_touching_a_shop(self) -> None:
        update, seen = h.query_update(CB.PHONE_RESTOCK, user_id=USER, chat_id=CHAT)
        self.assertEqual(asyncio.run(PF.entry(update, self.ctx)), RF.RESTOCK_MATCH)
        self.assertIn("دمو", str(seen[-1][1]), "در حالت آزمایشی راه دیدن مسیر روی صفحه است")
        update, seen = h.message_update("دمو", user_id=USER, chat_id=CHAT)
        update.effective_message.from_user = SimpleNamespace(id=USER)
        asyncio.run(RF.handle_search(update, self.ctx))
        self.assertIn("محصول آزمایشی", str(seen[-1][1]))
        update, seen = h.query_update(f"{CB.RESTOCK_PICK}:850001", user_id=USER, chat_id=CHAT)
        self.assertEqual(asyncio.run(RF.pick(update, self.ctx)), PF.COLLECT)
        update, _seen = h.message_update("قیمت 720000 تومان\nموجودی 5", user_id=USER, chat_id=CHAT)
        update.effective_message.message_id = 40
        asyncio.run(PF.on_text(update, self.ctx))
        card = plain([call for call in self.bot.calls if call[0] in ("send", "edit")][-1][2])
        self.assertIn("🧪 حالت آزمایشی", card)
        self.assertIn("698,000 ← 720,000", card)
        update, _seen = h.query_update("product:confirm", user_id=USER, chat_id=CHAT)
        self.assertEqual(asyncio.run(PF.confirm(update, self.ctx)), END)
        entry = products_ledger.recent(1)[0]
        self.assertEqual(entry["status"], "dry", "در rehearsal نباید کارت بگوید چیزی در سایت نوشته شد")
        self.assertEqual(entry["mode"], "update")
        result_card = plain(self.bot.of("edit")[-1][2])
        self.assertIn("چیزی در سایت عوض نشد", result_card)
        self.assertNotIn("به‌روز شد", result_card)
        self.assertEqual(entry["edit_url"], "", "لینکِ محصولی که نوشته نشده نباید باشد")


@needs_flow
class EveryButtonIsReal(unittest.TestCase):
    def test_the_update_buttons_are_conversation_handlers(self) -> None:
        wired = "|".join(h.conversation_patterns())
        for name in ("RESTOCK_PICK", "RESTOCK_CANCEL", "RESTOCK_RETRY_SEARCH"):
            value = getattr(CB, name).replace(":", r"\:")
            self.assertRegex(wired, value, f"{name} هندلرِ ConversationHandler ندارد")
        self.assertRegex(wired, CB.PHONE_RESTOCK.replace(":", r"\:"),
                         "دکمهٔ منو باید entry point باشد، نه یک هندلرِ بی‌حالت")
        self.assertIn("product:next:(new|update)", wired, "«اپدیت بعدی» روی کارت نتیجه باید entry point باشد")

    def test_the_screens_that_no_longer_exist_have_no_buttons_either(self) -> None:
        for gone in ("RESTOCK_APPLY", "RESTOCK_LINE", "RESTOCK_DIFF", "RESTOCK_REFRESH", "RESTOCK_ZIP",
                     "PHONE_IMAGE_KEEP", "PHONE_IMAGE_REPLACE"):
            self.assertFalse(hasattr(CB, gone), f"CB.{gone} باید همراه صفحه‌اش حذف شده باشد")

    def test_the_flow_guard_knows_how_to_close_an_update(self) -> None:
        self.assertIn("restock", flow_guard.registered())
        RF.sessions[USER] = RF.RestockSession()
        closed = flow_guard.close_others("product", USER)
        self.assertEqual(closed, ["اپدیت محصول موجود"])
        self.assertNotIn(USER, RF.sessions)


if __name__ == "__main__":
    unittest.main()
