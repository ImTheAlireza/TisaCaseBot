"""High-precision phone-model parser for real-world Telegram product posts.

Design goals:
- Treat the post as semi-structured data, not as a plain bag of words.
- Use section context (Apple / Samsung / Xiaomi) to reduce false positives.
- Preserve meaningful sub-brand prefixes such as Redmi, while removing the
  generic output prefixes Samsung and Xiaomi.
- Preserve compatibility groups written with slash notation as ONE output item.
- Normalize common messy spacing/casing (17promax, 14Pro, Xsmax, A21 s, ...).
- Never turn a Samsung A21s into A21, and never drop Redmi from Redmi Note.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Text cleaning
# ---------------------------------------------------------------------------

EMOJI_RANGES_RE = re.compile(
    "["
    "\\U0001F1E6-\\U0001F1FF"
    "\\U0001F300-\\U0001F5FF"
    "\\U0001F600-\\U0001F64F"
    "\\U0001F680-\\U0001F6FF"
    "\\U0001F700-\\U0001F77F"
    "\\U0001F780-\\U0001F7FF"
    "\\U0001F800-\\U0001F8FF"
    "\\U0001F900-\\U0001F9FF"
    "\\U0001FA00-\\U0001FAFF"
    "\\U00002600-\\U000027BF"
    "\\U00002300-\\U000023FF"
    "]+"
)

INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\u2066-\u2069\ufeff]")


def remove_emojis(text: str) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = INVISIBLE_RE.sub("", text)
    text = EMOJI_RANGES_RE.sub(" ", text)
    out: list[str] = []
    for ch in text:
        category = unicodedata.category(ch)
        if category in {"So", "Sk"} or ch in {"\ufe0e", "\ufe0f", "\u20e3", "\u200d"}:
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


# Persian/Arabic spellings of the variant words sellers use. Without this fold,
# «13 پرو مکس», «13 پرو» and «13» all parsed to the SAME label «iPhone 13» and
# three real models silently became one — losing variations and putting prices
# on the wrong model. Longest first, so «پرو مکس» never folds into «پرو».
_VARIANT_FOLD: tuple[tuple[str, str], ...] = (
    ("پرومکس", "pro max"),
    ("پرو مکس", "pro max"),
    ("پرو​مکس", "pro max"),
    ("پرو", "pro"),
    ("مکس", "max"),
    ("پلاس", "plus"),
    ("پلس", "plus"),
    ("مینی", "mini"),
    ("اولترا", "ultra"),
    ("التراس", "ultra"),
    ("ایر", "air"),
    ("فئ", "fe"),
    ("اف‌ای", "fe"),
    ("اف ای", "fe"),
    ("نوت", "note"),
    ("پرو پلاس", "pro plus"),
    ("پروپلاس", "pro plus"),
)
_VARIANT_RE = re.compile(
    "|".join(re.escape(word) for word, _ in _VARIANT_FOLD), re.IGNORECASE
)
_VARIANT_MAP = dict(_VARIANT_FOLD)

# Every variant word a model line may carry; used to detect two *different*
# writings collapsing onto one canonical label (see ``model_merge_conflicts``).
_VARIANT_MARKERS = (
    "pro max", "promax", "pro plus", "proplus", "pro", "max", "plus", "mini",
    "air", "ultra", "fe", "note", "lite", "s",
)


def fold_variant_words(text: str) -> str:
    """Replace Persian/Arabic variant words with the latin tokens the parser knows."""
    if not text:
        return ""
    return _VARIANT_RE.sub(lambda m: _VARIANT_MAP.get(m.group(0).strip(), m.group(0)), text)


def _clean(text: str) -> str:
    text = remove_emojis(text or "")
    text = text.replace("⁩", " ").replace("⁦", " ").replace("：", ":")
    text = text.replace("–", "-").replace("—", "-")
    text = fold_variant_words(text)
    # Only horizontal whitespace is collapsed: newlines are what separate the
    # brand section, the model line and the colour list, so \s+ would merge the
    # whole post into one line and break every parser below.
    return re.sub(r"[ \t\u00a0]+", " ", text)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PhoneModel:
    brand: str
    key: tuple[int, int, int, int]
    label: str


BRAND_ORDER = {"iPhone": 0, "Samsung": 1, "Xiaomi": 2}

# ---------------------------------------------------------------------------
# iPhone
# ---------------------------------------------------------------------------

IPHONE_VARIANTS = {
    "": (10, ""),
    "mini": (20, "Mini"),
    "air": (25, "Air"),
    "plus": (30, "Plus"),
    "pro": (40, "Pro"),
    "pro max": (50, "Pro Max"),
}


def _iphone_variant(raw: str) -> tuple[str, int, str]:
    compact = re.sub(r"[^a-z+]", "", (raw or "").lower())
    aliases = {
        "": "",
        "mini": "mini",
        "air": "air",
        "plus": "plus",
        "+": "plus",
        "pro": "pro",
        "promax": "pro max",
        "pro+max": "pro max",
        "max": "pro max",
    }
    normalized = aliases.get(compact, "")
    rank, display = IPHONE_VARIANTS[normalized]
    return normalized, rank, display


def _iphone_key(generation: int, rank: int) -> tuple[int, int, int, int]:
    return (generation, rank, 0, 0)


def _roman_key(token: str) -> tuple[int, int, int, int] | None:
    compact = re.sub(r"[^a-z]", "", token.lower())
    mapping = {
        "x": (10, 5),
        "xs": (10, 15),
        "xr": (10, 20),
        "xsmax": (10, 25),
    }
    value = mapping.get(compact)
    return _iphone_key(*value) if value else None


# The brand may be written either in Latin or in Persian («آیفون», «ایفون»,
# «اپل»). The Latin-only pattern used to make a Persian caption such as
# «آیفون 13 پرو مکس» yield *no* iPhone at all: the model disappeared from the
# product and, with it, every variation that should have been built for it.
_IPHONE_BRAND = r"(?:iphone|ipohne|اپل|apple|آي?فون|آي\u200c?فون|ایفون|آیفون)"
_IPHONE_BRAND_RE = re.compile(
    rf"(?i)(?<![A-Za-z0-9\u0600-\u06FF])(?:{_IPHONE_BRAND})(?![A-Za-z0-9\u0600-\u06FF])"
)
_IPHONE_TAIL_RE = re.compile(rf"(?i)(?:{_IPHONE_BRAND})\s*[:：]?\s*(.*)$")


def _iphone_tail(line: str) -> str:
    """What follows the brand word on a line («آیفون 13 پرو» → «13 پرو»)."""
    match = _IPHONE_TAIL_RE.search(line)
    return match.group(1).strip() if match else ""


def _parse_single_iphone(raw: str) -> PhoneModel | None:
    token = raw.strip().strip("|,;:.-")
    token = re.sub(rf"(?i)^(?:{_IPHONE_BRAND})\s*", "", token)
    token = re.sub(r"\s+", "", token)
    if not token:
        return None

    if re.fullmatch(r"(?i)x(?:s\s*max|smax|s|r)?", token):
        key = _roman_key(token)
        if not key:
            return None
        compact = token.lower()
        compact = re.sub(r"\s+", "", token.lower())
        labels = {"x": "iPhone X", "xs": "iPhone XS", "xr": "iPhone XR",
                  "xsmax": "iPhone XS Max"}
        return PhoneModel("iPhone", key, labels[compact])

    match = re.fullmatch(
        r"(?i)(\d{1,2})(pro\s*max|promax|max|mini|air|pro|plus|\+)?", token
    )
    if not match:
        return None

    generation = int(match.group(1))
    if not 1 <= generation <= 30:
        return None

    _normalized, rank, display = _iphone_variant(match.group(2) or "")
    label = f"iPhone {generation}" + (f" {display}" if display else "")
    return PhoneModel("iPhone", _iphone_key(generation, rank), label)


def _combine_iphone_group(models: list[PhoneModel]) -> PhoneModel | None:
    if not models:
        return None
    if len(models) == 1:
        return models[0]

    # Slash notation is intentionally ONE variable/item.
    # Example: 7+/8+ -> iPhone 7 Plus/8 Plus
    ordered = sorted(models, key=lambda m: m.key)
    labels = [m.label.removeprefix("iPhone ") for m in ordered]
    # If variants are equal, compact as "7/8"; otherwise preserve each side.
    label = "iPhone " + "/".join(labels)
    first = ordered[0]
    last = ordered[-1]
    return PhoneModel("iPhone", (first.key[0], first.key[1], 1, last.key[0]), label)


def extract_iphone_models(text: str) -> list[PhoneModel]:
    cleaned = _clean(text)
    found: dict[str, PhoneModel] = {}
    in_apple = False

    for raw_line in cleaned.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        low = line.lower()

        # Section/context markers.
        if not _iphone_tail(line) and _IPHONE_BRAND_RE.search(low):
            in_apple = True
            continue
        if re.search(r"^\s*(samsung|xiaomi|redmi|poco)\b", low):
            in_apple = False
            continue

        if not _IPHONE_BRAND_RE.search(low) and not in_apple:
            continue

        # In real Telegram posts the section header is often `iPhone:` and
        # the following lines contain only `17 Pro Max`, without repeating
        # the word iPhone. Treat those lines as iPhone models while the Apple
        # section is active.
        tail = _iphone_tail(line)
        if not tail and not in_apple:
            continue
        if not tail:
            tail = line.strip()
        # Remove trailing marketing prose after the first clearly model-like group.
        # Usually one line contains one group: iphone 17promax or iphone 7/8.
        # A defensive split on pipes/commas/semicolons handles several groups.
        for group in re.split(r"\s*[|,;]\s*", tail):
            parts = [p.strip() for p in re.split(r"\s*/\s*", group) if p.strip()]
            models: list[PhoneModel] = []
            for part in parts:
                # Keep only a model token at the beginning of each slash component.
                # `(?!\d)` is what stops a bare amount from becoming a phone: inside
                # an Apple section «1098» used to match the 2-digit prefix «10» and
                # invent an iPhone 10, adding a whole extra model (and its
                # variations) to the product.
                m = re.match(
                    r"(?i)(x(?:s\s*max|s|r)?|\d{1,2})(?!\d)"
                    r"(?:\s*(?:pro\s*max|promax|max|mini|air|pro|plus|\+))?",
                    part,
                )
                if not m:
                    continue
                model = _parse_single_iphone(m.group(0))
                if model:
                    models.append(model)
            grouped = _combine_iphone_group(models)
            if grouped:
                found[grouped.label.casefold()] = grouped

    return list(found.values())


# ---------------------------------------------------------------------------
# Samsung
# ---------------------------------------------------------------------------

SAMSUNG_VARIANT_RANK = {"": 0, "plus": 20, "fe": 30, "ultra": 40}


def _samsung_suffix(raw: str) -> tuple[str, int]:
    compact = re.sub(r"\s+", "", (raw or "").lower())
    if compact in {"ultra"}:
        return "Ultra", 40
    if compact in {"fe", "fе"}:
        return "FE", 30
    if compact in {"plus", "+"}:
        return "Plus", 20
    return "", 0


_SAMSUNG_HEADER_RE = re.compile(
    r"(?i)^\s*(?:samsung|galaxy(?:\s+samsung)?|سامسونگ|گلکسی)\s*:?\s*$"
)
_XIAOMI_HEADER_RE = re.compile(
    r"(?i)^\s*(?:xiaomi(?:\s*/\s*poco)?|poco|شیائومی(?:\s*/\s*پوکو)?|پوکو)"
    r"(?:\s*\([^)]*\))?\s*:?\s*$"
)


def extract_samsung_models(text: str) -> list[PhoneModel]:
    cleaned = _clean(text)
    found: dict[str, PhoneModel] = {}
    samsung_context = False
    xiaomi_context = False

    for raw_line in cleaned.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if _SAMSUNG_HEADER_RE.fullmatch(line):
            samsung_context = True
            xiaomi_context = False
            continue
        if _XIAOMI_HEADER_RE.fullmatch(line):
            samsung_context = False
            xiaomi_context = True
            continue
        if re.match(rf"(?i)^\s*(?:{_IPHONE_BRAND}|apple)\b", line):
            samsung_context = False
            xiaomi_context = False
            continue
        if re.match(r"(?i)^\s*(?:xiaomi|redmi|poco|شیائومی|ردمی|پوکو)\b", line):
            # A POCO model line inside a Xiaomi/POCO section is an entry, not a
            # new section; keep that context for the following shorthand rows.
            samsung_context = False
            continue

        # Under an explicit Xiaomi/POCO heading, A5/C71 is a product family,
        # not Samsung A5 followed by noise. Leave that row to the Xiaomi parser.
        if xiaomi_context and re.fullmatch(r"(?i)A\s*\d{1,3}\s*/\s*C\s*\d{1,3}", line):
            continue
        if not samsung_context and not re.search(r"(?i)(?<![A-Za-z0-9])[SA]\d{1,3}", line):
            continue

        # Start-of-model or clearly delimited model occurrences only.
        for m in re.finditer(
            r"(?i)(?<![A-Za-z0-9])(S\d{1,3}|A\d{1,3})"
            r"\s*(ultra|plus|\+|fe)?"
            r"(?:\s*(\d\s*G))?"
            r"(?:\s*(s))?",
            line,
        ):
            base = m.group(1).upper()
            suffix_raw = m.group(2) or ""
            network_raw = re.sub(r"\s+", "", m.group(3) or "").upper()
            trailing_s = bool(m.group(4)) and base.startswith("A")

            suffix, rank = _samsung_suffix(suffix_raw)
            # Galaxy A21s is a real distinct model; never discard the s.
            if trailing_s:
                suffix = "s"
                rank = 10

            label = base
            if suffix == "s":
                label += "s"
            elif suffix:
                label += f" {suffix}"
            if network_raw:
                label += f" {network_raw}"

            family_rank = 0 if base.startswith("S") else 1
            number = int(base[1:])
            key = (family_rank, number, rank, 0)
            found[label.casefold()] = PhoneModel("Samsung", key, label)

    return list(found.values())


# ---------------------------------------------------------------------------
# Xiaomi / Redmi / POCO
# ---------------------------------------------------------------------------

XIAOMI_VARIANT_RANK = {"": 0, "s": 10, "plus": 20, "pro": 30, "pro plus": 40}


def _xiaomi_variant(raw: str) -> tuple[str, int]:
    compact = re.sub(r"\s+", "", (raw or "").lower())
    if compact in {"proplus", "pro+"}:
        return "Pro Plus", 40
    if compact == "pro":
        return "Pro", 30
    if compact == "plus":
        return "Plus", 20
    if compact == "s":
        return "S", 10
    return "", 0


def _parse_note_component(raw: str) -> PhoneModel | None:
    """Read full or shortened Redmi Note tokens (``Note13Pro`` / ``12S``)."""
    token = _clean(raw).strip().strip("|,;:.- ")
    token = re.sub(r"(?i)^(?:(?:xiaomi|redmi)\s+)?note\s*", "", token)
    compact = re.sub(r"\s+", "", token)
    match = re.fullmatch(r"(?i)(\d{1,3})(s)?(proplus|pro\+|pro|plus)?(4g|5g)?", compact)
    if not match:
        return None
    number = int(match.group(1))
    if not 1 <= number <= 100:
        return None
    variant_raw = "s" if match.group(2) else (match.group(3) or "")
    variant, rank = _xiaomi_variant(variant_raw)
    label = f"Redmi Note {number}"
    if variant == "S":
        label += "S"
    elif variant:
        label += f" {variant}"
    network = (match.group(4) or "").upper()
    if network:
        label += f" {network}"
    return PhoneModel("Xiaomi", (number, rank, 0, 0), label)


def _parse_poco_component(raw: str, *, allow_short: bool = False) -> PhoneModel | None:
    """Read a POCO token, with bare M/C/X shorthand allowed in its own section."""
    token = _clean(raw).strip().strip("|,;:.- ")
    explicit = bool(re.match(r"(?i)^poco\b", token))
    if explicit:
        token = re.sub(r"(?i)^poco\s*", "", token, count=1)
    elif not allow_short:
        return None
    match = re.fullmatch(r"(?i)([xmc])\s*(\d{1,3})(?:\s*(pro\s*plus|pro\+|pro|plus))?", token)
    if not match:
        return None
    family, number_text, raw_variant = match.groups()
    family = family.upper()
    number = int(number_text)
    variant, rank = _xiaomi_variant(raw_variant or "")
    label = f"POCO {family}{number}" + (f" {variant}" if variant else "")
    return PhoneModel("Xiaomi", (1000 + number, rank, 0, 0), label)


def _parse_xiaomi_shorthand(raw: str) -> PhoneModel | None:
    """Read Redmi A/number-C and bare POCO families inside a Xiaomi/POCO list."""
    token = _clean(raw).strip().strip("|,;:.- ")
    match = re.fullmatch(r"(?i)A\s*(\d{1,3})", token)
    if match:
        number = int(match.group(1))
        return PhoneModel("Xiaomi", (number, 0, 0, 0), f"Redmi A{number}")
    match = re.fullmatch(r"(?i)(\d{1,3})\s*C", token)
    if match:
        number = int(match.group(1))
        return PhoneModel("Xiaomi", (1000 + number, 0, 1, 0), f"Redmi {number}C")
    return _parse_poco_component(token, allow_short=True)


def extract_xiaomi_models(text: str) -> list[PhoneModel]:
    cleaned = _clean(text)
    found: dict[str, PhoneModel] = {}
    xiaomi_context = False

    def keep(model: PhoneModel | None) -> None:
        if model is not None:
            found[model.label.casefold()] = model

    for raw_line in cleaned.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if _XIAOMI_HEADER_RE.fullmatch(line):
            xiaomi_context = True
            continue
        if _SAMSUNG_HEADER_RE.fullmatch(line) or re.match(
            rf"(?i)^\s*(?:{_IPHONE_BRAND}|apple|samsung|سامسونگ)\b", line
        ):
            xiaomi_context = False
            continue

        # Explicit POCO/Redmi families are useful even without a section title.
        for poco in re.finditer(r"(?i)\bpoco\s*[xmc]\s*\d{1,3}(?:\s*(?:pro|plus))?", line):
            keep(_parse_poco_component(poco.group(0)))
        for redmi in re.finditer(r"(?i)\bredmi\s+(A\s*\d{1,3}|\d{1,3}\s*C)\b", line):
            keep(_parse_xiaomi_shorthand(redmi.group(1)))

        # Slashes abbreviate the repeated family name in supplier posts:
        # NOTE9PRO/9S -> Note 9 Pro + Note 9S, and M6PRO after Note13PRO
        # is a separate POCO model, not another Redmi Note variant.
        groups = re.split(r"\s*[|,;]\s*", line)
        for group in groups:
            parts = [part.strip() for part in re.split(r"\s*/\s*", group) if part.strip()]
            has_note = any(re.search(r"(?i)(?:redmi\s+)?note\s*\d", part) for part in parts)
            if has_note:
                for part in parts:
                    note = _parse_note_component(part)
                    if note is not None:
                        keep(note)
                    else:
                        keep(_parse_xiaomi_shorthand(part))
                continue

            if xiaomi_context:
                for part in parts:
                    keep(_parse_xiaomi_shorthand(part))
            elif not parts and not re.search(r"(?i)\b(?:redmi\s+)?note\s*\d", line):
                continue

    return list(found.values())


# ---------------------------------------------------------------------------
# Combined extraction / output
# ---------------------------------------------------------------------------


def extract_phone_models(text: str) -> list[PhoneModel]:
    if not text:
        return []

    items = extract_iphone_models(text) + extract_samsung_models(text) + extract_xiaomi_models(text)
    unique: dict[tuple[str, str], PhoneModel] = {}
    for item in items:
        unique[(item.brand, item.label.casefold())] = item

    return sorted(
        unique.values(),
        key=lambda item: (BRAND_ORDER[item.brand], item.key, item.label.casefold()),
    )


def _model_key_of(line: str) -> str:
    """Numbers + variant words of a line, order-insensitive.

    Two lines with the same key really are the same model; same label but
    different keys means we merged two different models into one name.
    """
    low = _clean(line).lower()
    numbers = re.findall(r"\d{1,3}", low)
    marks = {
        marker
        for marker in _VARIANT_MARKERS
        if re.search(r"(?<![a-z])" + re.escape(marker) + r"(?![a-z])", low)
    }
    # «13 max», «13 promax» and «13 پرو مکس» are one model, so they must share a
    # key — otherwise the conflict detector cries wolf on every post.
    if marks & {"max", "promax", "pro max"}:
        marks -= {"max", "promax", "pro max", "pro"}
        marks.add("pro max")
    return " ".join(numbers + sorted(marks))


# Words that always mean «part of a model name». If one of them survives on a
# model line without appearing in the canonical label, the parser met a variant
# it could not apply — e.g. «13 پرو پلاس» (pro plus is not an iPhone) or a new
# spelling nobody added to the fold table yet.
_VARIANTISH_WORDS = (
    "pro", "max", "plus", "ultra", "mini", "air", "fe", "note", "lite", "neo",
    "prime", "edge", "flip", "fold", "series", "s", "g", "5g", "4g", "3",
)


def unmatched_model_words(text: str) -> list[tuple[str, list[str]]]:
    """Model lines whose words were not all applied, with what is left over.

    This is the anti-silent-loss tripwire. «13 پرو پلاس» used to become a
    plausible «iPhone 13 Pro»: a model the seller never listed, with the
    unmatched word thrown away. A variant we cannot read must never turn into a
    different, sellable model — so the leftover word is reported and the owner
    decides, instead of discovering a missing variation next month.
    """
    if not text:
        return []
    clean = _clean(text)
    labels = [model.label for model in extract_phone_models(clean)]
    if not labels:
        return []
    lowered = [label.lower() for label in labels]
    out: list[tuple[str, list[str]]] = []
    for raw_line in clean.splitlines():
        line = raw_line.strip()
        if not line or not re.search(r"\d", line):
            continue
        words = {
            word
            for word in _VARIANTISH_WORDS
            if re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", line.lower())
        }
        if not words:
            continue
        numbers = set(re.findall(r"\d{1,3}", line.lower()))
        candidates = [
            label for label, low in zip(labels, lowered, strict=False)
            # «iPhone 13 Pro Max» belongs to a line mentioning 13 (or to the
            # roman X family, which carries no digits of its own).
            if any(number in low.split() for number in numbers) or "x" in low
        ]
        if not candidates:
            continue          # no model came from this line at all: other checks own it
        for word in sorted(words):
            if not any(word in label.lower() for label in candidates):
                out.append((line, sorted({*(w for w in words if not any(
                    w in label.lower() for label in candidates))})))
                break
    return out


def normalize_caption(text: str) -> str:
    return " | ".join(item.label for item in extract_phone_models(text))


__all__ = [
    "PhoneModel",
    "extract_iphone_models",
    "extract_phone_models",
    "extract_samsung_models",
    "extract_xiaomi_models",
    "fold_variant_words",
    "normalize_caption",
    "remove_emojis",
    "unmatched_model_words",
]
