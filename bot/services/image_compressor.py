"""JPEG re-encoding for product photos (فشرده‌سازی عکس‌ها)."""
from __future__ import annotations

import re
from pathlib import Path

from PIL import Image, ImageOps

from bot.config import settings

SUPPORTED = {"jpg", "jpeg", "png", "webp", "bmp", "tiff"}

# Allow generously sized product photos but block decompression bombs that
# would exhaust memory on shared hosting.
Image.MAX_IMAGE_PIXELS = 120_000_000

# The store sometimes wants a heavier image; reading the quality per call (and
# not once at import) means an `.env` edit only needs a restart, not a deploy.
_QUALITY_RANGE = re.compile(r"\d+")


def _quality() -> int:
    return max(1, min(100, int(settings.image_quality)))


def compressed_size_savings(original: int, compressed: int) -> int:
    if original <= 0:
        return 0
    return max(0, round((1 - compressed / original) * 100))


def compress_image(src: Path, dest_dir: Path) -> Path:
    """Re-encode an image as JPEG with high visual quality and lower file size.

    Output is always .jpg. Dimensions are preserved. EXIF orientation is applied so
    the image is not rotated incorrectly. Alpha is composited onto white because
    JPEG does not support transparency.

    Anything we cannot decode safely (unknown extension, decompression bomb) is
    returned unchanged: dropping a product photo is worse than a large one.
    """
    suffix = src.suffix.lower().lstrip(".")
    if suffix not in SUPPORTED:
        return src

    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / f"{src.stem}_compressed.jpg"

    try:
        with Image.open(src) as opened:
            # exif_transpose returns a new image when the orientation tag needs
            # applying, so keep it in its own variable.
            im = ImageOps.exif_transpose(opened)

            if "A" in im.getbands():
                rgba = im.convert("RGBA")
                background = Image.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.getchannel("A"))
                save_im = background
            else:
                save_im = im.convert("RGB")

            save_im.save(
                out,
                format="JPEG",
                quality=_quality(),
                # Huffman optimization makes Pillow scan the pixels again. For
                # shop photos that are already Telegram-sized it saves very few
                # bytes while noticeably delaying a multi-image batch.
                optimize=False,
                progressive=True,
                subsampling="4:2:0",
            )
    except (Image.DecompressionBombError, OSError, ValueError):
        # Too large/broken to decode safely; keep the original instead of failing.
        return src

    if out.exists() and out.stat().st_size >= src.stat().st_size:
        return src          # never publish a "compressed" image that is bigger
    return out
