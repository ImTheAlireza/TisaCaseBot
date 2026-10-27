"""✂️ اتو کراپ ۱:۱ — the crop maths, the untouched quality, and the screen.

Three promises are tested where they are made:

* :mod:`bot.services.image_cropper` — the square is centred (equal margins on both
  sides) and the pixels are not resampled; a JPEG keeps its own quantization tables
  and chroma subsampling, so «کیفیت دست نمی‌خورد» is a checked claim, not a caption;
* an image that is already 1:1 comes back as the very same file, unopened for writing;
* :mod:`bot.modules.auto_crop` — the button is sudo/admin-gated, the file goes back as
  a *document* (a photo reply would be recompressed by Telegram), and a refused file
  says why instead of failing silently.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("SUDO_IDS", "1234567")

from PIL import Image

from bot.services.image_cropper import (
    SUPPORTED,
    UnsupportedImage,
    crop_square,
    square_box,
)

try:
    from bot.constants import CB
    from bot.modules import auto_crop

    HAS_TELEGRAM = True
except Exception:  # pragma: no cover - PTB missing
    auto_crop = None  # type: ignore[assignment]
    HAS_TELEGRAM = False

needs_telegram = unittest.skipUnless(HAS_TELEGRAM, "python-telegram-bot is not installed")

SUDO = 1234567
ADMIN = 7


def _write_image(path: Path, size: tuple[int, int], *, color=(120, 30, 30), mode="RGB", **save) -> Path:
    Image.new(mode, size, color).save(path, **save)
    return path


class CropMaths(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="tisa-crop-"))
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_the_square_is_centred_with_equal_margins(self) -> None:
        # 1000×600: red | green | blue thirds; only the middle square may survive.
        source = Image.new("RGB", (1000, 600), (0, 0, 255))
        source.paste((255, 0, 0), (0, 0, 200, 600))
        source.paste((0, 200, 0), (200, 0, 800, 600))
        src = self.dir / "wide.png"
        source.save(src)

        result = crop_square(src, self.dir / "out")

        self.assertTrue(result.changed)
        self.assertEqual(600, result.side)
        self.assertEqual("600×600", result.size_text)
        self.assertEqual("1000×600 → 600×600", result.change_text)
        cropped = Image.open(result.path)
        self.assertEqual((600, 600), cropped.size)
        for x in (0, 300, 599):
            self.assertEqual((0, 200, 0), cropped.getpixel((x, 300)), "باید فقط وسط بماند")
        cropped.close()

    def test_a_tall_photo_loses_the_same_from_top_and_bottom(self) -> None:
        source = Image.new("RGB", (600, 1000), (0, 0, 255))
        source.paste((0, 200, 0), (0, 200, 600, 800))
        src = self.dir / "tall.png"
        source.save(src)

        cropped = Image.open(crop_square(src, self.dir / "out").path)

        self.assertEqual((600, 600), cropped.size)
        self.assertEqual((0, 200, 0), cropped.getpixel((300, 0)), "۲۰۰ پیکسل بالا رفته")
        self.assertEqual((0, 200, 0), cropped.getpixel((300, 599)), "و ۲۰۰ پیکسل پایین")

    def test_square_box_never_invents_or_drops_a_column(self) -> None:
        for width, height in ((1200, 800), (800, 1200), (401, 400), (1, 9), (3000, 3000)):
            left, top, right, bottom = square_box(width, height)
            self.assertEqual(right - left, min(width, height))
            self.assertEqual(bottom - top, min(width, height))
            self.assertGreaterEqual(left, 0)
            self.assertGreaterEqual(top, 0)
            self.assertLessEqual(right, width)
            self.assertLessEqual(bottom, height)
            # equal margins, one pixel apart at most (a square cannot split a pixel)
            self.assertLessEqual(abs(left - (width - right)), 1)
            self.assertLessEqual(abs(top - (height - bottom)), 1)

    def test_a_jpeg_keeps_its_own_quantization_tables_and_subsampling(self) -> None:
        src = _write_image(self.dir / "photo.jpg", (1200, 800), quality=93, subsampling=2)
        with Image.open(src) as before:
            tables = {index: list(values) for index, values in before.quantization.items()}
            sampling = list(before.layer)

        result = crop_square(src, self.dir / "out")

        with Image.open(result.path) as after:
            self.assertEqual(
                {index: list(values) for index, values in after.quantization.items()}, tables
            )
            self.assertEqual(list(after.layer), sampling)
            self.assertEqual((800, 800), after.size)

    def test_the_original_is_not_resampled_only_cropped(self) -> None:
        """A pixel inside the square must come back as the pixel that was sent."""
        src = self.dir / "drawn.png"          # PNG: lossless, so equality is exact
        image = Image.new("RGB", (900, 300), (10, 20, 30))
        image.putpixel((450, 150), (255, 255, 255))
        image.save(src)

        result = crop_square(src, self.dir / "out")

        cropped = Image.open(result.path)
        self.assertEqual((255, 255, 255), cropped.getpixel((450 - 300, 150)))

    def test_an_image_that_is_already_square_is_returned_untouched(self) -> None:
        src = _write_image(self.dir / "square.png", (640, 640))
        before = src.stat().st_mtime_ns

        result = crop_square(src, self.dir / "out")

        self.assertFalse(result.changed)
        self.assertEqual(src, result.path, "فایل تازه‌ای نوشته نشود")
        self.assertEqual(640, result.side)
        self.assertIn("همین حالا ۱:۱", result.change_text)
        self.assertEqual(before, src.stat().st_mtime_ns)

    def test_every_supported_format_round_trips(self) -> None:
        for name, fmt in (("a.jpg", "JPEG"), ("b.png", "PNG"), ("c.webp", "WEBP"),
                          ("d.bmp", "BMP"), ("e.tiff", "TIFF")):
            with self.subTest(name=name):
                src = self.dir / name
                Image.new("RGB", (800, 400), (5, 90, 200)).save(src, format=fmt)
                result = crop_square(src, self.dir / "out")
                with Image.open(result.path) as cropped:
                    self.assertEqual((400, 400), cropped.size)
                self.assertEqual(name.split(".")[0] + "_square", Path(result.path).stem)

    def test_a_rotated_phone_photo_is_squared_as_it_is_seen(self) -> None:
        """EXIF orientation is a tag: 1200×800 tagged «rotate» is really 800×1200."""
        src = self.dir / "rotated.jpg"
        exif = Image.Exif()
        exif[274] = 6                     # 90° clockwise
        Image.new("RGB", (1200, 800), (40, 40, 40)).save(src, quality=90, exif=exif.tobytes())

        result = crop_square(src, self.dir / "out")

        with Image.open(result.path) as cropped:
            self.assertEqual((800, 800), cropped.size)

    def test_refusals_are_explicit(self) -> None:
        not_image = self.dir / "notes.txt"
        not_image.write_text("hello", encoding="utf-8")
        with self.assertRaises(UnsupportedImage):
            crop_square(not_image, self.dir / "out")

        broken = self.dir / "broken.jpg"
        broken.write_bytes(b"not really a jpeg")
        with self.assertRaises(UnsupportedImage):
            crop_square(broken, self.dir / "out")

        self.assertIn("jpg", SUPPORTED)


@needs_telegram
class FlowWiring(unittest.TestCase):
    def test_the_button_exists_and_is_offered_to_admins(self) -> None:
        from bot.buttons import BY_KEY, feature_allowed

        button = BY_KEY["auto_crop"]
        self.assertEqual(button.callback, CB.CROP)
        self.assertTrue(button.admin_eligible, "باید بشود به ادمین‌ها داد")
        self.assertTrue(feature_allowed(SUDO, "auto_crop"))
        self.assertFalse(feature_allowed(999999, "auto_crop"), "کاربرِ بی‌نقش نباید دسترسی داشته باشد")

    def test_it_is_a_registered_flow_and_a_live_handler(self) -> None:
        from bot.services import flow_guard

        self.assertIn("auto_crop", flow_guard.registered())
        app = mock.Mock()
        auto_crop.register(app)
        handled = app.add_handler.call_args_list
        self.assertEqual(1, len(handled))
        conversation = handled[0].args[0]
        entries = [handler.pattern.pattern for handler in conversation.entry_points]
        self.assertEqual([f"^{CB.CROP}$"], entries)


@needs_telegram
class FlowBehaviour(unittest.IsolatedAsyncioTestCase):
    """The screen: what a sudo/admin gets back after sending a photo."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="tisa-crop-flow-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        p1 = mock.patch.object(auto_crop, "TEMP_DIR", self.dir / "tmp")
        p2 = mock.patch.object(auto_crop, "feature_allowed", return_value=True)
        for patcher in (p1, p2):
            patcher.start()
            self.addCleanup(patcher.stop)
        auto_crop._active_data.clear()

    def _context(self, *, bot):
        return SimpleNamespace(bot=bot, user_data={}, user=SimpleNamespace(id=ADMIN), chat_data={})

    async def test_a_wide_photo_comes_back_as_a_square_document(self) -> None:
        source = _write_image(self.dir / "wide.jpg", (1200, 800), quality=92, subsampling=2)

        class Bot:
            def __init__(self) -> None:
                self.documents: list[dict] = []
                self.messages: list[str] = []

            async def get_file(self, file_id):
                async def download_to_drive(custom_path, **_kwargs):
                    shutil.copyfile(source, custom_path)
                    return Path(custom_path)

                return SimpleNamespace(file_size=source.stat().st_size, download_to_drive=download_to_drive)

            async def send_document(self, *args, **kwargs):
                payload = dict(kwargs)
                # The real call streams the handle and closes it; keep the bytes now.
                payload["bytes"] = payload["document"].read()
                self.documents.append(payload)
                return SimpleNamespace(message_id=2)

            async def send_message(self, *args, **kwargs):
                self.messages.append(kwargs.get("text", ""))
                return SimpleNamespace(message_id=3)

        bot = Bot()
        context = self._context(bot=bot)
        message = _Message(self.dir, photo_file_id="f1")
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=ADMIN, first_name="t", username="t"),
            effective_message=message,
            callback_query=None,
        )

        result = await auto_crop.on_media(update, context)

        self.assertEqual(auto_crop.WAITING, result)
        self.assertEqual(1, len(bot.documents))
        document = bot.documents[0]
        self.assertIn("800×800", document["caption"])
        self.assertIn("1200×800", document["caption"])
        self.assertEqual(9, document["chat_id"], "همان چتی که عکس از آن آمد")
        saved = self.dir / "sent"
        saved.mkdir()
        target = saved / str(document["filename"])
        target.write_bytes(document["bytes"])
        with Image.open(target) as cropped:
            self.assertEqual((800, 800), cropped.size)
        self.assertTrue(message.replies, "باید وضعیت را گفته باشد")
        self.assertEqual([], [text for text in bot.messages if "❌" in text])

    async def test_an_already_square_photo_is_not_re_sent(self) -> None:
        source = _write_image(self.dir / "square.png", (500, 500))

        class Bot:
            def __init__(self) -> None:
                self.documents: list[dict] = []

            async def get_file(self, file_id):
                async def download_to_drive(custom_path, **_kwargs):
                    shutil.copyfile(source, custom_path)
                    return Path(custom_path)

                return SimpleNamespace(file_size=source.stat().st_size, download_to_drive=download_to_drive)

            async def send_document(self, *args, **kwargs):  # pragma: no cover - must not happen
                self.documents.append(dict(kwargs))

        bot = Bot()
        message = _Message(self.dir, photo_file_id="f2", name="square.png", mime="image/png")
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=ADMIN, first_name="t", username="t"),
            effective_message=message,
            callback_query=None,
        )

        await auto_crop.on_media(update, self._context(bot=bot))

        self.assertEqual([], bot.documents, "عکسِ ۱:۱ نباید دوباره فرستاده شود")
        self.assertTrue(any("دست نزدم" in str(kwargs) for _kind, text, kwargs in message.replies)
                        or any("دست نزدم" in str(text) for _kind, text, _kwargs in message.replies))

    async def test_a_broken_file_explains_itself_instead_of_crashing(self) -> None:
        source = self.dir / "broken.jpg"
        source.write_bytes(b"not an image")

        class Bot:
            async def get_file(self, file_id):
                async def download_to_drive(custom_path, **_kwargs):
                    shutil.copyfile(source, custom_path)
                    return Path(custom_path)

                return SimpleNamespace(file_size=11, download_to_drive=download_to_drive)

        message = _Message(self.dir, photo_file_id="f3")
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=ADMIN, first_name="t", username="t"),
            effective_message=message,
            callback_query=None,
        )

        await auto_crop.on_media(update, self._context(bot=Bot()))

        self.assertTrue(any("❌" in str(text) for _kind, text, _kwargs in message.replies))

    async def test_text_says_what_is_needed_instead_of_being_ignored(self) -> None:
        message = _Message(self.dir, photo_file_id="ignored")
        message.text = "سلام"
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=ADMIN, first_name="t", username="t"),
            effective_message=message,
            callback_query=None,
        )
        await auto_crop.on_text(update, self._context(bot=SimpleNamespace()))

        self.assertTrue(message.replies)
        self.assertIn("عکس", str(message.replies[0][1]))

    async def test_a_stranger_is_turned_away_at_the_door(self) -> None:
        seen: list[tuple] = []

        class Query:
            data = CB.CROP
            message = None

            async def answer(self, text=None, **kwargs):
                seen.append((text, kwargs))

            async def edit_message_text(self, *args, **kwargs):  # pragma: no cover - must not run
                seen.append(("edit", args))

        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=999999, first_name="x", username="x"),
            effective_message=None,
            callback_query=Query(),
        )
        with mock.patch.object(auto_crop, "feature_allowed", return_value=False):
            result = await auto_crop.entry(update, self._context(bot=SimpleNamespace()))

        self.assertEqual(-1, result)                      # ConversationHandler.END
        self.assertNotIn(999999, auto_crop._active_data)
        self.assertEqual(1, len(seen))
        self.assertIn("⛔", str(seen[0][0]))
        self.assertTrue(seen[0][1].get("show_alert"))

    async def test_closing_the_flow_drops_its_leftover_files(self) -> None:
        leftover = self.dir / "tmp" / f"{ADMIN}_stale"
        leftover.mkdir(parents=True)
        (leftover / "half.jpg").write_bytes(b"x")

        self.assertTrue(auto_crop.close_for(ADMIN))

        self.assertFalse(leftover.exists())
        self.assertFalse(auto_crop.close_for(ADMIN), "بستنِ دوباره کاری نمی‌کند")

    async def test_nothing_is_left_behind_in_the_workspace(self) -> None:
        source = _write_image(self.dir / "wide.jpg", (600, 300))

        class Bot:
            async def get_file(self, file_id):
                async def download_to_drive(custom_path, **_kwargs):
                    shutil.copyfile(source, custom_path)
                    return Path(custom_path)

                return SimpleNamespace(file_size=source.stat().st_size, download_to_drive=download_to_drive)

            async def send_document(self, *args, **kwargs):
                return SimpleNamespace(message_id=2)

        message = _Message(self.dir, photo_file_id="f4")
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=ADMIN, first_name="t", username="t"),
            effective_message=message,
            callback_query=None,
        )
        await auto_crop.on_media(update, self._context(bot=Bot()))

        root = self.dir / "tmp"
        leftovers = list(root.iterdir()) if root.exists() else []
        self.assertEqual([], leftovers, "فضای موقت باید پاک شود")


class _Message:
    """The parts of a Telegram message this flow touches, with replies recorded."""

    def __init__(self, directory: Path, *, photo_file_id: str, name: str = "photo.jpg",
                 mime: str = "image/jpeg") -> None:
        self.chat_id = 9
        self.message_id = 42
        self.caption = ""
        self.text = ""
        self.photo = [SimpleNamespace(file_id=photo_file_id)]
        self.document = SimpleNamespace(
            file_id=photo_file_id,
            file_name=name,
            mime_type=mime,
        )
        self.replies: list[tuple[str, object, dict]] = []
        self.directory = directory

    async def reply_text(self, text=None, **kwargs):
        self.replies.append(("text", text, kwargs))
        return _Status(self.replies)


class _Status:
    def __init__(self, sink: list) -> None:
        self.sink = sink

    async def delete(self) -> None:
        self.sink.append(("delete", None, {}))

    async def edit_text(self, text=None, **kwargs) -> None:  # pragma: no cover - unused
        self.sink.append(("edit", text, kwargs))


__all__ = ["CropMaths", "FlowBehaviour", "FlowWiring"]
