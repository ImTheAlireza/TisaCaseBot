"""Where every product field came from — the "why" the preview must show.

The bot assembles a product from four sources (caption, filenames, OCR, the AI
model) and previously the result looked identical no matter how it was
produced. A wrong price and a correct one were indistinguishable, so the user
had to re-read everything instead of checking the two fields they were unsure
about.

:func:`bot.services.postmodel.evidence` is the single place that records a
source and a short quote for each field, which buys three things:

* the preview can say «قیمت از خط «قیمت 698» در کپشن» — a one-glance check;
* a policy decision (for example «وزن 250 گرم» not counted as a price) is
  stated out loud instead of silently overriding the user;
* corrections can be attributed: when the user edits a field whose source was
  ``caption``, that is the pattern to learn from (see
  :mod:`bot.services.learning`).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from bot.services import color_matrix, money, phone_parser
from bot.services.airpods_parser import AIRPODS_HEADER_RE, extract_airpods_models

CAPTION = "caption"
FILENAME = "filename"
OCR = "ocr"
AI = "ai"
USER = "user"
POLICY = "policy"
LEARNED = "learned"
UPDATE = "update"
VOCAB = "vocabulary"
INFO = "info"

#: Trust ladder, least to most trusted. The seller's own words beat any machine
#: reading, a policy decision beats raw text, a learned correction beats the
#: caption, and what the user typed now beats everything (see merge()).
SOURCES = (AI, OCR, FILENAME, POLICY, VOCAB, CAPTION, INFO, LEARNED, UPDATE, USER)

#: What the preview shows, in the order a seller checks them.
PREVIEW_FIELDS = (
    "title",
    "model",
    "price",
    "prices",
    "colors",
    "models",
    "sku_prefix",
    "category",
    "tags",
    "stock",
    "sale_price",
    "barcode",
    "description",
)

#: Sources that mean «a machine worked this out», i.e. the seller never wrote it.
#: A value in this tier is worth a question, not a silent publish — the review
#: screen asks about these (see product_flow._open_questions).
INFERRED_SOURCES = (AI, OCR, FILENAME)

_QUOTES = re.compile(r"[«”][^«»”]{1,80}[»“]")
_WHITESPACE = re.compile(r"\s+")


def _clip(text: str, limit: int = 90) -> str:
    text = _WHITESPACE.sub(" ", (text or "").strip())
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass(frozen=True)
class Evidence:
    """Where a value came from, with the quote that produced it."""

    source: str
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "note": self.note}

    def line(self) -> str:
        """One Persian line for the preview."""
        label = SOURCE_LABELS.get(self.source, self.source)
        return f"{label}: {self.note}" if self.note else label


SOURCE_LABELS = {
    CAPTION: "متن کپشن",
    FILENAME: "نام فایل",
    OCR: "تصویر (OCR)",
    AI: "هوش مصنوعی",
    USER: "ویرایش شما",
    POLICY: "قاعده",
    INFO: "متن اطلاعات محصول",
    LEARNED: "از اصلاح قبلی شما",
    UPDATE: "محصول موجود (برگشت از انبار)",
    VOCAB: "واژه‌نامهٔ فروشگاه",
}


def _trust(source: str) -> int:
    """Rank of a source; anything unknown is treated as the least trusted."""
    try:
        return SOURCES.index(source)
    except ValueError:
        return -1


def note(source: str, text: str = "", *, quote: str = "") -> Evidence:
    """Build evidence; ``quote`` is clipped and trimmed to the useful fragment.

    When a whole caption line is available we keep only the part around the
    quoted amount — the preview must stay readable.
    """
    text = (text or "").strip()
    if not text:
        return Evidence(source, _clip(quote))
    if quote and quote in text:
        return Evidence(source, _clip(quote))
    return Evidence(source, _clip(text))


def best_quote(line: str, needle: str, *, window: int = 46) -> str:
    """A short slice of ``line`` around ``needle`` («…قیمت 698 تومان…»)."""
    if not line:
        return ""
    index = line.find(needle) if needle else -1
    if index < 0:
        return _clip(line, window)
    start = max(0, index - 12)
    end = min(len(line), index + len(needle) + window - 12)
    prefix = "…" if start else ""
    suffix = "…" if end < len(line) else ""
    return f"{prefix}{_WHITESPACE.sub(' ', line[start:end].strip())}{suffix}"


def inferred_fields(evidence: dict[str, Evidence] | None) -> list[str]:
    """Which fields rest on a guess, in the order a seller checks them.

    Returns the fields whose *best* evidence is AI/OCR/filename: if the seller
    wrote the value, or a policy or a learned correction owns it, the field is not
    in this list. Ordering follows :data:`PREVIEW_FIELDS` so the questions read
    like the rest of the preview instead of like a dict dump.
    """
    wanted = {
        name
        for name, item in (evidence or {}).items()
        if isinstance(item, Evidence) and item.source in INFERRED_SOURCES
    }
    if not wanted:
        return []
    ordered = [name for name in PREVIEW_FIELDS if name in wanted]
    return ordered + sorted(wanted - set(ordered))


def merge(target: dict[str, Evidence], field_name: str, source: str,
          text: str = "", *, quote: str = "", overwrite: bool = False) -> None:
    """Record evidence for ``field_name`` unless a higher-trust source owns it.

    Trust follows :data:`SOURCES`: an AI guess cannot explain away the
    seller's own sentence, and nothing explains away what the user just
    typed. Pass ``overwrite`` when the payload really did change.
    """
    current = target.get(field_name)
    if current is not None and not overwrite and _trust(current.source) >= _trust(source):
        return
    target[field_name] = note(source if source in SOURCES else POLICY, text, quote=quote)


def from_dict(data: Any) -> dict[str, Evidence]:
    if not isinstance(data, dict):
        return {}
    out: dict[str, Evidence] = {}
    for key, value in data.items():
        if isinstance(value, Evidence):
            out[str(key)] = value
        elif isinstance(value, dict) and value.get("source") in SOURCES:
            out[str(key)] = Evidence(str(value["source"]), str(value.get("note") or ""))
    return out


def to_dict(evidence: dict[str, Evidence]) -> dict[str, Any]:
    return {key: value.to_dict() for key, value in evidence.items()}


def preview_lines(evidence: dict[str, Evidence]) -> list[str]:
    """The «منبع» block for the preview card: one line per explained field."""
    lines: list[str] = []
    for name in PREVIEW_FIELDS:
        item = evidence.get(name)
        if item is None:
            continue
        if not item.note and item.source in (CAPTION, FILENAME, OCR):
            continue          # no story worth telling
        lines.append(f"{_FIELD_LABELS.get(name, name)} ← {item.line()}")
    return lines


_FIELD_LABELS = {
    "title": "عنوان",
    "model": "مدل",
    "price": "قیمت",
    "prices": "قیمت گروه‌ها",
    "colors": "رنگ‌ها",
    "models": "مدل‌ها",
    "sku_prefix": "پیشوند SKU",
    "category": "دسته",
    "tags": "تگ‌ها",
    "stock": "موجودی",
    "sale_price": "قیمت ویژه",
    "barcode": "بارکد",
    "description": "توضیحات",
}


def explained(evidence: dict[str, Evidence], notes: list[str]) -> str:
    """Render the full explanation block: sources plus policy notes."""
    parts = preview_lines(evidence)
    parts.extend(f"ℹ️ {text}" for text in notes)
    if not parts:
        return ""
    return "🧭 از کجا می‌دانم:\n" + "\n".join(f"• {part}" for part in parts) + "\n\n"


__all__ = [
    "AI",
    "CAPTION",
    "FILENAME",
    "INFERRED_SOURCES",
    "LEARNED",
    "OCR",
    "POLICY",
    "PREVIEW_FIELDS",
    "SOURCES",
    "SOURCE_LABELS",
    "UPDATE",
    "USER",
    "VOCAB",
    "Evidence",
    "best_quote",
    "explained",
    "from_dict",
    "inferred_fields",
    "merge",
    "note",
    "preview_html",
    "preview_lines",
    "to_dict",
]


def preview_html(evidence: dict[str, Evidence], notes: list[str]) -> str:
    """The provenance block, HTML-escaped and ready for a Telegram message.

    Empty string when there is nothing to explain, so callers can append it
    unconditionally without leaving a blank section in the preview.
    """
    import html as _html

    parts: list[str] = []
    for name in PREVIEW_FIELDS:
        item = evidence.get(name)
        if item is None:
            continue
        if not item.note and item.source in (CAPTION, FILENAME, OCR):
            continue                      # nothing interesting to say
        label = _FIELD_LABELS.get(name, name)
        parts.append(f"• <b>{_html.escape(label)}</b> ← {_html.escape(item.line())}")
    for text in notes:
        parts.append(f"ℹ️ {_html.escape(str(text))}")
    if not parts:
        return ""
    return "\n<b>🧭 از کجا می‌دانم:</b>\n" + "\n".join(parts) + "\n"



# ---------------------------------------------------------------------------
# Blocks: the text model every parser should read
# ---------------------------------------------------------------------------
#
# The extractor used to hand one joined string to every rule, so each rule
# re-decided «is this line a price?» for itself and they disagreed: a weight
# line was a price for the scanner and prose for the title picker. A block is
# one physical line with its roles decided ONCE, plus where it came from
# (which message, which line number). Two structural bugs die here:
#
# * a ``meta`` block (weight/date/SKU/tracking code) can never be read as a
#   price again, because price rules only see blocks carrying the ``price``
#   role — not because a regex was tuned once more;
# * colors are attributed to the message that stated them, so a second
#   product's color list can no longer leak into the first product's preview.

ROLE_PRICE = "price"
ROLE_MODEL = "model"
ROLE_COLORS = "colors"
ROLE_META = "meta"
ROLE_BRAND = "brand"
ROLE_ATTRIBUTE = "attribute"
ROLE_PROSE = "prose"
ROLES = (ROLE_PRICE, ROLE_MODEL, ROLE_COLORS, ROLE_META, ROLE_BRAND, ROLE_ATTRIBUTE, ROLE_PROSE)

#: keys whose number is data about the parcel, never a price
_META_KEY_RE = re.compile(
    r"^\s*(?:sku|sku[_ ]?code|code|stock|count|weight|size|dimension|barcode|"
    r"شناسه|کد(?:\s*رهگیری)?|بارکد|تاریخ|وزن|سایز|ابعاد|گارانتی|تعداد|موجودی|بسته‌بندی|بسته‌بندي)"
    r"\s*[:：]?",
    re.I,
)
#: keys that hold a feature/attribute («ویژگی: …», «جنس: …»)
_ATTRIBUTE_KEY_RE = re.compile(
    r"^\s*(?:attribute|feature|ویژگی|خصوصیات|جنس|متریال|طرح|سبک|نوع|مدل\s*تولید)\s*[:：]",
    re.I,
)
#: a line that is only a section header («آیفون:», «Samsung 📱»)
_SECTION_RE = re.compile(
    r"^[\s\W_]*(?P<brand>iphone|apple|samsung|galaxy|xiaomi|redmi|poco|huawei|honour|honor|"
    r"oppo|vivo|realme|oneplus|nokia|google|pixel|Airpods|watch|tab|پد|"
    r"آیفون|ایفون|آيفون|اپل|سامسونگ|گلکسی|شیائومی|شاومی|ردمی|پوکو|هونر|اوپو|ویوو|ریلمی|وان‌پلاس|نوکیا|هوآوی)"
    r"[\s\W_]*[:：]?[\s\W_]*$",
    re.I,
)


@dataclass(frozen=True)
class Block:
    """One physical line, with its roles decided once.

    ``roles`` is ordered by how much the line is *about* that thing, so
    ``kind`` (the first role) is a good label for logs while ``has()`` is what
    rules should actually test — «S24 اولترا 768t» is a model line that also
    states a price, and pretending otherwise is what created the bugs.
    """

    raw: str
    line_no: int = 0
    message: str = ""
    roles: tuple[str, ...] = (ROLE_PROSE,)

    @property
    def kind(self) -> str:
        return self.roles[0] if self.roles else ROLE_PROSE

    def has(self, role: str) -> bool:
        return role in self.roles

    def text(self) -> str:
        return self.raw.strip()

    def to_dict(self) -> dict[str, Any]:
        return {"raw": self.raw, "line_no": self.line_no, "message": self.message, "roles": list(self.roles)}

    def __str__(self) -> str:                       # compact, for logs
        where = f"{self.message}:{self.line_no}" if self.message else str(self.line_no)
        return f"[{'|'.join(self.roles)}] @{where} {self.raw}"


#: A line that is only a model — «15 ultra», «17pro :», «S24 5g» — after the
#: Persian variant words have been folded. Such lines are never a product title,
#: and telling them apart from prose is what keeps a bare «15 اولترا» under an
#: «آیفون:» header from becoming the name of the product.
_BARE_MODEL_RE = re.compile(
    r"[A-Za-z]{0,3}[\s-]?\d{1,3}(?!\d)[\s/,]*[A-Za-z]{1,8}(?:[\s/]+[A-Za-z]{1,8})*"
)


def is_bare_model(text: str) -> bool:
    clean = re.sub(r"[\s:/.,()\u200c-–—]+$", "", (text or "").strip())
    if not clean or not re.search(r"\d", clean):
        return False
    if re.search(r"[\u0600-\u06FF]", clean):
        return False        # an unfold Persian word is there: this is prose, not a model
    if not re.search(r"[A-Za-z]{2,}", clean):
        return False        # «1098» and «698» are amounts, not models
    return bool(_BARE_MODEL_RE.fullmatch(clean))


def classify_line(line: str) -> tuple[str, ...]:
    """Decide the roles of one line. Pure and cheap — the AI is never called."""
    text = (line or "").strip(" \t\u200b")
    if not text or text in {"-", "—", "•"}:
        return ()
    roles: list[str] = []
    meta = bool(_META_KEY_RE.match(text))
    if _SECTION_RE.match(text) or AIRPODS_HEADER_RE.fullmatch(text):
        # A header line is nothing but a header: «Samsung» must not also be read
        # as a model or as prose, or the first product's section leaks into the
        # count of models.
        return (ROLE_BRAND,)
    if meta:
        roles.append(ROLE_META)
    elif money.looks_like_price_line(text):
        roles.append(ROLE_PRICE)
    if _ATTRIBUTE_KEY_RE.match(text):
        roles.append(ROLE_ATTRIBUTE)
    # «۱۵ اولترا» is a model line only once the Persian variant words are read;
    # without this it looked like prose and was chosen as the product title.
    folded = phone_parser.fold_variant_words(text)
    if money.is_modelish(text) or phone_parser.extract_phone_models(folded) or extract_airpods_models(text) or is_bare_model(folded):
        roles.append(ROLE_MODEL)
    if color_matrix.extract_colors(text, allow_unknown=False):
        roles.append(ROLE_COLORS)
    # A role-less line is prose — still a title candidate, never a price.
    return tuple(dict.fromkeys(roles)) or (ROLE_PROSE,)


def parse_blocks(text: str, *, message: str = "") -> list[Block]:
    """Split one message into classified blocks (``line_no`` is 1-based)."""
    out: list[Block] = []
    for index, raw_line in enumerate((text or "").splitlines(), start=1):
        clean = re.sub(r"[ \t]+", " ", raw_line).strip(" \t-–—•*")
        roles = classify_line(clean)
        if not clean or not roles:
            continue
        out.append(Block(raw=clean, line_no=index, message=message, roles=roles))
    return out


def parse_sources(sources: list[tuple[str, str]]) -> list[Block]:
    """Blocks for several labeled messages, in the given order.

    ``[("info", …), ("caption", …)]`` keeps the precedence the extractor
    depends on (PRODUCT INFO ahead of the caption) *and* remembers which
    message each line came from.
    """
    out: list[Block] = []
    for label, text in sources:
        out.extend(parse_blocks(text, message=label))
    return out


# Test-only: builds role-filtered blocks for the parser tests.
def with_role(blocks: list[Block], role: str) -> list[Block]:
    return [block for block in blocks if block.has(role)]


def describe(block: Block | None) -> str:
    """The line a value was read from, for an evidence note («خط ۴ …»).

    The line number is not decoration: when two messages repeat the same word,
    the seller must see which line the bot believed. Which *message* it was is
    carried by the evidence source itself, so it is not written twice here.
    """
    if block is None:
        return ""
    return f"خط {block.line_no} «{_clip(block.text(), 46)}»"
