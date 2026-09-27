"""Checks model and feature summaries produced by the compression tool."""
from __future__ import annotations

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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
