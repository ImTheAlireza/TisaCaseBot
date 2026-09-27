"""AI-assisted extraction of product data from free-form Telegram messages."""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any
from collections.abc import Callable, Sequence

import httpx

from bot.config import settings
from bot.services import learning, metrics, model_catalog, money, phone_parser
from bot.services.ai_normalizer import ai_client_session
from bot.services.postmodel import (
    Block,
    classify_line,
    parse_blocks,
    parse_sources,
)
from bot.services import postmodel as ev
from bot.services.category_taxonomy import apply_sku_category_policy
from bot.services.color_matrix import (
    color_key,
    confirmed_colors,
    extract_colors,
    model_signature,
)

logger = logging.getLogger(__name__)


@dataclass
class ProductData:
    title: str = ""
    price: int = 0
    prices: dict[str, int] = field(default_factory=dict)
    sku_prefix: str = ""
    models: list[str] = field(default_factory=list)
    attributes: dict[str, list[str]] = field(default_factory=dict)
    categories: list[str] = field(default_factory=list)
    # Per-model color limits: model label -> colors actually in stock for it.
    # The color attribute still lists EVERY color; this only narrows the
    # variations that get built (see bot/services/color_matrix.py).
    model_colors: dict[str, list[str]] = field(default_factory=dict)
    #: conflicts the model saw in the text but did not dare turn into a model
    #: label (catalog rejects); shown in the preview, never silently fixed
    warnings: list[str] = field(default_factory=list)
    #: what the owner already typed by hand: re-applied after every extraction
    #: (see bot/services/draft_edits.apply_locks) so a fix cannot be undone
    user_edits: dict[str, Any] = field(default_factory=dict)
    #: one-tap offers («منظورت Nokia بود؟»); index is the callback id
    suggestions: list[dict[str, Any]] = field(default_factory=list)
    #: «موجودی ۲۰» / «۲۰ عدد». ``None`` means the text said nothing about stock, and
    #: then no stock field is sent at all — inventing a number is how a shop ends up
    #: selling what it does not have.
    stock: int | None = None
    #: WooCommerce's own vocabulary: instock | outofstock | onbackorder ("" = not stated).
    stock_status: str = ""
    #: «قیمت ویژه ۴۹۸». 0 = no sale. A sale price never replaces ``price``: WooCommerce
    #: keeps both, so the strikethrough price stays right when the sale is removed later.
    sale_price: int = 0
    # Filled in by bot/modules/product_flow.py from bot/services/plan.py so the
    # preview, the REST payload and the ZIP manifest all quote one number.
    variation_count: int = 0
    # Categories the store does not have (the importer creates nothing by
    # accident); surfaced as warnings instead of being dropped silently.
    rejected_categories: list[str] = field(default_factory=list)
    # field name -> where that value came from, with the quote that produced it
    # (bot/services/postmodel.py). The preview shows this so a wrong guess is
    # spotted at a glance instead of after the product is live.
    evidence: dict[str, ev.Evidence] = field(default_factory=dict)
    # policy decisions the user must see, e.g. an amount we refused as a price
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


SYSTEM_PROMPT = """You extract WooCommerce variable-product data from informal Persian Telegram messages.
Return ONLY JSON with keys: title, price, prices, sale_price, stock, stock_status, sku_prefix, attributes, model_colors, categories.
sale_price is the optional discounted price, ONLY when the text says «قیمت ویژه» or «قیمت فروش ویژه», and it must be lower than price. stock is the number of pieces ONLY when the text states a stock count (e.g. «موجودی ۲۰», «۲۰ عدد») — never guess it, and never send 0 because of «ناموجود» (use stock_status outofstock instead). stock_status is exactly one of instock, outofstock, onbackorder, or omitted.
price is the fallback/common integer price in toman; if a bare 3-digit number is clearly in thousands, multiply by 1000. Read prices ONLY from explicit price/amount lines or amounts with a currency suffix such as 768t, 768 تومان, 768k. Never use a phone model number (for example the 17 in iPhone 17) as a price.
prices is an optional object for group pricing, using only keys iphone and android, for example {"iphone":698000,"android":598000}. When the text says «ایفون 698» and «اندروید 598», do not collapse them into one price.
sku_prefix is uppercase Latin letters such as BO. Do not invent values.
The phone models are supplied separately and must not be put in attributes.
attributes must be an object whose keys are Persian attribute names such as رنگ, طرح, جنس and whose values are arrays of distinct strings. Only create an attribute when it has at least TWO selectable values. A single value such as «زرد» is part of the product title/name, not an attribute. Words that describe the product name (for example «قاب پلومریا زرد») must stay in title and must not become attributes.
When the text lists colors per phone model (for example «17promax: سفید/مشکی/نارنجی» or «S25ultra (فقط سفید)» or a section scope such as «xiaomi (فقط سفید)»), the رنگ attribute must still contain EVERY color mentioned anywhere in the text — never only the colors of one model. The per-model limits belong in model_colors instead.
model_colors is an optional object mapping each phone model (use the exact label from PHONE MODELS) to the array of colors available for THAT model only. Fill it only for models whose colors the text states explicitly, and never invent a color that is not written in the text. Omit any model without an explicit color list.
Do not put product descriptions in the result. Do not guess categories from product appearance: only return categories supported by the messages, plus the unavoidable phone-brand path inferred from detected models. The «چاپی» category is controlled ONLY by the product SKU prefix: include it if and only if sku_prefix is CH or SB (case-insensitive; a generated numeric suffix is allowed). Never include «چاپی» for other SKU prefixes, even when the caption says چاپ، چاپی، پرینت, or «چاپ IMD»; those words may describe the design and are not category evidence. For CH/SB, include «چاپی» even if the caption only describes the print indirectly. categories must contain only exact paths from the supplied taxonomy, and never choose فروش ویژه, 💥 بلک فرایدی, or محصولات عمده.
The input has two labeled sources. PRODUCT INFO is the authoritative source for title, SKU, price and explicit attributes. Use CAPTION for those fields only when PRODUCT INFO does not contain them. Models may be merged from both sources. Never let a model number override an explicit price from either source.
"""


def _endpoint() -> str:
    base = settings.ai_base_url.rstrip("/")
    if not base:
        return ""
    return base if base.endswith("/chat/completions") else base + "/chat/completions"


def _dict_field(obj: dict[str, Any], key: str) -> dict[str, Any]:
    """A dict member of an AI response, or ``{}`` (never ``None``)."""
    value = obj.get(key)
    return value if isinstance(value, dict) else {}


def _list_field(obj: dict[str, Any], key: str) -> list[Any]:
    value = obj.get(key)
    return value if isinstance(value, list) else []


def _json_object(text: str) -> dict[str, Any]:
    try:
        obj = json.loads(text.strip())
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise ValueError("AI returned invalid JSON") from None
        obj = json.loads(match.group(0))
    if not isinstance(obj, dict):
        raise ValueError("AI response is not an object")
    return obj


def _digits(value: str) -> str:
    """Persian/Arabic digits → Latin (kept for callers/tests; see :mod:`bot.services.money`)."""
    return money.digits(value)


# Amount parsing lives in bot/services/money.py — one implementation for the
# deterministic path, the AI validation and the preview. ``_number_from_line``
# and ``_scan_prices`` stay as thin wrappers because tests and the flow import
# them by these names.
_number_from_line = money.parse_line_amount


def extract_accessory_models(text: str) -> list[str]:
    """Extract non-phone model families such as AirPods from product info.

    Phone normalization intentionally rejects accessories, so accessories need
    this small deterministic path. Slash notation remains one compatibility
    model (Airpods 1/2 and Airpods Pro/Pro2).
    """
    source = re.sub(r"[🌟•▪️*]+", " ", text or "")
    found: list[str] = []
    direct = re.findall(
        r"(?i)\bairpods?\s+(?:pro\s*/\s*pro\s*2|pro\s*3|[1-4](?:\s*/\s*[1-4])?)\b",
        source,
    )
    found.extend(re.sub(r"\s+", " ", value).strip() for value in direct)
    # A common Telegram format is `Airpods:` followed by bare values on the
    # next lines: 1/2, 3, 4, Pro/Pro2, Pro3.
    # [ 	]* (not \s*) after the colon: with \s* the match swallows the newline and
    # the *first* value line becomes the tail, so «Airpods:» + three bare values kept
    # only one of them. The tail must stay on the same line, and an empty tail is
    # exactly the signal that the values follow below.
    for match in re.finditer(r"(?im)^\s*airpods?[ 	]*:[ 	]*(.*)$", source):
        tail = match.group(1).strip()
        if tail:
            candidates = [tail]
        else:
            candidates = []
            for line in source[match.end():].splitlines():
                value = re.sub(r"^[^A-Za-z0-9]+", "", line).strip()
                if not value:
                    continue
                if re.fullmatch(r"(?i)(?:[1-4](?:\s*/\s*[1-4])?|pro\s*/\s*pro\s*2|pro\s*3)", value):
                    candidates.append(value)
                else:
                    break
        for value in candidates:
            normalized = re.sub(r"\s+", " ", value).strip()
            if re.fullmatch(r"(?i)(?:[1-4](?:\s*/\s*[1-4])?|pro\s*/\s*pro\s*2|pro\s*3)", normalized):
                found.append("Airpods " + normalized)
    return list(dict.fromkeys(found))


@dataclass
class PriceScan:
    """The prices one text block states, plus where each came from."""

    price: int = 0
    prices: dict[str, int] = field(default_factory=dict)
    evidence: dict[str, ev.Evidence] = field(default_factory=dict)
    #: numeric lines that were rejected as prices (weight, dates, SKUs)
    rejected: list[str] = field(default_factory=list)
    #: rejected lines where the seller *did* say «قیمت» — worth showing, because
    #: a missing price is what they are looking for; the rest would be noise.
    surprising: list[str] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return bool(self.price or self.prices)


def _scan_prices(items: Sequence[Block | str]) -> PriceScan:
    """Parse the prices out of ONE message, reading block roles.

    Three rules, learned the hard way from real posts:

    * a line must *be* about a price. «وزن 250 گرم», «تاریخ 1403/01/01» and
      «SKU: BO147» all contain numbers and none of them is a price. They used to
      win because «the last amount in the block wins» had no shape check; now
      they are classified as ``meta`` once, by :func:`bot.services.postmodel
      classify_line`, and price rules never see them at all;
    * within a block the last amount wins, so a follow-up correction
      («1098», then «قیمت 1098000 تومان») takes effect — with one asymmetry: a
      stated price is only replaced by another stated price, never by a bare
      number that happens to come after it;
    * every group mentioned on a line is read («ایفون 698 اندروید 598» used to
      return only the iPhone price, and Android silently inherited it);
    * a line that states a **sale** price is not a regular price at all. WooCommerce
      keeps both numbers (the strikethrough is the regular one), and
      :func:`scan_stock_and_sale` owns «قیمت ویژه» — so if the sale line also
      entered this scan, the "last stated price wins" rule let it overwrite the
      regular price («قیمت 500000» + «قیمت ویژه 420000» published 420000 twice and
      the discount disappeared).

    Plain strings are accepted for callers that have no blocks (and are
    classified on the spot), so this stays usable from tests and scripts.
    """
    scan = PriceScan()
    price_explicit = False
    for item in items:
        block = item if isinstance(item, Block) else _one_block(item)
        line = block.text()
        if not line:
            continue
        source = _source_of(block)
        if _SALE_LABEL_RE.match(line):
            # «قیمت ویژه …» قیمتِ اصلی نیست؛ مالِ scan_stock_and_sale است. بی این
            # خط، قاعدهٔ «آخرین قیمتِ اعلام‌شده برنده است» تخفیف را جای قیمت می‌زد.
            continue
        if block.has(ev.ROLE_META) or not block.has(ev.ROLE_PRICE):
            if money.amounts_in_line(line):
                # It had a number and we still said no. Only a line that
                # mentioned a price is surfaced — otherwise every «قاب ۱۳» and
                # every date would add a paragraph to the preview.
                scan.rejected.append(line)
                if money.states_price_explicitly(line):
                    scan.surprising.append(line)
            continue
        value = money.parse_line_amount(line)
        if not value:
            continue
        if not money.in_accepted_range(value):
            scan.rejected.append(line)
            scan.surprising.append(line)
            continue
        groups = money.group_amounts(line)
        for group, group_value in groups.items():
            scan.prices[group] = group_value
            ev.merge(scan.evidence, "prices", source, quote=ev.describe(block))
        if groups:
            continue
        explicit = money.states_price_explicitly(line)
        if explicit or not scan.price or not price_explicit:
            scan.price = value
            price_explicit = explicit
            ev.merge(scan.evidence, "price", source, quote=ev.describe(block))
    return scan


def _one_block(line: str) -> Block:
    """Classify a bare line on the spot (callers that have no blocks yet)."""
    clean = re.sub(r"\s+", " ", (line or "").strip())
    return Block(raw=clean, line_no=1, roles=classify_line(clean) or (ev.ROLE_PROSE,))


def _source_of(block: Block | None) -> str:
    """Which evidence source a line belongs to (PRODUCT INFO vs caption)."""
    if block is None:
        return ev.CAPTION
    return ev.INFO if block.message == "info" else ev.CAPTION


def _attach_suggestions(data: ProductData, text: str) -> None:
    """Fill ``data.suggestions`` with the typo offers the preview can act on.

    A warning that cannot be answered is a nag. Here the bot already knows the
    word it does not trust and the one brand that fits, so it offers the fix —
    and accepting it teaches the shop dictionary, not just this product.
    """
    from bot.services import draft_edits

    data.suggestions = draft_edits.brand_suggestions(text)


def _add_catalog_warnings(data: ProductData, text: str) -> None:
    """Attach brand/variant conflicts the catalog can see but the parser cannot.

    Never a hard stop: a new release may genuinely be missing from the table,
    so the bot says so and lets the owner decide, instead of dropping a model
    (or inventing one) on its own.
    """
    for brand, line, word in model_catalog.suspicious_lines(text):
        data.notes.append(
            f"«{_clip_line(line)}»: «{word}» برای {brand} وجود ندارد — "
            "اگر واقعاً این مدل است، در «✏️ اصلاح اطلاعات» بنویس"
        )
    for token in model_catalog.unknown_brand_words(text):
        data.notes.append(f"برند «{token}» در کاتالوگ ربات نیست؛ ممکن است مدلی از جا بماند")


def _clip_line(text: str, limit: int = 48) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _split_lines(text: str) -> list[str]:
    """Non-empty lines, whitespace-collapsed — the rule parse_blocks uses too."""
    return [block.text() for block in parse_blocks(text)]


#: How a seller writes the stock of a product: a labelled line, or a bare count.
#: Deliberately literal — a number that is not *called* stock stays a number.
_STOCK_LABEL_RE = re.compile(r"(?i)^\s*(?:موجودی|موجوديت\s*(?:فعلی)?|stock|quantity)\s*[:=]?\s*(.*)$")
_COUNT_SUFFIX_RE = re.compile(r"([\d\u0660-\u0669\u06f0-\u06f9][\d,\u0660-\u0669\u06f0-\u06f9]{0,6})\s*(?:عدد)\b")
_SALE_LABEL_RE = re.compile(r"(?i)^\s*(?:قیمت\s*(?:فروش\s*)?ویژه|قیمت\s*ویژه|sale[_ ]?price)\s*[:=]?\s*(.*)$")
_OUT_OF_STOCK_RE = re.compile(r"(?i)(?:تمام\s*شده|ناموجود|بدون\s*موجودی|out\s*of\s*stock)")
#: «پیش‌فروش» and «پیش فروش» differ by a ZWNJ, which ``\s`` does not match — so the
#: separator class has to name it, or pre-order products silently read as ordinary stock.
_SEP = r"[\s\u200c\u200f-]*"
_BACKORDER_RE = re.compile(rf"(?i)(?:پیش{_SEP}سفارش|پیش{_SEP}فروش|backorder)")


def _small_int(raw: object) -> int:
    """Digits of a stock count (never a money amount): «۲۰», «2,000»."""
    digits = _digits(str(raw or "")).replace(",", "")
    return int(digits) if digits.isdigit() and len(digits) <= 7 else 0


def scan_stock_and_sale(blocks: Sequence[Block | str]) -> dict[str, Any]:
    """Read «موجودی ۲۰» / «۲۰ عدد» / «قیمت ویژه ۴۹۸» out of the lines that *say* them.

    Two rules keep this boring on purpose:

    * a stock number must be called stock (a label, or an «عدد» suffix) — a bare «۲۰» in
      a caption is a model, a weight or a date, and guessing it would put a wrong
      quantity on the shop's shelf;
    * nothing is derived from anything else: «ناموجود» sets ``stock_status`` and leaves
      ``stock`` alone, because «صفر عدد» and «موجودی ردیابی نمی‌شود» are different
      statements and the second is the safe default.
    """
    lines = [block.text() if isinstance(block, Block) else str(block) for block in blocks]
    out: dict[str, Any] = {"stock": None, "stock_status": "", "sale_price": 0,
                           "stock_quote": "", "sale_quote": ""}
    for index, line in enumerate(lines):
        text = (line or "").strip()
        if not text:
            continue
        if not out["stock_status"]:
            if _OUT_OF_STOCK_RE.search(text):
                out["stock_status"] = "outofstock"
            elif _BACKORDER_RE.search(text):
                out["stock_status"] = "onbackorder"
        sale = _SALE_LABEL_RE.match(text)
        if sale and not out["sale_price"]:
            out["sale_price"] = money.parse_line_amount(sale.group(1)) or _small_int(sale.group(1))
            out["sale_quote"] = text[:60]
            continue
        label = _STOCK_LABEL_RE.match(text)
        if label and out["stock"] is None:
            value = _small_int(label.group(1))
            if not value:
                # «موجودی:» on its own line, the number on the next one — sellers do this.
                for follow in lines[index + 1:index + 3]:
                    value = _small_int(follow)
                    if value:
                        break
            if value:
                out["stock"] = value
                out["stock_quote"] = text[:60]
            continue
        count = _COUNT_SUFFIX_RE.search(text)
        if count and out["stock"] is None and not money.states_price_explicitly(text):
            value = _small_int(count.group(1))
            if value:
                out["stock"] = value
                out["stock_quote"] = text[:60]
    if out["stock"] == 0:
        out["stock"] = None      # «۰ عدد» is not a reading we can trust as an intent
    return out


def _fallback(
    text: str,
    models: list[str],
    price_blocks: list[str] | None = None,
    blocks: list[Block] | None = None,
    ignore_color_messages: frozenset[str] | set[str] = frozenset(),
) -> ProductData:
    """Read a product out of the text alone (no AI): prices, title, colors.

    ``blocks`` is the preferred input — the caller has already classified every
    line and knows which message it came from. ``price_blocks``/``text`` stay for
    scripts and tests: they are turned into blocks here, so there is still only
    one reading path.

    ``ignore_color_messages`` is the owner's answer to «این رنگ‌ها مال این محصول
    نیست»: those messages keep their title and price, but no color of theirs —
    including a color hidden inside a prose line — enters the list.
    """
    if blocks is None:
        groups = [parse_blocks(block) for block in (price_blocks or [text])]
    else:
        labels = list(dict.fromkeys(block.message for block in blocks))
        groups = [[b for b in blocks if b.message == label] for label in labels]
    all_blocks = [block for group in groups for block in group] or parse_blocks(text)

    scan = PriceScan()
    for group in groups:
        group_scan = _scan_prices(group)
        if not scan.price and group_scan.price:
            scan.price = group_scan.price
        for name, item in group_scan.evidence.items():
            ev.merge(scan.evidence, name, item.source, quote=item.note)
        for group_name, value in group_scan.prices.items():
            scan.prices.setdefault(group_name, value)
        scan.rejected.extend(group_scan.rejected)
        scan.surprising.extend(group_scan.surprising)
    price, prices = scan.price, scan.prices
    if not price and prices:
        price = next(iter(prices.values()))

    def flagged(block: Block, *roles: str) -> bool:
        return any(block.has(role) for role in roles)

    prefix = ""
    for block in all_blocks:
        if re.fullmatch(r"[A-Za-z]{1,12}", block.text()) and not flagged(block, ev.ROLE_PRICE, ev.ROLE_META):
            prefix = block.text().upper()
            break
    explicit_title = next(
        (
            re.sub(r"^\s*(?:عنوان|نام\s*محصول)\s*[:：]\s*", "", block.text()).strip()
            for block in all_blocks
            if re.match(r"^\s*(?:عنوان|نام\s*محصول)\s*[:：]", block.text(), re.I)
        ),
        "",
    )
    def availability_only(block: Block) -> bool:
        """خطی که چیزی جز وضعیت موجودی نمی‌گوید: «ناموجود»، «⛔ تمام شده»، «پیش‌فروش».

        این خط‌ها نام محصول نیستند. بدون این خط، پستی که پیام اطلاعاتش فقط
        «ناموجود» بود محصولی به نام «ناموجود» می‌ساخت (کپشنِ توصیفی، طبق قاعدهٔ
        اولویت، شانس نمی‌رسید). واژه‌ها از همان دو regex زندهٔ این ماژول می‌آیند،
        پس فهرست دومی برای «کلمات موجودی» وجود ندارد.
        """
        rest = _BACKORDER_RE.sub(" ", _OUT_OF_STOCK_RE.sub(" ", block.text()))
        return not re.sub(r"\W+", "", rest)

    # A title is the line that is *not* data: no price, no meta key, no section
    # header, no attribute list, not a stock status, and not a line that already
    # is a model name.
    candidates = [
        block
        for block in all_blocks
        if block.text() != prefix
        and not availability_only(block)
        and not flagged(block, ev.ROLE_META, ev.ROLE_BRAND, ev.ROLE_ATTRIBUTE)
        and not re.search(r"تومان|تومن|هزار|قیمت|price", block.text(), re.I)
        and not re.match(r"^\s*(?:sku|شناسه|کد|مدل|مدل‌ها|رنگ)\s*[:：]?", block.text(), re.I)
        and not re.fullmatch(r"\d[\d,،.]*[tTkKت]?", _digits(block.text()))
    ]
    def usable(block: Block) -> bool:
        return not any(model in block.text() for model in models)

    # A title is the line that *describes* the product. So prose blocks get the
    # first chance; a model or price line is used only if nothing else is left,
    # which is what keeps «15 اولترا» (a bare model under an «آیفون:» header)
    # from becoming the product title.
    def descriptive(block: Block) -> bool:
        # «15 اولترا» is a model, not a name: a line that is *only* a number and
        # a variant word never becomes the title, or the product is called «15
        # اولترا» and the real name (in another message) is dropped.
        return usable(block) and not ev.is_bare_model(phone_parser.fold_variant_words(block.text()))

    title = explicit_title or next(
        (block.text() for block in candidates if block.has(ev.ROLE_PROSE) and descriptive(block)),
        "",
    ) or next((block.text() for block in candidates if descriptive(block)), "")
    title_block = next((block for block in candidates if block.text() == title), None)

    attrs: dict[str, list[str]] = {}
    # Without the AI the only attribute that can be read reliably is the color
    # list. Prose and model lines are NOT a selectable attribute: dumping them
    # into a «ویژگی» axis used to multiply the variation count by the whole
    # caption. Colors are read from every non-title line (also «رنگ: …» and
    # model lines such as «S26ultra (صورتی و سفید)»); the per-model limits are
    # then added by bot/services/color_matrix.py.
    colors: list[str] = []
    seen_colors: set[str] = set()
    color_source: Block | None = None
    by_message: dict[str, list[str]] = {}
    ignored = set(ignore_color_messages or ())
    for block in all_blocks:
        if block.text() == title or block.message in ignored:
            continue
        for color in extract_colors(block.text(), allow_unknown=False):
            key = color_key(color)
            if key not in seen_colors:
                seen_colors.add(key)
                colors.append(color)
                by_message.setdefault(block.message or "متن", []).append(color)
                color_source = color_source or block
    if len(colors) >= 2:
        attrs["رنگ"] = colors

    evidence = dict(scan.evidence)
    notes: list[str] = []
    if title:
        ev.merge(
            evidence,
            "title",
            _source_of(title_block) if title_block is not None else ev.CAPTION,
            quote=ev.describe(title_block) if title_block is not None else title,
        )
    if prefix:
        ev.merge(evidence, "sku_prefix", ev.CAPTION, quote=prefix)
    if colors:
        ev.merge(
            evidence,
            "colors",
            _source_of(color_source) if color_source is not None else ev.CAPTION,
            quote=ev.describe(color_source) if color_source is not None else "، ".join(colors[:6]),
        )
    if models:
        ev.merge(evidence, "models", ev.CAPTION, quote="، ".join(models[:4]))
    for rejected in scan.surprising[:2]:
        notes.append(f"«{_clip_line(rejected)}» عدد داشت ولی قیمت نشد (خارج از بازه یا بی‌واژه)")
    if price and len(prices) < 2:
        notes.append("قیمت از متن محصول گرفته شد و برای همهٔ رنگ‌ها یکسان است")
    # Colors stated in two messages are merged on purpose (dropping them would
    # delete sellable variations), but a second message that also carries its
    # own title is usually a second product — say so, do not decide silently.
    if len(by_message) > 1:
        parts = " + ".join(f"{label} ({'، '.join(vals[:3])})" for label, vals in by_message.items())
        notes.append(f"رنگ‌ها از چند پیام جمع شد: {parts}")
        titled = [
            label
            for label in by_message
            if any(b.has(ev.ROLE_PROSE) for b in all_blocks if b.message == label)
        ]
        if len(titled) > 1:
            notes.append(
                "⚠️ بیش از یک پیام عنوان خودش را دارد؛ اگر پیام دوم محصول دیگری است، "
                "با «➖ حذف رنگ‌های این پیام» یا اصلاح دستی جداش کن"
            )
    stock_scan = scan_stock_and_sale(all_blocks)
    if stock_scan["stock"] is not None:
        ev.merge(evidence, "stock", ev.CAPTION, quote=stock_scan["stock_quote"])
    if stock_scan["sale_price"]:
        ev.merge(evidence, "sale_price", ev.CAPTION, quote=stock_scan["sale_quote"])
        if price and stock_scan["sale_price"] >= price:
            notes.append(
                f"قیمت ویژه ({money.format_toman(stock_scan['sale_price'])}) از قیمت اصلی کمتر نیست"
            )
    return ProductData(
        title=title,
        price=price,
        prices=prices,
        sku_prefix=prefix,
        models=models,
        attributes=attrs,
        stock=stock_scan["stock"],
        stock_status=stock_scan["stock_status"],
        sale_price=stock_scan["sale_price"],
        evidence=evidence,
        notes=notes,
    )


def _clean_model_colors(raw: dict[str, Any], models: list[str], source_text: str) -> dict[str, list[str]]:
    """Validate the AI's per-model colors against the real models and the text.

    A key must match a detected model by signature and every color must really
    appear in the source, so an AI guess can never delete a sellable variation
    or invent a color that is not in stock.
    """
    if not isinstance(raw, dict) or not raw:
        return {}
    signatures = {model_signature(model): str(model) for model in models if model}
    out: dict[str, list[str]] = {}
    for key, values in raw.items():
        if not isinstance(values, list):
            continue
        target = signatures.get(model_signature(str(key)))
        if not target:
            continue
        bucket = out.setdefault(target, [])
        known = {color_key(item) for item in bucket}
        for color in confirmed_colors([str(v) for v in values if str(v).strip()], source_text):
            if color_key(color) not in known:
                known.add(color_key(color))
                bucket.append(color)
    return {label: colors for label, colors in out.items() if colors}


def _apply_learned_terms(data: ProductData, where: str = "") -> ProductData:
    """Apply the owner's learned term corrections to titles and attribute values.

    Prices are handled inside ``_number_from_line`` (a learned scale is a numeric
    rule, not a text substitution); SKU prefixes are left alone because the owner
    sets those deliberately.

    ``where`` is the product text, used only to decide whether a rule the owner
    limited to one category applies here.
    """
    fired: list[str] = []
    before = data.title
    data.title = learning.apply_terms(data.title, where=where, fired=fired)
    if data.title != before:
        ev.merge(data.evidence, "title", ev.LEARNED, quote=f"«{before}» ← «{data.title}»")
    if data.attributes:
        for name, values in data.attributes.items():
            applied = learning.apply_terms_to_values(values, where=where, fired=fired)
            if applied != values:
                ev.merge(data.evidence, name, ev.LEARNED,
                         quote=f"«{'، '.join(values[:3])}» ← «{'، '.join(applied[:3])}»")
            data.attributes[name] = applied
    # «Which of my rules touched which product?» is the question the memory screen
    # answers; the rewrite above is the only moment both halves are known.
    for rule_id in dict.fromkeys(fired):
        learning.note_application(rule_id, title=data.title, effect="بازنویسی واژه اعمال شد")
    return data


def _drop_color_lines(text: str, label: str, suppressed: set[str]) -> str:
    """Remove the color-list lines of a message the owner marked as another product.

    The lines are cut from the text itself, not only from the deterministic
    result: the AI must read exactly what we read, or it re-adds the colors in
    the next round and the owner's decision quietly disappears.
    """
    if label not in suppressed or not text:
        return text
    from bot.services import draft_edits

    keep = [block.raw for block in draft_edits.suppress_colors(parse_blocks(text, message=label), {label})]
    return "\n".join(keep)


def _merge_stock_and_sale(
    fallback: ProductData,
    obj: dict[str, Any],
    *,
    evidence: dict[str, Any] | None = None,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    """Combine what the text said with what the model read. The text wins, always.

    Kept as one function (instead of inline in the AI branch) for two reasons: the
    no-AI path already read these fields, and a rule that exists twice is a rule the
    two paths can disagree about. ``ai_used`` is not decoration — a stock number a
    model inferred has to be said out loud on the card, so the owner checks it.
    """
    out: dict[str, Any] = {
        "stock": fallback.stock,
        "stock_status": fallback.stock_status,
        "sale_price": fallback.sale_price,
        "ai_used": False,
    }
    if out["stock"] is None:
        ai_stock = _small_int(obj.get("stock"))
        if ai_stock:
            out["stock"] = ai_stock
            out["ai_used"] = True
            ev.merge(evidence if evidence is not None else fallback.evidence, "stock", ev.AI,
                     quote="عدد موجودی را هوش مصنوعی خوانده", overwrite=True)
            (notes if notes is not None else fallback.notes).append(
                "موجودی را هوش مصنوعی از متن درآورده؛ اگر دقیق نیست با «✏️ ویرایش» عوضش کن"
            )
    if not out["sale_price"]:
        raw_sale = obj.get("sale_price")
        ai_sale = money.parse_line_amount(str(raw_sale or "")) or _small_int(raw_sale)
        if ai_sale:
            out["sale_price"] = ai_sale
            out["ai_used"] = True
            ev.merge(evidence if evidence is not None else fallback.evidence, "sale_price", ev.AI,
                     quote="قیمت ویژه را هوش مصنوعی خوانده", overwrite=True)
    wanted_status = str(obj.get("stock_status") or "").strip().lower()
    if not out["stock_status"] and wanted_status in ("instock", "outofstock", "onbackorder"):
        out["stock_status"] = wanted_status
    return out


async def extract_product(
    text: str,
    models: list[str],
    taxonomy: str,
    caption: str = "",
    info_text: str = "",
    color_suppressed: set[str] | None = None,
    *,
    client: httpx.AsyncClient | None = None,
    diagnostic: Callable[[str], None] | None = None,
) -> ProductData:
    # Keep one AI request, but preserve provenance. The deterministic parser
    # receives PRODUCT INFO first so its title/SKU/price precedence is stable.
    source_for_fallback = "\n".join(part for part in (info_text, caption) if part.strip()) or text
    # PRODUCT INFO stays authoritative over the caption, but within it the
    # owner's newest line is a correction of the older ones (see _scan_prices).
    # Labeled blocks are what makes that precedence explainable: every value
    # knows which message and which line it came from.
    suppressed = {x for x in (color_suppressed or set()) if x}
    if suppressed:
        caption = _drop_color_lines(caption, "caption", suppressed)
        info_text = _drop_color_lines(info_text, "info", suppressed)
        source_for_fallback = "\n".join(part for part in (info_text, caption) if part.strip()) or text
    blocks = parse_sources([("info", info_text), ("caption", caption)])
    if blocks:
        fallback = _fallback(source_for_fallback, models, blocks=blocks, ignore_color_messages=suppressed)
    else:
        fallback = _fallback(source_for_fallback, models, price_blocks=[info_text, caption])
    # Learned term corrections apply to the deterministic result as well, so a
    # shop with no AI configured still honors what the owner taught the bot.
    _apply_learned_terms(fallback, source_for_fallback)
    if not (settings.ai_base_url and settings.ai_token and settings.ai_model):
        fallback.categories = apply_sku_category_policy(fallback.categories, fallback.sku_prefix)
        _add_catalog_warnings(fallback, source_for_fallback)
        _attach_suggestions(fallback, source_for_fallback)
        return fallback
    # The AI is told the same rules explicitly, so the two paths cannot disagree
    # about a corrected term.
    learned_rules = learning.rules_for_prompt(source_for_fallback)
    rules_block = ""
    if learned_rules:
        rules_block = (
            "=== LEARNED OWNER RULES / قواعد یادگرفته‌شده از اصلاحات مالک ===\n"
            f"{learned_rules}\n\n"
        )
    # The catalog goes into the prompt as well, so the model proposes «iPhone 13
    # Pro Max» instead of «iPhone 13 Pro Plus» and reports what it cannot fit
    # instead of inventing a sellable variation for a phone that does not exist.
    catalog_block = model_catalog.prompt_block(models)
    user_message = (
        f"TAXONOMY:\n{taxonomy}\n\n"
        f"PHONE MODELS:\n{json.dumps(models, ensure_ascii=False)}\n\n"
        f"{catalog_block}"
        f"{rules_block}"
        f"=== CAPTION / کپشن عکس‌ها ===\n{caption or '<خالی>'}\n\n"
        f"=== PRODUCT INFO / متن اطلاعات محصول ===\n{info_text or '<خالی>'}\n\n"
        f"=== END INPUT ==="
    )
    payload = {
        "model": settings.ai_model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        "response_format": {"type": "json_object"},
    }
    metrics.incr("ai_calls")
    started = time.perf_counter()
    try:
        if client is None:
            async with ai_client_session(timeout=settings.ai_timeout_seconds) as owned_client:
                response = await owned_client.post(
                    _endpoint(),
                    headers={"Authorization": f"Bearer {settings.ai_token}"},
                    json=payload,
                )
        else:
            response = await client.post(
                _endpoint(),
                headers={"Authorization": f"Bearer {settings.ai_token}"},
                json=payload,
            )
        response.raise_for_status()
        metrics.observe("ai_latency_ms", metrics.elapsed(started))
        content = response.json()["choices"][0]["message"]["content"]
        obj = _json_object(content)
        attrs = _dict_field(obj, "attributes")
        clean_attrs = {
            str(k): list(dict.fromkeys(str(v) for v in vals if str(v).strip()))
            for k, vals in attrs.items()
            if isinstance(vals, list) and len({str(v).strip() for v in vals if str(v).strip()}) >= 2
        }
        raw_model_colors = _dict_field(obj, "model_colors")
        model_colors = _clean_model_colors(raw_model_colors, models, source_for_fallback)
        raw_prices = _dict_field(obj, "prices")
        prices = {}
        for key, value in raw_prices.items():
            group = str(key).casefold().strip()
            if group in {"iphone", "ایفون", "آیفون"}:
                group = "iphone"
            elif group in {"android", "اندروید", "samsung", "xiaomi", "شیائومی"}:
                group = "android"
            else:
                continue
            parsed = _number_from_line(str(value))
            if not parsed:
                try:
                    parsed = int(_digits(str(value)).replace(",", ""))
                except ValueError:
                    parsed = 0
            if parsed:
                prices[group] = parsed
        if not prices:
            prices = fallback.prices
        ai_categories = [str(category) for category in _list_field(obj, "categories")]
        final_sku_prefix = re.sub(
            r"[^A-Za-z0-9]", "", str(fallback.sku_prefix or obj.get("sku_prefix") or "")
        ).upper()
        categories = apply_sku_category_policy(ai_categories, final_sku_prefix)
        ai_price = int(_digits(str(obj.get("price") or 0)).replace(",", "") or 0)
        # A bare amount such as `768t` is deterministic and must win over an
        # AI hallucination based on a model number (for example iPhone 17).
        final_price = fallback.price if fallback.price else ai_price
        if fallback.price and not fallback.prices:
            prices = {}
        # Provenance: which field came from the model, and which AI answer the
        # deterministic reading of the text overrode. Everything the AI adds is
        # still an interpretation — the preview must not dress it as a fact.
        evidence = dict(fallback.evidence)
        notes = list(fallback.notes)
        ai_title = str(obj.get("title") or "").strip()
        if ai_title and ai_title != fallback.title:
            ev.merge(evidence, "title", ev.AI, quote=ai_title, overwrite=True)
            notes.append("عنوان را هوش مصنوعی نوشته؛ اگر لازم شد اصلاحش کن")
        if fallback.price and ai_price and ai_price != fallback.price:
            notes.append("قیمت هوش مصنوعی کنار گذاشته شد؛ عددی که خودت نوشتی معتبرتر است")
        if fallback.price and prices and not fallback.prices:
            notes.append("قیمت گروهی هوش مصنوعی حذف شد چون کپشن یک قیمت صریح داشت")
        if not fallback.prices and prices:
            ev.merge(evidence, "prices", ev.AI, quote="قیمت جدا برای هر گروه", overwrite=True)
        if suppressed:
            # The AI reads the same text we do, and it is eager: it would put
            # back the colors the owner just marked as «مال محصول دیگر». After a
            # suppression the color list is therefore only what the kept text
            # still says, never what the model inferred.
            kept_colors = [str(x) for x in (fallback.attributes.get("رنگ") or [])]
            clean_attrs.pop("رنگ", None)
            if kept_colors:
                clean_attrs["رنگ"] = kept_colors
            model_colors = {
                model: [c for c in colors if c in kept_colors]
                for model, colors in model_colors.items()
            }
            model_colors = {m: c for m, c in model_colors.items() if c}
            notes.append("رنگ فقط از پیام‌های باقی‌مانده خوانده شد (درخواست خودت)")
        if clean_attrs and not fallback.attributes:
            for name, values in clean_attrs.items():
                ev.merge(evidence, name if name in ev.PREVIEW_FIELDS else "colors",
                         ev.AI, quote="، ".join(values[:4]), overwrite=True)
        if ai_categories and not fallback.categories:
            ev.merge(evidence, "category", ev.AI, quote="، ".join(ai_categories)[:60], overwrite=True)
        if str(obj.get("description") or "").strip():
            ev.merge(evidence, "description", ev.AI, quote="نوشتهٔ هوش مصنوعی", overwrite=True)
        # Stock and the sale price: the deterministic reading of the text wins, exactly
        # like the price does — a model that sees «۲۰ عدد» is free to invent it too.
        stock_sale = _merge_stock_and_sale(fallback, obj, evidence=evidence, notes=notes)
        result = ProductData(
            title=str(obj.get("title") or fallback.title).strip(),
            price=final_price,
            prices=prices,
            stock=stock_sale["stock"],
            stock_status=stock_sale["stock_status"],
            sale_price=stock_sale["sale_price"],
            sku_prefix=final_sku_prefix,
            models=models,
            attributes=clean_attrs,
            categories=categories,
            model_colors=model_colors,
            evidence=evidence,
            notes=notes,
        )
        _add_catalog_warnings(result, source_for_fallback)
        _attach_suggestions(result, source_for_fallback)
        # What the model saw but refused to invent (a variant outside the
        # catalog) is a note for the owner, not a silent correction.
        result.warnings = [str(x).strip() for x in _list_field(obj, "warnings") if str(x).strip()]
        result.notes.extend(f"هوش مصنوعی گزارش داد: {text}" for text in result.warnings)
        if diagnostic is not None:
            diagnostic("استخراج جزئیات: پاسخ AI پردازش شد")
        return _apply_learned_terms(result, source_for_fallback)
    except Exception as exc:
        if diagnostic is not None:
            diagnostic(
                f"استخراج جزئیات: خطای {type(exc).__name__}؛ از متن و پارسر قطعی استفاده شد"
            )
        # Silent failure used to look like «the bot misread me»; say what
        # happened in the log and in the preview so the user knows the text was
        # read without the model's help.
        logger.warning("AI extraction failed (%s); continuing from the text alone", exc)
        metrics.incr("ai_failures")
        metrics.incr("extract_fallback_used")
        fallback.notes.append("هوش مصنوعی در دسترس نبود؛ فقط متن خودت خوانده شد")
        return fallback
