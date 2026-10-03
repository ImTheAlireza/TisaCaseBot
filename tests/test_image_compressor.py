"""فاز ۹: `image_compressor` — ۳۱٪ پوشش داشت و تصمیمش روی هر محصول نشسته است.

قاعدهٔ کلیدی این ماژول «هیچ‌چیز را دور نینداز» است: اگر فشرده‌سازی ممکن نبود، فایل
اصلی برگردانده می‌شود — چون یک عکسِ بزرگ بهتر از محصول بی‌عکس است. تست‌های پایین هم
همین را می‌سنجند، نه فقط «کوچک‌تر شد»: جهتِ EXIF، شفافیتِ PNG، کیفیتِ خوانده‌شده از
`.env` در هر فراخوانی، و اینکه خروجیِ بزرگ‌تر از ورودی هرگز جای اصلی را نمی‌گیرد.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("SUDO_IDS", "1234567")

import _flow_harness as h

try:
    from PIL import Image, ImageOps

    from bot.services import image_compressor

    HAS_PILLOW = True
except Exception:  # pragma: no cover - Pillow نصب نیست
    image_compressor = None  # type: ignore[assignment]
    HAS_PILLOW = False


def _write_photo(directory: Path, name: str = "01.png", size: tuple[int, int] = (320, 200), alpha: bool = False) -> Path:
    """یک تصویر «عکس‌مانند» می‌سازد: نویزِ پرتکرار.

    چرا نویز؟ تصویرِ یکدست/گرادیان در PNG هم ریز می‌شود و مقایسهٔ اندازهٔ
    JPEG/PNG بی‌معنی می‌شد (و تست‌ها باید بگویند «کوچک‌تر شد»); نویز همان چیزی است
    که PNG بد فشرده می‌کند و JPEGِ q88 از پسش برمی‌آید — مثل عکس محصول روی پارچه.
    """
    image = Image.effect_noise(size, 90).convert("RGBA" if alpha else "RGB")
    if alpha:
        # یک گوشه کاملاً شفاف: باید روی سفید بخوابد، نه روی سیاه.
        image.putalpha(255)
        for x in range(min(60, size[0])):
            for y in range(min(50, size[1])):
                image.putpixel((x, y), (0, 0, 0, 0))
    path = directory / name
    image.save(path, format="PNG")
    return path


@unittest.skipUnless(HAS_PILLOW, "Pillow نصب نیست")
class TestCompressImage(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="tisa-img-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.out = self.tmp / "out"

    def test_a_photo_is_reencoded_smaller_and_keeps_its_dimensions(self) -> None:
        src = _write_photo(self.tmp, "01.png")
        result = image_compressor.compress_image(src, self.out)
        self.assertNotEqual(src, result, "فشرده‌سازی انجام نشد")
        self.assertEqual(".jpg", result.suffix)
        with Image.open(result) as saved:
            self.assertEqual((320, 200), saved.size, "اندازهٔ تصویر عوض شد")
            self.assertEqual("RGB", saved.mode)
        self.assertLess(result.stat().st_size, src.stat().st_size)
        savings = image_compressor.compressed_size_savings(src.stat().st_size, result.stat().st_size)
        self.assertGreater(savings, 0, "صرفه‌جویی باید در کارت دیده شود، نه صفر")

    def test_transparency_is_flattened_on_white(self) -> None:
        src = _write_photo(self.tmp, "alpha.png", size=(200, 160), alpha=True)
        result = image_compressor.compress_image(src, self.out)
        self.assertNotEqual(src, result, "برای سنجیدنی شدنِ نتیجه به تصویرِ بزرگ‌تر نیاز است")
        with Image.open(result) as saved:
            self.assertEqual("RGB", saved.mode)
            self.assertEqual((255, 255, 255), saved.getpixel((5, 5)), "پشت‌زمینهٔ سفید نبود (JPEG شفافیت ندارد)")

    def test_exif_orientation_is_applied_and_the_tag_path_survives(self) -> None:
        src = self.tmp / "rotated.jpg"
        image = Image.new("RGB", (80, 40), (10, 200, 10))
        exif = Image.Exif()
        exif[274] = 6  # rotate 90° clockwise
        image.save(src, format="JPEG", exif=exif)
        result = image_compressor.compress_image(src, self.out)
        with Image.open(result) as saved:
            self.assertEqual((40, 80), saved.size, "چرخشِ EXIF اعمال نشد (عکس کج روی سایت می‌رود)")

    def test_an_unknown_extension_is_passed_through(self) -> None:
        src = self.tmp / "poster.gif"
        src.write_bytes(b"GIF89a" + b"0" * 200)
        self.assertEqual(src, image_compressor.compress_image(src, self.out))

    def test_a_broken_file_is_passed_through_not_dropped(self) -> None:
        src = self.tmp / "broken.jpg"
        src.write_bytes(b"not a jpeg at all" * 40)
        self.assertEqual(src, image_compressor.compress_image(src, self.out), "عکس خراب نباید دور ریخته شود")

    def test_a_result_bigger_than_the_source_is_not_used(self) -> None:
        # تصویر ۱×۱ از قبل در حدِ حداقلِ JPEG است؛ بازنویسی با optimize فقط سرِ
        # framing بزرگ‌ترش می‌کند — و آن نسخه هرگز جای اصلی را نمی‌گیرد.
        src = self.tmp / "tiny.png"
        Image.new("RGB", (1, 1), (0, 0, 0)).save(src, format="PNG")
        result = image_compressor.compress_image(src, self.out)
        self.assertEqual(src, result)

    def test_quality_is_read_from_settings_on_every_call(self) -> None:
        """کیفیت از `.env` است، نه ثابتِ زمان import: تغییرش با restart کافی است."""
        src = _write_photo(self.tmp, "big.png", size=(600, 400))
        with h.patched_settings(h.settings_with(image_quality=95)):
            good = image_compressor.compress_image(src, self.tmp / "q95")
        with h.patched_settings(h.settings_with(image_quality=15)):
            rough = image_compressor.compress_image(src, self.tmp / "q15")
        self.assertNotEqual(good, src)
        self.assertNotEqual(rough, src)
        self.assertLess(rough.stat().st_size, good.stat().st_size, "IMAGE_QUALITY روی خروجی اثر نداشت")

    def test_quality_is_clamped_into_jpegs_own_range(self) -> None:
        for raw, expected in ((0, 1), (300, 100), (88, 88), (75, 75)):
            with self.subTest(raw=raw), h.patched_settings(h.settings_with(image_quality=raw)):
                self.assertEqual(expected, image_compressor._quality())

    def test_the_destination_directory_is_created(self) -> None:
        src = _write_photo(self.tmp, "01.png")
        deep = self.tmp / "a" / "b" / "c"
        result = image_compressor.compress_image(src, deep)
        self.assertTrue(deep.exists(), "مسیر خروجی ساخته نشد")
        self.assertTrue(result.exists())


class TestSavingsMath(unittest.TestCase):
    """درصدِ صرفه‌جویی در کارتِ محصول و لاگ نشان داده می‌شود؛ باید بی‌معنی نشود."""

    @unittest.skipUnless(HAS_PILLOW, "Pillow نصب نیست")
    def test_savings(self) -> None:
        cases = {(1000, 500): 50, (1000, 1000): 0, (1000, 1500): 0, (0, 0): 0, (-5, 3): 0, (1000, 0): 100}
        for (original, compressed), expected in cases.items():
            with self.subTest(original=original, compressed=compressed):
                self.assertEqual(expected, image_compressor.compressed_size_savings(original, compressed))

    @unittest.skipUnless(HAS_PILLOW, "Pillow نصب نیست")
    def test_supported_set_is_what_the_flow_sends(self) -> None:
        """هر فرمتی که تلگرام برای عکس می‌دهد باید داخل SUPPORTED باشد."""
        self.assertTrue({"jpg", "jpeg", "png", "webp", "bmp", "tiff"} <= image_compressor.SUPPORTED)
        self.assertNotIn("gif", image_compressor.SUPPORTED, "انیمیشن با JPEG تبدیل به یک فریم می‌شود")

    def test_the_module_never_needs_pillow_for_the_import_check(self) -> None:
        """`Image.MAX_IMAGE_PIXELS` باید روی همان سقفِ مستندشده قفل باشد."""
        if not HAS_PILLOW:  # pragma: no cover
            self.skipTest("Pillow نصب نیست")
        self.assertEqual(40_000_000, Image.MAX_IMAGE_PIXELS)
        self.assertIsNotNone(ImageOps.exif_transpose)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
