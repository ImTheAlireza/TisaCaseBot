"""Barcode/tracking-code helpers shared by ``processor.py`` and the bot's tests.

The Tisa systems export a 24-digit barcode, but Excel happily turns such a long
number into a float and destroys it (``1.93E+23``). Detecting that is cheap;
the important part is what we do about it — **nothing may enter the output CSV
that we know is broken**, because the file is imported straight into the
tracking system.
"""
from __future__ import annotations

import re

from bot.config import settings

_SCIENTIFIC_RE = re.compile(r"[eE][+-]?\d+|\.\d*[1-9]$|^\d+\.\d+$")



# IEEE-754 doubles keep ~15-17 significant digits. A 24-digit barcode stored as
# a *number* is therefore always rounded — and openpyxl hands it back as a plain
# int, so the cell type alone is not enough: length is the reliable signal.
_FLOAT_SAFE_DIGITS = 16


def _stored_as_number(raw: object) -> bool:
    """Whether the cell came back as a number — the fact Excel rounds on."""
    return isinstance(raw, (int, float)) and not isinstance(raw, bool)


def looks_float_destroyed(raw: object, cleaned: str) -> bool:
    """True when a barcode was stored as a number and lost its digits."""
    if cleaned and _SCIENTIFIC_RE.search(cleaned):
        return True                       # «1.93E+23» survived as text
    return _stored_as_number(raw) and len(cleaned or "") > _FLOAT_SAFE_DIGITS


def number_is_suspicious(raw: object, cleaned: str) -> bool:
    """Numeric cell that is short enough to be exact, but still worth a warning."""
    return _stored_as_number(raw) and bool(cleaned) and len(cleaned) <= _FLOAT_SAFE_DIGITS


#: GTIN lengths whose last digit is a mod-10 check digit (EAN-8, UPC-A, EAN-13,
#: ITF-14). A warehouse sheet is full of these: the product's own barcode, pasted
#: next to the tracking code. They are real barcodes, but a mistyped one passes a
#: length test happily — which is why a length we know how to verify is verified.
CHECK_DIGIT_LENGTHS = frozenset({8, 12, 13, 14})

_CHECK_NAMES = {8: "EAN-8", 12: "UPC-A", 13: "EAN-13", 14: "ITF-14"}


def gtin_check_digit(data: str) -> int:
    """The check digit a GTIN must end with (the same rule for every length)."""
    total = sum(int(ch) * (3 if index % 2 == 0 else 1) for index, ch in enumerate(reversed(data)))
    return (10 - total % 10) % 10


def classify(cleaned: str) -> tuple[str, str]:
    """``(state, note)`` for a barcode that survived the float checks.

    ``state`` is ``"ok"`` (write it), ``"warn"`` (write it, say something) or
    ``"error"`` (never write it). Length is the shop's own rule
    (``BARCODE_LENGTHS``); a GTIN length additionally has to add up, because
    «بارکد ۱۳ رقمی» with a wrong last digit is a typo, not a product code.
    """
    digits = cleaned or ""
    digits = digits.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    if not re.fullmatch(r"[0-9]+", digits):
        return "error", "بارکد عدد نیست"
    if len(digits) in settings.barcode_lengths:
        return "ok", ""
    if len(digits) in CHECK_DIGIT_LENGTHS:
        name = _CHECK_NAMES[len(digits)]
        if gtin_check_digit(digits[:-1]) == int(digits[-1]):
            return "warn", f"بارکد {name} است، نه کد رهگیری تیسا — اگر مطمئنی درست است، می‌ماند"
        return "error", (
            f"بارکد {name}: رقم کنترلی نمی‌خورد "
            f"(آخرش باید {gtin_check_digit(digits[:-1])} باشد)"
        )
    allowed = "/".join(str(x) for x in sorted(settings.barcode_lengths))
    return "error", f"بارکد نامعتبر: {len(digits)} رقم (باید {allowed} رقم باشد)"


def order_code_is_valid(cleaned: str) -> bool:
    return bool(re.fullmatch(r"[0-9]{6}", cleaned or ""))


def order_code_is_short(cleaned: str) -> bool:
    return bool(re.fullmatch(r"[0-9]{5}", cleaned or ""))


__all__ = [
    "CHECK_DIGIT_LENGTHS",
    "classify",
    "gtin_check_digit",
    "looks_float_destroyed",
    "number_is_suspicious",
    "order_code_is_short",
    "order_code_is_valid",
]
