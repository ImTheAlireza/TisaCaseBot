"""فاز ۸: شمارنده‌های عملیاتی و کارتِ جمع‌وجورِ محصول.

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
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("SUDO_IDS", "1234567")

import _flow_harness as h

from bot.constants import CB

try:
    from bot import rbac
    from bot.services import product_journal
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

    def test_check_config_names_the_metrics_database(self):
        """صفحهٔ وضعیت رفته؛ تنها جایی که دیتابیسِ مرده دیده می‌شود خروجیِ استارت است."""
        import io
        import main
        from contextlib import redirect_stdout

        out = io.StringIO()
        with h.temp_metrics() as m, h.patched_settings(h.settings_with(woo_dry_run=False)), \
                redirect_stdout(out):
            expected = str(m.path())
            rc = main.check_config()
        printed = out.getvalue()
        self.assertIn("metrics :", printed)
        self.assertIn(expected, printed)
        self.assertEqual(0, rc, "شمارنده‌های خراب نباید پیکربندی را رد کنند")

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


# --- سیم‌کشی -------------------------------------------------------------------


@needs_flow
class TestWiring(unittest.TestCase):
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
            self.assertEqual("🏓 Ping" in labels, visible, f"نقش {role} نباید این دکمه را ببیند")

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
