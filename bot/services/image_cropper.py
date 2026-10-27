"""Center-crop a photo to a 1:1 square — and change nothing else.

The shop wants square images; the seller does not want the bot deciding how much
quality to throw away on the way. Those two wishes are compatible only if this
module is strictly a *crop*:

* **No resampling.** Pixels inside the square are the pixels that were sent —
  ``Image.crop`` copies; it never filters, so nothing is blurred or sharpened.
* **No re-tuned quality.** A JPEG is re-encoded with *its own* quantization tables
  and its own chroma subsampling (``quality="keep"`` / ``subsampling="keep"``), so
  the encoder settings are byte-for-byte the ones the photographer chose. PNG is
  written losslessly, WEBP as ``lossless=True``.
* **No downscaling.** A 4000×3000 photo becomes 3000×3000, not 1080×1080.
* **Equal margins.** The square is centred, so both sides lose the same number of
  pixels (one pixel may differ on odd dimensions — that is arithmetic, not choice).
* **Already square → untouched.** The original file is handed back as-is; there is
  nothing to improve, so nothing is re-encoded at all.

Everything the caller needs to report is in :class:`CropResult`, so the chat module
never has to guess what happened.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

#: Extensions we can crop. Anything else is refused *before* the work starts: a
#: half-processed file the user cannot open is worse than a clear «این فرمت را
#: نمی‌توانم».
SUPPORTED = {"jpg", "jpeg", "png", "webp", "bmp", "tiff", "tif"}

# Allow generously sized product photos but block decompression bombs that would
# exhaust memory on shared hosting.
MAX_IMAGE_PIXELS = 40_000_000
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS


class UnsupportedImage(ValueError):
    """The file is not something this module can crop (or it is unsafe to open)."""


@dataclass(frozen=True)
class CropResult:
    """What one crop produced, in the words the chat screen needs."""

    path: Path
    #: size of the input as the user sees it (EXIF orientation already applied)
    source_width: int
    source_height: int
    #: side of the resulting square — both output dimensions are this
    side: int
    #: False when the input was already 1:1: ``path`` is then the *original* file
    changed: bool

    @property
    def size_text(self) -> str:
        return f"{self.side}×{self.side}"

    @property
    def change_text(self) -> str:
        """«۱۲۰۰×۸۰۰ → ۸۰۰×۸۰۰», the one line worth reading in a chat."""
        if not self.changed:
            return f"{self.source_width}×{self.source_height} (همین حالا ۱:۱)"
        return f"{self.source_width}×{self.source_height} → {self.size_text}"


def square_box(width: int, height: int) -> tuple[int, int, int, int]:
    """The centred square inside a ``width``×``height`` image, as a PIL crop box.

    Equal margins on both sides: for an odd difference the extra pixel stays on the
    right/bottom, because a square cannot be split in half a pixel.
    """
    side = min(width, height)
    left = (width - side) // 2
    top = (height - side) // 2
    return left, top, left + side, top + side


def _dest_name(src: Path) -> str:
    suffix = src.suffix if src.suffix else ".jpg"
    return f"{src.stem}_square{suffix}"


def _save_unchanged_quality(
    cropped: Image.Image, opened: Image.Image, dest: Path, image_format: str
) -> None:
    """Write ``cropped`` keeping the source's own encoder settings.

    The two ``"keep"`` values are the whole point of this module: re-encoding a JPEG
    at ``quality=85`` would quietly degrade every cropped photo, and no caption could
    apologize for that. Pillow needs the format, the quantization tables and the
    subsampling (``layer``) on the *image object* to be able to honour them.
    """
    options: dict[str, Any] = {"format": image_format}
    if image_format == "JPEG":
        # Pillow reads the source's tables/subsampling off the image object; the two
        # "keep" values below are only legal once they are carried over.
        cropped.format = "JPEG"  # type: ignore[attr-defined]
        cropped.quantization = opened.quantization  # type: ignore[attr-defined]
        cropped.layer = list(opened.layer)  # type: ignore[attr-defined]
        options["quality"] = "keep"
        options["subsampling"] = "keep"
        options["progressive"] = bool(opened.info.get("progressive", False))
        if opened.info.get("exif"):
            options["exif"] = opened.info["exif"]
        if opened.info.get("dpi"):
            options["dpi"] = opened.info["dpi"]
    elif image_format == "WEBP":
        options["lossless"] = True
    if opened.info.get("icc_profile"):
        options["icc_profile"] = opened.info["icc_profile"]
    cropped.save(dest, **options)


def crop_square(src: Path, dest_dir: Path) -> CropResult:
    """Crop ``src`` to a centred 1:1 square into ``dest_dir``; never resample.

    Raises :class:`UnsupportedImage` for a file this module cannot handle safely, so
    the caller can say why instead of sending a broken image back.
    """
    suffix = src.suffix.lower().lstrip(".")
    if suffix not in SUPPORTED:
        raise UnsupportedImage(f"فرمت «{suffix or 'نامشخص'}» پشتیبانی نمی‌شود.")

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            opened = Image.open(src)
            opened.load()
    except (Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        raise UnsupportedImage("تصویر بیش از حد بزرگ است.") from exc
    except (OSError, ValueError) as exc:  # unreadable/truncated file
        raise UnsupportedImage("فایل به‌عنوان تصویر باز نشد.") from exc

    with opened:
        if opened.width * opened.height > MAX_IMAGE_PIXELS:
            raise UnsupportedImage("تصویر بیش از حد بزرگ است.")
        if getattr(opened, "is_animated", False) and getattr(opened, "n_frames", 1) > 1:
            raise UnsupportedImage("تصویر متحرک (GIF/انیمیشن) پشتیبانی نمی‌شود.")
        image_format = (opened.format or "JPEG").upper()
        # Orientation is a tag, not pixels: a phone photo «۱۲۰۰×۸۰۰» is often really
        # 800×1200. Transposing first means the square is centred on what the user sees.
        upright = ImageOps.exif_transpose(opened)
        width, height = upright.size
        if width == height:
            return CropResult(src, width, height, width, False)

        box = square_box(width, height)
        cropped = upright.crop(box)
        side = box[2] - box[0]
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / _dest_name(src)
        if dest.resolve() == src.resolve():  # pragma: no cover - defensive naming clash
            dest = dest_dir / f"{src.stem}_1x1{src.suffix}"
        try:
            _save_unchanged_quality(cropped, opened, dest, image_format)
        except (OSError, ValueError) as exc:
            raise UnsupportedImage(f"نوشتن تصویر ممکن نشد: {type(exc).__name__}") from exc
    return CropResult(dest, width, height, side, True)


__all__ = ["SUPPORTED", "CropResult", "UnsupportedImage", "crop_square", "square_box"]
