"""Per-model color matrix for real-world phone-case Telegram posts.

A typical post lists every phone model together with the colors that are
actually in stock for *that* model::

    Apple
    📱17promax :
    سفید/مشکی/نارنجی
    📱17pro :
    مشکی
    ...
    xiaomi (فقط سفید)
    📱Note 14 pro 4g

WooCommerce, however, only has flat product attributes. The right mapping is:

* the **color attribute** carries the union of every color mentioned in the
  post (so the drop-down stays complete), while
* the **variations** are built only from the model↔color pairs the seller
  actually listed (so iPhone 17 Pro never gets a «سفید» variation).

This module is the deterministic half of that mapping: it parses the post into
a :class:`ColorMatrix` and exposes the combination builder that both the
WooCommerce writer and the Telegram preview use, so the number shown to the
user is the number of variations that will really be created.

Model keys are matched by *signature* (brand words, spaces, ZWNJ, ``+`` and
``pro max``/``promax`` spelling removed) instead of by exact string, because the
final model labels may come from the AI normalizer rather than from the
deterministic parser.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any
from collections.abc import Iterable, Sequence

from bot.services.phone_parser import EMOJI_RANGES_RE, fold_variant_words
from bot.services.airpods_parser import (
    AIRPODS_START_RE,
    canonical_airpods_model,
    extract_airpods_models,
    is_airpods_attribute,
)

# ---------------------------------------------------------------------------
# Text normalization
# ---------------------------------------------------------------------------

# ZWNJ / ZWJ / bidi marks become a *space* (not removed) so that «سرمه‌ای»
# normalizes to «سرمه ای» and stays matchable as two tokens.
INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\u2066-\u2069\ufeff]")

_LETTER_FIXES = str.maketrans({"ي": "ی", "ك": "ک", "ة": "ه", "ۀ": "ه", "إ": "ا", "أ": "ا"})


def normalize_text(text: str) -> str:
    """Strip emojis/invisible marks and unify Arabic-Persian letter variants."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = INVISIBLE_RE.sub(" ", text)
    text = EMOJI_RANGES_RE.sub(" ", text)
    out: list[str] = []
    for ch in text:
        category = unicodedata.category(ch)
        if category in {"So", "Sk"} or ch in {"\ufe0e", "\ufe0f", "\u20e3", "\u200d"}:
            out.append(" ")
        else:
            out.append(ch)
    text = "".join(out).translate(_LETTER_FIXES)
    text = text.replace("：", ":").replace("–", "-").replace("—", "-")
    return re.sub(r"[ \t\u00a0]+", " ", text)


def _squash(value: str) -> str:
    """Remove spaces/ZWNJ/punctuation so «Pro Max» == «promax» == «pro max»."""
    return re.sub(r"[\s\u200c_/\\,.;:!?\-–—()（）\[\]{}«»\"'’`*+•·|]+", "", value or "")


# ---------------------------------------------------------------------------
# Color vocabulary
# ---------------------------------------------------------------------------

# canonical display name -> surface spellings seen in real posts.
# Longer spellings are matched first, so «مشکی مات» never collapses to «مشکی».
COLOR_LEXICON: dict[str, tuple[str, ...]] = {
    "سفید": ("سفید", "سفيد", "وایت", "white"),
    "سفید صدفی": ("سفید صدفی", "صدفی سفید", "pearl white", "white pearl"),
    "استخوانی": ("استخوانی", "bone", "off white", "offwhite"),
    "شیری": ("شیری", "milky"),
    "کرم": ("کرم", "cream", "creme"),
    "بژ": ("بژ", "beige", "بیژ"),
    "نود": ("نود", "nude"),
    "شتری": ("شتری", "camel"),
    "عسلی": ("عسلی", "honey"),
    "قهوه‌ای": ("قهوه ای", "قهوه‌ای", "brown", "براون", "شکلاتی", "chocolate"),
    "نسکافه‌ای": ("نسکافه ای", "نسکافه‌ای", "nescafe"),
    "مشکی": ("مشکی", "مشكي", "سیاه", "black", "بلک"),
    "مشکی مات": ("مشکی مات", "مات مشکی", "matte black", "black matte"),
    "زغالی": ("زغالی", "ذغالی", "charcoal"),
    "گرافیت": ("گرافیت", "graphite"),
    "طوسی": ("طوسی", "خاکستری", "دودی", "gray", "grey", "گری"),
    "نقره‌ای": ("نقره ای", "نقره‌ای", "silver", "سیلور"),
    "کروم": ("کروم", "chrome"),
    "سرمه‌ای": ("سرمه ای", "سرمه‌ای", "سورمه ای", "navy", "آبی نفتی"),
    "آبی": ("آبی", "blue", "بلو"),
    "آبی آسمانی": ("آبی آسمانی", "آسمانی", "sky blue", "sky"),
    "فیروزه‌ای": ("فیروزه ای", "فیروزه‌ای", "turquoise"),
    "سیرابلو": ("سیرابلو", "سیرا بلو", "آبی سیرا", "sierra blue", "sierrablue"),
    "میدنایت": ("میدنایت", "midnight"),
    "استارلایت": ("استارلایت", "starlight", "star light"),
    "یاسی": ("یاسی", "lilac", "lavender", "لوندر"),
    "بنفش": ("بنفش", "purple", "ویولت", "violet", "ارغوانی"),
    "سبز": ("سبز", "green", "گرین"),
    "سبز یشمی": ("سبز یشمی", "یشمی", "jade"),
    "زیتونی": ("زیتونی", "olive"),
    "نعنایی": ("نعنایی", "نعنائی", "mint"),
    "فسفری": ("فسفری", "neon", "شبرنگ"),
    "قرمز": ("قرمز", "red"),
    "زرشکی": ("زرشکی", "burgundy", "شرابی", "مارون", "maroon", "wine", "گیلاسی", "cherry"),
    "آجری": ("آجری", "brick", "تراکوتا", "terracotta"),
    "صورتی": ("صورتی", "صورتي", "pink", "پینک"),
    "سرخابی": ("سرخابی", "magenta", "ماژنتا"),
    "گلبهی": ("گلبهی", "peach", "salmon"),
    "مرجانی": ("مرجانی", "coral"),
    "نارنجی": ("نارنجی", "orange", "اورنج"),
    "زرد": ("زرد", "yellow", "یلو"),
    "لیمویی": ("لیمویی", "لیموئی", "lemon"),
    "خردلی": ("خردلی", "mustard"),
    "طلایی": ("طلایی", "طلائی", "gold", "گلد"),
    "رزگلد": ("رزگلد", "رز گلد", "rose gold", "rosegold"),
    "نچرال": ("نچرال", "natural", "ناتورال", "نچرال تیتانیوم"),
    "دیزرت": ("دیزرت", "دزرت", "desert", "دیزرت تیتانیوم"),
    "تیتانیوم": ("تیتانیوم", "titanium"),
    "خاکی": ("خاکی", "khaki"),
    "شفاف": ("شفاف", "بی رنگ", "بیرنگ", "بی‌رنگ", "clear", "transparent", "کریستال", "crystal"),
    "مات": ("مات", "matte", "matt"),
    "براق": ("براق", "شاین", "shiny", "shine", "gloss", "glossy"),
    "پولکی": ("پولکی", "اکلیلی", "glitter", "sparkle", "شنی"),
    "هولوگرام": ("هولوگرام", "هولوگرافیک", "holographic", "hologram"),
    "رنگین‌کمانی": ("رنگین کمانی", "رنگین‌کمانی", "rainbow"),
}

# Words that sit next to a color but are not colors themselves.
COLOR_STOPWORDS = {
    "فقط", "تنها", "صرفا", "صرفاً", "رنگ", "رنگبندی", "رنگ بندی", "موجود",
    "ناموجود", "شارژ", "شد", "مجدد", "جدید", "و", "یا", "هم", "با", "بدون",
    "سلفی", "تمام", "همه", "مدل", "لیست", "جور", "غیره", "دارد", "ندارد",
    "تک", "یکی", "نوع", "جنس", "طرح", "قاب", "کاور", "گلس", "گوشی", "موبایل",
    "color", "colour", "colors", "colours", "only", "just", "and", "or", "the",
    "in", "stock", "out", "of",
}

# A brand, model or material word can never be a color, not even via fallback.
_NON_COLOR_WORDS = set(COLOR_STOPWORDS) | {
    "apple", "iphone", "samsung", "galaxy", "xiaomi", "redmi", "poco", "huawei",
    "honor", "nokia", "motorola", "airpods", "airpod", "watch", "tab", "tablet",
    "pro", "max", "plus", "mini", "air", "ultra", "fe", "note", "lite", "prime",
    "اپل", "آیفون", "ایفون", "سامسونگ", "گلکسی", "شیائومی", "ردمی", "پوکو",
    "هواوی", "آنر", "نوکیا", "موتورولا", "ایرپاد", "ایرپادز", "واچ", "تبلت",
    "پرو", "مکس", "پلاس", "مینی", "ایر", "اولترا", "نوت", "لایت",
    "promax", "pro max", "airskin", "airsin",
    # Materials / shapes that sit in the same parentheses as a color.
    "سیلیکونی", "سیلیکون", "ژله ای", "ژله‌ای", "چرم", "چرمی", "پلاستیک",
    "پلاستیکی", "فلز", "فلزی", "شیشه ای", "شیشه‌ای", "کربن", "طلق", "سخت",
    "نرم", "ضخیم", "نازک", "گارد", "بند", "هولدر", "استند", "مگنت", "مگنتی",
    "silicone", "leather", "plastic", "metal", "glass", "carbon", "soft", "hard",
}


def _variant_key(variant: str) -> str:
    return _squash(normalize_text(variant)).casefold()


def _build_color_patterns() -> list[tuple[str, str]]:
    """Return ``(regex_source, canonical)`` pairs, longest surface form first."""
    entries: list[tuple[str, str]] = []
    for canonical, variants in COLOR_LEXICON.items():
        for variant in variants:
            surface = normalize_text(variant).strip()
            if surface:
                entries.append((surface, canonical))
    # Longest first so a compound color wins over its own prefix.
    entries.sort(key=lambda item: len(item[0]), reverse=True)
    seen: set[str] = set()
    patterns: list[tuple[str, str]] = []
    for surface, canonical in entries:
        escaped = re.escape(surface).replace(r"\ ", r"\s+")
        if escaped in seen:
            continue
        seen.add(escaped)
        patterns.append((escaped, canonical))
    return patterns


_COLOR_PATTERNS = _build_color_patterns()
# «چسبنده» یعنی کاراکتری که رنگ را به همسایه‌اش وصل می‌کند و مرز کلمه را می‌شکند.
# فهرستِ حرف‌ها دستی انتخاب شده (نه بازۀ \u0600-\u06FF)، چون جداکننده‌های فارسی/عربی
# (، ؛ ؟ و اعراب) *داخل* همان بازه‌اند: با نسخۀ قبلی، «مشکی، سفید» این‌طور خوانده
# می‌شد که «مشکی» به ویرگول چسبیده پس رنگِ شناخته‌شده نیست؛ از مسیر «رنگ ناشناس»
# می‌آمد، ترتیب رنگ‌ها برعکس می‌شد و «سیاه،» هرگز به «مشکی» اصلاح نمی‌شد.
_GLUE = r"[0-9A-Za-z\u0621-\u063A\u0641-\u064A\u0660-\u0669\u0671-\u06D3\u06F0-\u06F9\u06FA-\u06FC]"
_LEADING_BOUNDARY = rf"(?<!{_GLUE})"
_TRAILING_BOUNDARY = rf"(?!{_GLUE})"
_COLOR_RE = re.compile(
    "|".join(_LEADING_BOUNDARY + source + _TRAILING_BOUNDARY for source, _ in _COLOR_PATTERNS),
    re.IGNORECASE,
)
_CANONICAL_BY_KEY: dict[str, str] = {_variant_key(c): c for c in COLOR_LEXICON}
_CANONICAL_BY_KEY.update({_variant_key(v): c for c, vs in COLOR_LEXICON.items() for v in vs})

_TOKEN_SPLIT_RE = re.compile(r"[/،,|+•\s]+|\s+و\s+")
_PLAIN_TOKEN_RE = re.compile(r"^[\u0600-\u06FFA-Za-z\u200c]{2,18}$")
# List separators: an unknown word only counts as a color when it is written as
# part of a list («سفید/لاجوردی», «سفید و لاجوردی»). That keeps a typo'd filler
# («فقت سفید») and a SKU tail («SKU: AS») out of the color attribute.
_DELIMITER_CHARS = "/،,|+•"
_SKU_LIKE_RE = re.compile(r"^[A-Z]{1,4}$")


def color_key(value: str) -> str:
    """A comparison key for a color value (canonical when it is a known color)."""
    squashed = _squash(normalize_text(str(value or ""))).casefold()
    if squashed in _CANONICAL_BY_KEY:
        return _variant_key(_CANONICAL_BY_KEY[squashed])
    return squashed


def _delimiter_attached(text: str, start: int, end: int) -> bool:
    before = text[:start].rstrip()
    after = text[end:].lstrip()
    if before and before[-1] in _DELIMITER_CHARS:
        return True
    if after and after[0] in _DELIMITER_CHARS:
        return True
    return before.endswith(" و") or after.startswith("و ")


def _fallback_token(token: str) -> bool:
    """Is an unknown token plausible enough to be treated as a color name?"""
    token = token.strip()
    if not token or len(token) > 18 or any(char.isdigit() for char in token):
        return False
    if not _PLAIN_TOKEN_RE.match(token.replace(" ", "")):
        return False
    words = token.split()
    if len(words) > 2:
        return False
    if any(word.casefold() in _NON_COLOR_WORDS for word in words):
        return False
    # Uppercase short Latin tokens are SKU prefixes (BO, CH, AS), not colors.
    return not _SKU_LIKE_RE.match(token)


def extract_colors(segment: str, allow_unknown: bool = False) -> list[str]:
    """Pull every color out of a text fragment, in order of appearance.

    Known colors are canonicalized («سیاه» -> «مشکی»). With ``allow_unknown`` a
    brand-new color name survives too, which matters because dropping it would
    silently delete a model's variation:

    * in a fragment that already holds a known color («سفید/لاجوردی»,
      «سلفی مشکی») every other short token is accepted unless it is a
      brand/model/filler word;
    * in a fragment with no known color at all («لاجوردی/یشمی») the whole
      fragment must be list-shaped — one rejected token discards all of it, so
      marketing prose never becomes a color.
    """
    text = normalize_text(segment or "")
    found: list[str] = []
    seen: set[str] = set()
    known_spans: list[tuple[int, int]] = []

    for match in _COLOR_RE.finditer(text):
        canonical = _CANONICAL_BY_KEY.get(_variant_key(match.group(0)), match.group(0).strip())
        known_spans.append(match.span())
        key = color_key(canonical)
        if key and key not in seen:
            seen.add(key)
            found.append(canonical)
    if not allow_unknown:
        return found

    if len(text.strip()) > 48:
        return found
    confirmed = bool(found)
    for token_match in re.finditer(r"[^\s/،,|+•]+", text):
        start, end = token_match.span()
        if any(start < known_end and known_start < end for known_start, known_end in known_spans):
            continue  # part of a known (possibly multi-word) color
        token = token_match.group(0).strip(" \t-–—.:()（）")
        plausible = bool(token) and _fallback_token(token) and _delimiter_attached(text, start, end)
        if not plausible:
            if not confirmed:
                # Nothing in here is a known color and it is not a clean list
                # either: prose, a SKU line, a price — not a color list.
                return []
            continue
        key = color_key(token)
        if key and key not in seen:
            seen.add(key)
            found.append(token)
    return found


# ---------------------------------------------------------------------------
# Model / attribute signatures
# ---------------------------------------------------------------------------

_BRAND_WORDS = (
    "iphone", "apple", "samsung", "galaxy", "xiaomi", "redmi", "poco",
    "آیفون", "ایفون", "اپل", "سامسونگ", "گلکسی", "شیائومی", "ردمی", "پوکو",
)
_BRAND_RE = re.compile(r"(?i)(?:" + "|".join(re.escape(word) for word in _BRAND_WORDS) + r")")


def model_signature(label: str) -> str:
    """Brand/case/space-insensitive key for a phone model label.

    «iPhone 17 Pro Max», «📱17promax», «iphone 17pro max» and — the common case in
    this shop — «۱۷ پرو مکس» all collapse to ``17promax``, so the caption spelling
    and the final WooCommerce option can be matched reliably. The Persian variant
    words are folded with the phone parser's own table (no second vocabulary
    here); without that, a model line written in Persian matched *no* model, and
    the seller's per-model colour list was quietly attached to another phone.
    Network variants remain distinct: 4G/5G can have different physical compatibility.
    """
    canonical = canonical_airpods_model(str(label or ""))
    text = normalize_text(canonical or str(label or "")).strip()
    text = _BRAND_RE.sub(" ", text)
    text = fold_variant_words(text)
    text = re.sub(r"(?i)\bpro\s*max\b", "promax", text)
    text = text.replace("+", "plus")
    return _squash(text).casefold()


def attribute_signature(name: str) -> str:
    """Normalized attribute name («رنگ‌بندی» -> «رنگبندی»)."""
    return _squash(normalize_text(str(name or ""))).casefold()


MODEL_ATTRIBUTE_NAMES = {"مدل", "model", "models", "مدلگوشی"}
COLOR_ATTRIBUTE_NAMES = {"رنگ", "رنگبندی", "color", "colors", "colour", "colours"}


def is_model_attribute(name: str) -> bool:
    return attribute_signature(name) in MODEL_ATTRIBUTE_NAMES


def is_color_attribute(name: str) -> bool:
    return attribute_signature(name) in COLOR_ATTRIBUTE_NAMES


# ---------------------------------------------------------------------------
# Section / line scanning
# ---------------------------------------------------------------------------

_SECTION_WORDS = {
    "apple": "apple", "اپل": "apple", "آیفون": "apple", "ایفون": "apple", "iphone": "apple",
    "samsung": "samsung", "سامسونگ": "samsung", "galaxy": "samsung", "گلکسی": "samsung",
    "xiaomi": "xiaomi", "شیائومی": "xiaomi", "redmi": "xiaomi", "ردمی": "xiaomi",
    "poco": "xiaomi", "پوکو": "xiaomi", "huawei": "other", "هواوی": "other",
    "honor": "other", "آنر": "other", "nokia": "other", "نوکیا": "other",
    "airpods": "airpods", "airpod": "airpods", "ایرپاد": "airpods", "ایرپادز": "airpods",
}
_SECTION_RE = re.compile(
    r"^\s*(?P<word>" + "|".join(re.escape(word) for word in _SECTION_WORDS) + r")\b",
    re.IGNORECASE,
)

_IPHONE_CORE = (
    r"(?:iphone|apple|آیفون|ایفون|اپل)?\s*"
    r"(?:x(?:smax|s|r)?|\d{1,2})\s*(?:mini|air|pro\s*max|promax|pro|plus|\+)?"
)
_MODEL_PREFIXES = {
    "apple": re.compile(rf"^(?:{_IPHONE_CORE})(?:\s*[/,]\s*(?:{_IPHONE_CORE}))*", re.IGNORECASE),
    "samsung": re.compile(
        r"^(?:samsung|galaxy|سامسونگ|گلکسی)?\s*[SA]\d{1,3}\s*(?:ultra|plus|\+|fe)?\s*(?:\d\s*[gG])?\s*s?",
        re.IGNORECASE,
    ),
    "xiaomi": re.compile(
        r"^(?:(?:xiaomi|redmi|poco|شیائومی|ردمی|پوکو)\s+)?note\s*\d{1,3}\s*s?"
        r"\s*(?:pro\s*plus|pro\s*\+|pro|plus)?\s*(?:[45]\s*[gG])?",
        re.IGNORECASE,
    ),
}
# A brand is only implied by section context; outside it, the line must name it
# (or be an unambiguous Samsung/Xiaomi token) before we look for a model.
_BRAND_START = {
    "apple": re.compile(r"^(?:iphone|apple|آیفون|ایفون|اپل)\b", re.IGNORECASE),
    "samsung": re.compile(r"^(?:(?:samsung|galaxy|سامسونگ|گلکسی)\b|[SA]\d{1,3})", re.IGNORECASE),
    "xiaomi": re.compile(r"^(?:(?:xiaomi|redmi|poco|شیائومی|ردمی|پوکو)\b|note\s*\d)", re.IGNORECASE),
}

_COLOR_HINT_RE = re.compile(r"(?i)رنگ|color|colour")

# A line labelled as some *other* attribute («طرح: ساده / براق», «جنس: سیلیکون»)
# is never a color list — without this guard «براق» (which is also a known
# finish color) would be attached to the model above it.
_OTHER_ATTRIBUTE_LABEL_RE = re.compile(
    r"^\s*(?:طرح|طرحها|طرح‌ها|جنس|سایز|اندازه|ابعاد|وزن|ضخامت|برند|مارک|کد|شناسه"
    r"|نام|عنوان|ویژگی|ویژگیها|ویژگی‌ها|مشخصات|قیمت|موجودی|گارانتی"
    r"|size|weight|brand|code|sku|name|title|material|design)\s*[:：=]",
    re.IGNORECASE,
)


def _strip_leading(line: str) -> str:
    """Drop bullets/emoji leftovers so «📱17promax» starts at the model token."""
    return re.sub(r"^[^0-9A-Za-z\u0600-\u06FF]+", "", line.strip())


def _canonical_labels(section: str, fragment: str) -> list[str]:
    """Canonical labels for a model fragment, via the shared phone parser."""
    from bot.services.phone_parser import (
        extract_iphone_models,
        extract_samsung_models,
        extract_xiaomi_models,
    )

    if section == "apple":
        return [model.label for model in extract_iphone_models("Apple\n" + fragment)]
    if section == "samsung":
        return [model.label for model in extract_samsung_models("Samsung\n" + fragment)]
    if section == "xiaomi":
        return [model.label for model in extract_xiaomi_models("xiaomi\n" + fragment)]
    return []


def _models_on_line(line: str, section: str | None) -> tuple[list[str], str | None, str | None]:
    """Return ``(canonical labels, section after this line, brand of the labels)``.

    ``brand`` is the family the model tokens actually belong to, which is not
    necessarily the open section: a Samsung «A35» typed after the xiaomi block
    is still Samsung, and must not inherit xiaomi's color scope.
    """
    # The model tokens are folded with the phone parser's own variant table
    # («۱۳ پرو مکس» → «13 pro max») before they are matched, because
    # ``_MODEL_PREFIXES`` only knows the latin spellings. Only ``core`` is folded:
    # colour extraction keeps reading the seller's own words.
    core = fold_variant_words(_strip_leading(line))
    if not core:
        return [], section, None

    if AIRPODS_START_RE.match(core):
        section = "airpods"
    marker = _SECTION_RE.match(core)
    if marker:
        section = _SECTION_WORDS.get(marker.group("word").casefold(), section)

    labels: list[str] = []
    brand: str | None = None
    if section == "airpods":
        labels = extract_airpods_models("AirPods:\n" + core)
        if labels:
            return labels, section, "airpods"
    for candidate in ("apple", "samsung", "xiaomi"):
        if section != candidate and not _BRAND_START[candidate].match(core):
            continue
        prefix = _MODEL_PREFIXES[candidate].match(core)
        if not prefix:
            continue
        fragment = prefix.group(0).strip(" :,")
        if not fragment:
            continue
        for label in _canonical_labels(candidate, fragment):
            if label not in labels:
                labels.append(label)
        if labels:
            brand = candidate
            break
    return labels, section, brand


# Separators and filler words that may surround a color list without being
# colors themselves («فقط سفید», «سفید و مشکی», «مشکی /سفید/نچرال»).
_COLOR_LINE_FILLER_RE = re.compile(
    r"[/،,|+•\s\-–—:()（）\u200c]+|(?:"
    + "|".join(
        re.escape(word)
        for word in ("فقط", "تنها", "صرفا", "رنگبندی", "رنگ بندی", "رنگ", "موجود", "و", "یا")
    )
    + r")"
)


def is_color_list_line(line: str, colors: Sequence[str]) -> bool:
    """True when a line is essentially *only* a list of ``colors``.

    This is what separates a model's color list («سفید/مشکی/نارنجی») from a
    sentence that merely mentions a color («مشکی موجود شد»). Only the first one
    may be attached to the model above it — otherwise the separate product-info
    message that follows a caption would paint the caption's last model.

    The *surface* spellings are removed, not the canonical names, so an English
    list («white/black» -> «سفید»، «مشکی») is still recognized as a color list.
    """
    if not colors:
        return False
    rest = _COLOR_RE.sub(" ", normalize_text(line))
    for color in colors:
        surface = normalize_text(str(color))
        if surface and surface in rest:
            rest = rest.replace(surface, " ")  # unknown colors kept verbatim
    return not _COLOR_LINE_FILLER_RE.sub("", rest).strip()


def _color_segments(line: str, has_models: bool) -> list[tuple[str, bool]]:
    """``(segment, allow_unknown)`` pairs — the parts of a line that may hold colors.

    Parentheses and a ``:`` / ``=`` / ``→`` / ``-`` tail are explicit color
    slots, so an unknown color name is accepted there. A bare line only counts
    as a color list when it is waiting to be attached to the model above it, and
    a line labelled as a *different* attribute («طرح: …») is skipped entirely.
    """
    stripped = _strip_leading(line)
    if _OTHER_ATTRIBUTE_LABEL_RE.match(stripped) and not _COLOR_HINT_RE.search(
        stripped.split(":", 1)[0]
    ):
        return []

    segments: list[tuple[str, bool]] = []
    for match in re.finditer(r"[(（]([^)）]*)[)）]", line):
        if match.group(1).strip():
            segments.append((match.group(1), True))
    body = re.sub(r"[(（][^)）]*[)）]", " ", line)
    tail = ""
    if ":" in body:
        tail = body.split(":", 1)[1]
    elif "=" in body or "→" in body:
        tail = re.split(r"[=→]", body, maxsplit=1)[1]
    elif has_models and "-" in body:
        # «S25ultra - سفید و مشکی»: the dash separates the model from its colors.
        # Only after a model token, and a non-color tail («- موجود شد») yields
        # nothing because extract_colors rejects it.
        tail = body.split("-", 1)[1]
    if tail.strip():
        segments.append((tail, True))
    if not has_models:
        remainder = body.split(":", 1)[-1]
        if remainder.strip():
            # Without a pending model a loose line is usually prose, so only a
            # real delimiter list («سفید/مشکی») may introduce unknown colors.
            allow_unknown = bool(re.search(r"[/،,|+•]|\sو\s", remainder)) or bool(
                _COLOR_HINT_RE.search(remainder)
            )
            segments.append((remainder, allow_unknown))
    return segments


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ColorMatrix:
    """Colors per model, plus the union of every color mentioned in the post."""

    colors: list[str] = field(default_factory=list)
    by_model: dict[str, list[str]] = field(default_factory=dict)
    defaults: list[str] = field(default_factory=list)
    sections: dict[str, list[str]] = field(default_factory=dict)
    models_seen: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.colors and self.by_model)

    @property
    def by_signature(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for label, colors in self.by_model.items():
            out.setdefault(model_signature(label), colors)
        return out

    def colors_for(self, model: str) -> list[str] | None:
        """Allowed colors for a model, or ``None`` when it is unrestricted."""
        if not self.by_model:
            return None
        return self.by_signature.get(model_signature(model))

    def restrictions_for(self, models: Sequence[str]) -> dict[str, list[str]]:
        """Map final model labels to their allowed colors (unrestricted omitted)."""
        out: dict[str, list[str]] = {}
        for label in models:
            colors = self.colors_for(str(label))
            if colors:
                out[str(label)] = list(colors)
        return out

    def all_colors(self, extra: Iterable[str] = ()) -> list[str]:
        """Union of every assigned color, plus extra values worth keeping."""
        options = list(self.colors)
        seen = {color_key(value) for value in options}
        for value in extra:
            for part in _TOKEN_SPLIT_RE.split(normalize_text(str(value or ""))):
                part = part.strip(" \t-–—.:()")
                if not part:
                    continue
                key = color_key(part)
                if key and key not in seen:
                    seen.add(key)
                    options.append(_CANONICAL_BY_KEY.get(key, part))
        return options

    def summary(self, limit: int = 0) -> str:
        """Human-readable trace for the log group / diagnostics."""
        if not self.by_model:
            return "ماتریس رنگ تشخیص داده نشد."
        lines = [
            f"🎨 ماتریس رنگ: {len(self.by_model)} مدل محدود شد | "
            f"همهٔ رنگ‌ها ({len(self.colors)}): {'، '.join(self.colors)}"
        ]
        items = list(self.by_model.items())
        for label, colors in (items[:limit] if limit else items):
            lines.append(f"  • {label}: {' / '.join(colors)}")
        if limit and len(items) > limit:
            lines.append(f"  … و {len(items) - limit} مدل دیگر")
        if self.unresolved:
            lines.append(f"  ⚠️ بدون محدودیت رنگ (همهٔ رنگ‌ها): {'، '.join(self.unresolved)}")
        return "\n".join(lines)


def parse_color_matrix(text: str) -> ColorMatrix:
    """Parse a free-form product post into a :class:`ColorMatrix`.

    Colors may sit on the model line (``S26ultra (صورتی و سفید)``), on the
    following line (``📱17promax :`` then ``سفید/مشکی``), or on a section header
    that scopes every model below it (``xiaomi (فقط سفید)``).
    """
    matrix = ColorMatrix()
    if not text or not text.strip():
        return matrix

    section: str | None = None
    pending: list[str] = []
    mentioned: dict[str, str] = {}
    # Models whose color list has not shown up yet:
    # (label, section in effect, brand the label actually belongs to).
    awaiting: list[tuple[str, str | None, str | None]] = []

    def assign(labels: Sequence[str], colors: Sequence[str]) -> None:
        for color in colors:
            mentioned.setdefault(color_key(color), color)
        for label in labels:
            if label not in matrix.models_seen:
                matrix.models_seen.append(label)
            bucket = matrix.by_model.setdefault(label, [])
            known = {color_key(value) for value in bucket}
            for color in colors:
                if color_key(color) not in known:
                    known.add(color_key(color))
                    bucket.append(color)

    for raw_line in normalize_text(text).splitlines():
        line = raw_line.strip()
        if not line:
            continue

        previous_section = section
        labels, section, brand = _models_on_line(line, section)
        colors = [
            color
            for segment, allow_unknown in _color_segments(line, bool(labels))
            for color in extract_colors(segment, allow_unknown)
        ]

        if labels:
            if colors:
                assign(labels, colors)
                pending = []
            else:
                # The color list usually sits on the next line.
                awaiting.extend((label, section, brand) for label in labels)
                pending = labels
            continue

        # A model keeps waiting for its color line only while the next lines
        # still *are* color lists. A section change, prose, or the separate
        # «product info» message that follows the caption ends the wait, so an
        # unrelated color word can never be attached to the caption's last model.
        color_list = is_color_list_line(line, colors)
        if section != previous_section or not color_list:
            pending = []

        if colors and pending and color_list:
            assign(pending, colors)
            resolved = set(pending)
            awaiting = [item for item in awaiting if item[0] not in resolved]
            pending = []
            continue

        if colors and _SECTION_RE.match(_strip_leading(line)):
            matrix.sections[section or "default"] = colors
        elif colors and (_COLOR_HINT_RE.search(line) or not matrix.models_seen):
            for color in colors:
                if color_key(color) not in {color_key(x) for x in matrix.defaults}:
                    matrix.defaults.append(color)

    # Models without their own color list inherit the section scope
    # («xiaomi (فقط سفید)») or a global «رنگ: …» line; anything left over stays
    # unrestricted so no sellable variation is ever dropped.
    for label, label_section, label_brand in awaiting:
        if label in matrix.by_model:
            continue
        # A section scope only covers its own family: a Samsung «A35» written
        # after the xiaomi block must not inherit xiaomi's «فقط سفید».
        inherited = None
        if label_section and label_brand == label_section:
            inherited = matrix.sections.get(label_section)
        if not inherited:
            inherited = matrix.defaults
        if inherited:
            assign([label], inherited)
        elif label not in matrix.unresolved:
            matrix.unresolved.append(label)

    matrix.colors = list(mentioned.values())
    return matrix


def colors_in_text(text: str) -> list[str]:
    """Every *known* color mentioned anywhere in a post, in order of appearance.

    Used to validate colors suggested by the AI: a value that never occurs in
    the source text is a hallucination and must not restrict (or extend) the
    real stock list.
    """
    found: dict[str, str] = {}
    for line in normalize_text(text or "").splitlines():
        for color in extract_colors(line, allow_unknown=False):
            found.setdefault(color_key(color), color)
    return list(found.values())


def confirmed_colors(values: Iterable[str], source_text: str) -> list[str]:
    """Keep only the values whose color really appears in ``source_text``."""
    allowed = {color_key(color) for color in colors_in_text(source_text)}
    out: list[str] = []
    seen: set[str] = set()
    for value in values or ():
        key = color_key(value)
        if key and key in allowed and key not in seen:
            seen.add(key)
            out.append(str(value).strip())
    return out


def prune_unused_colors(
    options: Sequence[str], models: Sequence[str], restrictions: dict[str, list[str]]
) -> list[str]:
    """Drop color options no model can actually select.

    Only meaningful when *every* model is restricted; a single unrestricted
    model can still sell any color, so the full list is kept in that case.
    """
    values = [str(option) for option in options if str(option).strip()]
    if not restrictions or not models or len(restrictions) < len(models):
        return values
    usable = {color_key(color) for colors in restrictions.values() for color in colors}
    kept = [value for value in values if color_key(value) in usable]
    return kept or values


# ---------------------------------------------------------------------------
# Variation building (shared by the WooCommerce writer and the preview)
# ---------------------------------------------------------------------------


MAX_COMBINATIONS = 1000
MAX_AXES = 8


class VariationLimitError(ValueError):
    pass


def build_combinations(
    attrs: Iterable[tuple[str, Sequence[str]]],
    restrictions: dict[str, list[str]] | None = None,
) -> list[dict[str, str]]:
    """Bounded product, pruning unavailable pairs before expanding later axes."""
    axes = [(str(name), list(dict.fromkeys(str(option) for option in options if str(option).strip())))
            for name, options in attrs]
    axes = [(name, values) for name, values in axes if values]
    if len(axes) > MAX_AXES or any(len(values) > MAX_COMBINATIONS for _, values in axes):
        raise VariationLimitError("تعداد محور/گزینه‌ها از سقف ایمن ساخت واریژن بیشتر است")
    allowed = {model_signature(name): {color_key(color) for color in colors}
               for name, colors in (restrictions or {}).items()}
    model_names = [name for name, _ in axes if is_model_attribute(name) or is_airpods_attribute(name)]
    color_names = [name for name, _ in axes if is_color_attribute(name)]
    combos: list[dict[str, str]] = []

    def expand(index: int, combo: dict[str, str]) -> None:
        if index == len(axes):
            combos.append(dict(combo))
            if len(combos) > MAX_COMBINATIONS:
                raise VariationLimitError(f"بیش از {MAX_COMBINATIONS} واریژن؛ فهرست را به چند محصول تقسیم کن")
            return
        name, values = axes[index]
        for value in values:
            combo[name] = value
            valid = all(
                model_signature(combo[model]) not in allowed
                or color_key(combo[color]) in allowed[model_signature(combo[model])]
                for model in model_names if model in combo
                for color in color_names if color in combo
            )
            if valid:
                expand(index + 1, combo)
        combo.pop(name, None)

    expand(0, {})
    return combos


def restrict_combinations(
    combos: list[dict[str, str]], restrictions: dict[str, list[str]]
) -> list[dict[str, str]]:
    """Drop combinations whose color is not stocked for their model.

    Two safety valves keep this from ever *losing* sellable variations: a model
    with no entry stays unrestricted, and a model whose allowed colors do not
    intersect the real attribute options (a spelling mismatch between the AI
    and the caption) stays unrestricted too. If filtering would remove
    everything, the unfiltered matrix is returned instead.
    """
    if not combos or not restrictions:
        return combos

    allowed: dict[str, set[str]] = {}
    for model, colors in restrictions.items():
        values = {color_key(color) for color in colors or [] if str(color).strip()}
        allowed.setdefault(model_signature(model), set()).update(values)
    if not allowed:
        return combos

    keys = list(combos[0].keys())
    model_keys = [key for key in keys if is_model_attribute(key) or is_airpods_attribute(key)]
    color_keys = [key for key in keys if is_color_attribute(key)]
    if not model_keys or not color_keys:
        return combos

    available = {color_key(str(combo[key])) for combo in combos for key in color_keys}

    kept: list[dict[str, str]] = []
    for combo in combos:
        keep = True
        for model_key in model_keys:
            permitted = allowed.get(model_signature(str(combo[model_key])))
            if permitted is None:
                continue
            usable = permitted & available
            if not usable:
                keep = False
                break
            if any(color_key(str(combo[name])) not in usable for name in color_keys):
                keep = False
                break
        if keep:
            kept.append(combo)

    return kept


def variation_count(
    models: Sequence[str],
    attributes: dict[str, Any],
    restrictions: dict[str, list[str]] | None = None,
) -> int:
    """How many variations will really be created (preview == reality)."""
    attrs: list[tuple[str, Sequence[str]]] = []
    if len(models) >= 2:
        attrs.append(("مدل", list(models)))
    for name, values in (attributes or {}).items():
        if not isinstance(values, list) or len(values) < 2 or is_model_attribute(name):
            continue
        attrs.append((str(name), values))
    return len(build_combinations(attrs, restrictions or {}))


__all__ = [
    "COLOR_LEXICON",
    "ColorMatrix",
    "attribute_signature",
    "build_combinations",
    "color_key",
    "colors_in_text",
    "confirmed_colors",
    "extract_colors",
    "is_color_attribute",
    "is_model_attribute",
    "model_signature",
    "normalize_text",
    "parse_color_matrix",
    "prune_unused_colors",
    "restrict_combinations",
    "variation_count",
]
