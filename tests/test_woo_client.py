"""سیاستِ مشترک HTTP فروشگاه: auth، User-Agent، backoff و سانسور (بند 4.1، بخش سوم فاز ۴).

`bot/services/woo_client.py` تنها جایی است که سوکت باز می‌کند؛ این فایل هم سیاست را
تست می‌کند و هم نگه می‌دارد که هیچ‌کس دوباره یک client موازی نسازد — چون دقیقاً همان
چهار کپیِ هم‌خانوادگی (دو مدلِ مختلف User-Agent، retry فقط در مسیر نوشتن) دلیل این split بود.
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from _flow_harness import TransportScript, no_sleep, patched_settings, respond, settings_with

try:
    import httpx

    from bot import __version__
    from bot.services import woo_client
    from bot.services.woo_client import (
        CHALLENGE_COOKIE, USER_AGENT, ChallengeJar, WooClient, WooCommerceAPIError, body_snippet,
        challenge_cookie, check, describe_exception,
    )
    from bot.services.woocommerce import ping_woocommerce
    # نام‌گذاری مجدد، وگرنه pytest این دو تابعِ async را به‌اشتباه «تست» جمع می‌کند
    from bot.services.woocommerce_product_test import test_product_with_image as probe_product
    from bot.services.wordpress_media import test_wordpress_media as probe_media

    HAS_HTTPX = True
except Exception:  # pragma: no cover - httpx روی هاستِ اشتَری ممکن است نصب نباشد
    HAS_HTTPX = False

needs_httpx = unittest.skipUnless(HAS_HTTPX, "httpx is not installed")

_REAL_ASYNC_CLIENT = httpx.AsyncClient if HAS_HTTPX else None

SHARED: dict[str, str] = {
    "woocommerce_url": "https://shop.example",
    "woocommerce_key": "ck_test",
    "woocommerce_secret": "cs_topsecret",
    "wordpress_url": "https://shop.example",
    "wordpress_username": "admin",
    "wordpress_app_password": "aaaa-bbbb-cccc",
}


def _client(script: TransportScript, **over: Any) -> WooClient:
    kwargs: dict[str, Any] = {"transport": script.transport(), "attempts": 3}
    kwargs.update(over)
    return WooClient(**kwargs)


@needs_httpx
class TestInjectedPolicy(unittest.IsolatedAsyncioTestCase):
    """آنچه هر caller باید یک‌بار بنویسد و هیچ‌کدام نباید بنویسد.\""""

    async def test_query_auth_and_agent_are_added_by_the_client(self) -> None:
        script = TransportScript(respond(200, []))
        audit = woo_client.Audit()
        with patched_settings(settings_with(**SHARED)):
            async with WooClient(audit=audit, transport=script.transport()) as client:
                await client.get("https://shop.example/wp-json/wc/v3/products", params={"per_page": 3})
        self.assertIn("[http:start] #1 GET /wp-json/wc/v3/products", audit.text())
        self.assertIn("[http:done] #1 GET /wp-json/wc/v3/products → HTTP 200", audit.text())
        self.assertNotIn("consumer_secret", audit.text())
        request = script.requests[0]
        self.assertEqual("3", request.url.params["per_page"], "پارامترهای caller گم نشوند")
        self.assertEqual("ck_test", request.url.params["consumer_key"])
        self.assertEqual("cs_topsecret", request.url.params["consumer_secret"])
        self.assertEqual(USER_AGENT, request.headers["user-agent"])
        self.assertIn(__version__, USER_AGENT, "نسخهٔ ربات باید در سرور قابل تشخیص باشد")

    async def test_basic_route_uses_app_password_and_no_secrets_in_the_url(self) -> None:
        script = TransportScript(respond(200, {"id": 1}))
        with patched_settings(settings_with(**SHARED)):
            async with _client(script) as client:
                await client.post("https://shop.example/wp-json/wp/v2/media", content=b"x", basic=True)
        request = script.requests[0]
        self.assertIn("authorization", request.headers)
        self.assertNotIn("consumer_key", request.url.params, "کلید ووکامرس در مسیر وردپرس جایش نیست")
        self.assertNotIn("force", request.url.params)

    async def test_unconfigured_app_password_does_not_send_a_bare_request(self) -> None:
        """بدون اعتبارنامهٔ وردپرس، client چیزی را جعل نمی‌کند؛ خودِ فراخوان تصمیم می‌گیرد.\""""
        script = TransportScript(respond(200, {"id": 1}))
        with patched_settings(settings_with(**{**SHARED, "wordpress_app_password": ""})):
            async with _client(script) as client:
                await client.post("https://shop.example/wp-json/wp/v2/media", content=b"x", basic=True)
        self.assertNotIn("authorization", script.requests[0].headers)

    async def test_error_text_never_carries_the_url_or_the_secret(self) -> None:
        script = TransportScript(respond(400, {"message": "Invalid or duplicated SKU."}))
        audit = woo_client.Audit()
        with patched_settings(settings_with(**SHARED)):
            async with _client(script, audit=audit) as client:
                response = await client.post("https://shop.example/wp-json/wc/v3/products", json={})
                with self.assertRaises(WooCommerceAPIError) as ctx:
                    check(response)
        self.assertEqual(400, ctx.exception.status_code)
        self.assertEqual("Invalid or duplicated SKU.", str(ctx.exception))
        for blob in (str(ctx.exception), "\n".join(audit.lines), body_snippet(response)):
            self.assertNotIn("cs_topsecret", blob, "رمز نباید در هیچ متن خطایی باشد")
            self.assertNotIn("consumer_secret", blob)


@needs_httpx
class TestRetryPolicy(unittest.IsolatedAsyncioTestCase):
    """چه چیزی دوباره ارسال می‌شود — و چرا نوشتن روی ۵۰۰ نه.\""""

    async def test_connect_error_is_retried_even_on_a_post(self) -> None:
        script = TransportScript(httpx.ConnectError("refused"), httpx.ConnectError("refused"), respond(201, {"id": 7}))
        with patched_settings(settings_with(**SHARED)), no_sleep() as delays:
            async with _client(script) as client:
                response = await client.post("https://shop.example/wp-json/wc/v3/products", json={})
        self.assertEqual(201, response.status_code)
        self.assertEqual(3, script.sends)
        self.assertEqual([1, 2], delays, "backoff باید نمایی باشد، نه یکنواخت")

    async def test_timeout_on_a_write_is_not_retried(self) -> None:
        """۵۰۲/تایم‌اوت روی POST یعنی «شاید اعمال شد»؛ تکرارش یعنی محصول دومی.\""""
        script = TransportScript(httpx.ReadTimeout(""))
        audit = woo_client.Audit()
        with patched_settings(settings_with(**SHARED)), no_sleep() as delays:
            async with WooClient(audit=audit, transport=script.transport(), attempts=3) as client:
                with self.assertRaises(httpx.ReadTimeout):
                    await client.post("https://shop.example/wp-json/wc/v3/products", json={})
        self.assertEqual(1, script.sends, "نفرست دوباره")
        self.assertEqual([], delays)
        self.assertIn("[http:error] #1 POST /wp-json/wc/v3/products", audit.text())
        self.assertIn("ReadTimeout:", audit.text())
        self.assertIn("پس از ", audit.text())
        self.assertIn("تلاش 1/3", audit.text())
        self.assertIn("مهلت هر فاز=45s", audit.text())
        self.assertIn("ممکن است درخواست اعمال شده باشد", audit.text())
        self.assertNotIn("cs_topsecret", audit.text())

    async def test_transport_error_summary_fills_empty_timeout_message(self) -> None:
        self.assertEqual(
            "ReadTimeout: پاسخ فروشگاه نرسید؛ ممکن است درخواست اعمال شده باشد",
            describe_exception(httpx.ReadTimeout("")),
        )
        self.assertEqual(
            "ConnectTimeout: مهلت اتصال به فروشگاه تمام شد",
            describe_exception(httpx.ConnectTimeout("")),
        )

    async def test_rate_limit_is_retried_on_a_write(self) -> None:
        """۴۲۹ یعنی «نپذیرفتم» — هیچ چیزی اعمال نشده، پس ارسال دوباره بی‌خطر است.\""""
        script = TransportScript(respond(429, {"message": "too many"}), respond(201, {"id": 8}))
        with patched_settings(settings_with(**SHARED)), no_sleep():
            async with _client(script) as client:
                response = await client.post("https://shop.example/wp-json/wc/v3/products", json={})
        self.assertEqual(201, response.status_code)
        self.assertEqual(2, script.sends)

    async def test_retry_after_is_respected(self) -> None:
        script = TransportScript(
            httpx.Response(429, headers={"Retry-After": "7"}, json={"message": "slow down"}),
            respond(201, {"id": 8}),
        )
        with patched_settings(settings_with(**SHARED)), no_sleep() as delays:
            async with _client(script) as client:
                response = await client.post("https://shop.example/wp-json/wc/v3/products", json={})
        self.assertEqual(201, response.status_code)
        self.assertEqual([7.0], delays, "سرور اگر زمان داد، backoff محلی نباید زودتر درخواست بزند")

    async def test_publish_client_paces_and_serializes_requests(self) -> None:
        script = TransportScript(respond(200, []), respond(200, []), respond(200, []))
        with patched_settings(settings_with(**SHARED)), no_sleep() as delays:
            async with WooClient(
                transport=script.transport(), attempts=1,
                min_request_interval=0.2, max_concurrent_requests=1,
            ) as client:
                await __import__("asyncio").gather(*(
                    client.get("https://shop.example/wp-json/wc/v3/products") for _ in range(3)
                ))
        self.assertEqual(3, script.sends)
        self.assertGreaterEqual(len(delays), 2, "درخواست‌های سریع باید gap رعایت کنند")
        self.assertTrue(all(0 < delay <= 0.2 for delay in delays))

    async def test_server_error_on_a_read_is_retried_but_500_is_not(self) -> None:
        script = TransportScript(respond(503), respond(200, []))
        with patched_settings(settings_with(**SHARED)), no_sleep():
            async with _client(script) as client:
                response = await client.get("https://shop.example/wp-json/wc/v3/products")
        self.assertEqual(200, response.status_code, "۵۰۳ گیت/پروکسی معمولاً با یک صبر رفع می‌شود")
        self.assertEqual(2, script.sends)

        script = TransportScript(respond(500, {"message": "Internal Server Error"}))
        with patched_settings(settings_with(**SHARED)), no_sleep() as delays:
            async with _client(script) as client:
                response = await client.get("https://shop.example/wp-json/wc/v3/products")
        self.assertEqual(500, response.status_code)
        self.assertEqual(1, script.sends, "۵۰۰ ووکامرس یعنی PHP fatal؛ یک ثانیه بعد هم همان است")
        self.assertEqual([], delays, "پس ادمین هم معطل backoff نمی‌شود")

    async def test_server_error_on_a_post_is_returned_not_retried(self) -> None:
        script = TransportScript(respond(502), respond(201, {"id": 9}))
        with patched_settings(settings_with(**SHARED)), no_sleep():
            async with _client(script) as client:
                response = await client.post("https://shop.example/wp-json/wc/v3/products", json={})
        self.assertEqual(502, response.status_code, "پاسخ اول به caller برمی‌گردد تا خودش تصمیم بگیرد")
        self.assertEqual(1, script.sends)

    async def test_attempts_one_never_retries(self) -> None:
        """دکمهٔ عیب‌یابی باید جواب بدهد، نه با هاست شلوغ بجود.\""""
        script = TransportScript(httpx.ConnectError("refused"), respond(200))
        with patched_settings(settings_with(**SHARED)):
            async with _client(script, attempts=1) as client:
                with self.assertRaises(httpx.ConnectError):
                    await client.get("https://shop.example/wp-json/wc/v3/products")
        self.assertEqual(1, script.sends)

    async def test_rehearsal_ignores_the_test_transport(self) -> None:
        """dry-run نباید با یک transport دیگر قابل‌حواله باشد؛ وگرنه «تمرین» می‌شود نوشتن.\""""
        script = TransportScript(respond(500, {"message": "این نباید اجرا شود"}))
        audit = woo_client.Audit()
        with patched_settings(settings_with(**SHARED, woo_dry_run=True)):
            async with WooClient(audit=audit, dry_run=True, transport=script.transport()) as client:
                response = await client.post("https://shop.example/wp-json/wc/v3/products", json={"sku": "BO1"})
        self.assertEqual(201, response.status_code)
        self.assertEqual(0, script.sends)
        self.assertTrue(any(line.startswith("[dry-run] POST") for line in audit.lines))


@needs_httpx
class TestToolsUseTheSameClient(unittest.IsolatedAsyncioTestCase):
    """🏓 Ping و 🔧 ابزارهایش: همان client، همان اعتبارنامه، بدون کپیِ دستی.\""""

    def _inject(self, script: TransportScript) -> None:
        """ساخت client را در `WooClient` به یک transport ساختگی وصل می‌کند.

        تنها چیزی که عوض می‌شود سوکت است، پس مسیر واقعی (auth تزریق‌شده، attempts=1،
        redirect) همان اجرا می‌شود. `original` در سطح ماژول گرفته می‌شود تا دو بار patch
        کردن (حلقهٔ subTest) اورجینالِ قبلی را «واقعی» نپندارد.
        """
        real = _REAL_ASYNC_CLIENT

        def factory(**kwargs: Any) -> httpx.AsyncClient:
            kwargs.pop("transport", None)
            return real(transport=script.transport(), **kwargs)

        self.addCleanup(setattr, woo_client.httpx, "AsyncClient", real)
        woo_client.httpx.AsyncClient = factory

    async def test_ping_authenticates_through_the_client(self) -> None:
        script = TransportScript(respond(200, [{"id": 1}]))
        self._inject(script)
        result = await ping_woocommerce("https://shop.example", "ck_new", "cs_new")
        self.assertTrue(result.ok, result.message)
        request = script.requests[0]
        self.assertEqual("ck_new", request.url.params["consumer_key"], "کلید نامزدِ تست، نه کلیدِ .env")
        self.assertEqual(USER_AGENT, request.headers["user-agent"])
        self.assertEqual("1", request.url.params["per_page"])

    async def test_ping_distinguishes_auth_from_a_block(self) -> None:
        for status, needle in ((401, "Authentication failed"), (403, "Forbidden"), (404, "Endpoint not found")):
            with self.subTest(status=status):
                self._inject(TransportScript(respond(status, {"message": "nope"})))
                result = await ping_woocommerce("https://shop.example", "ck", "cs")
                self.assertFalse(result.ok)
                self.assertIn(needle, result.message)

    async def test_ping_reports_timeout_kind_even_when_httpx_message_is_empty(self) -> None:
        self._inject(TransportScript(httpx.ReadTimeout("")))
        result = await ping_woocommerce("https://shop.example", "ck", "cs")
        self.assertFalse(result.ok)
        self.assertIsNone(result.status_code)
        self.assertIn("ReadTimeout", result.message)
        self.assertIn("پاسخ فروشگاه نرسید", result.message)

    async def test_media_tool_uploads_and_cleans_up(self) -> None:
        script = TransportScript(respond(201, {"id": 55}), respond(200, {}))
        self._inject(script)
        with patched_settings(settings_with(**SHARED)):
            result = await probe_media()
        self.assertTrue(result.ok, result.message)
        self.assertTrue(result.deleted)
        self.assertEqual(55, result.media_id)
        self.assertEqual(["POST /wp-json/wp/v2/media", "DELETE /wp-json/wp/v2/media/55"], script.methods)
        self.assertIn("authorization", script.requests[0].headers)
        self.assertNotIn("consumer_key", script.requests[0].url.params)

    async def test_media_tool_reports_a_waf_block(self) -> None:
        self._inject(TransportScript(respond(403, {"message": "ModSecurity"})))
        with patched_settings(settings_with(**SHARED)):
            result = await probe_media()
        self.assertFalse(result.ok)
        self.assertIn("ModSecurity", result.message)

    async def test_media_tool_names_the_host_layer_when_it_is_refused(self) -> None:
        """۴۱۸ روی مسیر رسانه یعنی «کاربر و رمز را چک کن» نصیحتِ غلطی است."""
        self._inject(TransportScript(respond(418)))
        with patched_settings(settings_with(**SHARED)):
            result = await probe_media()
        self.assertFalse(result.ok)
        self.assertIn("418", result.message)
        self.assertIn("وایت‌لیست", result.message)
        self.assertNotIn("Application Password", result.message)

    async def test_product_tool_removes_the_media_when_the_product_fails(self) -> None:
        """تست نباید چیز نیمه‌کاره روی سایت بگذارد.\""""
        script = TransportScript(respond(201, {"id": 77}), respond(403, {"message": "no"}), respond(200, {}))
        self._inject(script)
        with patched_settings(settings_with(**SHARED)):
            result = await probe_product()
        self.assertFalse(result.ok)
        self.assertIn("DELETE /wp-json/wp/v2/media/77", script.methods, "تصویرِ یتیم پاک شد")

    async def test_product_tool_creates_attaches_and_cleans(self) -> None:
        script = TransportScript(
            respond(201, {"id": 78}),
            respond(201, {"id": 4412}),
            respond(200, {"deleted": True}),
            respond(200, {"deleted": True}),
        )
        self._inject(script)
        with patched_settings(settings_with(**SHARED)):
            result = await probe_product()
        self.assertTrue(result.ok, result.message)
        self.assertTrue(result.cleaned)
        self.assertEqual(4412, result.product_id)
        product = script.requests[1]
        self.assertEqual("ck_test", product.url.params["consumer_key"])
        self.assertEqual(["POST /wp-json/wp/v2/media", "POST /wp-json/wc/v3/products",
                          "DELETE /wp-json/wc/v3/products/4412", "DELETE /wp-json/wp/v2/media/78"],
                         script.methods)


@needs_httpx
class TestNoSecondImplementation(unittest.TestCase):
    """نگهبانِ ضدواگرایی: ماژول‌های فروشگاه نباید خودشان client بسازند یا اعتبارنامه بچینند.

    چهار فایل، هر کدام با یک کپی از «چطور با ووکامرس حرف بزنیم»، دقیقاً همان باگ بود (retry
    فقط در مسیر نوشتن، User-Agent دو مدل، timeout چهار عدد مختلف). اینجا عدد نیست که
    فراموش شود — تست است.
    """

    FORBIDDEN = (
        "httpx.AsyncClient(",
        '"User-Agent"',
        '"consumer_key":',
        '"consumer_secret":',
        "auth=(settings.",
        "TisaPostToWP/",
    )
    FILES = (
        "bot/services/woocommerce_direct.py",
        "bot/services/woocommerce.py",
        "bot/services/woocommerce_product_test.py",
        "bot/services/wordpress_media.py",
        "bot/services/sku.py",
    )

    def test_shop_modules_do_not_build_their_own_requests(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        offenders: list[str] = []
        for relative in self.FILES:
            text = (root / relative).read_text(encoding="utf-8")
            hits = [token for token in self.FORBIDDEN if token in text]
            if hits:
                offenders.append(f"{relative}: {hits}")
        self.assertEqual([], offenders, "لایهٔ HTTP یکی است؛ اگر این تست ترکید، یا یک کپی تازه "
                                       "ساخته‌ای یا client را دور زده‌ای")

    def test_the_client_is_the_only_socket(self) -> None:
        """Only the Woo policy and shared AI session factory construct clients."""
        from pathlib import Path

        allowed = {
            "bot/services/woo_client.py",          # فروشگاه
            "bot/services/ai_normalizer.py",        # AI session factory shared by both stages
        }
        root = Path(__file__).resolve().parents[1]
        users = {path.relative_to(root).as_posix() for path in (root / "bot").rglob("*.py")
                 if "httpx.AsyncClient(" in path.read_text(encoding="utf-8")}
        self.assertEqual(allowed, users, "هر client تازه باید بگوید با چه سرویسی حرف می‌کند")


#: The page the shop's host really answers with (2026‑10‑15), abbreviated in the middle: the two
#: lines a client must read are the challenge value and the page's own ``Max-Age``. Anything the
#: parser accepts must be readable off this text — not off a fixture we invented.
GATE_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Redirecting...</title></head>
<body>
<noscript>JavaScript is required.</noscript>
<script>
(() => {
    "use strict";
    const challenge =
        "_dgjsc=c871a8a72570e8e31f9decb5bd2afde3:20261015130000";
    const match =
        /^_dgjsc=([a-f0-9]{32}):([0-9]{14})$/.exec(challenge);
    if (!match) {
        document.body.textContent = "Invalid challenge.";
        return;
    }
    const cookieName = "_dgjsc";
    const cookieValue = `${match[1]}:${match[2]}`;
    document.cookie =
        `${cookieName}=${cookieValue}; ` +
        "Max-Age=7200; Path=/; Secure; SameSite=Lax";
    window.location.reload();
})();
</script>
</body></html>
"""

GATE_VALUE = "c871a8a72570e8e31f9decb5bd2afde3:20261015130000"


def gate_page(status: int = 418):
    """`httpx.Response` برای صفحهٔ چالش — همان متنی که میزبان می‌فرستد."""
    return httpx.Response(status, html=GATE_PAGE)


def temp_jar(test: unittest.TestCase) -> tuple[ChallengeJar, Path]:
    """جارِ چالش با مسیرِ موقت؛ تست نباید داخل `data/` واقعی بنویسد."""
    directory = tempfile.TemporaryDirectory()
    test.addCleanup(directory.cleanup)
    path = Path(directory.name) / "shop-challenge.json"
    return ChallengeJar(path=path), path


@needs_httpx
class TestChallengeParsing(unittest.TestCase):
    """خواندنِ صفحهٔ چالش: دقیقاً همان چیزی که میزبان می‌فرستد، نه بیشتر."""

    def test_the_real_page_is_read_value_and_lifetime(self) -> None:
        pair = challenge_cookie(gate_page())
        self.assertIsNotNone(pair, "صفحهٔ واقعیِ میزبان باید خوانده شود")
        value, max_age = pair  # type: ignore[misc]
        self.assertEqual(GATE_VALUE, value)
        self.assertEqual(7200.0, max_age, "عمرِ کوکی را خودِ صفحه می‌گوید؛ ما حدس نمی‌زنیم")

    def test_only_a_refusal_status_is_read(self) -> None:
        self.assertIsNone(challenge_cookie(httpx.Response(200, html=GATE_PAGE)),
                          "۲۰۰ صفحهٔ چالش نیست")
        self.assertIsNone(challenge_cookie(httpx.Response(403, html=GATE_PAGE)),
                          "فقط کدِ مسدودسازیِ همین سایت چالش خوانده می‌شود")

    def test_a_refusal_without_the_page_is_not_a_challenge(self) -> None:
        self.assertIsNone(challenge_cookie(httpx.Response(418, text="<html>Access denied</html>")))
        self.assertIsNone(challenge_cookie(httpx.Response(418, json={"code": "rest_forbidden"})))

    def test_the_jar_forgets_what_is_expired_or_unreadable(self) -> None:
        jar, path = temp_jar(self)
        path.write_text(json.dumps({"https://shop.example": ["value", time.time() - 1]}), "utf-8")
        self.assertIsNone(ChallengeJar(path=path).value("https://shop.example"),
                          "کوکیِ منقضی نباید فرستاده شود")
        path.write_text("{ not json", "utf-8")
        self.assertIsNone(ChallengeJar(path=path).value("https://shop.example"),
                          "فایلِ خراب یعنی «چالشی نداریم»، نه خطا")
        self.assertIsNone(ChallengeJar(path=path.parent / "nope.json").value("https://shop.example"))
        self.assertIsNone(jar.value(""), "originِ خالی یعنی هیچ")

    def test_the_jar_writes_what_the_next_process_needs(self) -> None:
        _jar, path = temp_jar(self)
        ChallengeJar(path=path).remember("https://shop.example", "abc:123", 7200)
        stored = json.loads(path.read_text("utf-8"))["https://shop.example"]
        self.assertEqual("abc:123", stored[0])
        self.assertGreater(stored[1], time.time() + 7000, "عمرِ ذخیره‌شده باید واقعی باشد")
        self.assertEqual("abc:123", ChallengeJar(path=path).value("https://shop.example"))


@needs_httpx
class TestChallengeHandshake(unittest.IsolatedAsyncioTestCase):
    """۲۰۲۶‑۱۰‑۱۵: پاسخِ ۴۱۸ یک چالشِ جاوااسکریپتی بود، نه «بلاک شدنِ همیشگی».

    مرورگر همان صفحه را اجرا می‌کند، کوکی را ست می‌کند، یک‌بار رفرش می‌کند و هیچ‌وقت نمی‌فهمد؛
    `curl` و `httpx` این کار را نمی‌کنند و همیشه ۴۱۸ می‌خوانند. ربات حالا وسطِ راهِ مرورگر
    می‌ایستد: کوکی را از خودِ صفحه برمی‌دارد و همان درخواست را یک‌بار تکرار می‌کند.
    """

    async def test_the_gate_page_is_answered_and_the_request_repeated(self) -> None:
        script = TransportScript(gate_page(), respond(200, {"ok": True}))
        audit = woo_client.Audit()
        jar, _ = temp_jar(self)
        with patched_settings(settings_with(**SHARED)):
            async with WooClient(audit=audit, transport=script.transport(), challenges=jar) as client:
                response = await client.get("https://shop.example/wp-json/wc/v3/tisa-health")
        self.assertEqual(200, response.status_code, "پاسخِ واقعیِ سایت، نه صفحهٔ چالش")
        self.assertEqual(2, script.sends, "یکی چالش، یکی تکرار — نه بیشتر")
        self.assertNotIn("cookie", script.requests[0].headers, "درخواستِ اول بی‌کوکی می‌رود")
        self.assertEqual(f"{CHALLENGE_COOKIE}={GATE_VALUE}", script.requests[1].headers["cookie"])
        self.assertIn("[http:gate]", audit.text())
        self.assertIn(CHALLENGE_COOKIE, audit.text())

    async def test_the_cookie_is_reused_so_later_requests_skip_the_gate(self) -> None:
        script = TransportScript(gate_page(), respond(200, {"id": 1}), respond(200, {"id": 2}))
        jar, _ = temp_jar(self)
        with patched_settings(settings_with(**SHARED)):
            async with WooClient(transport=script.transport(), challenges=jar) as client:
                await client.get("https://shop.example/wp-json/wc/v3/products")
                await client.get("https://shop.example/wp-json/wc/v3/products")
        self.assertEqual(3, script.sends, "درخواستِ دوم دیگر نباید ۴۱۸ بخورد")
        self.assertEqual(f"{CHALLENGE_COOKIE}={GATE_VALUE}", script.requests[2].headers["cookie"])

    async def test_a_write_is_repeated_too_because_php_never_ran(self) -> None:
        """تکرارِ نوشتن هم بی‌خطر است: چالش ثابت می‌کند درخواست به PHP نرسیده."""
        script = TransportScript(gate_page(), respond(201, {"id": 5}))
        jar, _ = temp_jar(self)
        with patched_settings(settings_with(**SHARED)):
            async with WooClient(transport=script.transport(), challenges=jar) as client:
                response = await client.post("https://shop.example/wp-json/wc/v3/products",
                                             json={"name": "قاب"})
        self.assertEqual(201, response.status_code)
        self.assertEqual(["POST /wp-json/wc/v3/products"] * 2, script.methods)

    async def test_a_418_without_the_page_stays_a_refusal_for_the_fence(self) -> None:
        script = TransportScript(respond(418, {"code": "rest_forbidden"}))
        jar, _ = temp_jar(self)
        with patched_settings(settings_with(**SHARED)):
            async with WooClient(transport=script.transport(), challenges=jar) as client:
                response = await client.get("https://shop.example/wp-json/wc/v3/tisa-health")
        self.assertEqual(418, response.status_code)
        self.assertEqual(1, script.sends, "بدون چالش چیزی برای تکرار نیست")
        self.assertIsNone(jar.value("https://shop.example"))

    async def test_the_handshake_survives_a_restart(self) -> None:
        """ری‌استارت نباید با یک ۴۱۸ شروع شود — وگرنه پایش، سایت را 🔒 مسدود می‌بیند."""
        _jar, path = temp_jar(self)
        first = TransportScript(gate_page(), respond(200, {}))
        with patched_settings(settings_with(**SHARED)):
            async with WooClient(transport=first.transport(), challenges=ChallengeJar(path=path)) as client:
                await client.get("https://shop.example/wp-json/")
            second = TransportScript(respond(200, {}))
            async with WooClient(transport=second.transport(), challenges=ChallengeJar(path=path)) as client:
                await client.get("https://shop.example/wp-json/")
        self.assertEqual(1, second.sends, "کوکی از دیسک آمد؛ پروسهٔ تازه چالش نمی‌گیرد")
        self.assertEqual(f"{CHALLENGE_COOKIE}={GATE_VALUE}", second.requests[0].headers["cookie"])

    async def test_a_second_challenge_is_returned_not_looped(self) -> None:
        """دو ۴۱۸ پشت‌سرهم ⇒ صف و 🔒، نه حلقه روی گیتی که ناراضی است."""
        script = TransportScript(gate_page(), gate_page(), respond(200, {}))
        jar, _ = temp_jar(self)
        with patched_settings(settings_with(**SHARED)):
            async with WooClient(transport=script.transport(), challenges=jar) as client:
                response = await client.get("https://shop.example/wp-json/")
        self.assertEqual(418, response.status_code)
        self.assertEqual(2, script.sends, "دقیقاً یک تکرار، حتی اگر پاسخ باز هم چالش باشد")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
