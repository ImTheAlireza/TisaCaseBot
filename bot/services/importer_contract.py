"""قرارداد افزونهٔ واردکنندهٔ ZIP — چه چیزی از بستهٔ ربات روی سایت می‌نشیند.

ربات و افزونه دو چیز جدا هستند که فقط یک فایل میان‌شان می‌گذرد: ``product.zip``.
ربات از نسخهٔ ۰٫۱۱٫۰ سه کلیدِ ``sale_price`` / ``stock`` / ``stock_status`` را داخل بسته
می‌گذارد، ولی افزونهٔ ۰٫۷٫۰ آن‌ها را **نمی‌خواند** — یعنی محصولی که با ZIP وارد شود
بدون تخفیف و موجودی ساخته می‌شود، بدون هیچ خطایی (مسیرِ REST هر سه را می‌نویسد).

این‌جا همان جمله‌ای است که ``--check-config`` از روی *فایلِ واقعیِ
افزونه* جواب می‌دهد، نه از روی حرفِ README: نسخه از هدرِ خودِ ``.php`` داخل zip خوانده
می‌شود. اگر روزی افزونه از source ساخته و تازه شود، عدد همین‌جا عوض می‌شود و خطای
زردِ صفحه هم خودبه‌خود می‌رود.
"""

from __future__ import annotations

import logging
import re
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

#: افزونهٔ کنارِ ربات: همان فایلی که در ``wp-admin`` نصب می‌شود.
PLUGIN_ZIP = Path(__file__).resolve().parents[2] / "tisa-product-importer.zip"

#: از این نسخه به بعد، افزونه ``sale_price`` / ``stock`` / ``stock_status`` را می‌خواند.
#: یک ترفندِ نسخه‌ای نیست: هرچه پایین‌تر از این باشد، یعنی آن سه کلید در بسته هست ولی
#: روی سایت نوشته نمی‌شود — و کاربر باید بداند کدام محصول‌هایش تخفیف‌شان را باخته‌اند.
MIN_VERSION_FOR_PRICE_FIELDS = (0, 8)

#: سه کلیدی که نسخه‌های قدیمی بی‌خواندن رد می‌کنند (برای متنِ هشدار، در یک جا).
PRICE_FIELDS = "sale_price / stock / stock_status"

_VERSION_RE = re.compile(r"^[ \t\**/]*Version:[ \t]*([0-9]+(?:\.[0-9]+)*)", re.MULTILINE)


def _plugin_php(root: Path) -> str | None:
    """متن فایل اصلی افزونه از داخل zip (``None`` اگر فایل نباشد یا خوانده نشود)."""
    try:
        with zipfile.ZipFile(root) as archive:
            for name in archive.namelist():
                if name.endswith("tisa-product-importer.php"):
                    return archive.read(name).decode("utf-8", "replace")
    except (OSError, zipfile.BadZipFile) as exc:
        logger.debug("importer contract: cannot read %s (%s)", root, exc)
    return None


def read_version(root: Path | None = None) -> tuple[int, ...] | None:
    """نسخهٔ اعلام‌شدهٔ افزونه، یا ``None`` وقتی فایل نیست/خوانده نمی‌شود."""
    path = root or PLUGIN_ZIP
    if not path.exists():
        return None
    text = _plugin_php(path)
    if not text:
        return None
    match = _VERSION_RE.search(text)
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def version_text(version: tuple[int, ...] | None) -> str:
    return ".".join(str(part) for part in version) if version else "نامشخص"


def describe(root: Path | None = None) -> tuple[str, str, str]:
    """``(state, detail, fix)`` برای ``--check-config``.

    ``warn`` یعنی «منتشر کردن کار می‌کند، ولی نتیجهٔ ZIP با چیزی که ربات ساخته یکی
    نیست»: نه بلا است، نه می‌توان نادیده‌اش گرفت.
    """
    version = read_version(root)
    path = root or PLUGIN_ZIP
    if version is None:
        return (
            "warn",
            f"فایل افزونه کنار ربات نیست ({path.name})",
            "اگر محصولات را با ZIP وارد می‌کنی، نسخهٔ نصب‌شده روی سایت را از "
            "wp-admin ببین؛ سازگاری‌اش در docs/IMPORTER-CONTRACT.md نوشته شده.",
        )
    if version < MIN_VERSION_FOR_PRICE_FIELDS:
        return (
            "warn",
            f"نسخهٔ افزونه: {version_text(version)} — {PRICE_FIELDS} را نمی‌خواند",
            f"محصولِ ZIPی بدون تخفیف/موجودی ساخته می‌شود. یا افزونه را به "
            f"≥ {'.'.join(str(part) for part in MIN_VERSION_FOR_PRICE_FIELDS)} تازه کن، "
            "یا همان محصول را با «📤 انتشار مستقیم» (REST) بفرست که هر سه را می‌نویسد.",
        )
    return "ok", f"نسخهٔ افزونه: {version_text(version)} — بستهٔ ربات کامل خوانده می‌شود", ""


__all__ = [
    "MIN_VERSION_FOR_PRICE_FIELDS",
    "PLUGIN_ZIP",
    "PRICE_FIELDS",
    "describe",
    "read_version",
    "version_text",
]
