"""What models may legitimately exist — the catalog the AI is held to.

The extractor used to ask the model «what phones are in this text?» with only a
system prompt as a brake. That is how «iPhone 13 Pro Plus» could come back: no
such phone exists, but the sentence looked plausible, and a wrong model label
means a wrong SKU, a wrong variation, and a product page nobody can find.

This module is the explicit answer to «is this a real variant of this brand?»:

* :func:`brand_of` / :func:`known_variants` — what the shop sells;
* :func:`unsupported_variant` / :func:`unknown_brand_words` — what to warn about;
* :func:`prompt_block` — the same table, handed to the AI so it proposes inside
  the catalog instead of from memory.

Shops can extend it without a code change by dropping
``data/model_catalog.json`` next to the other state files::

    {"version": 1, "brands": {"nubia": {"label": "Nubia",
        "words": ["nubia", "نوبیا"], "variants": ["pro", "ultra", "air"]}}}
"""
from __future__ import annotations

import re
from typing import Any

from bot.config import data_dir
from bot.services import jsonstore
from bot.services.phone_parser import fold_variant_words

#: کنارِ بقیهٔ state، یعنی زیر `TISA_DATA_DIR` — نه هاردکد در پوشهٔ کد؛ یک فایلِ
#: بیرون از آن دایرکتوری با اولین `git clean -fdx` یا دیپلویِ بعدی گم می‌شود
#: («📊 وضعیت» دقیقاً برای گفتنِ همین است).
DATA_FILE = data_dir() / "model_catalog.json"

#: The brands the bot knows out of the box, with the spellings sellers use.
#: ``variants`` are suffixes that legitimately follow that brand's models.
BASE: dict[str, dict[str, Any]] = {
    "iphone": {
        "label": "iPhone",
        "words": ["iphone", "apple", "آیفون", "ایفون", "آيفون", "اپل"],
        "variants": ["pro", "pro max", "mini", "plus", "air"],
        "forbidden_variants": ["ultra", "fe", "note", "plus max", "pro plus", "neo", "prime"],
    },
    "samsung": {
        "label": "Samsung",
        "words": ["samsung", "galaxy", "سامسونگ", "گلکسی"],
        "variants": ["pro", "pro plus", "ultra", "fe", "edge", "neo", "flip", "fold", "lite"],
    },
    "xiaomi": {
        "label": "Xiaomi",
        "words": ["xiaomi", "redmi", "poco", "شیائومی", "شاومی", "ردمی", "پوکو"],
        "variants": ["pro", "pro plus", "ultra", "fe", "prime", "neo", "lite", "note"],
    },
    "huawei": {
        "label": "Huawei",
        "words": ["huawei", "honour", "هواوی", "آنر"],
        "variants": ["pro", "pro plus", "ultra", "lite", "neo", "y"],
    },
    "oppo": {
        "label": "Oppo",
        "words": ["oppo", "oneplus", "realme", "اوپو", "وان‌پلاس", "ریلمی"],
        "variants": ["pro", "plus", "ultra", "neo", "fe", "race"],
    },
    "vivo": {
        "label": "Vivo",
        "words": ["vivo", "iqoo", "ویوو", "آی‌کو"],
        "variants": ["pro", "plus", "ultra", "neo", "t"],
    },
    "nokia": {
        "label": "Nokia",
        "words": ["nokia", "نوکیا"],
        "variants": ["plus", "pro"],
    },
    "pixel": {
        "label": "Google Pixel",
        "words": ["pixel", "google", "پیکسل", "گوگل"],
        "variants": ["pro", "xl", "a"],
    },
    # Accessories are sold like phones here (a family + a generation), so they
    # belong in the same table instead of a special case in the parser.
    "airpods": {
        "label": "AirPods",
        "words": ["airpods", "airpod", "air pods", "ایرپاد", "ايرپاد", "ایرپادز", "هندزفری اپل"],
        "variants": ["1", "2", "1/2", "3", "4", "pro", "pro 2", "pro2", "pro 3", "pro3", "max"],
    },
    "watch": {
        "label": "Watch",
        "words": ["watch", "ساعت هوشمند", "اپل واچ"],
        "variants": ["se", "ultra", "pro", "classic"],
    },
    "tab": {
        "label": "Tablet",
        "words": ["tab", "ipad", "pad", "تبلت", "پد", "آیپد"],
        "variants": ["pro", "air", "mini", "lite", "plus"],
    },
}

_CACHE: dict[str, dict[str, Any] | None] = {"merged": None}


def _catalog() -> dict[str, dict[str, Any]]:
    cached = _CACHE.get("merged")
    if isinstance(cached, dict):
        return cached
    merged: dict[str, dict[str, Any]] = {
        key: {**value, "words": list(value.get("words", [])), "variants": list(value.get("variants", []))}
        for key, value in BASE.items()
    }
    extra = jsonstore.read_json(DATA_FILE, None)
    brands = extra.get("brands") if isinstance(extra, dict) else None
    if isinstance(brands, dict):
        for key, value in brands.items():
            if not isinstance(value, dict):
                continue
            name = str(key).casefold().strip()
            entry = dict(merged.get(name, {"label": str(key).title(), "words": [name]}))
            if value.get("label"):
                entry["label"] = str(value["label"])
            for list_key in ("words", "variants", "forbidden_variants"):
                add = value.get(list_key)
                if isinstance(add, list):
                    merged_words = [str(x).casefold().strip() for x in add if str(x).strip()]
                    entry[list_key] = list(dict.fromkeys([*(entry.get(list_key) or []), *merged_words]))
            merged[name] = entry
    _CACHE["merged"] = merged
    return merged


def invalidate() -> None:
    """Drop the cache after editing ``data/model_catalog.json``."""
    _CACHE["merged"] = None


def brands() -> dict[str, dict[str, Any]]:
    return _catalog()


def brand_of(text: str) -> str | None:
    """Which catalogued brand this text talks about, if any."""
    low = fold_variant_words(text or "").casefold()
    for name, entry in _catalog().items():
        for word in entry.get("words", []):
            if re.search(rf"(?<![a-z0-9\u0600-\u06FF]){re.escape(str(word))}(?![a-z0-9\u0600-\u06FF])", low):
                return name
    return None


def known_variants(brand: str) -> tuple[str, ...]:
    entry = _catalog().get(brand.casefold()) or {}
    return tuple(entry.get("variants") or ())


def forbidden_variants(brand: str) -> tuple[str, ...]:
    """Variants a seller writes but this brand does not make («iPhone ultra»)."""
    entry = _catalog().get(brand.casefold()) or {}
    return tuple(entry.get("forbidden_variants") or ())


def unsupported_variant(brand: str, variant: str) -> bool:
    """True when ``variant`` is explicitly not made for this brand.

    Unknown-but-plausible words are *not* rejected here (a new release would be
    dropped silently); only the list of «this brand has never made that» is.
    """
    word = (variant or "").casefold().strip()
    if not word:
        return False
    return word in {x.casefold() for x in forbidden_variants(brand)}


_VARIANTISH = re.compile(r"(?<![a-z])(pro|max|ultra|plus|mini|air|fe|neo|lite|prime|edge|flip|fold|note|series|young|race|classic|se)(?![a-z])")
#: two-word variants are their own product line («pro max» ≠ «pro» + «max»), and
#: it is exactly the pair a shop gets wrong: «iPhone 13 Pro Plus» does not exist.
_VARIANT_PAIRS = re.compile(r"(pro\s*(?:max|plus|ultra|mini))")


def variant_words(text: str) -> list[str]:
    """Variant words and pairs found in a line, in reading order."""
    low = fold_variant_words(text).casefold()
    found = [m.strip() for m in _VARIANT_PAIRS.findall(low)]
    found += _VARIANTISH.findall(low)
    return list(dict.fromkeys(found))


def _brand_of_label(line: str) -> str | None:
    """Brand of the models this line parses to, if any («S24 Ultra» -> samsung)."""
    from bot.services import phone_parser

    for model in phone_parser.extract_phone_models(fold_variant_words(line)):
        name = str(model.brand).casefold()
        if name in _catalog():
            return name
        for key, entry in _catalog().items():
            if str(entry.get("label", "")).casefold().split() and name in str(entry["label"]).casefold():
                return key
    return None


def suspicious_lines(text: str) -> list[tuple[str, str, str]]:
    """Model lines whose words do not fit the brand: ``(brand, line, word)``.

    «۱۳ پرو پلاس» and «آیفون 15 اولترا» both read like a real model and both
    create a variation for a product that cannot exist. The catalog is the only
    place that can say so, so the flow can warn instead of shipping it.

    Real posts name the brand once («آیفون:») and then list bare model lines, so
    a section header carries over until the next one — the same reading rule the
    phone parser uses, or every follow-up line would be judged brand-less.
    """
    out: list[tuple[str, str, str]] = []
    context: str | None = None
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if not re.search(r"\d", line):
            # A header line («سامسونگ:», «Samsung 📱») sets the section instead of
            # being checked itself.
            found = brand_of(line)
            if found and not variant_words(line):
                context = found
            elif found is None:
                context = context
            continue
        # A line may carry its brand without naming it: «S24 اولترا 768t» parses
        # to a Samsung label. Taking that first stops a preceding «آیفون:»
        # header from putting a Samsung model in the wrong family.
        brand = _brand_of_label(line) or brand_of(line) or context
        if not brand:
            continue
        for word in variant_words(line):
            if unsupported_variant(brand, word):
                out.append((brand, line, word))
                break
    return out


def unknown_brand_words(text: str) -> list[str]:
    """Capitalised brand-ish words the catalog does not know («Pocophone», «Nubia»).

    A brand we do not know is not a reason to drop the post — but it is a reason
    to say «این برند در کاتالوگ نیست» rather than to invent a family for it.
    """
    known = {word for entry in _catalog().values() for word in entry.get("words", [])}
    out: list[str] = []
    for token in re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", fold_variant_words(text or "")):
        low = token.casefold()
        if low in known or len(low) < 4:
            continue
        # Only flag words that sit next to a model number — otherwise every
        # English word in a description would look like an unknown brand.
        # «Nubia Z60», «Z60 Nubia», «Pocophone X7» — a brand word touching a
        # model number is the signal; an English word in prose is not.
        if re.search(rf"{re.escape(token)}\s*[A-Za-z]?\d|\d[A-Za-z]?\s*{re.escape(token)}", text or "", re.I):
            out.append(token)
    return list(dict.fromkeys(out))


def prompt_block(models: list[str] | None = None) -> str:
    """The catalog as a prompt section, so the AI proposes inside the lines.

    Only the brands actually detected are listed: a table of every brand the
    store has ever sold is noise the model has to read past, and noise is where
    hallucinations come from.
    """
    if not models:
        return ""
    wanted = {brand_of(label) for label in models} - {None}
    entries = {name: entry for name, entry in _catalog().items() if name in wanted}
    if not entries:
        return ""
    lines = [
        "=== MODEL CATALOG / کاتالوگ مجاز مدل‌ها ===",
        "Only these brands and variant words exist. Never invent a variant, and",
        "never attach a forbidden variant to a brand (it must be reported, not sold).",
    ]
    for name, entry in sorted(entries.items()):
        allowed = " | ".join(entry.get("variants") or []) or "-"
        banned = " | ".join(entry.get("forbidden_variants") or [])
        # The key is printed too: «model_colors» must be keyed by it, and an
        # AI that invents a key silently drops a whole variation.
        line = f"{entry['label']} (key={name}): variants = {allowed}"
        if banned:
            line += f" ; NEVER = {banned}"
        lines.append(line)
    lines.append("If the text states a variant outside these lists, keep the label verbatim"
                 " and put it in the JSON key \"warnings\" (array of strings).")
    return "\n".join(lines) + "\n\n"


__all__ = [
    "BASE", "DATA_FILE", "brand_of", "brands", "forbidden_variants", "invalidate",
    "known_variants", "prompt_block", "suspicious_lines", "unknown_brand_words",
    "unsupported_variant", "variant_words",
]
