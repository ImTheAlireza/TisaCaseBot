"""فاز ۸: شمارنده‌های عملیاتی، کارتِ جمع‌وجورِ محصول، و صفحه‌های «📊 وضعیت» / «🩺 عیب‌یابی».

سه چیز اینجا میخ می‌خورد، چون همان‌ها هستند که در بقیهٔ پروژه «صفحه هست ولی داده نیست»
می‌سازند:

* :mod:`bot.services.metrics` — هر کلید یک نویسنده و یک خواننده دارد؛ عددی که هیچ‌جا
  نوشته نشود در صفحه هم نمی‌آید، و خرابیِ دیتابیس نباید انتشار را بشکند؛
* :mod:`bot.services.product_journal` — خط‌های جریان **ثبت** می‌شوند و در پایانِ محصول
  به‌صورت **یک کارت** می‌روند (قبلاً چهارده پیام پراکنده بود که عملاً هیچ‌کس نمی‌خواند);
* :mod:`bot.modules.ops` — وضعیت هیچ درخواست شبکه‌ای نمی‌زند، عیب‌یابی هیچ بررسی
  ناتمامی را «موفق» گزارش نمی‌کند، و هر دو صفحه فقط برای سودو باز می‌شوند.
"""

from __future__ import annotations

import asyncio
import csv
import io
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("SUDO_IDS", "1234567")

import _flow_harness as h

from bot.constants import CB

try:
    from bot import rbac
    from bot.buttons import BY_KEY, feature_allowed
    from bot.modules import ops
    from bot.services import product_journal, sku
    from bot.services.product_journal import Journal

    HAS_FLOW = True
except Exception:  # pragma: no cover - PTB/httpx نصب نباشد، صفحه‌ها تست نمی‌شوند
    HAS_FLOW = False

needs_flow = unittest.skipUnless(HAS_FLOW, "python-telegram-bot is not installed")

SUDO = 1234567
STRANGER = 7
REPO = Path(__file__).resolve().parents[1]


def run(coroutine):
    return asyncio.run(coroutine)


# --- شمارنده‌ها ----------------------------------------------------------------


@needs_flow
class TestMetrics(unittest.TestCase):
    def test_counts_accumulate_across_writers(self):
        with h.temp_metrics() as m:
            m.incr("products_created")
            m.incr("products_created", by=2)
            self.assertEqual(m.snapshot()["products_created"][0], 3)

    def test_schema_and_wal_are_initialized_once_per_database_file(self):
        from unittest import mock

        with h.temp_metrics() as m, mock.patch.object(m, "_initialize", wraps=m._initialize) as initialize:
            m.incr("products_created")
            m.incr("products_created")
            m.snapshot()
            self.assertEqual(1, initialize.call_count)

    def test_schema_is_recreated_if_the_database_file_is_removed(self):
        with h.temp_metrics() as m:
            m.incr("products_created")
            m.DB_PATH.unlink()
            m.incr("products_created")
            self.assertEqual(1, m.snapshot()["products_created"][0])

    def test_a_key_nobody_declared_is_dropped_not_written(self):
        with h.temp_metrics() as m:
            m.incr("typo_in_a_caller")
            self.assertEqual(m.snapshot(), {})
            self.assertEqual(m.probe(), (0, ""))

    def test_latency_keeps_mean_and_peak_not_just_a_total(self):
        with h.temp_metrics() as m:
            m.observe("ai_latency_ms", 100.0)
            m.observe("ai_latency_ms", 300.0)
            count, total, peak, _updated = m.snapshot()["ai_latency_ms"]
            self.assertEqual((count, total, peak), (2, 400.0, 300.0))
            line = next(row for row in m.status_lines() if "⏱" in row)
            self.assertIn("200 ms میانگین", line)
            self.assertIn("300 ms بیشینه", line)

    def test_the_status_card_lists_only_what_actually_happened(self):
        with h.temp_metrics() as m:
            self.assertEqual(m.status_lines(), [])
            m.incr("products_created", by=4)
            m.incr("ai_calls", by=4)
            m.incr("ai_failures")
            lines = m.status_lines()
            self.assertEqual(len(lines), 4)  # سه شمارنده + یک نسبتِ حساب‌شده
            self.assertTrue(any("محصول ساخته‌شده: 4" in line for line in lines))
            self.assertTrue(any("نرخ شکست هوش مصنوعی: 25.0٪" in line for line in lines))

    def test_a_missing_ratio_is_absent_not_zero(self):
        with h.temp_metrics() as m:
            self.assertIsNone(m.rate("ai_failures", "ai_calls"))
            m.incr("ai_calls", by=2)
            self.assertEqual(m.rate("ai_failures", "ai_calls"), 0.0)

    def test_rehearsal_does_not_inflate_the_shop_numbers(self):
        with h.temp_metrics() as m:
            with h.patched_settings(h.settings_with(woo_dry_run=True)):
                m.incr_shop("images_uploaded", by=9)
                m.observe_shop("ai_latency_ms", 5.0)
            self.assertEqual(m.snapshot(), {})
            m.incr_shop("images_uploaded")
            self.assertEqual(m.snapshot()["images_uploaded"][0], 1)
            self.assertEqual(m.probe()[0], 1)

    def test_denials_and_abandons_are_counted_with_their_place(self):
        with h.temp_metrics() as m, self.assertLogs("bot.services.metrics", level="INFO") as logs:
            m.note_denial("ops")
            m.note_denial("admins")
            m.note_abandoned("product flow")
            seen = m.snapshot()
        self.assertEqual(seen["buttons_denied"][0], 2)
        self.assertEqual(seen["flow_abandoned"][0], 1)
        # عددِ تنهایی تصمیم‌ساز نیست؛ «کجا» باید در لاگ بماند
        self.assertTrue(any("ops" in line for line in logs.output))

    def test_export_is_one_row_per_declared_counter(self):
        with h.temp_metrics() as m:
            m.incr("tracking_converted", by=2)
            rows = list(csv.reader(io.StringIO(m.export_csv())))
        self.assertEqual(rows[0], ["key", "counter", "n", "total", "peak", "updated_iso"])
        keys = [row[0] for row in rows[1:]]
        self.assertEqual(keys, list(m.COUNTERS) + list(m.RATIOS))
        converted = next(row for row in rows[1:] if row[0] == "tracking_converted")
        self.assertEqual(converted[2], "2")
        # نسبتِ بی‌داده باید خالی بماند، نه «۰٪»: «اصلاً فراخوانی نشده» با
        # «همه سالم بود» یک عدد نیست.
        ratio = next(row for row in rows[1:] if row[0] == "ai_failure_rate")
        self.assertEqual(ratio[2], "")

    def test_a_broken_database_costs_numbers_but_nothing_else(self):
        with h.temp_metrics() as m:
            m.DB_PATH.write_bytes(b"not a database at all")
            self.assertIn("متریک", m.probe()[1])
            self.assertEqual(m.snapshot(), {})
            self.assertEqual(m.status_lines(), [])
            m.incr("products_created")  # بلعیده می‌شود؛ انتشار نباید بشکند
            self.assertEqual(m.DB_PATH.read_bytes(), b"not a database at all")

    def test_the_products_ledger_is_the_writer_for_published_products(self):
        # شمارنده از *همان* جایی می‌آید که کاربر «منتشر شد» را می‌خواند (دفترِ محصولات)،
        # پس اگر روزی دفتر و صفحه جدا بشمارند، این تست می‌گیردش.
        with h.temp_ledger(), h.temp_metrics() as m:
            from bot.services import products_ledger

            products_ledger.record(user_id=SUDO, status="created", product_id=800001, variations=2)
            products_ledger.record(user_id=SUDO, status="queued")
            products_ledger.record(user_id=SUDO, status="failed", error="500")
            products_ledger.record(user_id=SUDO, status="dry")
            seen = m.snapshot()
        self.assertEqual(seen["products_created"][0], 1)  # یک رکورد = یک محصول
        self.assertEqual(seen["variations_created"][0], 2)  # واریژن‌ها جدا شمرده می‌شوند
        self.assertEqual(seen["publish_queued"][0], 1)
        self.assertEqual(seen["publish_failed"][0], 1)
        self.assertNotIn("dry", str(seen))

    def test_an_open_intent_is_counted_when_it_closes_and_not_twice(self):
        # مسیرِ انتشار اول کارت را «pending» می‌نویسد و بعد با update می‌بندد؛ اگر
        # شمارنده فقط در record بود، «دیشب چند محصول ساخته شد» عملاً صفر می‌ماند.
        with h.temp_ledger(), h.temp_metrics() as m:
            from bot.services import products_ledger

            products_ledger.record(user_id=SUDO, status="pending", key="k1")
            self.assertEqual(m.snapshot(), {})  # حسابِ باز عدد نمی‌سازد
            products_ledger.update("k1", status="created", product_id=7, variations=3)
            products_ledger.update("k1", status="created", product_id=7)  # تلاشِ دوبارهٔ صف
            seen = m.snapshot()
        self.assertEqual(seen["products_created"][0], 1)
        self.assertEqual(seen["variations_created"][0], 3)

    def test_every_counter_has_a_writer_in_the_code(self):
        # یک شمارندهٔ بی‌نویسنده در صفحه «همیشه صفر» می‌ماند و آدم را به سمتِ باگِ
        # اشتباه می‌فرستد؛ پس هر کلید باید جای دیگری هم نوشته شده باشد.
        from bot.services import metrics

        helpers = {"flow_abandoned": "note_abandoned", "buttons_denied": "note_denial"}
        sources = {path.name: path.read_text(encoding="utf-8")
                   for path in sorted((REPO / "bot").rglob("*.py"))
                   if path.name != "metrics.py"}
        for name, counter in metrics.COUNTERS.items():
            needle = helpers.get(name, f'"{name}"')  # دو شمارنده از راهِ helper نوشته می‌شوند
            hits = [file for file, text in sources.items() if needle in text]
            self.assertTrue(hits, f"شمارندهٔ {counter.key} هیچ‌جا نوشته نمی‌شود")


# --- کارتِ محصول --------------------------------------------------------------


@needs_flow
class TestProductJournal(unittest.TestCase):
    def test_the_card_is_one_message_built_from_what_really_happened(self):
        journal = Journal()
        journal.stage("📥 دریافت ۳ تصویر")
        journal.stage("🧠 استخراج متن")
        journal.fact(title="کفش رانینگ", product_id=800001, variations=2, images=3)
        journal.warn("قیمت از سقف رد شد")
        card = journal.card("created")
        self.assertIn("✅ محصول ساخته شد · کفش رانینگ", card)
        self.assertIn("🆔 محصول 800001", card)
        self.assertIn("🎨 2 واریژن", card)
        self.assertIn("🧭 📥 دریافت ۳ تصویر ← 🧠 استخراج متن", card)
        self.assertIn("⚠️ 1 هشدار", card)
        self.assertIn("قیمت از سقف رد شد", card)

    def test_every_product_card_contains_compact_http_and_ai_diagnostics(self):
        journal = Journal()
        journal.line(
            "[http:done] #1 GET /wp-json/wc/v3/products → HTTP 200 در 120 ms"
        )
        journal.line(
            "[http:done] #2 POST /wp-json/wc/v3/products → HTTP 201 در 80 ms"
        )
        journal.line("[retry] #3 درخواست پس از 1s تکرار شد")
        journal.line("[ai:summary] استخراج محصول: 2 درخواست، 900 ms")
        journal.line("[ai:diagnostic] نرمال‌سازی مدل: خطای ReadTimeout؛ پارسر قطعی حفظ شد")
        journal.line("[http:error] #4 GET /wp-json/wc/v3/categories پس از 500 ms: ConnectError")
        card = journal.card("created")
        self.assertIn("HTTP: 2 درخواست؛ جمع پاسخ‌ها 200 ms", card)
        self.assertIn("تلاش مجدد شبکه: 1", card)
        self.assertIn("استخراج محصول: 2 درخواست، 900 ms", card)
        self.assertIn("خطای ReadTimeout", card)
        self.assertIn("[http:error] #4", card)

    def test_a_fact_the_flow_never_reached_reads_as_a_dash_not_zero(self):
        journal = Journal()
        journal.line("یک خط trace")
        card = journal.card("queued")
        self.assertIn("🕐 به صفِ تلاشِ دوباره رفت", card)
        self.assertIn("— واریژن", card)
        self.assertNotIn("0 واریژن", card)

    def test_repeated_status_lines_do_not_bloat_the_card(self):
        journal = Journal()
        for _ in range(6):
            journal.stage("⏳ در حال انتشار")
        self.assertEqual(len(journal.stages), 1)

    def test_a_long_card_says_it_was_cut(self):
        journal = Journal()
        journal.fact(title="ع" * 4000)
        card = journal.card("created")
        self.assertLessEqual(len(card), 3900)
        self.assertIn("کارت بلند بود", card)

    def test_recording_a_step_sends_nothing_and_flush_sends_exactly_one_card(self):
        context = h.context()
        with h.patched_settings(h.settings_with(log_chat_id=-100123)):
            from bot.modules import product_flow

            run(product_flow._telegram_log(context, "[product:9] عکس ۱ دریافت شد"))
            run(product_flow._telegram_log(context, "[product:9] مدل‌ها: x"))
            self.assertEqual(context.bot.messages, [])  # دیگه پیامِ پر‌شونده فرستاده نمی‌شود
            journal = product_journal.journal_for(context)
            self.assertEqual(len(journal.trace), 2)
            self.assertIsNotNone(run(product_journal.flush(context, status="created")))
        self.assertEqual(len(context.bot.messages), 1)
        self.assertIn("✅ محصول ساخته شد", context.bot.messages[0]["text"])
        self.assertEqual(context.bot.messages[0]["chat_id"], -100123)
        # یک‌بار flush یعنی یک‌بار: کارتِ دوم برای همان محصول نباید برود
        self.assertIsNone(run(product_journal.flush(context, status="created")))
        self.assertEqual(len(context.bot.messages), 1)

    def test_verbose_log_adds_the_trace_as_extra_messages(self):
        context = h.context()
        with h.patched_settings(h.settings_with(log_chat_id=-100123, verbose_log=True)):
            journal = product_journal.journal_for(context)
            journal.line("جزئیات ۱")
            journal.line("جزئیات ۲")
            run(product_journal.flush(context, status="failed"))
        self.assertEqual(len(context.bot.messages), 2)
        self.assertIn("❌ منتشر نشد", context.bot.messages[0]["text"])
        self.assertIn("جزئیات ۱", context.bot.messages[1]["text"])

    def test_without_a_log_chat_the_flow_keeps_recording_and_warns(self):
        context = h.context()
        with h.patched_settings(h.settings_with(log_chat_id=None)), self.assertLogs(
            "bot.services.product_journal", level="WARNING"
        ) as logs:
            journal = product_journal.journal_for(context)
            journal.line("چیزی که فقط در logs/bot.log می‌ماند")
            self.assertIsNotNone(run(product_journal.flush(context, status="dry")))
        self.assertEqual(context.bot.messages, [])
        self.assertIn("LOG_CHAT_ID is empty", " ".join(logs.output))

    def test_a_context_without_chat_data_is_not_an_error(self):
        # جریان‌هایی که chat_data ندارند (هندلرِ تنها، تستِ جدا) نباید بترکند.
        self.assertIsNone(product_journal.journal_for(SimpleNamespace()))
        self.assertIsNone(run(product_journal.flush(SimpleNamespace(), status="dry")))


# --- صفحه‌ها -------------------------------------------------------------------


@needs_flow
class TestStatusScreen(unittest.TestCase):
    def test_status_is_rendered_without_touching_the_network(self):
        # «وضعیت» باید همان لحظه‌ای هم که شبکه مرده کار کند؛ پس هر درخواستِ واقعی
        # در این تست عمداً می‌ترکد.
        async def no_network(*_args, **_kwargs):
            raise AssertionError("«📊 وضعیت» نباید درخواست شبکه بزند")

        # httpx زیرِ پای هر دو است (هم PTB و هم WooClient)؛ یک patch کافی است.
        with mock.patch("httpx.AsyncClient.send", no_network), h.temp_ledger(), h.temp_metrics() as m:
            m.incr("products_created", by=3)
            text = ops.status_text()
        self.assertIn("<b>وضعیت</b>", text)
        self.assertIn("محصول ساخته‌شده: 3", text)
        self.assertIn("صف ارسال", text)
        self.assertIn("سقف‌ها", text)

    def test_every_line_of_the_config_trouble_is_shown(self):
        with h.temp_ledger(), h.temp_metrics(), h.patched_settings(
            h.settings_with(problems=("WOOCOMMERCE_KEY تنظیم نشده",))
        ):
            text = ops.status_text()
        self.assertIn("⚠️", text)
        self.assertIn("WOOCOMMERCE_KEY تنظیم نشده", text)

    def test_a_missing_sudo_id_is_reported_as_a_blocker(self):
        with h.patched_settings(h.settings_with(sudo_ids=())):
            line = ops._role_check().line()
        self.assertIn("🔴", line)
        self.assertIn("SUDO_IDS", line)

    def test_a_log_chat_that_is_not_set_is_explained_not_hidden(self):
        with h.patched_settings(h.settings_with(log_chat_id=None)):
            check = ops._log_check()
        self.assertEqual(check.state, "warn")
        self.assertIn("LOG_CHAT_ID", check.fix)
        with h.patched_settings(h.settings_with(log_chat_id=-100123456, verbose_log=True)):
            check = ops._log_check()
        self.assertEqual(check.state, "ok")
        self.assertIn("VERBOSE_LOG", check.detail)

    def test_data_inside_the_code_directory_is_warned_about(self):
        # با `git clean -fdx` یا خروجِ dapper، تاریخچهٔ SKU و صف ارسال داخلِ کد می‌میرند.
        with mock.patch("bot.modules.ops.data_dir", return_value=REPO / "data"):
            check = ops._state_dir_check()
        self.assertEqual(check.state, "warn")
        self.assertIn("TISA_DATA_DIR", check.fix)
        with mock.patch("bot.modules.ops.data_dir", return_value=Path("/var/lib/tisaposttowp")):
            self.assertEqual(ops._state_dir_check().state, "ok")

    def test_a_quiet_queue_in_dry_run_says_why_it_is_quiet(self):
        # «۰ در صف» در dry-run دو معنا دارد: چیزی نیست، یا تخلیه خاموش است.
        with h.temp_ledger(), h.temp_metrics(), h.patched_settings(h.settings_with(woo_dry_run=True)):
            lines = "\n".join(check.line() for check in ops._store_checks())
        self.assertIn("صف تخلیه نمی‌شود", lines)
        with h.temp_ledger(), h.temp_metrics(), h.patched_settings(h.settings_with(woo_dry_run=False)):
            lines = "\n".join(check.line() for check in ops._store_checks())
        self.assertNotIn("صف تخلیه نمی‌شود", lines)

    def test_stuck_flows_show_up_as_their_own_line(self):
        with mock.patch("bot.modules.ops.flow_state.pending", return_value=[{"a": 1}, {"b": 2}]):
            lines = [check.line() for check in ops._store_checks()]
        self.assertTrue(any("جریان‌های نیمه‌کاره" in line and "2 جریان" in line for line in lines))


@needs_flow
class TestDiagnose(unittest.TestCase):
    """یک کارت از همهٔ بررسی‌ها — پس هر تست باید فقط *یک* چیز را عوض کند."""

    @contextmanager
    def _green(self, **over):
        """همه‌جا «سبز» مگر همان یک موردی که تست می‌خواهد خراب باشد.

        `shutil.which` هم همین‌جا supervisorctl را می‌بیند، وگرنه در CIِ بی‌supervisor
        خودِ صفحه 🟡 می‌شود و شمارشِ خطوطِ کارت به محیطِ تست وابسته می‌ماند.
        """

        async def ok_token(bot):
            return ops.Check("توکن تلگرام", "ok", "@tisa")

        async def ok_chat(bot):
            return ops.Check("چت لاگ", "ok", "لاگ")

        async def ok_woo():
            return ops.Check("WooCommerce", "ok", "HTTP 200")

        async def ok_wp():
            return ops.Check("رسانهٔ وردپرس", "ok", "آپلود و حذف شد")

        async def ok_sku():
            return ops.Check("افزونهٔ SKU", "ok", "SKU بعدی: T1001")

        stubs = {
            "_check_token": ok_token,
            "_check_log_chat": ok_chat,
            "_check_woocommerce": ok_woo,
            "_check_wordpress": ok_wp,
            "_check_sku_plugin": ok_sku,
        }
        stubs.update(over)
        patches = [mock.patch.object(ops, name, stub) for name, stub in stubs.items()]
        for handle in patches:
            handle.start()
        try:
            with (
                mock.patch("shutil.which", return_value="/usr/bin/supervisorctl"),
                h.temp_ledger(),
                h.temp_metrics(),
            ):
                yield
        finally:
            for handle in patches:
                handle.stop()

    def test_verdict_counts_broken_and_half_working_separately(self):
        ok = ops.Check("x", "ok")
        warn = ops.Check("y", "warn", fix="این را درست کن")
        bad = ops.Check("z", "bad", "چون...")
        self.assertIn("✅ همه‌چیز سبز است", ops.diagnose_text([ok], seconds=0.4))
        self.assertIn("🟡 1 مورد نیازمندِ توجه", ops.diagnose_text([ok, warn], seconds=0.4))
        text = ops.diagnose_text([ok, warn, bad], seconds=1.25)
        self.assertIn("🔴 1 مورد بلا‌است · 🟡 1 مورد نیمه‌کاره", text)
        self.assertIn("1.2 ثانیه", text)
        self.assertIn("این را درست کن", text)

    def test_html_specials_in_a_detail_cannot_break_the_card(self):
        line = ops.Check("سایت", "bad", "<b>هشدار</b> & چیز دیگر").line()
        self.assertIn("&lt;b&gt;", line)
        self.assertNotIn("<b>هشدار</b>", line)

    def test_a_check_that_could_not_run_is_never_reported_as_a_pass(self):
        async def explode(bot):
            raise RuntimeError("توکن باطل")

        with self._green(_check_token=explode):
            checks = run(ops.run_checks(SimpleNamespace()))
        failed = [check for check in checks if check.name == "بررسی ناتمام"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].state, "bad")
        self.assertIn("توکن باطل", failed[0].detail)
        self.assertIn("🔴 1 مورد بلا‌است", ops.diagnose_text(checks, seconds=0.1))

    def test_every_service_has_a_check_and_green_services_mean_a_green_card(self):
        async def sku_missing():
            return ops.Check("افزونهٔ SKU", "warn", "نصب نیست (HTTP 404)")

        with self._green(_check_sku_plugin=sku_missing):
            checks = run(ops.run_checks(SimpleNamespace()))
        states = {check.name: check.state for check in checks}
        for name in ("توکن تلگرام", "چت لاگ", "WooCommerce", "رسانهٔ وردپرس", "ری‌استارت",
                     "صف ارسال", "دیسک"):
            self.assertEqual(states.get(name), "ok", f"{name} در عیب‌یابی نیست یا سبز نشد")
        # افزونهٔ SKU عمداً 🟡 است: نبودنش اشکال نیست، ربات SKU را از کاتالوگ می‌خواند.
        self.assertEqual(states["افزونهٔ SKU"], "warn")
        # افزونهٔ ZIP هم همین‌طور (نسخهٔ کنارِ ریپو sale_price را نمی‌خواند): 🟡 با ↳.
        importer = next(c for c in checks if c.name == "افزونهٔ واردکنندهٔ ZIP")
        self.assertIn(importer.state, {"ok", "warn"})
        if importer.state == "warn":
            self.assertTrue(importer.fix, "هشدارِ نسخهٔ افزونه باید بگوید چه کار کنی")
        text = ops.diagnose_text(checks, seconds=0.1)
        self.assertIn("نیازمندِ توجه", text)
        self.assertIn("<b>عیب‌یابی</b>", text)

    def test_the_sku_probe_asks_the_route_the_writer_uses(self):
        # اگر روزی مسیر REST عوض شود و عیب‌یابی مسیر قدیمی را بپرسد، «نصب است» دروغ
        # می‌شود؛ پس هر دو باید از یک ثابت بخوانند.
        asked: list[str] = []

        class StubClient:
            def __init__(self, *_args, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

            async def get(self, url, **kwargs):
                asked.append(url)
                return SimpleNamespace(status_code=404, json=dict)

        with h.patched_settings(h.settings_with()), mock.patch.object(ops, "WooClient", StubClient):
            check = run(ops._check_sku_plugin())
        self.assertEqual(asked, [f"https://shop.example{sku.PLUGIN_ROUTE}"])
        self.assertEqual(check.state, "warn")
        self.assertIn("404", check.detail)

    def test_missing_credentials_are_named_not_blamed_on_the_site(self):
        async def unused():
            raise AssertionError("با پیکربندی ناقص نباید درخواستی برود")

        with h.patched_settings(h.settings_with(woocommerce_key="")), mock.patch.object(
            ops, "ping_woocommerce", unused
        ):
            check = run(ops._check_woocommerce())
        self.assertEqual(check.state, "bad")
        self.assertIn("کامل نیست", check.detail)
        self.assertIn("WOOCOMMERCE", check.fix)


@needs_flow
class TestAccess(unittest.TestCase):
    def test_the_status_button_is_not_something_an_admin_can_be_given(self):
        self.assertIn("ops_status", BY_KEY)
        self.assertEqual(BY_KEY["ops_status"].label, "📊 وضعیت")
        self.assertFalse(BY_KEY["ops_status"].admin_eligible)
        self.assertFalse(feature_allowed(STRANGER, "ops_status"))
        self.assertTrue(feature_allowed(SUDO, "ops_status"))

    def test_a_denied_click_gets_one_alert_and_counts_itself(self):
        update, seen = h.query_update(CB.OPS_STATUS, user_id=STRANGER)
        with h.temp_metrics() as m:
            run(ops.cb_status(update, h.context()))
            denied = m.snapshot().get("buttons_denied", (0,))[0]
        self.assertEqual(denied, 1)
        self.assertEqual([entry[0] for entry in seen], ["answer"])
        self.assertIn("⛔", seen[0][1])
        self.assertTrue(seen[0][2].get("show_alert"))

    def test_sudo_gets_the_card_and_the_three_next_steps(self):
        update, seen = h.query_update(CB.OPS_STATUS, user_id=SUDO)
        with h.temp_ledger(), h.temp_metrics():
            run(ops.cb_status(update, h.context()))
        self.assertEqual(seen[0][0], "answer")  # اول یک answer، نه دو تا
        card = next(entry for entry in seen if entry[0] == "text")
        self.assertIn("<b>وضعیت</b>", card[1])
        rows = card[2]["reply_markup"].inline_keyboard
        labels = [button.text for row in rows for button in row]
        self.assertIn("🩺 عیب‌یابی کامل", labels)
        self.assertIn("📥 فایل متریک‌ها", labels)
        self.assertEqual(rows[-1][0].text, "⬅️ بازگشت به منو")

    def test_diagnose_answers_the_query_once_then_edits_its_own_note(self):
        async def all_ok(bot):
            return [ops.Check("WooCommerce", "ok", "HTTP 200")]

        update, seen = h.query_update(CB.OPS_DIAGNOSE, user_id=SUDO)
        with mock.patch.object(ops, "run_checks", all_ok), h.temp_ledger(), h.temp_metrics():
            run(ops.cb_diagnose(update, h.context()))
        self.assertEqual(len([entry for entry in seen if entry[0] == "answer"]), 1)
        kinds = [entry[0] for entry in seen]
        self.assertEqual(kinds[1:3], ["text", "edit"])  # «⏳…» اول، بعد کارت جای خودش
        self.assertIn("<b>عیب‌یابی</b>", seen[2][1])

    def test_export_sends_the_csv_file_and_only_to_the_owner(self):
        for user_id, allowed in ((SUDO, True), (STRANGER, False)):
            bot = h.FakeBot()
            update, _sent = h.message_update("/export_metrics", user_id=user_id)
            update.effective_chat = SimpleNamespace(id=user_id)
            with h.temp_metrics() as m:
                m.incr("images_uploaded", by=5)
                run(ops.cmd_export_metrics(update, h.context(bot)))
                documents = list(bot.documents)
            if allowed:
                self.assertEqual(len(documents), 1)
                body = documents[0]["document"].read().decode("utf-8-sig")
                self.assertIn("images_uploaded", body)
                self.assertIn(",5,", body)
                self.assertRegex(str(documents[0]["filename"]), r"^tisa-metrics-\d{8}-\d{4}\.csv$")
            else:
                self.assertEqual(documents, [])
                self.assertIn("🔒", _sent[0][1])

    def test_the_metrics_button_answers_the_chat_that_pressed_it(self):
        update, _seen = h.query_update(CB.OPS_METRICS, user_id=SUDO, chat_id=4242)
        bot = h.FakeBot()
        with h.temp_metrics():
            run(ops.cb_metrics(update, h.context(bot)))
        self.assertEqual(bot.documents[0]["chat_id"], 4242)


@needs_flow
class TestWiring(unittest.TestCase):
    def test_the_new_buttons_are_registered_handlers_not_dead_labels(self):
        patterns = "|".join(h.all_handler_patterns())
        for name in ("OPS_STATUS", "OPS_DIAGNOSE", "OPS_METRICS"):
            value = getattr(CB, name)
            self.assertIn(value.split(":")[-1], patterns, f"CB.{name} هیچ هندلری ندارد")
        self.assertIn("cmd:export_metrics", patterns)

    def test_no_callback_in_the_constants_is_left_unanswered(self):
        # صفحه‌ها زیاد شده‌اند؛ این همان چیزی است که فاز ۳ و ۵ را گرفت — لیبلِ زنده‌ای
        # که هیچ هندلری پشتش نیست.
        patterns = "|".join(h.all_handler_patterns())
        for name in dir(CB):
            if name.startswith("_"):
                continue
            value = getattr(CB, name)
            if isinstance(value, str):
                self.assertTrue(value in patterns or value.split(":")[-1] in patterns,
                                f"CB.{name} = {value!r} هیچ هندلری ندارد")

    def test_which_buttons_appear_in_the_main_menu_for_which_role(self):
        from bot.keyboards import main_menu_keyboard

        for role, visible in ((rbac.SUDO, True), (rbac.ADMIN, False)):
            with mock.patch("bot.keyboards.main_menu.rbac.role", lambda _id, _r=role: _r), \
                 mock.patch("bot.services.preferences.button_visible", return_value=True):
                markup = main_menu_keyboard(1)
            labels = [button.text for row in markup.inline_keyboard for button in row]
            self.assertEqual("📊 وضعیت" in labels, visible, f"نقش {role} نباید این دکمه را ببیند")

    def test_the_command_name_telegram_accepts_is_the_one_documented(self):
        # «/export-metrics» در تلگرام ممکن نیست (نام دستور فقط حرف/عدد/آندر‌اسکور است).
        # پس نامِ درست باید در راهنما باشد، و نامِ غلط فقط مجاز است روی خطی که خودش
        # توضیح می‌دهد چرا نیست — وگرنه یک راهنما کاربر را به دستورِ ناموجود می‌فرستد.
        from bot.app import BOT_COMMANDS

        names = {command.command for command in BOT_COMMANDS}
        self.assertIn("export_metrics", names)
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        self.assertIn("/export_metrics", readme)
        for path in ("README.md", "CHANGELOG.md", "docs/runbook.md",
                     "docs/archive/CODE-REVIEW-AND-UPGRADE-PLAN.md"):
            full = REPO / path
            if not full.exists():
                continue
            lines = full.read_text(encoding="utf-8").splitlines()
            for index, line in enumerate(lines):
                if "/export-metrics" not in line:
                    continue
                # توضیح می‌تواند در خطِ بعدِ همین پاراگرافِ شکسته‌شده باشد؛ آنچه
                # برایش ارزش دارد، «دستورِ اشتباه در راهنما» است، نه جایِ نقطه.
                window = " ".join(lines[max(0, index - 2) : index + 3])
                self.assertTrue("نمی‌پذیرد" in window or "به‌جایش" in window,
                                f"{path} نامِ غیرممکن را بدون توضیح می‌گوید: {line[:80]}")

    def test_verbose_log_is_a_documented_knob(self):
        # هر کلیدِ .env باید در جدولِ README هم باشد (این قواعد را فاز ۰ گذاشت).
        env = (REPO / ".env.example").read_text(encoding="utf-8")
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        self.assertIn("VERBOSE_LOG", env)
        self.assertIn("VERBOSE_LOG", readme)





# --- قرارداد افزونهٔ ZIP -------------------------------------------------------


@needs_flow
class TestImporterContract(unittest.TestCase):
    """یک zip، یک عدد: چه چیزی از بستهٔ ربات روی سایت می‌نشیند."""

    def _zip_with(self, version: str) -> Path:
        import zipfile

        path = Path(tempfile.mkdtemp(prefix="tisa-plugin-")) / "tisa-product-importer.zip"
        header = (
            "<?php\n/**\n * Plugin Name: Tisa Product ZIP Importer\n"
            f" * Version: {version}\n * Requires Plugins: woocommerce\n */\n"
        )
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("tisa-product-importer/tisa-product-importer.php", header)
        return path

    def test_the_version_comes_from_the_plugin_file_not_from_a_doc(self):
        # README را نمی‌خوانیم: اگر عدد از متن بیاید، هر ویرایشِ نگارشی «سازگاری» را عوض می‌کند.
        from bot.services import importer_contract

        self.assertEqual(importer_contract.read_version(self._zip_with("0.9.1")), (0, 9, 1))
        self.assertIsNone(importer_contract.read_version(self._zip_with("no digits")))

    def test_the_plugin_beside_the_repo_is_readable(self):
        from bot.services import importer_contract

        version = importer_contract.read_version()
        self.assertIsNotNone(version, "هدرِ Version در tisa-product-importer.zip پیدا نشد")
        state, detail, _fix = importer_contract.describe()
        self.assertIn(importer_contract.version_text(version), detail)
        self.assertIn(state, {"ok", "warn"})

    def test_an_old_plugin_is_a_warning_that_names_the_lost_fields(self):
        from bot.services import importer_contract

        state, detail, fix = importer_contract.describe(self._zip_with("0.7.0"))
        self.assertEqual(state, "warn")
        self.assertIn("sale_price", detail)
        self.assertIn("stock", detail)
        self.assertIn("REST", fix)  # راه‌حلِ دوم باید گفته شود، نه فقط «افزونه را تازه کن»
        self.assertEqual(importer_contract.describe(self._zip_with("0.8.0"))[0], "ok")

    def test_a_missing_plugin_file_is_explained_not_faked(self):
        from bot.services import importer_contract

        state, detail, fix = importer_contract.describe(Path("/nonexistent/tisa-product-importer.zip"))
        self.assertEqual(state, "warn")
        self.assertIn("نیست", detail)
        self.assertIn("wp-admin", fix)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
