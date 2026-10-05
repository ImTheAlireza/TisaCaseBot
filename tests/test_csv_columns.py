"""برشِ بی‌صدای CSV بالای ۵۱۲ ستون — یک خروجیِ ۶۰۰ستونی نباید «۵۱۲ستونی» خوانده شود.

گزارشِ امنیتی/عملکردی: خروجیِ ۶۰۰ ستونیِ سامانه (ستون «بارکد» در ایندکس ۵۹۰) در جدولی
۵۱۲ ستونی خوانده می‌شد — ستون بارکد بی‌خبر از میان می‌رفت و ربات از کاربر دربارهٔ دو
ستونِ اشتباه (۵۰۲ و ۵۰۳) می‌پرسید. pandas وقتی ``names`` از تعداد فیلدهای خط کوتاه‌تر
باشد نه خطا می‌دهد و نه هشدار: ستون‌های اضافی را دور می‌ریزد. پس عرضِ واقعی باید خودش
شمرده شود، و اگر فایل از سقفِ ۴۰۹۶ ستون هم بگذرد، به‌جای جدولِ ناقص یک جملهٔ صریح بگیرد.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("SUDO_IDS", "1234567")

try:
    from bot.services import processor

    HAS_PROCESSOR = True
except Exception:  # pragma: no cover - pandas missing
    processor = None  # type: ignore[assignment]
    HAS_PROCESSOR = False

needs_processor = unittest.skipUnless(HAS_PROCESSOR, "pandas is not installed")

BARCODE = "610001573845123456789012"
CODE = "654321"


def _wide_rows(columns: int, *, barcode_at: int | None = None, rows: int = 3) -> list[str]:
    """یک CSV پهن: هدر + چند ردیف، همه با عرضِ یکسان."""
    header = [f"c{i}" for i in range(columns)]
    if barcode_at is not None:
        header[barcode_at] = "بارکد"
        header[barcode_at + 1] = "کد رهگیری"
    lines = [",".join(header)]
    for row in range(rows):
        data = [str(row * 1000 + i) for i in range(columns)]
        if barcode_at is not None:
            data[barcode_at] = BARCODE
            data[barcode_at + 1] = CODE
        lines.append(",".join(data))
    return lines


@needs_processor
class TestTheColumnCountIsNeverSilentlyCapped(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="tisa-cols-"))

    def _write(self, lines: list[str], name: str = "orders.csv") -> Path:
        path = self.dir / name
        path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
        return path

    def test_a_600_column_file_keeps_all_600_columns(self):
        path = self._write(_wide_rows(600))
        frame, _sheet, _notes = processor._read_table(path, ".csv")
        self.assertEqual((4, 600), frame.shape,  # هدر + ۳ ردیفِ داده
                         "ستون‌های ۵۱۲ به بعد نباید دور ریخته شوند")
        self.assertEqual(600, processor._csv_width(path.read_text(encoding="utf-8"), ","))

    def test_a_barcode_past_column_512_is_still_offered(self):
        # همان شکلِ گزارش: «بارکد» روی ایندکس ۵۹۰ و «کد رهگیری» روی ۵۹۱.
        path = self._write(_wide_rows(600, barcode_at=590))
        report = processor.process_file(path, "orders.csv")
        self.assertTrue(report.questions, "فایلِ دارای دو ستونِ نام‌دار باید سؤال بپرسد")
        indexes = {option.index for option in report.questions[0].options}
        self.assertIn(590, indexes, "ستونِ «بارکد»ِ ایندکس ۵۹۰ باید در گزینه‌ها باشد")
        self.assertIn(591, indexes)
        self.assertNotIn(502, indexes, "دیگر نباید از ستون‌های اشتباهِ ۵۱۲ستونی بپرسد")

    def test_a_line_wider_than_the_head_does_not_lose_columns(self):
        # هدر باریک است و ردیف‌های بعدی پهن: نمونهٔ ۲۰۰ خطِ اول این را نمی‌بیند.
        lines = _wide_rows(2, rows=1) + _wide_rows(600, barcode_at=590, rows=2)
        path = self._write(lines, name="late_wide.csv")
        frame, _sheet, _notes = processor._read_table(path, ".csv")
        self.assertEqual(600, frame.shape[1], "عرضِ واقعیِ فایل، نه عرضِ هدر")

    def test_a_file_over_the_cap_is_refused_with_its_real_width(self):
        over = processor.MAX_CSV_COLUMNS + 1
        path = self._write(_wide_rows(over, rows=1), name="over_cap.csv")
        with self.assertRaises(processor.ColumnLimitError) as caught:
            processor.process_file(path, "over_cap.csv")
        message = str(caught.exception)
        self.assertIn(f"{over:,}", message, "عددِ واقعیِ ستون‌ها باید در پیام باشد")
        self.assertIn(f"{processor.MAX_CSV_COLUMNS:,}", message)
        self.assertTrue(issubclass(processor.ColumnLimitError, processor.RowLimitError),
                        "جریان، همین خانواده را با «🚧 …» جواب می‌دهد")

    def test_short_rows_are_still_padded_not_dropped(self):
        path = self._write(["ردیف,بارکد", "1," + BARCODE, "2," + BARCODE + ",اضافه"])
        frame, _sheet, _notes = processor._read_table(path, ".csv")
        self.assertEqual(3, len(frame))
        self.assertEqual("", frame.iat[1, 2], "فیلدِ نداشته = سلولِ خالی")
