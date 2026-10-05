"""Checks model and feature summaries produced by the compression tool."""
from __future__ import annotations

import asyncio
import importlib.util
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("SUDO_IDS", "1234567")

from bot.services import product_text_summary as summary


class TestImageCompressAnalysis(unittest.IsolatedAsyncioTestCase):
    def test_format_uses_pipe_separated_models_and_features(self) -> None:
        text = summary.format_product_summary(
            ["iPhone 13", "iPhone 14", "iPhone 13"],
            {"رنگ": ["مشکی", "سفید"], "طرح": ["گل‌دار"]},
        )
        self.assertIn("مدل‌ها: iPhone 13 | iPhone 14", text)
        self.assertIn("رنگ: مشکی | سفید", text)
        self.assertIn("طرح: گل‌دار", text)

    def test_untrusted_text_is_escaped_for_html(self) -> None:
        text = summary.format_product_summary(["<b>model</b>"], {"<tag>": ["<value>"]})
        self.assertIn("&lt;b&gt;model&lt;/b&gt;", text)
        self.assertIn("&lt;tag&gt;: &lt;value&gt;", text)

    @unittest.skipUnless(
        importlib.util.find_spec("telegram") and importlib.util.find_spec("httpx"),
        "Telegram/httpx runtime dependencies are not installed",
    )
    async def test_log_wrapper_always_uses_shared_sender(self) -> None:
        from bot.modules import image_compress

        bot = SimpleNamespace()
        context = SimpleNamespace(bot=bot)
        sender = AsyncMock(return_value=False)
        with patch.object(image_compress.product_journal, "send_log_message", new=sender):
            await image_compress._log_to_group(context, "trace")

        sender.assert_awaited_once_with(bot, "trace", parse_mode=None)

    @unittest.skipUnless(
        importlib.util.find_spec("telegram") and importlib.util.find_spec("httpx"),
        "Telegram/httpx runtime dependencies are not installed",
    )
    async def test_metadata_adapter_uses_product_creation_parser(self) -> None:
        product = SimpleNamespace(attributes={"رنگ": ["مشکی", "سفید"]})

        async def fake_extract(session, *, learn=True):
            session.models = ["iPhone 13", "iPhone 14"]
            return product

        with patch("bot.modules.product_flow._extract", new=AsyncMock(side_effect=fake_extract)) as extract:
            from bot.modules.product_flow import extract_product_metadata

            models, attributes = await extract_product_metadata(
                "iPhone 13 | iPhone 14", "رنگ: مشکی، سفید"
            )

        self.assertEqual(["iPhone 13", "iPhone 14"], models)
        self.assertEqual({"رنگ": ["مشکی", "سفید"]}, attributes)
        feature_line = summary.format_product_summary(models, attributes).splitlines()[-1]
        self.assertEqual("ویژگی‌ها: رنگ: مشکی | سفید", feature_line)
        session = extract.await_args.args[0]
        self.assertEqual("iPhone 13 | iPhone 14", session.model_text)
        self.assertEqual("رنگ: مشکی، سفید", session.info_text)
        self.assertFalse(extract.await_args.kwargs["learn"])

    @unittest.skipUnless(
        importlib.util.find_spec("telegram") and importlib.util.find_spec("httpx"),
        "Telegram/httpx runtime dependencies are not installed",
    )
    async def test_download_retries_transient_network_error_without_partial_file(self) -> None:
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from telegram.error import TimedOut

        from bot.modules import image_compress

        calls = 0

        async def download_to_drive(*, custom_path):
            nonlocal calls
            calls += 1
            if calls == 1:
                Path(custom_path).write_bytes(b"partial")
                raise TimedOut("read timed out")
            Path(custom_path).write_bytes(b"complete-image")

        tg_file = SimpleNamespace(file_size=14, download_to_drive=download_to_drive)
        context = SimpleNamespace(bot=SimpleNamespace(get_file=AsyncMock(return_value=tg_file)))
        with TemporaryDirectory() as directory, patch.object(image_compress.asyncio, "sleep", new=AsyncMock()):
            target = Path(directory) / "photo.jpg"
            size = await image_compress._download(context, "file-id", target)
            self.assertEqual(14, size)
            self.assertEqual(2, calls)
            self.assertEqual(b"complete-image", target.read_bytes())
        self.assertEqual(2, context.bot.get_file.await_count)

    @unittest.skipUnless(
        importlib.util.find_spec("telegram") and importlib.util.find_spec("httpx"),
        "Telegram/httpx runtime dependencies are not installed",
    )
    async def test_application_uses_longer_telegram_timeouts_and_separate_polling_pool(self) -> None:
        from bot.app import build_application

        app = build_application()
        try:
            polling_request, api_request = app.bot._request
            api_timeout = api_request._client_kwargs["timeout"]
            poll_timeout = polling_request._client_kwargs["timeout"]
            self.assertEqual(45.0, api_timeout.read)
            self.assertEqual(90.0, api_request._media_write_timeout)
            self.assertEqual(40.0, poll_timeout.read)
            self.assertEqual(2, polling_request._client_kwargs["limits"].max_connections)
        finally:
            for request in app.bot._request:
                await request.shutdown()

    @unittest.skipUnless(
        importlib.util.find_spec("telegram") and importlib.util.find_spec("httpx"),
        "Telegram/httpx runtime dependencies are not installed",
    )
    async def test_polling_read_errors_are_not_reported_as_unhandled_bot_failures(self) -> None:
        from telegram.error import NetworkError

        from bot.modules import fallback

        sender = AsyncMock(return_value=True)
        context = SimpleNamespace(error=NetworkError("httpx.ReadError"), job=None, bot=SimpleNamespace())
        with patch.object(fallback.product_journal, "send_log_message", new=sender):
            await fallback.on_error(None, context)
        sender.assert_not_awaited()

    @unittest.skipUnless(
        importlib.util.find_spec("telegram") and importlib.util.find_spec("httpx"),
        "Telegram/httpx runtime dependencies are not installed",
    )
    async def test_album_is_downloaded_and_compressed_concurrently_with_one_status_and_analysis(self) -> None:
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from bot.modules import image_compress

        compressed_ids: list[str] = []
        status_edits = AsyncMock()

        def photo(message_id: int, caption: str):
            return SimpleNamespace(
                message_id=message_id,
                media_group_id="album-1",
                caption=caption,
                photo=[SimpleNamespace(file_id=f"photo-{message_id}")],
                document=None,
                reply_text=AsyncMock(return_value=SimpleNamespace(edit_text=status_edits)),
            )

        messages = [photo(44, "iPhone 15"), photo(45, "iPhone 16")]
        bot = SimpleNamespace(send_document=AsyncMock())
        context = SimpleNamespace(bot=bot, user_data={})
        def update(message):
            return SimpleNamespace(
                effective_message=message, effective_user=SimpleNamespace(id=7)
            )

        async def downloaded(_context, file_id, target):
            target.write_bytes(file_id.encode())
            return len(file_id)

        def compressed(source, directory):
            directory.mkdir(parents=True, exist_ok=True)
            output = directory / f"{source.stem}_compressed.jpg"
            output.write_bytes(b"compressed")
            compressed_ids.append(source.read_text())
            return output

        from _flow_harness import temp_ledger

        async def run_inline(function, *args, **kwargs):
            """همان فراخوانی، ولی در همین پروسه: تابعِ patchشدهٔ محلی به ورکر pickle نمی‌شود."""
            kwargs.pop("timeout", None)
            return function(*args, **kwargs)

        with (
            temp_ledger(),        # کاربر ۷ ادمین است؛ دروازهٔ دسترسی واقعاً اجرا می‌شود
            TemporaryDirectory() as directory,
            patch.object(image_compress, "TEMP_DIR", Path(directory)),
            patch.object(image_compress.worker, "run", new=run_inline),
            patch.object(image_compress, "_download", new=downloaded),
            patch.object(image_compress, "compress_image", new=compressed),
            patch.object(image_compress, "_log_to_group", new=AsyncMock()) as log,
            patch.object(image_compress, "_try_send_analysis", new=AsyncMock()) as analysis,
            patch.object(image_compress.metrics, "observe"),
            patch.object(image_compress.metrics, "incr"),
            patch.object(image_compress.asyncio, "sleep", new=AsyncMock()),
        ):
            for message in messages:
                await image_compress.on_media(update(message), context)
            tasks = list(image_compress.album_tasks.values())
            await asyncio.gather(*tasks)

        self.assertCountEqual(["photo-44", "photo-45"], compressed_ids)
        self.assertEqual(2, bot.send_document.await_count)
        self.assertEqual(1, messages[0].reply_text.await_count)
        messages[1].reply_text.assert_not_awaited()
        analysis.assert_awaited_once()
        self.assertTrue(any("دستهٔ 2 عکس" in call.args[1] for call in log.await_args_list))

    @unittest.skipUnless(
        importlib.util.find_spec("telegram") and importlib.util.find_spec("httpx"),
        "Telegram/httpx runtime dependencies are not installed",
    )
    async def test_a_progress_message_timeout_does_not_abort_compression(self) -> None:
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from telegram.error import TimedOut

        from bot.modules import image_compress

        message = SimpleNamespace(
            message_id=44,
            caption=None,
            photo=[SimpleNamespace(file_id="photo-id")],
            document=None,
            reply_text=AsyncMock(side_effect=TimedOut("temporary Telegram timeout")),
        )
        bot = SimpleNamespace(send_document=AsyncMock())
        context = SimpleNamespace(bot=bot, user_data={})
        update = SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=7))

        async def downloaded(_context, _file_id, target):
            target.write_bytes(b"original")
            return 8

        def compressed(_source, directory):
            directory.mkdir(parents=True, exist_ok=True)
            output = directory / "ready.jpg"
            output.write_bytes(b"compressed")
            return output

        from _flow_harness import temp_ledger

        async def run_inline(function, *args, **kwargs):
            """همان فراخوانی، ولی در همین پروسه: تابعِ patchشدهٔ محلی به ورکر pickle نمی‌شود."""
            kwargs.pop("timeout", None)
            return function(*args, **kwargs)

        with (
            temp_ledger(),        # کاربر ۷ ادمین است؛ دروازهٔ دسترسی واقعاً اجرا می‌شود
            TemporaryDirectory() as directory,
            patch.object(image_compress, "TEMP_DIR", Path(directory)),
            patch.object(image_compress.worker, "run", new=run_inline),
            patch.object(image_compress, "_download", new=downloaded),
            patch.object(image_compress, "compress_image", new=compressed),
            patch.object(image_compress, "_log_to_group", new=AsyncMock()),
            patch.object(image_compress, "_try_send_analysis", new=AsyncMock()),
        ):
            result = await image_compress.on_media(update, context)

        self.assertEqual(image_compress.WAITING, result)
        bot.send_document.assert_awaited_once()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
