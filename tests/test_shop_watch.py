"""🛰 فاز ۱۴: پایشِ خودکار — «خطا از ربات است یا سایت؟» نباید منتظرِ آدم بماند.

ردپای ۲۰۲۶-۱۰-۰۵: ساعت‌ها بین «ساخت محصول گیر می‌کند» و «خودِ هاست سرویس نمی‌داد» فاصله افتاد،
چون هر پرسش با یک آدمِ دکمه‌زننده جواب می‌گرفت. این سوئیت چهار قولِ ماژول را می‌گیرد:

* فقط *تغییر* خبر می‌شود (نه هر ۱۵ دقیقه یک «هنوز پایین است»)،
* بازگشت یعنی **تخلیهٔ صفِ ارسال**، نه فقط یک 🟢،
* هیچ‌وقت نمی‌پرسد وقتی پرسیدن دروغ است (dry-run، یا کلیدِ کامل‌نشده)،
* و ``/watch`` پاسخِ تازه می‌دهد، آن هم فقط به مدیر.

اجرا: ``python3 -m pytest tests/test_shop_watch.py`` یا ``python3 -m unittest discover -s tests``
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("SUDO_IDS", "1234567")

from _flow_harness import patched_settings, settings_with, temp_ledger

try:
    from bot.modules import shop_watch
    from bot.services.shop_network import ShopNetwork, ShopProbe

    HAS_WATCH = True
except Exception:                                       # pragma: no cover - PTB missing
    HAS_WATCH = False

needs_watch = unittest.skipUnless(HAS_WATCH, "python-telegram-bot is not installed")

SHARED = {
    "woocommerce_url": "https://shop.example",
    "woocommerce_key": "ck_test",
    "woocommerce_secret": "cs_test",
    "wordpress_url": "https://shop.example",
    "wordpress_username": "admin",
    "wordpress_app_password": "aaaa bbbb",
}
OWNER = 1234567            # همان SUDO_IDS بالا؛ پیامِ سودو باید به این برود
LOG_CHAT = -1001


def _network(ok: bool, verdict: str = "فروشگاه به هیچ درخواستی پاسخ نداد") -> ShopNetwork:
    probe = ShopProbe(label="صفحهٔ اصلی", path="/", status=None if not ok else 200,
                      ms=10400.0 if not ok else 120.0, detail="ReadTimeout" if not ok else "")
    return ShopNetwork(ok, verdict, "به میزبان بگویید.", (probe, probe, probe, probe), 10403.0)


def _answering(result: ShopNetwork):
    """A ``probe_shop_network`` that always returns ``result`` (no socket, no waiting)."""

    async def probe(*_args: object, **_kwargs: object) -> ShopNetwork:
        return result

    return probe


@needs_watch
class OnlyTransitionsAreNews(unittest.TestCase):
    """میزبانی که نه ساعت پایین است، خبرِ نه‌ساعته نمی‌فرستد."""

    def test_a_healthy_first_pass_is_silent(self) -> None:
        state, message = shop_watch.evaluate({}, ok=True, verdict="همه‌چیز جواب داد", now=1000.0)
        self.assertEqual("", message, "اولین پاسِ سبز نباید چت را شلوغ کند")
        self.assertTrue(state["ok"])

    def test_a_shop_that_was_already_down_is_said_out_loud(self) -> None:
        _state, message = shop_watch.evaluate({}, ok=False, verdict="هاست سرویس نمی‌دهد", now=1000.0)
        self.assertIn("از اولین بررسی", message)
        self.assertIn("هاست سرویس نمی‌دهد", message, "علت همان‌جا باشد که کسی تصمیم می‌گیرد")

    def test_an_outage_and_its_end_are_the_two_news(self) -> None:
        down, first = shop_watch.evaluate({"ok": True, "since": 0.0, "reminded": 0.0},
                                         ok=False, verdict="بی‌پاسخ", now=3600.0)
        self.assertIn("⛔", first)
        _back, second = shop_watch.evaluate(down, ok=True, verdict="همه جواب داد", now=5400.0)
        self.assertIn("🟢", second)
        self.assertIn("30 دقیقه", second, "چقدر پایین بود، بخشی از خودِ خبر است")

    def test_a_long_outage_says_one_reminder_and_nothing_else(self) -> None:
        state = {"ok": False, "since": 0.0, "reminded": 0.0, "verdict": "بی‌پاسخ"}
        messages = []
        for index in range(1, 37):                      # ۳۶ پاسِ ۱۵دقیقه‌ای = نه ساعت
            state, message = shop_watch.evaluate(
                state, ok=False, verdict="بی‌پاسخ", now=index * 900.0
            )
            if message:
                messages.append(message)
        self.assertEqual(1, len(messages), "یک یادآوری هر شش ساعت، نه بیشتر")
        self.assertIn("هنوز پایین است", messages[0])
        self.assertIn("6 ساعت", messages[0], "از «پایین بودنِ اول» شمارد، نه از یادآوری")

    def test_the_reminder_does_not_reset_the_outage_clock(self) -> None:
        state = {"ok": False, "since": 0.0, "reminded": 0.0}
        _state, message = shop_watch.evaluate(state, ok=False, verdict="x", now=6 * 3600.0)
        self.assertIn("6 ساعت", message)
        self.assertEqual(0.0, float(state["since"]), "سنِ قطعی با یادآوری صفر نمی‌شود")


@needs_watch
class ThePassItself(unittest.IsolatedAsyncioTestCase):
    """هر پاس: پرسیدن، نوشتنِ حالت، و تخلیهٔ صف در لحظهٔ بازگشت."""

    def setUp(self) -> None:
        self._stack = contextlib.ExitStack()
        self._stack.enter_context(temp_ledger())
        self._settings = patched_settings(
            settings_with(log_chat_id=LOG_CHAT, sudo_ids=(OWNER,), **SHARED)
        )
        self._settings.__enter__()
        self.tmp = Path(tempfile.mkdtemp(prefix="tisa-watch-"))
        self.state = self.tmp / "shop_watch.json"
        self.sent: list[dict] = []

        async def send_message(**kwargs: object) -> None:
            self.sent.append(dict(kwargs))

        self.app = type("App", (), {})()
        self.app.bot = type("Bot", (), {"send_message": staticmethod(send_message)})()
        self.context = type("Ctx", (), {"application": self.app})()
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        self._settings.__exit__(None, None, None)
        self._stack.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, result: ShopNetwork, **extra: object):
        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(shop_watch, "state_path", lambda: self.state))
        stack.enter_context(patch.object(shop_watch, "probe_shop_network", _answering(result)))
        for name, value in extra.items():
            stack.enter_context(patch.object(shop_watch.outbox_flow, name, value))
        return stack

    async def test_it_asks_nothing_in_a_rehearsal_or_without_keys(self) -> None:
        """سه حالتی که «سبز» دروغ است: خشک، بی‌کلید، یا خاموش — در هیچ‌کدام پرسیده نمی‌شود."""
        for over in ({"woo_dry_run": True}, {"woocommerce_key": ""}, {"shop_watch_minutes": 0}):
            with self.subTest(**over):
                self.sent.clear()
                with patched_settings(settings_with(**{**SHARED, **over})), \
                        patch.object(shop_watch, "state_path", lambda: self.state), \
                        patch.object(shop_watch, "probe_shop_network") as probe:
                    await shop_watch.check(self.context)
                probe.assert_not_called()
                self.assertEqual([], self.sent)
                self.assertFalse(self.state.exists(), "بی‌پرسش، بی‌نوشتن")

    async def test_a_green_pass_is_recorded_without_a_message(self) -> None:
        with self._run(_network(True)):
            await shop_watch.check(self.context)
        self.assertEqual([], self.sent)
        self.assertTrue(self.state.is_file(), "حالت باید بنشیند تا پاسِ بعدی مقایسه کند")

    async def test_the_first_red_pass_speaks_once_to_the_log_and_the_owner(self) -> None:
        with self._run(_network(False)):
            await shop_watch.check(self.context)
        chats = {int(item["chat_id"]) for item in self.sent}
        self.assertEqual({LOG_CHAT, OWNER}, chats, "لاگ‌چت برای رکورد، سودو برای بیدار‌ماندن")

    async def test_the_second_red_pass_is_quiet(self) -> None:
        with self._run(_network(False)):
            await shop_watch.check(self.context)
            self.sent.clear()
            await shop_watch.check(self.context)
        self.assertEqual([], self.sent, "دو پاسِ یکسان، یک خبر")

    async def test_a_recovery_drains_the_send_queue(self) -> None:
        drained: list[int] = []

        async def drain_once(app: object) -> int:
            drained.append(1)
            return 2

        with self._run(_network(False)):
            await shop_watch.check(self.context)
        self.sent.clear()
        with self._run(_network(True), drain_once=drain_once):
            await shop_watch.check(self.context)
        self.assertEqual([1], drained, "بازگشتِ سایت باید صف را بیدار کند، نه فقط خبر بدهد")
        texts = [str(item.get("text")) for item in self.sent]
        self.assertTrue(any("🟢" in text for text in texts), texts)
        self.assertTrue(any("2 مورد از صفِ ارسال تلاش شد" in text for text in texts), texts)

    async def test_start_schedules_the_sweep_only_when_it_can_ask(self) -> None:
        jobs: list[dict] = []

        class Queue:
            def run_repeating(self, callback: object, **kwargs: object) -> None:
                jobs.append({"callback": callback, **kwargs})

        app = type("App", (), {"job_queue": Queue()})()
        with patched_settings(settings_with(**SHARED, shop_watch_minutes=20)):
            self.assertEqual(0, await shop_watch.start(app))
        self.assertEqual(1, len(jobs))
        self.assertEqual(20 * 60, jobs[0]["interval"])
        jobs.clear()
        with patched_settings(settings_with(**SHARED, shop_watch_minutes=0)):
            await shop_watch.start(app)
        self.assertEqual([], jobs, "صفر یعنی خاموش، نه «هر صفر ثانیه»")


@needs_watch
class TheWatchCommand(unittest.IsolatedAsyncioTestCase):
    """/watch: پاسخِ تازه برای کسی که نمی‌خواهد تا پاسِ بعدی صبر کند — و فقط برای مدیر.

    فقط سودو: پرسیدنِ سایت بی‌خطر است، ولی جوابش را هر کسی نباید ببیند.
    """

    async def _reply(self, *, user_id: int, state: dict | None) -> list[str]:
        sent: list[str] = []

        async def reply_text(text: str, **kwargs: object) -> None:
            sent.append(text)

        update = type("U", (), {
            "effective_message": type("M", (), {"reply_text": staticmethod(reply_text)})(),
            "effective_user": type("U2", (), {"id": user_id})(),
        })()
        with patch.object(shop_watch, "read_state", lambda: dict(state or {})), \
                patch.object(shop_watch, "probe_shop_network", _answering(_network(False))):
            await shop_watch.cmd_watch(update, None)      # type: ignore[arg-type]
        return sent

    async def test_a_stranger_gets_no_answer_and_no_request(self) -> None:
        with patch.object(shop_watch, "probe_shop_network") as probe:
            sent = await self._reply(user_id=4242, state=None)
            probe.assert_not_called()
        self.assertIn("⛔", sent[0])

    async def test_the_owner_gets_the_card_with_the_watchdogs_age(self) -> None:
        now = time.time()
        sent = await self._reply(user_id=OWNER, state={"ok": False, "since": now - 3600, "reminded": now})
        card = sent[-1]
        self.assertIn("فروشگاه به هیچ درخواستی پاسخ نداد", card)
        self.assertIn("⛔ پایین است", card)
        self.assertIn("1 ساعت", card, "چقدر است که پایین است")

    async def test_a_fresh_install_says_it_has_not_looked_yet(self) -> None:
        card = shop_watch.watch_card(_network(True), {})
        self.assertIn("هنوز پاسی انجام نداده", card)

    def test_the_card_escapes_what_the_shop_said(self) -> None:
        result = ShopNetwork(False, "سایت گفت <b>down</b>", "", (ShopProbe("x", "/", 503, 1.0),), 1.0)
        card = shop_watch.watch_card(result, {"ok": False, "since": 0.0, "reminded": 0.0})
        self.assertIn("&lt;b&gt;down&lt;/b&gt;", card)


if __name__ == "__main__":
    unittest.main()
