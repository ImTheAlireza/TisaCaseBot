"""The safety preflight: it must fail fast, and it must say *which* thing is broken.

A real outage on 2026-10-05 looked exactly like this in the log group::

    📤 در حال آپلود عکس‌ها و ساخت پیش‌نویس مستقیم در ووکامرس...
    · [http:start] #1 GET /wp-json/wc/v3/tisa-health (تلاش 1/3؛ مهلت هر فاز=45s)
    · [http:error] #1 GET /wp-json/wc/v3/tisa-health پس از 46851 ms: ReadTimeout
    ⌛ جریان رها شد (تایم‌اوت) · — بی‌عنوان

Four things were wrong with that, and each is a test here:

* the one request that precedes *every* write spent the writer's whole 45 s budget, twice per
  queued attempt, and then the queue gave up on a shop we had not even touched;
* «پاسخ فروشگاه نرسید» was said about a request that could have been repeated safely — the no-
  retry-on-timeout rule exists for ``POST /products``, not for a read-only probe;
* a shop that answers nothing and a shop whose plugin does not register the route were the same
  sentence, although one is a bad minute on the host and the other is a deployment task;
* and the seller's silence while the queue kept knocking was recorded as an abandoned product.

Run: ``python3 -m pytest tests/test_publish_preflight.py`` (or the same file under
``unittest discover -s tests``).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("SUDO_IDS", "1234657")

import httpx

try:
    from _flow_harness import (
        TransportScript,
        context as make_context,
        no_sleep,
        message_update,
        patched_settings,
        query_update,
        settings_with,
        temp_ledger,
    )
except ImportError as exc:                                   # pragma: no cover - dev deps missing
    raise unittest.SkipTest("این فایل به وابستگی‌های dev (httpx/PTB) نیاز دارد") from exc

from bot.modules import product_flow as PF
from bot.services import outbox, plan as plan_service, woo_fencing
from bot.services.product_extractor import ProductData
from bot.services.woo_client import Audit, WooClient, WooCommerceAPIError, origin_of

BASE = "https://shop.example/wp-json/wc/v3/products"
HEALTH = {"contract": 1, "batch_fencing": True, "variation_fencing": True,
          "parent_cas": True, "stock_cas": True}


def _data(**over: object) -> ProductData:
    kwargs: dict[str, object] = {
        "title": "قاب چرم کرم باغ خرگوش",
        "price": 598_000,
        "sku_prefix": "AR",
        "models": ["iPhone 13", "iPhone 14"],
        "attributes": {"رنگ": ["کرم"]},
        "categories": ["قاب گوشی"],
    }
    kwargs.update(over)
    data = ProductData(**kwargs)  # type: ignore[arg-type]
    data.variation_count = plan_service.plan_from_dict(data.to_dict()).count
    return data


class Preflight(unittest.TestCase):
    """:func:`woo_fencing.require` against every way a shop can fail to answer."""

    def setUp(self) -> None:
        # The probe's backoff is real seconds in production and must not be in the suite —
        # the same seam `test_woo_client` uses for the client's own retries.
        self._real_sleep = woo_fencing._sleep

        async def no_sleep(_delay: float) -> None:
            return None

        woo_fencing._sleep = no_sleep
        self.addCleanup(setattr, woo_fencing, "_sleep", self._real_sleep)

    def _require(self, *steps: object):
        """Run the preflight over ``steps``: ``(error_or_None, audit_text, script, delays)``.

        ``no_sleep`` silences the *client's* backoff as well, so the retry policy stays visible
        (the delays are returned) without the suite paying for it.
        """
        script = TransportScript(*steps)  # type: ignore[arg-type]
        audit = Audit()

        async def run() -> WooCommerceAPIError | None:
            async with WooClient(audit=audit, transport=script.transport()) as client:
                try:
                    await woo_fencing.require(client, BASE)
                except WooCommerceAPIError as exc:
                    return exc
            return None

        with patched_settings(settings_with()), no_sleep() as delays:
            return asyncio.run(run()), audit.text(), script, delays

    # — the shop says nothing at all —
    def test_silence_is_a_queued_problem_not_a_plugin_lecture(self) -> None:
        dead = httpx.ReadTimeout("timed out")
        error, audit, script, _delays = self._require(dead, dead, dead)
        self.assertIsNotNone(error)
        assert error is not None
        self.assertEqual(503, error.status_code, "۵۰۳ تنها حالتی است که صف قبول می‌کند")
        self.assertTrue(outbox.is_transient(error), "سایت خوابیده خطای «دستی» نیست")
        self.assertTrue(outbox.is_silent(error), "«پاسخی نبود» همان حالتی است که صف نیم‌ساعت صبر می‌کند")
        self.assertIn("هیچ درخواستی", str(error))
        self.assertIn("entry processes", str(error), "باید بگوید کجا را نگاه کند")
        self.assertEqual(
            ["GET /wp-json/wc/v3/tisa-health"] * 2 + ["GET /wp-json/"], script.methods,
            "دو تلاش برای خودِ بررسی، و بعد *یک* سؤالِ زنده‌باش — نه بیشتر",
        )
        self.assertIn("بی‌پاسخ ماند", audit)

    def test_a_site_that_answers_without_our_keys_blames_the_host_rule(self) -> None:
        dead = httpx.ReadTimeout("timed out")
        error, audit, script, _delays = self._require(dead, dead, httpx.Response(200, json={}))
        assert error is not None
        self.assertEqual(503, error.status_code)
        self.assertTrue(outbox.is_silent(error), "قاعدهٔ فایروال هم با یک تلاشِ تازه درست نمی‌شود")
        self.assertIn("فایروال", str(error))
        self.assertIn("پاسخ داد", audit, "«سایت بیدار است» باید در ردپا خوانده شود")
        probe = script.requests[-1]
        self.assertNotIn("consumer_key", dict(probe.url.params),
                         "پروب زنده‌باش هیچ کلیدی در آدرس ندارد (وگرنه همان گیر را تکرار می‌کرد)")
        self.assertIn("consumer_key", dict(script.requests[0].url.params),
                      "خودِ بررسی ایمنی همان روش همیشگی را دارد: query-string auth")

    def test_a_socket_that_never_opens_is_not_asked_again(self) -> None:
        """A dead port needs no liveness probe: «connection failed» already is the answer.

        The client's own policy retries a connect error three times; the fence must not stack
        another round (or a probe) on top of that — every extra one is a second of waiting in a
        chat where somebody is watching a spinner.
        """
        refused = httpx.ConnectError("all connection attempts failed")
        error, audit, script, delays = self._require(refused, refused, refused)
        assert error is not None
        self.assertEqual(503, error.status_code)
        self.assertTrue(outbox.is_transient(error))
        self.assertIn("اتصال به فروشگاه برقرار نشد", str(error))
        self.assertEqual(["GET /wp-json/wc/v3/tisa-health"] * 3, script.methods)
        self.assertNotIn("GET /wp-json/", script.methods, "پروب بی‌مورد فرستاده نشد")
        self.assertNotIn(woo_fencing.PROBE_BACKOFF_SECONDS, delays,
                        "صفحهٔ بررسی ایمنی روی خطای اتصال خواب اضافه نمی‌کند")
        self.assertIn("بی‌پاسخ ماند", audit)

    # — the shop answers, and the answer is not our contract —
    def test_a_route_that_does_not_exist_is_never_queued(self) -> None:
        error, _audit, script, _delays = self._require(
            httpx.Response(404, json={"code": "rest_no_route", "message": "No route"})
        )
        assert error is not None
        self.assertEqual(412, error.status_code)
        self.assertFalse(outbox.is_transient(error), "۴۰۴ فردا هم ۴۰۴ است؛ صف نباید بیدار بماند")
        self.assertEqual(1, script.sends, "پاسخِ *دارا* ارزش تلاشِ دوبارهٔ بی‌مورد است")

    def test_a_busy_shop_on_the_probe_is_queued_like_any_5xx(self) -> None:
        error, _audit, script, _delays = self._require(
            httpx.Response(500, json={"message": "Internal Server Error"})
        )
        assert error is not None
        self.assertEqual(503, error.status_code)
        self.assertTrue(outbox.is_transient(error))
        self.assertFalse(outbox.is_silent(error), "۵۰ *پاسخ* است؛ backoff معمولی، نه نیم‌ساعت سکوت")
        self.assertIn("Internal Server Error", str(error), "علتِ خودِ سایت هم باید خوانده شود")
        self.assertEqual(1, script.sends)

    def test_refused_credentials_name_the_permission_that_is_missing(self) -> None:
        error, _audit, _script, _delays = self._require(
            httpx.Response(403, json={"code": "woocommerce_rest_cannot_view",
                                      "message": "Sorry, you cannot view these resources."})
        )
        assert error is not None
        self.assertEqual(403, error.status_code)
        self.assertFalse(outbox.is_transient(error))
        self.assertIn("ویرایش محصول", str(error))

    def test_an_old_plugin_is_reported_with_the_version_on_the_server(self) -> None:
        error, _audit, _script, _delays = self._require(
            httpx.Response(200, json={**HEALTH, "stock_cas": False, "version": "0.7.0"})
        )
        assert error is not None
        self.assertEqual(412, error.status_code)
        self.assertIn("0.7.0", str(error), "«افزونه را تازه کن» بدون عدد، حدس است")

    # — the two cases nobody may get wrong —
    def test_the_happy_path_costs_exactly_one_request(self) -> None:
        error, _audit, script, _delays = self._require(httpx.Response(200, json=HEALTH))
        self.assertIsNone(error)
        self.assertEqual(["GET /wp-json/wc/v3/tisa-health"], script.methods)

    def test_a_rehearsal_probes_nothing(self) -> None:
        script = TransportScript()

        async def run() -> None:
            async with WooClient(dry_run=True, transport=script.transport()) as client:
                await woo_fencing.require(client, BASE)

        asyncio.run(run())
        self.assertEqual(0, script.sends, "dry-run حتی نباید *بپرسد*")

    def test_the_probe_shortens_its_own_socket(self) -> None:
        """The audit line and the socket must agree; 45 s of waiting on a read is the bug."""
        dead = httpx.ReadTimeout("timed out")
        _error, audit, _script, _delays = self._require(dead, dead, dead)
        self.assertIn(f"مهلت هر فاز={woo_fencing.PROBE_TIMEOUT_SECONDS:g}s", audit)
        self.assertNotIn("مهلت هر فاز=45s", audit)

    def test_the_probe_is_bounded_below_the_client_budget(self) -> None:
        """A client with a *shorter* timeout must not be talked into a longer one."""
        script = TransportScript(httpx.Response(200, json=HEALTH))
        audit = Audit()

        async def run() -> None:
            async with WooClient(audit=audit, timeout=4.0, transport=script.transport()) as client:
                await woo_fencing.require(client, BASE)

        asyncio.run(run())
        self.assertIn("مهلت هر فاز=4s", audit.text())


class FlowAfterAFailedPreflight(unittest.IsolatedAsyncioTestCase):
    """What the seller's chat does — and does not do — when the shop never answered."""

    def setUp(self) -> None:
        self._stack = contextlib.ExitStack()
        self._stack.enter_context(temp_ledger())
        self.addCleanup(self._stack.close)
        self.tmp = Path(tempfile.mkdtemp(prefix="tisa-preflight-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for store in (PF.sessions, PF.album_buffers, PF.album_tasks):
            store.clear()
        self.addCleanup(self._clear)
        # The outbox's own files live next to the isolated DB (temp_ledger points it at tmp).
        self.addCleanup(shutil.rmtree, outbox.FILES_DIR, True)

    def _clear(self) -> None:
        for store in (PF.sessions, PF.album_buffers, PF.album_tasks):
            store.clear()
        outbox.clear_for_tests()

    def _session(self) -> PF.ProductSession:
        workspace = self.tmp / "ws"
        workspace.mkdir(parents=True, exist_ok=True)
        image = workspace / "01.jpg"
        image.write_bytes(b"z" * 64)
        session = PF.ProductSession(
            mode="new", data=_data(), model_text="قاب", info_text="AR 598t",
            files=[image], chat_id=9, workspace=workspace,
        )
        PF.sessions[7] = session
        return session

    async def test_a_blip_while_reporting_the_failure_still_queues_and_unlocks(self) -> None:
        """The whole point of the queue is this path; Telegram must not be able to eat it.

        The publish failed (the shop is silent) and then the *card edit* raised too — which used
        to escape as «خطای مدیریت‌نشدهٔ بات», with no queue row and ``submitting`` stuck on.
        """
        from telegram.error import NetworkError

        session = self._session()
        real = PF.create_draft

        async def silent_shop(*args: object, **kwargs: object) -> tuple[int, str]:
            raise httpx.ReadTimeout("timed out")

        async def angry_edit(*args: object, **kwargs: object) -> None:
            raise NetworkError("httpx.ReadError: ")

        update, seen = query_update("product:confirm", user_id=7, chat_id=9)
        update.callback_query.edit_message_text = angry_edit       # type: ignore[method-assign]
        PF.create_draft = silent_shop                               # type: ignore[assignment]
        self.addCleanup(setattr, PF, "create_draft", real)
        try:
            with patched_settings(settings_with()):
                result = await PF.confirm(update, make_context())
        finally:
            PF.create_draft = real

        self.assertEqual(PF.REVIEW, result, "جریان باز می‌ماند تا مالک دوباره بزند")
        self.assertFalse(session.submitting, "نشست قفل نمانده: «یک ساخت در جریان است» نباید بمیرد")
        self.assertEqual(1, outbox.pending(), "خطای موقتِ بدونِ نوشتن، در صف می‌نشیند")
        self.assertTrue(outbox.is_queued(session.queued_batch),
                        "نشست می‌داند کدام قلم را صف نگه داشته — تایمرِ بی‌فعالیتی از همین می‌پرسد")
        self.assertTrue(any(item[0] == "answer" for item in seen), "تپ باید پاسخ گرفته باشد، هرچقدر هم بی‌متن")

    async def test_idle_timeout_does_not_call_a_queued_publish_abandoned(self) -> None:
        """900 s of silence while the queue retries is not an abandoned product."""
        session = self._session()
        batch = "a" * 24
        self.assertTrue(outbox.enqueue(
            batch_id=batch, chat_id=9, user_id=7, payload=_data().to_dict(),
            images=[], delay=600, error="ReadTimeout",
        ))
        session.queued_batch = batch

        counted: list[str] = []
        real_note = PF.metrics.note_abandoned
        PF.metrics.note_abandoned = lambda kind: counted.append(kind)  # type: ignore[assignment]
        self.addCleanup(setattr, PF.metrics, "note_abandoned", real_note)

        update, sent = message_update("", user_id=7, chat_id=9)
        with patched_settings(settings_with()):
            await PF.on_timeout(update, make_context())

        self.assertEqual([], counted, "شمارندهٔ «رهاشده» برای یک قلمِ در صف نباید تکان بخورد")
        text = str(sent[-1][1]) if sent else ""
        self.assertIn("صفِ تلاشِ دوباره", text)
        self.assertNotIn("به‌خاطر بی‌فعالیت بسته شد", text)
        self.assertNotIn(7, PF.sessions, "نشست و فایل‌های موقتش با این حال پاک می‌شوند")
        self.assertEqual(1, outbox.pending(), "صف لغو نشد؛ همان قلم سر جای خودش است")

    async def test_idle_timeout_still_abandons_an_ordinary_quiet_flow(self) -> None:
        """The other half: with nothing in the queue, the old sentence must survive."""
        self._session()
        counted: list[str] = []
        real_note = PF.metrics.note_abandoned
        PF.metrics.note_abandoned = lambda kind: counted.append(kind)  # type: ignore[assignment]
        self.addCleanup(setattr, PF.metrics, "note_abandoned", real_note)

        update, sent = message_update("", user_id=7, chat_id=9)
        with patched_settings(settings_with()):
            await PF.on_timeout(update, make_context())

        self.assertEqual(["product"], counted)
        self.assertIn("به‌خاطر بی‌فعالیت بسته شد", str(sent[-1][1]))


class OriginParsing(unittest.TestCase):
    """The probe must not invent a host, and must not forward one's credentials."""

    def test_the_origin_keeps_scheme_host_and_port_only(self) -> None:
        self.assertEqual("https://shop.example", origin_of(BASE))
        self.assertEqual("http://shop.example:8080",
                         origin_of("http://admin:pw@shop.example:8080/wp-json/wc/v3/products"))
        self.assertEqual("", origin_of("not a url"))
        self.assertEqual("", origin_of(""))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
