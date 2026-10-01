"""انتخاب SKU: افزونه ← کش ← اسکن کاتالوگ، و اینکه هیچ‌کدام مرجع نهایی نیستند (بند 4.4).

`bot/services/sku.py` تنها جایی است که «شمارهٔ بعدیِ این فروشگاه» را تعیین می‌کند. دو چیز
اینجا مهم است: هر منبعی که می‌گوید «این آزاد است» باید با درخواستِ exact-SKU تأیید شود
(کش می‌پوسد، اسکن ممکن است نیمه‌کاره باشد، شمارندهٔ افزونه ممکن است جا بماند)، و تعداد
درخواست در هر انتشار باید کم باشد — وگرنه ادمین روی کارت «در حال ساخت» گیر می‌کند
(P1-10: تا ۱۰۰ درخواست سریالی، ۱۰ تا ۶۰ ثانیه روی هاست اشتراکی).
"""
from __future__ import annotations

import contextlib
import time
import unittest
from typing import Any

from _flow_harness import (
    TransportScript,
    no_sleep,
    patched_settings,
    respond,
    settings_with,
    temp_ledger,
)

try:
    from bot.services import sku
    from bot.services.woo_client import Audit, WooClient, WooCommerceAPIError

    HAS_HTTPX = True
except Exception:  # pragma: no cover - httpx روی هاستِ اشتَری ممکن است نصب نباشد
    HAS_HTTPX = False

needs_httpx = unittest.skipUnless(HAS_HTTPX, "httpx is not installed")

BASE = "https://shop.example/wp-json/wc/v3/products"
# بدون wordpress_url افزونهٔ next-sku رد می‌شود (اعتبارنامه نیست) تا مسیر کش/اسکن تنها راه بماند.
NO_PLUGIN: dict[str, Any] = {
    "woocommerce_url": "https://shop.example",
    "woocommerce_key": "ck_test",
    "woocommerce_secret": "cs_test",
    "wordpress_url": "",
    "wordpress_username": "",
    "wordpress_app_password": "",
}


@needs_httpx
class TestSkuResolution(unittest.IsolatedAsyncioTestCase):
    """تعیین شماره، با کمترین درخواست ممکن و با تأیید هر ادعا.\""""

    def setUp(self) -> None:
        stack = contextlib.ExitStack()
        stack.enter_context(temp_ledger())          # ledger و sku_state.json، هر دو
        stack.enter_context(patched_settings(settings_with(**NO_PLUGIN)))
        stack.enter_context(no_sleep())
        self.addCleanup(stack.close)
        self.audit = Audit()
        self.script = TransportScript()

    async def resolve(
        self, *steps: Any, prefix: str = "BO", default: Any = (200, []), verify_candidate: bool = True
    ) -> str:
        """یک client روی اسکریپت ساختگی؛ `default` یعنی «هر چیز دیگری: جوابِ خالی».\""""
        self.script = TransportScript(*steps, default=default)
        async with WooClient(audit=self.audit, transport=self.script.transport(), attempts=1) as client:
            return await sku.next_free(
                client, BASE, prefix, self.audit, verify_candidate=verify_candidate
            )

    @property
    def lines(self) -> str:
        return "\n".join(self.audit.lines)

    async def test_first_product_scans_and_starts_at_one(self) -> None:
        sku_value = await self.resolve(respond(200, []))
        self.assertEqual("BO1", sku_value)
        self.assertIn("اسکن با search=«BO»", self.lines)
        self.assertIn("شروع جستجو از: BO1", self.lines)
        self.assertEqual("BO1", self.script.requests[-1].url.params["sku"], "کاندید باید قبل از دادن، تأیید شود")

    async def test_second_product_does_not_scan_again(self) -> None:
        await self.resolve(respond(200, []))                          # BO1
        scan_requests = self.script.sends
        sku_value = await self.resolve()                              # کش: بدون اسکن
        self.assertEqual("BO2", sku_value, "کش می‌گوید BO1 رفته؛ پس از BO2 ادامه بده")
        self.assertGreater(scan_requests, 1, "تست اول واقعاً اسکن کرد")
        self.assertEqual(1, self.script.sends, "فقط یک درخواست تأییدِ کاندید")
        self.assertNotIn("search", self.script.requests[0].url.params)
        self.assertIn("کش محلی", self.lines)

    async def test_a_taken_number_is_stepped_over_not_trusted(self) -> None:
        """مهم‌ترین ویژگی: کش «نقطهٔ شروع» است، نه مجوز.\""""
        sku.remember("BO", 10)
        await self.resolve(respond(200, [{"id": 5}]))                 # BO11 اشغال
        self.assertEqual("BO12", self.script.requests[-1].url.params["sku"])
        self.assertEqual(12, sku.cached_max("BO"), "کش هم به همان عددِ استفاده‌شده می‌رسد")
        self.assertIn("اشغال است", self.lines)

    async def test_empty_prefix_sends_nothing(self) -> None:
        self.assertEqual("", await self.resolve(prefix=""))
        self.assertEqual(0, self.script.sends)
        self.assertIn("پیشوند SKU خالی است", self.lines)

    async def test_probing_is_bounded_and_says_so(self) -> None:
        """۲۰۰ کاندیدِ اشغال یعنی جدول lookup قفل کرده؛ باید صریح بترکد، نه آهسته بمیرد.\""""
        with self.assertRaises(WooCommerceAPIError) as ctx:
            await self.resolve(default=(200, [{"id": 1}]))            # همهٔ کاندیدها اشغال
        self.assertIn("یافتن SKU آزاد برای پیشوند «BO» ممکن نشد", str(ctx.exception))
        probes = sum(1 for request in self.script.requests if "sku" in request.url.params)
        self.assertEqual(200, probes, "سقف پراب همان چیزی است که در کد نوشته شده — و بیشتر نه")

    async def test_fresh_local_high_water_mark_skips_plugin_and_uses_one_read(self) -> None:
        with patched_settings(settings_with(
            woocommerce_url="https://shop.example", woocommerce_key="ck", woocommerce_secret="cs",
            wordpress_url="https://shop.example", wordpress_username="admin",
            wordpress_app_password="aaaa bbbb",
        )):
            sku.remember("BO", 3)
            await self.resolve()
        self.assertEqual(1, self.script.sends, "کشِ تازه افزونه/اسکن را حذف می‌کند؛ فقط تأیید اختیاری می‌ماند")
        self.assertEqual("BO4", self.script.requests[0].url.params["sku"])
        self.assertNotIn("next-sku", self.script.requests[0].url.path)
        self.assertIn("کش محلی", self.lines)

    async def test_cold_cache_uses_the_plugin_before_scanning(self) -> None:
        with patched_settings(settings_with(
            woocommerce_url="https://shop.example", woocommerce_key="ck", woocommerce_secret="cs",
            wordpress_url="https://shop.example", wordpress_username="admin",
            wordpress_app_password="aaaa bbbb",
        )):
            await self.resolve(respond(200, {"sku": "BO900"}), respond(200, []))
        self.assertEqual("BO900", self.script.requests[-1].url.params["sku"])
        self.assertIn("افزونهٔ next-sku پاسخ داد", self.lines)
        self.assertNotIn("اسکن با search", self.lines, "جواب افزونه یعنی نیازی به اسکن نیست")

    async def test_writer_can_skip_the_non_reserving_exact_sku_read(self) -> None:
        with patched_settings(settings_with(
            woocommerce_url="https://shop.example", woocommerce_key="ck", woocommerce_secret="cs",
            wordpress_url="https://shop.example", wordpress_username="admin",
            wordpress_app_password="aaaa bbbb",
        )):
            sku.remember("BO", 3)
            value = await self.resolve(verify_candidate=False)
        self.assertEqual("BO4", value)
        self.assertEqual(0, self.script.sends, "POST خود محصول یکتایی را کنترل می‌کند")
        self.assertIn("یکتایی هنگام POST", self.lines)

    async def test_product_post_handles_a_stale_sku_without_probe_gets(self) -> None:
        import json

        from bot.services.woocommerce_direct import _create_with_sku_retry

        script = TransportScript(
            respond(400, {"message": "Invalid or duplicated SKU."}),
            respond(201, {"id": 77, "sku": "BO12"}),
        )
        audit = Audit()
        with patched_settings(settings_with(**NO_PLUGIN)):
            async with WooClient(audit=audit, transport=script.transport(), attempts=1) as client:
                response = await _create_with_sku_retry(
                    client, BASE, {"name": "قاب", "sku": "BO11"}, "BO", "BO11", audit
                )
        self.assertEqual(201, response.status_code)
        self.assertEqual(["POST /wp-json/wc/v3/products"] * 2, script.methods)
        self.assertEqual("BO11", json.loads(script.requests[0].content)["sku"])
        self.assertEqual("BO12", json.loads(script.requests[1].content)["sku"])


@needs_httpx
class TestSkuCache(unittest.TestCase):
    """خودِ کش: یکنوا رو به بالا، پوسیده، و بی‌خطر در برابر فایل خراب.\""""

    def setUp(self) -> None:
        stack = contextlib.ExitStack()
        stack.enter_context(temp_ledger())
        self.addCleanup(stack.close)

    def test_it_follows_the_configured_state_dir(self) -> None:
        """کش هم مثل بقیهٔ حالت، زیر `TISA_DATA_DIR` می‌نشیند (پاک‌کردنش کافی است)."""
        from bot.config import data_dir

        self.assertEqual(data_dir(), sku.DATA_DIR, "مسیر از config می‌آید، نه از یک هاردکد")
        self.assertEqual("sku_state.json", sku.STATE_FILE.name)

    def test_unknown_prefix_is_a_miss_not_a_borrow(self) -> None:
        sku.remember("BO", 14)
        self.assertEqual(14, sku.cached_max("BO"))
        self.assertIsNone(sku.cached_max("IP15"), "هر پیشوند شمارندهٔ خودش را دارد")

    def test_never_walks_backwards(self) -> None:
        sku.remember("BO", 50)
        sku.remember("BO", 3)
        self.assertEqual(50, sku.cached_max("BO"), "عددِ پایین‌تر یعنی اسکنِ بیهوده و برخورد قطعی")

    def test_expires(self) -> None:
        from bot.services import jsonstore

        sku.remember("BO", 40)
        state = jsonstore.read_json(sku.STATE_FILE, {})
        state["prefixes"]["BO"]["ts"] = time.time() - sku.CACHE_TTL_SECONDS - 1
        jsonstore.write_json(sku.STATE_FILE, state)
        jsonstore.invalidate(sku.STATE_FILE)
        self.assertIsNone(sku.cached_max("BO"), "SKU دستی‌افزوده‌شده در پیشخوان نباید ابدی نادیده بماند")

    def test_corrupt_file_is_ignored_and_rewritten(self) -> None:
        from bot.services import jsonstore

        sku.STATE_FILE.write_text("{not json at all", encoding="utf-8")
        jsonstore.invalidate(sku.STATE_FILE)
        self.assertIsNone(sku.cached_max("BO"))
        sku.remember("BO", 7)
        self.assertEqual(7, sku.cached_max("BO"), "کش خراب نباید انتشار را متوقف کند")

    def test_empty_prefix_is_not_stored(self) -> None:
        sku.remember("", 9)
        self.assertFalse(sku.STATE_FILE.exists(), "برای «بدون پیشوند» هیچ شمارنده‌ای معنا ندارد")

    def test_forget_for_tests_clears_the_cache(self) -> None:
        sku.remember("BO", 42)
        self.assertEqual(sku.cached_max("BO"), 42)
        sku.forget_for_tests()
        self.assertIsNone(sku.cached_max("BO"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
