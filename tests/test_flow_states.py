"""Product intake's one-card UX, state transitions, and chat routing.

Free text is appended in both COLLECT and REVIEW; previews edit the same bot
message and progress uses only Telegram's transient chat action. The routing
guard still ensures one flow per user and keeps all work in its starting chat
and forum topic.

Run with ``python3 -m unittest discover -s tests``; the whole module skips itself
when python-telegram-bot is not installed.
"""

from __future__ import annotations

import asyncio
import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("SUDO_IDS", "1234567")

try:
    from bot.modules import image_compress as IC
    from bot.modules import product_flow as PF
    from bot.services import flow_guard
    from bot.services.product_extractor import ProductData

    HAS_FLOW = True
except Exception:                       # pragma: no cover - PTB missing
    PF = IC = flow_guard = None
    HAS_FLOW = False

needs_flow = unittest.skipUnless(HAS_FLOW, "python-telegram-bot is not installed")


def _message(text="", **extra):
    sent = []

    async def reply_text(body, **kwargs):
        sent.append(("text", body, kwargs))
        return SimpleNamespace(message_id=1)

    async def reply_html(body, **kwargs):
        sent.append(("html", body, kwargs))
        return SimpleNamespace(message_id=2)

    message = SimpleNamespace(text=text, chat_id=extra.get("chat_id", 7),
                              message_thread_id=extra.get("thread_id"),
                              reply_text=reply_text, reply_html=reply_html,
                              media_group_id=extra.get("group"), message_id=10)
    return message, sent


def _update(text="", **extra):
    message, sent = _message(text, **extra)
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=7, username="t", first_name="t"),
        effective_message=message,
    ), sent


def _context():
    """Small Telegram bot stub that records card edits, sends and chat actions."""
    calls = {"edits": [], "messages": [], "actions": []}

    async def edit_message_text(text=None, **kwargs):
        calls["edits"].append({"text": text, **kwargs})
        return SimpleNamespace(message_id=kwargs.get("message_id", 99))

    async def send_message(text=None, **kwargs):
        calls["messages"].append({"text": text, **kwargs})
        return SimpleNamespace(message_id=100 + len(calls["messages"]))

    async def send_chat_action(**kwargs):
        calls["actions"].append(kwargs)

    bot = SimpleNamespace(
        edit_message_text=edit_message_text,
        send_message=send_message,
        send_chat_action=send_chat_action,
    )
    return SimpleNamespace(bot=bot, chat_data={}), calls


def _query(data, *, text=None, chat_id=7, thread_id=None):
    sent = []

    async def answer(*a, **k):
        sent.append(("answer", a, k))
        return

    async def reply_text(body, **kwargs):
        sent.append(("text", body, kwargs))
        return SimpleNamespace(message_id=3)

    async def reply_html(body, **kwargs):
        sent.append(("html", body, kwargs))
        return SimpleNamespace(message_id=4)

    async def edit_message_text(body=None, **kwargs):
        sent.append(("edit", body, kwargs))
        return SimpleNamespace(message_id=5)

    message = SimpleNamespace(chat_id=chat_id, message_thread_id=thread_id, message_id=1,
                              reply_text=reply_text, reply_html=reply_html,
                              edit_message_text=edit_message_text, text=text)
    query = SimpleNamespace(data=data, from_user=SimpleNamespace(id=7), message=message,
                            answer=answer, edit_message_text=edit_message_text)
    return SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=7),
                           effective_message=message), sent


class FlowStateTestCase(unittest.TestCase):
    def setUp(self):
        PF.sessions.clear()
        PF.album_buffers.clear()
        PF.album_tasks.clear()
        self._allowed = PF.feature_allowed
        PF.feature_allowed = lambda user_id, key: True
        self.addCleanup(setattr, PF, "feature_allowed", self._allowed)
        self.addCleanup(self._clear)

    def _clear(self):
        PF.sessions.clear()
        PF.album_buffers.clear()
        PF.album_tasks.clear()

    def fake_extract(self, result=None):
        """Replace the AI/parsing step: the tests are about routing, not parsing."""
        calls = []

        async def _extract(session):
            calls.append(session.info_text)
            session.data = result or ProductData(title="قاب", price=698000)
            return session.data

        original = PF._extract
        PF._extract = _extract
        self.addCleanup(setattr, PF, "_extract", original)
        return calls


@needs_flow
class TestCollectVersusReview(FlowStateTestCase):
    def test_text_while_collecting_is_the_product_information(self):
        session = PF.ProductSession(mode="new", status_message_id=77)
        session.files = [Path("/tmp/1.jpg")]
        PF.sessions[7] = session
        self.fake_extract()
        update, user_replies = _update("قیمت 698000")
        context, calls = _context()
        result = asyncio.run(PF.on_text(update, context))
        self.assertEqual(result, PF.REVIEW, "a preview was rendered ⇒ we are reviewing now")
        self.assertEqual(session.info_text, "قیمت 698000")
        self.assertEqual([], user_replies, "text intake must not create a bot reply")
        self.assertEqual([], calls["messages"], "the anchored card must be edited, not replaced")
        self.assertEqual(77, calls["edits"][0]["message_id"])

    def test_text_before_photos_still_updates_the_same_preview_card(self):
        session = PF.ProductSession(mode="new", status_message_id=77)
        PF.sessions[7] = session
        self.fake_extract()
        update, user_replies = _update("قیمت 698000")
        context, calls = _context()
        result = asyncio.run(PF.on_text(update, context))
        self.assertEqual(result, PF.REVIEW)
        self.assertEqual(session.info_text, "قیمت 698000")
        self.assertIsNotNone(session.data)
        self.assertIn("عکسی برای این محصول", calls["edits"][0]["text"])
        self.assertEqual([], user_replies)
        self.assertEqual([], calls["messages"])

    def test_text_on_the_review_screen_is_appended_without_confirmation(self):
        session = PF.ProductSession(
            mode="new", info_text="قیمت 698000", status_message_id=77
        )
        session.data = ProductData(title="قاب", price=698000)
        PF.sessions[7] = session
        calls_to_extract = self.fake_extract()
        update, user_replies = _update("رنگ: مشکی | سفید")
        context, calls = _context()
        result = asyncio.run(PF.on_review_text(update, context))
        self.assertEqual(result, PF.REVIEW)
        self.assertEqual(session.info_text, "قیمت 698000\nرنگ: مشکی | سفید")
        self.assertFalse(hasattr(session, "pending_text"))
        self.assertEqual(calls_to_extract, [session.info_text])
        self.assertEqual([], user_replies)
        self.assertEqual([], calls["messages"])
        self.assertEqual([77], [edit["message_id"] for edit in calls["edits"]])
        self.assertIn("پیش‌نمایش", calls["edits"][0]["text"])

    def test_every_followup_text_edits_one_anchored_message(self):
        session = PF.ProductSession(mode="new", status_message_id=77)
        session.data = ProductData(title="قاب", price=698000)
        PF.sessions[7] = session
        parsed = self.fake_extract()
        context, calls = _context()
        first, first_replies = _update("قیمت 698000")
        second, second_replies = _update("رنگ: سفید")
        self.assertEqual(PF.REVIEW, asyncio.run(PF.on_text(first, context)))
        self.assertEqual(PF.REVIEW, asyncio.run(PF.on_review_text(second, context)))
        self.assertEqual("قیمت 698000\nرنگ: سفید", session.info_text)
        self.assertEqual(2, len(parsed))
        self.assertEqual([], first_replies + second_replies)
        self.assertEqual([], calls["messages"])
        self.assertEqual([77, 77], [edit["message_id"] for edit in calls["edits"]])
        self.assertEqual(2, len(calls["actions"]), "typing status is transient, not a message")

    def test_stale_proposal_button_is_a_noop(self):
        session = PF.ProductSession(mode="new", info_text="قیمت 698000")
        session.data = ProductData()
        PF.sessions[7] = session
        update, sent = _query("product:prop:yes")
        result = asyncio.run(PF.accept_proposal(update, SimpleNamespace()))
        self.assertEqual(PF.REVIEW, result)
        self.assertEqual("قیمت 698000", session.info_text)
        self.assertEqual("answer", sent[0][0])

    def test_review_text_without_a_draft_falls_back_to_information(self):
        session = PF.ProductSession(mode="new", status_message_id=77)
        session.files = [Path("/tmp/1.jpg")]
        PF.sessions[7] = session
        self.fake_extract()
        update, _user_replies = _update("قیمت 500000")
        context, calls = _context()
        result = asyncio.run(PF.on_review_text(update, context))
        self.assertEqual(result, PF.REVIEW)
        self.assertEqual(session.info_text, "قیمت 500000")
        self.assertEqual(77, calls["edits"][0]["message_id"])

    def test_ai_added_model_is_marked_as_a_guess_for_review(self):
        from _flow_harness import patched_settings, settings_with
        from bot.services.product_extractor import ProductData

        session = PF.ProductSession(mode="new", model_text="iPhone 15")
        PF.sessions[7] = session

        async def fake_normalize(_raw, _deterministic, job_log=None, *, client=None):
            return "iPhone 16"

        async def fake_details(_text, models, _taxonomy, **_kwargs):
            return ProductData(title="قاب", models=list(models))

        with (
            patched_settings(settings_with(
                ai_base_url="https://ai.example/v1", ai_token="test", ai_model="test-model"
            )),
            patch.object(PF, "ai_normalize", new=fake_normalize),
            patch.object(PF, "extract_product", new=fake_details),
        ):
            asyncio.run(PF._extract(session, learn=False))

        self.assertEqual(["iPhone 16"], session.data.models)
        self.assertEqual("ai", session.data.evidence["models"].source)
        self.assertIn("مدل‌ها", " ".join(PF._open_questions(session)))

    def test_add_more_uses_a_toast_and_leaves_the_preview_in_place(self):
        session = PF.ProductSession(mode="new")
        session.data = ProductData(title="قاب")
        PF.sessions[7] = session
        update, sent = _query("product:addmore")
        result = asyncio.run(PF.add_more(update, SimpleNamespace()))
        self.assertEqual(result, PF.REVIEW)
        self.assertEqual(["answer"], [item[0] for item in sent])
        self.assertIn("پیش‌نمایش خودکار", sent[0][1][0])


@needs_flow
class TestFinishMedia(FlowStateTestCase):
    def test_pending_album_is_flushed_immediately(self):
        session = PF.ProductSession(mode="new", status_message_id=88)
        session.info_text = "قیمت 698000"
        PF.sessions[7] = session
        PF.album_buffers[(7, "grp")] = [SimpleNamespace(message_id=1)]
        # an already-finished collector task (a stub, so no event loop is needed)
        PF.album_tasks[(7, "grp")] = SimpleNamespace(done=lambda: True, cancel=lambda: None)
        prepared = []

        async def fake_prepare(user_id, messages, context):
            prepared.append((user_id, len(messages)))
            session.files = [Path("/tmp/1.jpg")]

        original = PF._prepare_files
        PF._prepare_files = fake_prepare
        self.addCleanup(setattr, PF, "_prepare_files", original)
        self.fake_extract()

        update, _sent = _query("product:mediaend")
        context, _calls = _context()
        result = asyncio.run(PF.finish_media(update, context))
        self.assertEqual(prepared, [(7, 1)])
        self.assertEqual(PF.album_buffers, {}, "the buffer must be empty afterwards")
        self.assertEqual(result, PF.REVIEW)

    def test_no_images_yet_keeps_the_user_collecting(self):
        session = PF.ProductSession(mode="new")
        PF.sessions[7] = session
        update, sent = _query("product:mediaend")
        result = asyncio.run(PF.finish_media(update, SimpleNamespace()))
        self.assertEqual(result, PF.COLLECT)
        self.assertEqual(sent[0][0], "answer", "a toast, not a new message")

    def test_still_processing_is_said_not_faked(self):
        session = PF.ProductSession(mode="new")
        session.files = [Path("/tmp/1.jpg")]
        session.processing_media = True
        PF.sessions[7] = session
        update, sent = _query("product:mediaend")
        result = asyncio.run(PF.finish_media(update, SimpleNamespace()))
        self.assertEqual(result, PF.COLLECT)
        self.assertIn("آماده", sent[0][1][0])


@needs_flow
class TestModelCaptionsFromBatches(FlowStateTestCase):
    def test_models_from_later_photo_batches_do_not_replace_earlier_captions(self):
        import tempfile
        from unittest.mock import AsyncMock, patch

        from bot.services.phone_parser import normalize_caption
        from bot.services.product_extractor import ProductData

        first_caption = """Samsung:
A06
A07"""
        second_caption = """XIAOMI / POCO:
NOTE11/11S/12S"""
        root = Path(tempfile.mkdtemp()) / "session"
        root.mkdir(parents=True)
        self.addCleanup(__import__("shutil").rmtree, root.parent, True)
        session = PF.ProductSession(mode="new", workspace=root, chat_id=9)
        PF.sessions[7] = session

        async def download(_context, _file_id, target):
            target.write_bytes(b"original-image")
            return len(b"original-image")

        def compress(source, directory):
            directory.mkdir(parents=True, exist_ok=True)
            output = directory / f"{source.stem}_compressed.jpg"
            output.write_bytes(b"compressed")
            return output

        parse_calls = 0

        async def parse(session):
            nonlocal parse_calls
            parse_calls += 1
            session.models = normalize_caption(session.model_text).split(" | ")
            session.data = ProductData(title="قاب", models=session.models)
            session.color_summary = ""
            return session.data

        class Bot:
            async def send_message(self, *args, **kwargs):
                return SimpleNamespace(message_id=1)

            async def edit_message_text(self, *args, **kwargs):
                return SimpleNamespace(message_id=kwargs.get("message_id", 1))

            async def send_chat_action(self, **kwargs):
                return None

        def photo(message_id, caption):
            return SimpleNamespace(
                message_id=message_id,
                caption=caption,
                text=None,
                photo=[SimpleNamespace(file_id=f"file-{message_id}")],
                document=None,
            )

        context = SimpleNamespace(bot=Bot())

        async def run_batches():
            with (
                patch.object(PF, "_download_with_retry", new=download),
                patch.object(PF, "compress_image", new=compress),
                patch.object(PF, "_telegram_log", new=AsyncMock()),
                patch.object(PF, "_status", new=AsyncMock()),
                patch.object(PF, "_extract", new=parse),
                patch.object(PF.flow_state, "record"),
            ):
                await PF._prepare_files(7, [photo(1, first_caption)], context)
                await PF._prepare_files(7, [photo(2, second_caption)], context)
                await PF._prepare_files(7, [photo(3, second_caption)], context)

        asyncio.run(run_batches())
        self.assertEqual(2, parse_calls, "an exact repeated caption must not trigger another AI parse")
        self.assertEqual(3, len(session.files), "all media should still be kept")
        self.assertEqual(first_caption + "\n\n" + second_caption, session.model_text)
        self.assertTrue(
            {"A06", "A07", "Redmi Note 11", "Redmi Note 11S", "Redmi Note 12S"}.issubset(
                set(session.models)
            ),
            session.models,
        )


@needs_flow
class TestExtractionFastPaths(FlowStateTestCase):
    def test_empty_media_intake_does_not_call_ai(self):
        from unittest.mock import AsyncMock, patch

        async def run():
            session = PF.ProductSession(mode="new")
            with (
                patch.object(PF, "ai_normalize", new=AsyncMock()) as normalize,
                patch.object(PF, "extract_product", new=AsyncMock()) as extract,
            ):
                await PF._extract(session, learn=False)
            normalize.assert_not_awaited()
            extract.assert_not_awaited()
            self.assertEqual([], session.models)
            self.assertIsNotNone(session.data)

        asyncio.run(run())

    def test_model_caption_intake_defers_the_product_details_ai_call(self):
        from unittest.mock import AsyncMock, patch

        from bot.services.product_extractor import ProductData

        async def run():
            session = PF.ProductSession(mode="new", model_text="Samsung A06", defer_details=True)
            with (
                patch.object(PF, "ai_normalize", new=AsyncMock(return_value="A06")) as normalize,
                patch.object(PF, "extract_product", new=AsyncMock(return_value=ProductData())) as extract,
            ):
                await PF._extract(session, learn=False)
            normalize.assert_awaited_once()
            extract.assert_not_awaited()
            self.assertEqual(["A06"], session.models)
            self.assertEqual(["A06"], session.data.models)

        asyncio.run(run())

    def test_model_and_detail_ai_calls_share_one_http_client(self):
        from unittest.mock import AsyncMock, patch

        from _flow_harness import patched_settings, settings_with
        from bot.services.product_extractor import ProductData

        class Client:
            closed = False

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                self.closed = True

        async def run():
            client = Client()
            session = PF.ProductSession(
                mode="new", model_text="iPhone 15", info_text="قیمت 698000"
            )
            with (
                patched_settings(settings_with(
                    ai_base_url="https://ai.example/v1", ai_token="test-token", ai_model="test-model"
                )),
                patch("httpx.AsyncClient", return_value=client) as make_client,
                patch.object(PF, "ai_normalize", new=AsyncMock(return_value="iPhone 15")) as normalize,
                patch.object(PF, "extract_product", new=AsyncMock(return_value=ProductData())) as extract,
            ):
                await PF._extract(session, learn=False)
            make_client.assert_called_once()
            normalize.assert_awaited_once()
            extract.assert_awaited_once()
            self.assertIs(normalize.await_args.kwargs["client"], client)
            self.assertIs(extract.await_args.kwargs["client"], client)
            self.assertTrue(client.closed)

        asyncio.run(run())


@needs_flow
class TestMediaDownloadSafety(FlowStateTestCase):
    def test_partial_telegram_download_is_removed_before_retry(self):
        from tempfile import TemporaryDirectory
        from unittest.mock import AsyncMock, patch

        from telegram.error import TimedOut

        calls = 0

        async def download_to_drive(*, custom_path):
            nonlocal calls
            calls += 1
            if calls == 1:
                Path(custom_path).write_bytes(b"partial")
                raise TimedOut("read timed out")
            Path(custom_path).write_bytes(b"complete")

        telegram_file = SimpleNamespace(file_size=8, download_to_drive=download_to_drive)
        context = SimpleNamespace(bot=SimpleNamespace(get_file=AsyncMock(return_value=telegram_file)))
        with TemporaryDirectory() as directory, patch.object(PF.asyncio, "sleep", new=AsyncMock()):
            target = Path(directory) / "image.jpg"
            size = asyncio.run(PF._download_with_retry(context, "opaque-file-id", target))
            self.assertEqual(8, size)
            self.assertEqual(b"complete", target.read_bytes())
        self.assertEqual(2, calls)
        self.assertEqual(2, context.bot.get_file.await_count)


@needs_flow
class TestNoPhantomSession(FlowStateTestCase):
    def test_media_without_a_session_ends_the_flow_instead_of_inventing_one(self):
        update, sent = _update("", chat_id=7)
        update.effective_message.media_group_id = None
        result = asyncio.run(PF.on_media(update, SimpleNamespace()))
        self.assertEqual(result, -1, "ConversationHandler.END — the flow is over")
        self.assertNotIn(7, PF.sessions, "a stale photo must not start a product")
        self.assertIn("بسته شده", sent[0][1])

    def test_text_without_a_session_ends_the_flow_too(self):
        update, sent = _update("قیمت 698000")
        result = asyncio.run(PF.on_text(update, SimpleNamespace()))
        self.assertEqual(result, -1)
        self.assertNotIn(7, PF.sessions)
        self.assertIn("بسته شده", sent[0][1])

    def test_review_text_without_a_session_ends_the_flow(self):
        update, sent = _update("سلام")
        result = asyncio.run(PF.on_review_text(update, SimpleNamespace()))
        self.assertEqual(result, -1)
        self.assertNotIn(7, PF.sessions)


@needs_flow
class TestFlowGuard(FlowStateTestCase):
    def test_every_flow_is_registered(self):
        # «شارژ محصول موجود» shares the builder's conversation but is its own flow to close:
        # an approved diff must not outlive the start of a new product.
        self.assertEqual(flow_guard.registered(), ["compress", "product", "restock"])

    def test_product_closer_reports_and_cleans(self):
        session = PF.ProductSession(mode="new")
        PF.sessions[7] = session
        self.assertTrue(PF.close_for(7))
        self.assertNotIn(7, PF.sessions)
        self.assertFalse(PF.close_for(7), "closing an unopened flow is a no-op")

    def test_compress_closer_removes_leftover_workspaces(self):
        root = IC.TEMP_DIR / f"7_{int(time.time())}"
        root.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        (root / "image.jpg").write_bytes(b"x" * 10)
        self.assertTrue(IC.close_for(7))
        self.assertFalse(root.exists())
        self.assertFalse(IC.close_for(7))

    def test_starting_a_product_closes_the_other_flow_and_says_so(self):
        closed = []
        original = flow_guard.close_others

        def fake(name, user_id):
            closed.append((name, user_id))
            return ["فشرده‌سازی عکس‌ها"]

        flow_guard.close_others = fake
        self.addCleanup(setattr, flow_guard, "close_others", original)
        update, sent = _query("phone:new")
        result = asyncio.run(PF.entry(update, SimpleNamespace()))
        self.assertEqual(closed, [("product", 7)])
        self.assertEqual(result, PF.COLLECT)
        self.assertIn("فشرده‌سازی عکس‌ها", sent[-1][1])


@needs_flow
class TestChatRouting(FlowStateTestCase):
    def test_target_prefers_the_started_chat_and_thread(self):
        self.assertEqual(PF._target(None, 5), {"chat_id": 5})
        session = PF.ProductSession(chat_id=77, thread_id=9)
        self.assertEqual(PF._target(session, 5), {"chat_id": 77, "message_thread_id": 9})

    def test_entry_records_chat_and_thread(self):
        update, _sent = _query("phone:new", chat_id=77, thread_id=42)
        asyncio.run(PF.entry(update, SimpleNamespace()))
        session = PF.sessions[7]
        self.assertEqual((session.chat_id, session.thread_id), (77, 42))

    def test_progress_uses_a_transient_action_not_a_status_message(self):
        session = PF.ProductSession(chat_id=77, thread_id=42)
        context, calls = _context()

        async def run():
            await PF._status(context, 7, session, "📥 در حال دریافت…")
            async with PF._typing_indicator(context, 7, session):
                await asyncio.sleep(0)

        asyncio.run(run())
        self.assertEqual(1, len(calls["actions"]))
        self.assertEqual(calls["actions"][0]["chat_id"], 77)
        self.assertEqual(calls["actions"][0]["message_thread_id"], 42)
        self.assertEqual([], calls["messages"] + calls["edits"])
        self.assertIsNone(session.status_message_id, "progress must not create a new message")

    def test_zip_result_document_and_card_follow_the_flow(self):
        """مسیر ZIP: فایل و کارت هم باید به همان چت/تاپیک بروند.

        این تست عمداً مسیر «.zip» را end-to-end می‌راند: کارت نتیجه و send_document
        مدت‌ها `user.id` را hard-code داشتند و هیچ تستی آن مسیر را اجرا نمی‌کرد —
        یعنی دقیقاً همان‌جا که فاز ۳c قول همسویی داده بود، پوششی وجود نداشت.
        """
        import shutil
        import tempfile

        from bot.services import products_ledger
        from bot.services.product_extractor import ProductData

        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        image = tmp / "01.jpg"
        image.write_bytes(b"z" * 64)

        self._temp_dir = PF.TEMP_DIR
        PF.TEMP_DIR = tmp
        self.addCleanup(setattr, PF, "TEMP_DIR", self._temp_dir)
        self._ledger_file = products_ledger.FILE
        products_ledger.FILE = tmp / "recent_products.json"
        self.addCleanup(setattr, products_ledger, "FILE", self._ledger_file)
        self._sudo = PF.rbac.is_sudo
        PF.rbac.is_sudo = lambda user_id: True
        self.addCleanup(setattr, PF.rbac, "is_sudo", self._sudo)

        session = PF.ProductSession(
            mode="update", files=[image], chat_id=77, thread_id=5, workspace=tmp,
            data=ProductData(title="قاب سیلیکونی", price=698_000, sku_prefix="BO",
                             models=["iPhone 17 Pro Max", "iPhone 17 Pro"],
                             attributes={"رنگ": ["سفید", "مشکی"]}),
        )
        PF.sessions[7] = session

        sent: list[dict] = []

        async def send_message(**kwargs):
            sent.append(kwargs)
            return SimpleNamespace(message_id=1)

        doc_names: list[str] = []

        async def send_document(**kwargs):
            sent.append(kwargs)
            doc_names.append(getattr(kwargs.get("document"), "name", ""))
            return SimpleNamespace(message_id=2)

        context = SimpleNamespace(
            bot=SimpleNamespace(send_message=send_message, send_document=send_document,
                                edit_message_text=lambda **k: None),
            chat_data={}, job_queue=SimpleNamespace(run_once=lambda *a, **k: None),
        )
        update, _seen = _query("product:confirm", chat_id=77, thread_id=5)
        result = asyncio.run(PF.confirm(update, context))

        self.assertEqual(result, -1, "ZIP هم جریان را تمام می‌کند")
        self.assertTrue(sent, "پیامی باید رفته باشد")
        for item in sent:
            self.assertEqual(item.get("chat_id"), 77, f"به چت خصوصی رفت: {item}")
            self.assertEqual(item.get("message_thread_id"), 5, "تاپیک گم شد")
        self.assertTrue(any("filename" in item for item in sent), "فایل ZIP باید ارسال شده باشد")
        self.assertTrue(any(Path(n).name.startswith("product_7_") for n in doc_names),
                        f"فایل باید در TEMP_DIR جریان ساخته شده باشد، نه: {doc_names}")

    def test_a_message_in_a_thread_adopts_that_thread(self):
        session = PF.ProductSession(mode="new")
        session.files = [Path("/tmp/1.jpg")]
        PF.sessions[7] = session
        self.fake_extract()
        update, _sent = _update("قیمت 698000", chat_id=555, thread_id=7)
        context, _calls = _context()
        asyncio.run(PF.on_text(update, context))
        self.assertEqual((session.chat_id, session.thread_id), (555, 7))


if __name__ == "__main__":
    unittest.main()
