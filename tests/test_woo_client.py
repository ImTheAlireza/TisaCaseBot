"""سیاستِ مشترک HTTP فروشگاه: auth، User-Agent، backoff و سانسور (بند 4.1، بخش سوم فاز ۴).

`bot/services/woo_client.py` تنها جایی است که سوکت باز می‌کند؛ این فایل هم سیاست را
تست می‌کند و هم نگه می‌دارد که هیچ‌کس دوباره یک client موازی نسازد — چون دقیقاً همان
چهار کپیِ هم‌خانوادگی (دو مدلِ مختلف User-Agent، retry فقط در مسیر نوشتن) دلیل این split بود.
"""
from __future__ import annotations

import unittest
from typing import Any

from _flow_harness import TransportScript, no_sleep, patched_settings, respond, settings_with

try:
    import httpx

    from bot import __version__
    from bot.services import woo_client
    from bot.services.woo_client import (
        USER_AGENT, WooClient, WooCommerceAPIError, body_snippet, check, describe_exception,
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
            "ReadTimeout: سرور فروشگاه در مهلت مقرر پاسخ نداد؛ ممکن است درخواست اعمال شده باشد",
            describe_exception(httpx.ReadTimeout("")),
        )
        self.assertEqual(
            "ConnectTimeout: اتصال به سرور فروشگاه در مهلت مقرر برقرار نشد",
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
        self.assertIn("در مهلت مقرر پاسخ نداد", result.message)

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
        """فهرستِ صریح، نه «هیچ‌کس»: AI یک سرویس دیگر است و client خودش حقش است."""
        from pathlib import Path

        allowed = {
            "bot/services/woo_client.py",          # فروشگاه
            "bot/services/ai_normalizer.py",        # ارائه‌دهندهٔ AI
            "bot/services/product_extractor.py",    # ارائه‌دهندهٔ AI
        }
        root = Path(__file__).resolve().parents[1]
        users = {path.relative_to(root).as_posix() for path in (root / "bot").rglob("*.py")
                 if "httpx.AsyncClient(" in path.read_text(encoding="utf-8")}
        self.assertEqual(allowed, users, "هر client تازه باید بگوید با چه سرویسی حرف می‌کند")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
