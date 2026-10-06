"""The no-terminal shop diagnosis: four GETs, four different verdicts, zero writes.

Someone running the bot on a shared host with no SSH must be able to answer «چرا منتشر نمی‌شود؟»
from Telegram alone. What this service promises — and what these tests hold it to:

* every question is a ``GET`` (so pressing it during an outage cannot double a product);
* exactly one of the four carries credentials — the rest must answer *without* them, otherwise a
  firewall that blocks key-bearing URLs and a dead host look identical;
* «no answer» and «answered 404» produce different sentences with different next steps.
"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("SUDO_IDS", "1234567")

import httpx

try:
    from _flow_harness import patched_settings, settings_with
except ImportError as exc:                                   # pragma: no cover - dev deps missing
    raise unittest.SkipTest("این فایل به وابستگی‌های dev (httpx/PTB) نیاز دارد") from exc

from bot.services.shop_network import ShopProbe, _read_verdict, probe_shop_network

HANG = "hang"
FENCE = "/wp-json/wc/v3/tisa-health"
PRODUCTS = "/wp-json/wc/v3/products"
CONTRACT = {"contract": 1, "batch_fencing": True, "variation_fencing": True,
            "parent_cas": True, "stock_cas": True, "version": "0.8.1"}


def _shop(home: object = 200, rest: object = 401, fence_open: object = 404,
          fence_keyed: object = 200) -> tuple[httpx.MockTransport, list[tuple[str, str, bool]]]:
    """A shop that answers each of the four questions in its own way (``"hang"`` = silence).

    Keyed on *path + whether our keys were in the query string*, because the probes run
    concurrently and anything that depends on their order would flake.
    """
    calls: list[tuple[str, str, bool]] = []

    def answer(spec: object) -> httpx.Response:
        if spec is HANG:
            raise httpx.ReadTimeout("timed out")
        if isinstance(spec, int):
            return httpx.Response(spec, json={} if spec >= 400 else {"name": "TisaCase"})
        return httpx.Response(200, json=dict(spec))

    def handle(request: httpx.Request) -> httpx.Response:
        keyed = "consumer_key" in dict(request.url.params)
        calls.append((request.method, request.url.path, keyed))
        if request.url.path == FENCE:
            return answer(fence_keyed if keyed else fence_open)
        if request.url.path == PRODUCTS:
            return answer(rest)
        return answer(home)

    return httpx.MockTransport(handle), calls


def _probe(spec: object, detail: str | None = None) -> ShopProbe:
    """A probe shaped like an answer. ``detail`` defaults to «the plugin said it is ready».

    The default matters: the green verdict is not just «HTTP 200», so a test that wants a 200
    *without* a contract has to say so — which is exactly the case worth pinning down.
    """
    if spec is HANG:
        return ShopProbe("x", "/x", None, 12.0, "ReadTimeout", answered=False)
    status = spec if isinstance(spec, int) else 200
    if detail is None:
        detail = f"افزونه {CONTRACT['version']} · قرارداد کامل" if status == 200 else ""
    return ShopProbe("x", "/x", status, 5.0, detail)


class Verdicts(unittest.TestCase):
    """The sentences are the product. This checks each shape gets its own."""

    def _say(self, **spec: object) -> tuple[bool, str, str]:
        return _read_verdict(_probe(spec.get("home", 200)), _probe(spec.get("rest", 401)),
                             _probe(spec.get("fence_open", 404)), _probe(spec.get("fence_keyed", HANG)))

    def test_silence_everywhere_is_the_host(self) -> None:
        ok, verdict, fix = self._say(home=HANG, rest=HANG, fence_open=HANG)
        self.assertFalse(ok)
        self.assertIn("هاست", verdict)
        self.assertIn("entry processes", fix, "باید بداند در پنل چه چیزی را ببیند")
        self.assertIn("صفِ تلاشِ دوباره", fix, "باید بداند محصولش گم نشده است")

    def test_silence_only_with_keys_is_the_firewall(self) -> None:
        ok, verdict, fix = self._say()
        self.assertFalse(ok)
        self.assertIn("فایروال", verdict)
        self.assertIn("ModSecurity", verdict)
        self.assertIn("consumer_secret", fix)

    def test_a_missing_route_blames_the_plugin_not_the_host(self) -> None:
        ok, verdict, fix = self._say(fence_keyed=404)
        self.assertFalse(ok)
        self.assertIn("tisa-health", verdict)
        self.assertIn("wp-admin", fix, "راه‌حل باید بدون ترمینال باشد")

    def test_refused_credentials_point_at_the_key_tester(self) -> None:
        ok, verdict, _fix = self._say(fence_keyed=403)
        self.assertFalse(ok)
        self.assertIn("403", verdict)

    def test_a_full_contract_is_the_only_green(self) -> None:
        ok, verdict, _fix = _read_verdict(_probe(200), _probe(401), _probe(401),
                                         _probe(200, "پاسخ ۲۰۰ ولی بی‌قرارداد"))
        self.assertFalse(ok, "HTTP 200 به‌تنهایی سبز نیست: باید قرارداد را هم بگوید")
        self.assertIn("غیرمنتظره", verdict)
        ok, verdict, fix = _read_verdict(_probe(200), _probe(401), _probe(401), _probe(200))
        self.assertTrue(ok)
        self.assertIn("0.8.1", verdict, "نسخهٔ فعالِ افزونه باید خوانده شود")
        self.assertEqual("", fix)


class Probing(unittest.IsolatedAsyncioTestCase):
    async def _run(self, **spec: object):
        transport, calls = _shop(**spec)
        with patched_settings(settings_with()):
            result = await probe_shop_network(transport=transport)
        return result, calls

    async def test_exactly_one_request_carries_credentials(self) -> None:
        result, calls = await self._run()
        self.assertEqual([("GET", "/", False), ("GET", PRODUCTS, False),
                          ("GET", FENCE, False), ("GET", FENCE, True)],
                         sorted_by(calls), f"ترتیب مهم نیست، هویت هر چهارتا مهم است: {calls}")
        self.assertEqual(4, len(calls))

    async def test_nothing_is_written_and_the_card_says_so(self) -> None:
        _result, calls = await self._run()
        self.assertEqual({"GET"}, {method for method, _path, _keyed in calls})

    async def test_the_report_lists_every_probe_with_its_timing(self) -> None:
        result, _calls = await self._run(home=HANG, rest=HANG, fence_open=HANG, fence_keyed=HANG)
        lines = result.report().splitlines()
        self.assertEqual(4, sum(1 for line in lines if line.startswith("⏳")),
                         "هر چهار سؤال باید خط خودش را داشته باشد")
        self.assertIn("بی‌پاسخ", result.report())
        self.assertEqual(4, len(result.probes))
        self.assertTrue(result.elapsed_ms >= 0)

    async def test_a_healthy_shop_answers_green(self) -> None:
        result, _calls = await self._run(home=200, rest=401, fence_open=401, fence_keyed=CONTRACT)
        self.assertTrue(result.ok, result.verdict)
        self.assertIn("0.8.1", result.verdict)

    async def test_summary_is_numbers_not_prose(self) -> None:
        result, _calls = await self._run(fence_keyed=HANG)
        self.assertIn("tisa-health=بی‌پاسخ", result.summary)
        self.assertIn("products=401", result.summary)


def sorted_by(calls: list[tuple[str, str, bool]]) -> list[tuple[str, str, bool]]:
    return sorted(calls, key=lambda call: (call[1], call[2]))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
