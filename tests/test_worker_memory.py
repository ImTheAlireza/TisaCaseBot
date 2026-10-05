"""سقفِ حافظهٔ کارگر یک «بودجهٔ فایل» است، نه سقفِ کلِ پردازه.

گزارشِ فروشگاه: سه فایل — xlsx، pdf و csv — هر سه **۲۲ کیلوبایتی**، در یک دقیقه، هر سه با
`MemoryError` رد شدند. فایلِ ۲۲KB نمی‌تواند ۷۶۸MB حافظه بخواهد؛ پس مصرف‌کنندهٔ حافظه خودِ
فایل نبود، *پایهٔ* خودِ پردازه بود: سقفِ مطلقِ `RLIMIT_AS` پیش از رسیدنِ نوبت به فایل، با
ایمپورت‌های کارگر (python + pandas + کتابخانه‌های هاست) پر شده بود و هیچ تخصیصی برای فایل
موفق نمی‌شد — به همین دلیل هر سه فرمت یک‌جور شکست خوردند.

این‌ها آن حالت را می‌بندند:

* سقف = «آن‌چه فرزند همین حالا دارد + ``WORKER_MEMORY_MB``» — پس پایهٔ سنگین، بودجهٔ فایل را
  نمی‌خورد (تستِ حسابِ خالص، بدون فرض دربارهٔ هاست)؛
* فرزندی که جا ندارد، **قبل** از کار با :class:`~bot.services.worker.WorkerNoMemory` جواب
  می‌دهد — نه با `MemoryError`ِ وسطِ pandas — و جریان آن را به جملهٔ «حافظه پر است + چه چیزی را
  بالا ببر» ترجمه می‌کند؛
* `MemoryError`ِ داخلِ خواننده‌ها (pandas/pymupdf) هم به همان جملهٔ فارسی می‌رسد، با نامِ فنی
  داخلِ پرانتز، نه به‌تنهایی.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("SUDO_IDS", "1234567")

import _flow_harness as h

from bot.services import worker

try:
    from bot.services import processor

    HAS_PROCESSOR = True
except Exception:  # pragma: no cover - pandas missing
    processor = None  # type: ignore[assignment]
    HAS_PROCESSOR = False

try:
    import pymupdf

    HAS_PYMUPDF = True
except Exception:  # pragma: no cover
    pymupdf = None  # type: ignore[assignment]
    HAS_PYMUPDF = False

try:
    from bot.modules import tracking_converter as TC

    HAS_FLOW = True
except Exception:  # pragma: no cover - PTB missing
    TC = None  # type: ignore[assignment]
    HAS_FLOW = False

needs_processor = unittest.skipUnless(HAS_PROCESSOR, "pandas is not installed")
needs_pdf = unittest.skipUnless(HAS_PROCESSOR and HAS_PYMUPDF, "pandas/pymupdf are not installed")
needs_flow = unittest.skipUnless(HAS_PROCESSOR and HAS_FLOW, "pandas or python-telegram-bot is not installed")

MB = 1024 * 1024


# --- shared, spawn-safe helpers (the child must be able to import these) ---------


def _noop(_label: str) -> str:
    """A job that needs nothing — the point is whether the child can start it at all."""
    return "done"


class TestTheCapIsRelative(unittest.TestCase):
    """حسابِ خالص: هیچ فرضی دربارهٔ هاستِ کاربر، فقط خودِ معادله."""

    def test_a_heavy_baseline_never_eats_the_files_budget(self):
        cap, headroom = worker._memory_target(700 * MB, 768 * MB, -1)
        self.assertEqual(headroom, 768 * MB, "بودجهٔ فایل باید کامل بماند")
        self.assertEqual(cap, (700 + 768) * MB, "سقف = پایه + بودجه، نه یک عدد مطلق")

    def test_a_host_ceiling_always_wins_and_the_headroom_is_honest(self):
        # systemd `LimitAS=768M` (یا `ulimit -v`) روی خودِ پردازه: همان برنده است و
        # فضای آزاد هم هرچه مانده — نه عددِ خوش‌بینانه.
        cap, headroom = worker._memory_target(200 * MB, 768 * MB, 768 * MB)
        self.assertEqual(cap, 768 * MB)
        self.assertEqual(headroom, 568 * MB)

    def test_a_child_with_no_room_reports_it_instead_of_rounding_up(self):
        _cap, headroom = worker._memory_target(700 * MB, 768 * MB, 600 * MB)
        self.assertLess(headroom, worker.MIN_HEADROOM_MB * MB)

    def test_the_budget_is_the_env_setting_and_nonsense_falls_back(self):
        self.assertEqual(worker._budget_bytes(SimpleNamespace(worker_memory_mb=512)), 512 * MB)
        for bad in (0, -5, float("nan"), "abc"):
            self.assertEqual(
                worker._budget_bytes(SimpleNamespace(worker_memory_mb=bad)),
                worker.MEMORY_MB * MB,
                f"{bad!r} باید به پیش‌فرض برگردد، نه به سقفِ صفر",
            )
        self.assertEqual(worker._budget_bytes(None), worker.MEMORY_MB * MB)


class TestACrampedChildRefusesHonestly(unittest.TestCase):
    """فرزندِ بدونِ جا: جملهٔ صریح، نه `MemoryError`ِ خام."""

    def test_a_small_budget_stops_before_the_job_not_inside_it(self):
        with (
            h.patched_settings(h.settings_with(worker_memory_mb=64)),
            self.assertRaises(worker.WorkerNoMemory) as raised,
        ):
            asyncio.run(worker.run(_noop, "x", timeout=60))
        message = str(raised.exception)
        self.assertIn("WORKER_MEMORY_MB", message)
        self.assertIn("free", message)
        self.assertNotIn("MemoryError", message)

    def test_the_same_job_runs_with_a_workable_budget(self):
        with h.patched_settings(h.settings_with(worker_memory_mb=512)):
            self.assertEqual(asyncio.run(worker.run(_noop, "x", timeout=60)), "done")
        self.assertEqual(worker._idle, [], "کارگرِ پارک‌شده نباید بماند")


@needs_processor
class TestAMemoryErrorBecomesASentence(unittest.TestCase):
    """خطای حافظه در هر سه خواننده به جملهٔ «چه کار کنم» می‌رسد."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def _assert_honest(self, call) -> str:
        with self.assertRaises(processor.MemoryLimitError) as raised:
            call()
        text = str(raised.exception)
        self.assertIn("WORKER_MEMORY_MB", text, "جمله باید بگوید چه چیزی را بالا ببرد")
        self.assertIn("logs/bot.log", text, "عددها آن‌جا هستند")
        self.assertIn("MemoryError", text, "نامِ فنی می‌ماند تا در لاگ پیدا شود")
        # …ولی خطِ خالی «MemoryError» که کاربر دید، دیگر هیچ‌جا نیست.
        self.assertNotIn("\nMemoryError", text)
        return text

    def test_the_error_family_is_the_one_the_flow_answers_with_a_warning(self):
        # اگر این ارث‌بری بشکند، پیام به «❌ خطا در پردازش» برمی‌گردد؛ یک تستِ ارزان
        # برای رفتارِ جریان، نه برای پیاده‌سازی.
        self.assertTrue(issubclass(processor.MemoryLimitError, processor.RowLimitError))

    def test_a_csv_parser_that_runs_out_of_memory(self):
        path = self.dir / "orders.csv"
        path.write_text("ردیف,بارکد\n1,610001573845123456789012\n", encoding="utf-8")
        with mock.patch("pandas.read_csv", side_effect=MemoryError()):
            self._assert_honest(lambda: processor.process_file(path, "orders.csv"))

    def test_an_xlsx_that_cannot_be_opened_for_lack_of_memory(self):
        from openpyxl import Workbook

        path = self.dir / "orders.xlsx"
        book = Workbook()
        book.active.append(["ردیف", "بارکد"])
        book.active.append([1, "610001573845123456789012"])
        book.save(path)
        with mock.patch("pandas.ExcelFile", side_effect=MemoryError()):
            self._assert_honest(lambda: processor.process_file(path, "orders.xlsx"))

    def test_a_pdf_whose_page_cannot_be_read_for_lack_of_memory(self):
        doc = pymupdf.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((430, 90), "610001573845123456789012", fontsize=9)
        path = self.dir / "orders.pdf"
        doc.save(path)
        doc.close()
        # همان‌جایی که خطای خام از آن می‌آمد: ساختنِ rawdictِ یک صفحه.
        with mock.patch.object(processor, "_page_lines", side_effect=MemoryError()):
            self._assert_honest(lambda: processor.process_file(path, "orders.pdf"))


@needs_flow
class TestTheFlowSaysWhatToRaise(unittest.TestCase):
    """جریانِ چت: «حافظهٔ ربات پر است» + نامِ کلیدی که باید بالا برود."""

    def _run_with(self, error: BaseException) -> str:
        async def boom(*_args, **_kwargs):
            raise error

        original = TC.worker.run
        TC.worker.run = boom
        try:
            # `_run` یک پیامِ آمادهٔ فرستادن برمی‌گرداند، نه استثنا: جریان هیچ استثنایی
            # را به کاربر نشان نمی‌دهد.
            return asyncio.run(TC._run(Path("orders.xlsx"), "orders.xlsx"))
        finally:
            TC.worker.run = original

    def test_a_worker_with_no_memory_left(self):
        message = self._run_with(TC.worker.WorkerNoMemory("only 12 MB free under the cap"))
        self.assertIn("WORKER_MEMORY_MB", message)
        self.assertIn("logs/bot.log", message)
        self.assertIn("ulimit", message, "سقفِ خودِ هاست هم باید نام برده شود")
        self.assertNotIn("MemoryError", message)
        self.assertNotIn("خطا در پردازش", message)

    def test_a_bare_memoryerror_never_reaches_the_chat_as_a_class_name(self):
        # دفاعِ لایهٔ دوم: حتی اگر MemoryError از جایی بیرونِ خواننده‌ها بالا بیاید.
        message = self._run_with(MemoryError())
        self.assertIn("WORKER_MEMORY_MB", message)
        self.assertNotIn("❌ خطا در پردازش", message)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

@needs_processor
class TestTheLimitMessagesSayWhatToDo(unittest.TestCase):
    """سقف‌ها باید به فارسی بگویند «چه کار کنم» — نه یک کلمهٔ ناتمامِ انگلیسی."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="tisa-limits-"))
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_an_xlsx_with_too_many_inner_files_is_refused_in_persian(self):
        import zipfile

        path = self.dir / "orders.xlsx"
        with zipfile.ZipFile(path, "w") as archive:
            for index in range(2001):
                archive.writestr(f"part{index}.xml", "x")
        with self.assertRaises(processor.RowLimitError) as caught:
            processor.process_file(path, "orders.xlsx")
        message = str(caught.exception)
        self.assertIn("۲۰۰۰", message, "عددِ واقعیِ سقف باید در پیام باشد")
        self.assertTrue("CSV" in message or "بخش" in message, "پیام باید راهِ حل بدهد")

    def test_the_row_limit_message_has_no_english_filler(self):
        with self.assertRaises(processor.RowLimitError) as caught:
            processor._guard_rows(int(processor.settings.max_rows) + 1)
        message = str(caught.exception)
        self.assertIn(f"{int(processor.settings.max_rows):,}", message)
        self.assertNotIn("slow", message, "«slow» یک کلمهٔ انگلیسیِ جاافتاده در جملهٔ فارسی بود")
