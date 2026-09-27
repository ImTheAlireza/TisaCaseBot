"""فاز ۹: پوششِ کاملِ `ai_normalizer` — لایه‌ای که تا حالا ۲۸٪ تست داشت.

AI تنها جایی است که *می‌تواند* مدل بسازد؛ پس قاعدهٔ اصلی اینجاست: هر وقت AI جواب
 نداد، پاسخِ ناقص داد، یا چیزِ بی‌ربط گفت، ربات باید همان مسیر deterministic را ادامه
دهد و **بگوید** چه شد (لاگ + شمارنده). این فایل هم تابع‌های خالصِ پارس/پاک‌سازی را
می‌سنجد و هم همان سه مسیر شبکه را — بدون شبکه، با `httpx.AsyncClient.post`ِ جعلی.
"""

from __future__ import annotations

import asyncio
import logging
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("SUDO_IDS", "1234567")

import _flow_harness as h

try:
    import httpx

    from bot.services import ai_normalizer

    HAS_AI = True
except Exception:  # pragma: no cover - httpx نصب نیست
    ai_normalizer = None  # type: ignore[assignment]
    HAS_AI = False


def _response(payload: object, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        json=payload,
        request=httpx.Request("POST", "https://ai.example/v1/chat/completions"),
    )


def _completion(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


def _count(snapshot: dict, key: str) -> int:
    """چند بار شده؟ (از snapshotِ گرفته‌شده — همان چیزی که «📊 وضعیت» می‌خواند)."""
    return int(snapshot.get(key, (0, 0.0, 0.0, 0.0))[0])


class _FailingPost:
    """جای‌گزینِ `httpx.AsyncClient.post`: یا یک پاسخ می‌دهد یا خطا.

    یک *نمونه* (نه تابع) به‌عنوان attribute کلاس ست می‌شود، پس descriptor نیست و
    `self` را تحویل نمی‌گیرد — دقیقاً همان `post(url, **kwargs)` را می‌بیند.
    """

    def __init__(self, result: object, status: int = 200) -> None:
        self.result = result
        self.status = status
        self.calls: list[dict] = []

    async def __call__(self, url: str, **kwargs: object) -> httpx.Response:
        self.calls.append({"url": url, **kwargs})
        if isinstance(self.result, Exception):
            raise self.result
        return _response(self.result, self.status)


@unittest.skipUnless(HAS_AI, "httpx نصب نیست")
class TestPureHelpers(unittest.TestCase):
    def test_endpoint_accepts_a_bare_base_or_a_full_url(self) -> None:
        for base, expected in (
            ("https://ai.example/v1", "https://ai.example/v1/chat/completions"),
            ("https://ai.example/v1/", "https://ai.example/v1/chat/completions"),
            ("https://ai.example/v1/chat/completions", "https://ai.example/v1/chat/completions"),
            ("", ""),
        ):
            with self.subTest(base=base), patch.object(ai_normalizer, "AI_BASE_URL", base):
                self.assertEqual(expected, ai_normalizer._endpoint())

    def test_json_is_found_even_when_the_model_wraps_it_in_prose(self) -> None:
        self.assertEqual({"models": ["iPhone 15"]}, ai_normalizer._extract_json('{"models":["iPhone 15"]}'))
        wrapped = "درود! پاسخ:\n```json\n{\"models\": [\"iPhone 15\"]}\n```\nموفق باشی"
        self.assertEqual({"models": ["iPhone 15"]}, ai_normalizer._extract_json(wrapped))
        for bad in ("", "نه", "[1,2]"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ai_normalizer._extract_json(bad)

    def test_clean_model_list_applies_the_safety_rules(self) -> None:
        values = [
            "Samsung Galaxy S24",  # برند حذف می‌شود (قاعدهٔ ۵)
            "Xiaomi Redmi Note 12 4G",  # فقط «Xiaomi» حذف می‌شود، Redmi می‌ماند
            "Xiaomi 13",  # اینجا خودِ Xiaomi نام محصول است: باید بماند
            "  iPhone 15 Pro , ",  # فاصله و ویرگول حاشیه‌ای
            "iPhone 15 Pro",  # تکراری (casefold) حذف می‌شود
            "AirPods Pro 2",  # لوازم جانبی به لیست مدل گوشی راه ندارد
            "A16/A26",  # shorthandهای slashدار باید از parser و به‌صورت جداگانه بیایند
            "Redmi Note 9 Pro/9S",
            "iPhone 7/8",  # گروه سازگاری آیفون همچنان یک گزینه است
            42,  # ورودیِ غیررشته‌ای نادیده گرفته می‌شود
        ]
        self.assertEqual(
            ["Galaxy S24", "Redmi Note 12 4G", "Xiaomi 13", "iPhone 15 Pro", "iPhone 7/8"],
            ai_normalizer._clean_model_list(values),
        )

    def test_clean_model_list_refuses_a_non_array(self) -> None:
        for bad in (None, "iPhone 15", {"models": []}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ai_normalizer._clean_model_list(bad)

    def test_deterministic_list_is_flagged_when_it_carries_the_known_traps(self) -> None:
        for risky in ("Samsung A55 | iPhone 15", "Xiaomi 13", "Redmi A21 s"):
            with self.subTest(risky=risky):
                self.assertFalse(ai_normalizer._deterministic_is_safe(risky))
        self.assertTrue(ai_normalizer._deterministic_is_safe("iPhone 15 Pro Max | Galaxy S24"))
        self.assertTrue(ai_normalizer._deterministic_is_safe("Xiaomi Redmi Note 12"))
        self.assertFalse(ai_normalizer._deterministic_is_safe(""))


@unittest.skipUnless(HAS_AI, "httpx نصب نیست")
class TestAiNormalizePaths(unittest.TestCase):
    def test_without_configuration_the_deterministic_list_is_returned(self) -> None:
        with patch.object(ai_normalizer, "AI_BASE_URL", ""), patch.object(ai_normalizer, "AI_TOKEN", ""), \
                patch.object(ai_normalizer, "AI_MODEL", ""), \
                self.assertLogs("bot.services.ai_normalizer", level="INFO") as logs:
            out = asyncio.run(ai_normalizer.ai_normalize("13 پرو مکس", "iPhone 13 Pro Max"))
        self.assertEqual("iPhone 13 Pro Max", out)
        self.assertIn("AI is not configured", "\n".join(logs.output))

    def test_no_ai_and_a_suspicious_list_is_said_loudly(self) -> None:
        """بدون AI هیچ نظرِ دومی نیست؛ باید دستِ کم *گفته* شود، نه بی‌صدا رد شود."""
        with patch.object(ai_normalizer, "AI_BASE_URL", ""), patch.object(ai_normalizer, "AI_TOKEN", ""), \
                patch.object(ai_normalizer, "AI_MODEL", ""), \
                self.assertLogs("bot.services.ai_normalizer", level="WARNING") as logs:
            out = asyncio.run(ai_normalizer.ai_normalize("سامسونگ A21 s", "Samsung A21 s"))
        self.assertEqual("Samsung A21 s", out, "جایگزینی ساخته نمی‌شود؛ فقط هشدار داده می‌شود")
        self.assertIn("suspicious", "\n".join(logs.output))

    def test_a_good_answer_becomes_the_canonical_list(self) -> None:
        fake = _FailingPost(_completion('{"models": ["iPhone 15 Pro Max", "iPhone 15", "Galaxy S24"]}'))
        with h.temp_metrics() as metrics, \
                patch.object(ai_normalizer, "AI_BASE_URL", "https://ai.example/v1"), \
                patch.object(ai_normalizer, "AI_TOKEN", "sk-test"), \
                patch.object(ai_normalizer, "AI_MODEL", "gpt-x"), \
                patch("httpx.AsyncClient.post", new=fake):
            out = asyncio.run(ai_normalizer.ai_normalize("15promax | 15", "15promax | 15"))
            # خواندن *داخل* with: بیرونش DB_PATH برمی‌گردد به فایل واقعیِ repo.
            counts = dict(metrics.snapshot())
        self.assertEqual("iPhone 15 Pro Max | iPhone 15 | Galaxy S24", out)
        self.assertEqual(1, _count(counts, "ai_calls"))
        sent = fake.calls[0]
        self.assertEqual("https://ai.example/v1/chat/completions", sent["url"])
        self.assertEqual("Bearer sk-test", sent["headers"]["Authorization"])
        self.assertEqual(0, sent["json"]["temperature"], "مدلِ خلاق برای canonicalize ممنوع")
        self.assertIn("DETERMINISTIC CANDIDATE", sent["json"]["messages"][1]["content"])

    def test_ai_cannot_silently_drop_deterministic_candidates(self) -> None:
        raw = """Samsung:
S24 FE
A55"""
        deterministic = "S24 FE | A55"
        fake = _FailingPost(_completion('{"models": ["S24 FE"]}'))
        with patch.object(ai_normalizer, "AI_BASE_URL", "https://ai.example/v1"), \
                patch.object(ai_normalizer, "AI_TOKEN", "sk-test"), \
                patch.object(ai_normalizer, "AI_MODEL", "gpt-x"), \
                patch("httpx.AsyncClient.post", new=fake), \
                self.assertLogs("bot.services.ai_normalizer", level="WARNING") as logs:
            out = asyncio.run(ai_normalizer.ai_normalize(raw, deterministic))
        self.assertEqual("S24 FE | A55", out)
        self.assertIn("omitted 1 deterministic model", "\n".join(logs.output))

    def test_ai_slash_groups_cannot_collapse_non_iphone_variants(self) -> None:
        raw = "Samsung A16/A26\nXiaomi / POCO NOTE9PRO/9S"
        deterministic = "A16 | A26 | Redmi Note 9 Pro | Redmi Note 9S"
        fake = _FailingPost(_completion('{"models": ["A16/A26", "Redmi Note 9 Pro/9S"]}'))
        with (
            patch.object(ai_normalizer, "AI_BASE_URL", "https://ai.example/v1"),
            patch.object(ai_normalizer, "AI_TOKEN", "sk-test"),
            patch.object(ai_normalizer, "AI_MODEL", "gpt-x"),
            patch("httpx.AsyncClient.post", new=fake),
            self.assertLogs("bot.services.ai_normalizer", level="WARNING"),
        ):
            out = asyncio.run(ai_normalizer.ai_normalize(raw, deterministic))
        models = out.split(" | ")
        self.assertTrue({"A16", "A26", "Redmi Note 9 Pro", "Redmi Note 9S"}.issubset(set(models)))
        self.assertFalse(any("/" in model for model in models))

    def test_the_learned_owner_rules_travel_in_the_system_prompt(self) -> None:
        """AI باید همان قواعدی را بداند که مسیر deterministic اعمال می‌کند."""
        fake = _FailingPost(_completion('{"models": ["iPhone 15"]}'))
        with patch.object(ai_normalizer, "AI_BASE_URL", "https://ai.example/v1"), \
                patch.object(ai_normalizer, "AI_TOKEN", "sk-test"), \
                patch.object(ai_normalizer, "AI_MODEL", "gpt-x"), \
                patch.object(ai_normalizer.learning, "rules_for_prompt", lambda *_a, **_k: "«گوشی ۱۵» = iPhone 15"), \
                patch("httpx.AsyncClient.post", new=fake):
            asyncio.run(ai_normalizer.ai_normalize("گوشی ۱۵", "iPhone 15"))
        system = fake.calls[0]["json"]["messages"][0]["content"]
        self.assertIn("LEARNED OWNER RULES", system)
        self.assertIn("«گوشی ۱۵»", system)

    def test_a_server_error_falls_back_and_is_counted(self) -> None:
        fake = _FailingPost(httpx.ConnectError("boom"))
        with h.temp_metrics() as metrics, \
                patch.object(ai_normalizer, "AI_BASE_URL", "https://ai.example/v1"), \
                patch.object(ai_normalizer, "AI_TOKEN", "sk-test"), \
                patch.object(ai_normalizer, "AI_MODEL", "gpt-x"), \
                patch("httpx.AsyncClient.post", new=fake), \
                self.assertLogs("bot.services.ai_normalizer", level="WARNING") as logs:
            out = asyncio.run(ai_normalizer.ai_normalize("15promax", "iPhone 15 Pro Max"))
            counts = dict(metrics.snapshot())
        self.assertEqual("iPhone 15 Pro Max", out, "خرابی AI نباید جریان را متوقف کند")
        self.assertEqual(1, _count(counts, "ai_failures"))
        self.assertIn("ConnectError", "\n".join(logs.output))

    def test_garbage_from_the_model_is_a_failure_not_a_empty_answer(self) -> None:
        """جوابِ بی‌شکل = خطا؛ وگرنه یک «هیچ مدلی نیست» بی‌صدا تبدیل به ربات می‌شود."""
        for payload in ({"choices": []}, _completion("متن بدون JSON"), _completion('{"nope": 1}')):
            with self.subTest(payload=payload):
                fake = _FailingPost(payload)
                with h.temp_metrics() as metrics, \
                        patch.object(ai_normalizer, "AI_BASE_URL", "https://ai.example/v1"), \
                        patch.object(ai_normalizer, "AI_TOKEN", "sk-test"), \
                        patch.object(ai_normalizer, "AI_MODEL", "gpt-x"), \
                        patch("httpx.AsyncClient.post", new=fake), \
                        self.assertLogs("bot.services.ai_normalizer", level=logging.WARNING):
                    out = asyncio.run(ai_normalizer.ai_normalize("15promax", "iPhone 15 Pro Max"))
                    counts = dict(metrics.snapshot())
                self.assertEqual("iPhone 15 Pro Max", out)
                self.assertEqual(1, _count(counts, "ai_failures"))

    def test_a_non_2xx_status_is_a_failure_too(self) -> None:
        fake = _FailingPost({"error": "quota"}, status=429)
        with h.temp_metrics() as metrics, \
                patch.object(ai_normalizer, "AI_BASE_URL", "https://ai.example/v1"), \
                patch.object(ai_normalizer, "AI_TOKEN", "sk-test"), \
                patch.object(ai_normalizer, "AI_MODEL", "gpt-x"), \
                patch("httpx.AsyncClient.post", new=fake), \
                self.assertLogs("bot.services.ai_normalizer", level="WARNING"):
            out = asyncio.run(ai_normalizer.ai_normalize("15promax", "iPhone 15 Pro Max"))
            counts = dict(metrics.snapshot())
        self.assertEqual("iPhone 15 Pro Max", out)
        self.assertEqual(1, _count(counts, "ai_failures"))

    def test_the_legacy_job_log_adapter_still_works_and_never_breaks(self) -> None:
        """`job_log` فقط پذیرفته می‌شود (میراث OPTION) — اگر خودش خطا داد هم.»"""

        class _Exploding:
            def add(self, *_args: object) -> None:
                raise RuntimeError("no sink for you")

        with patch.object(ai_normalizer, "AI_BASE_URL", ""), \
                patch.object(ai_normalizer, "AI_TOKEN", ""), \
                patch.object(ai_normalizer, "AI_MODEL", ""):
            out = asyncio.run(ai_normalizer.ai_normalize("x", "iPhone 15", _Exploding()))
        self.assertEqual("iPhone 15", out)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
