"""AirPods compatibility labels and the phone/AirPods attribute policy.

Pure, shared rules: captions, AI answers, manual edits and variation planning
must agree on both the spelling of a model and which axis it belongs to.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence

AIRPODS_ATTRIBUTE = "ایرپاد"
AIRPODS_BRAND_PATTERN = r"(?:air[ \t]*pods?|(?:ایر|اير)[ \t\u200c]*پاد(?:ز|س)?)"
AIRPODS_BRAND_RE = re.compile(
    rf"(?i)(?<![A-Za-z0-9\u0600-\u06FF]){AIRPODS_BRAND_PATTERN}(?=$|[\s\d:：/,;|]|pro(?:[ \t]*[1-3])?(?=$|[^a-z])|max(?=$|[^a-z]))"
)
AIRPODS_START_RE = re.compile(rf"(?i)^\s*(?:apple\s+)?{AIRPODS_BRAND_PATTERN}(?=$|[\s\d:：/,;|]|pro(?:[ \t]*[1-3])?(?=$|[^a-z])|max(?=$|[^a-z]))")
AIRPODS_HEADER_RE = re.compile(rf"(?i)^[\s\W_]*(?:apple\s+)?{AIRPODS_BRAND_PATTERN}[\s\W_]*$")

_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\u2066-\u2069\ufeff]")
_BRAND_PREFIX_RE = re.compile(rf"(?i)^(?:apple[ \t]+)?{AIRPODS_BRAND_PATTERN}[ \t:：]*")
_TOKEN = r"(?:pro(?:[ \t]*[1-3](?!\d))?(?![ \t]*\d)|max|[1-4](?!\d))"
# A slash is a compatibility group, not a separator between selectable models.
_GROUP_RE = re.compile(
    rf"(?i)^(?P<group>{_TOKEN}(?:[ \t]*/[ \t]*(?:(?:apple[ \t]+)?{AIRPODS_BRAND_PATTERN}[ \t]*)?{_TOKEN})*)"
    r"(?![A-Za-z0-9/\u0600-\u06FF]|[ \t]+(?:pro|max|ultra|mini|plus|lite|se)\b)"
)
_END_SECTION_RE = re.compile(
    r"(?i)^\s*(?:iphone|apple|samsung|galaxy|xiaomi|redmi|poco|آیفون|ایفون|اپل|سامسونگ|شیائومی|ردمی|پوکو)\b"
)
_META_RE = re.compile(
    r"(?i)^\s*(?:قیمت|موجودی|تعداد|کد|شناسه|sku|price|stock|وزن|تاریخ)\b|^\d{3,}(?:[tTkKت]|\s|$)"
)


def _clean(text: str) -> str:
    value = unicodedata.normalize("NFKC", text or "").translate(_DIGITS)
    value = _INVISIBLE_RE.sub("", value).replace("ي", "ی").replace("ك", "ک")
    value = re.sub(r"پرو", "pro", value)
    value = re.sub(r"(?:ماکس|مکس)", "max", value)
    return re.sub(r"[ \t\u00a0]+", " ", value)


def _canonical_group(group: str) -> str:
    parts: list[str] = []
    for raw in group.split("/"):
        token = _BRAND_PREFIX_RE.sub("", raw.strip())
        token = re.sub(r"\s+", "", token).casefold()
        if token in {"pro", "pro1"}:
            label = "Pro"
        elif token.startswith("pro"):
            label = "Pro " + token[3:]
        elif token == "max":
            label = "Max"
        else:
            label = token
        if label not in parts:
            parts.append(label)
    return "AirPods " + "/".join(parts)


def canonical_airpods_model(value: str) -> str | None:
    """Canonicalize one explicit model; reject bare brands and unknown variants."""
    text = _clean(value).strip(" \t\n:：,;|.-")
    prefix = _BRAND_PREFIX_RE.match(text)
    if prefix is None:
        return None
    tail = text[prefix.end():].strip()
    match = _GROUP_RE.fullmatch(tail)
    return _canonical_group(match.group("group")) if match else None


def is_airpods_attribute(name: str) -> bool:
    return bool(AIRPODS_HEADER_RE.fullmatch(_clean(name).strip()))


def extract_airpods_models(text: str) -> list[str]:
    """Read branded lines or bare models in an explicitly opened AirPods section.

    Examples: AirPod 1/2, AirPod pro2, ایرپاد پرو ۲, or AirPods: followed by
    1/2 and Pro2. A number elsewhere, a bare brand or a price is not a model.
    """
    found: dict[str, str] = {}
    in_airpods = False
    for raw_line in _clean(text).splitlines():
        line = re.sub(r"^[^A-Za-z0-9\u0600-\u06FF]+", "", raw_line).strip()
        if not line:
            continue
        branded = list(AIRPODS_BRAND_RE.finditer(line))
        if AIRPODS_HEADER_RE.fullmatch(line):
            in_airpods = True
            continue
        if _END_SECTION_RE.match(line) and not AIRPODS_START_RE.match(line):
            in_airpods = False
        sku_like = re.fullmatch(r"[A-Z]{1,12}", line) and not (
            in_airpods and _GROUP_RE.fullmatch(line)
        )
        if _META_RE.search(line) or sku_like:
            in_airpods = False
            if not branded:
                continue
        consumed = 0
        for brand in branded:
            if brand.start() < consumed:
                continue
            tail = line[brand.end():].lstrip(" :：")
            match = _GROUP_RE.match(tail)
            if match:
                label = _canonical_group(match.group("group"))
                found.setdefault(label.casefold(), label)
                consumed = brand.end() + len(line[brand.end():]) - len(tail) + match.end()
                if AIRPODS_START_RE.match(line):
                    in_airpods = True
        if not branded and in_airpods:
            for fragment in re.split(r"[|,;،]+", line):
                match = _GROUP_RE.match(fragment.strip())
                if match:
                    label = _canonical_group(match.group("group"))
                    found.setdefault(label.casefold(), label)
    return list(found.values())


def split_device_axes(
    models: Sequence[str], attributes: Mapping[str, Sequence[str]] | None = None
) -> tuple[list[str], dict[str, list[str]]]:
    """Phone + AirPods → two axes; AirPods-only → the ordinary model axis.

    Works for already-routed data too, and does not mutate the caller's lists.
    Other model families and ordinary attributes retain their original values.
    """
    primary: dict[str, str] = {}
    airpods: dict[str, str] = {}
    attrs: dict[str, list[str]] = {}
    for model in models:
        value = re.sub(r"\s+", " ", str(model)).strip()
        if not value:
            continue
        canonical = canonical_airpods_model(value)
        if canonical:
            airpods.setdefault(canonical.casefold(), canonical)
        else:
            primary.setdefault(value.casefold(), value)
    for name, values in (attributes or {}).items():
        options = list(values) if isinstance(values, (list, tuple, set)) else [str(values)]
        if is_airpods_attribute(name):
            for option in options:
                canonical = canonical_airpods_model(str(option)) or canonical_airpods_model("AirPods " + str(option))
                if canonical:
                    airpods.setdefault(canonical.casefold(), canonical)
        else:
            attrs[name] = [str(option) for option in options]
    if primary and airpods:
        attrs[AIRPODS_ATTRIBUTE] = list(airpods.values())
        return list(primary.values()), attrs
    return list(primary.values()) or list(airpods.values()), attrs


def airpods_category_leaves(models: Sequence[str]) -> list[str]:
    """Exact taxonomy leaves; Pro 2 must not also select Pro or regular 2/3."""
    leaves: dict[str, None] = {}
    for model in models:
        canonical = canonical_airpods_model(model)
        if canonical is None:
            continue
        for variant in canonical.removeprefix("AirPods ").split("/"):
            if variant in {"1", "2"}:
                leaf = "Airpods 1/2"
            elif variant in {"3", "4", "Pro", "Pro 2", "Pro 3"}:
                leaf = "Airpods " + variant
            else:
                continue
            leaves[leaf] = None
    return list(leaves)
