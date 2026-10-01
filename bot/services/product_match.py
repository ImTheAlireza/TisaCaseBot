"""Finding «همان محصول» in the shop, and reading what the shop says today (plan 5.1).

An update used to start with «برو در پیشخوان وردپرس جستجو کن، SKU را یادت باشد، برگرد
اینجا». That is two places to be wrong in: the seller picks one product, the bot changes
another. So the lookup happens here instead — the seller types a SKU or a few words of the
title, and the shop's own answer is shown back as buttons.

Two rules shape this module:

* **read-only.** Nothing here writes; :mod:`bot.services.update_apply` does. A module that
  both finds and mutates is how a typo in a search box becomes a price change.
* **the shop's numbers are the diff's baseline.** `read()` returns price/stock *as the store
  answered them*, because «موجودی ۱۲ واریژن ۰→۳» is only honest if the ۰ came from the shop
  and not from our memory of the last publish.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from bot.services.color_matrix import color_key, is_color_attribute, is_model_attribute, model_signature
from bot.services.woo_client import Audit, WooClient, WooCommerceAPIError, check, error_message, products_base

#: «BO148», «SB-12», «IP15-3» — one token, letters and digits together. That is a SKU the
#: seller has no reason to type any other way, and it is worth an exact lookup first: a
#: `search=` of a title fragment on a shared host can return the wrong product, an SKU cannot.
_SKU_LIKE_RE = re.compile(r"^[A-Za-z]{1,10}[0-9]{1,7}[A-Za-z0-9_-]{0,8}$")


def _money(row: dict[str, Any], key: str) -> int:
    raw = row.get(key)
    if raw in (None, ""):
        return 0
    try:
        return int(float(str(raw).replace(",", "")))
    except (TypeError, ValueError):
        return 0


def _body(response: Any) -> Any:
    """The JSON of a *successful* response — or a clear error instead of a ``JSONDecodeError``.

    A shop in maintenance (or behind a login wall) answers 200 with an HTML page; that must read as
    «the shop did not answer», never as an exception from deep inside a parser.
    """
    try:
        return response.json()
    except ValueError as exc:
        raise WooCommerceAPIError(
            getattr(response, "status_code", 0), "پاسخ فروشگاه JSON نبود (تعمیرات یا خطای هاست؟)") from exc


def _image_id(raw: Any) -> int:
    """The media id inside WooCommerce's ``image`` object (``None`` / ``{}`` / ``{"id": 0}`` → 0)."""
    if not isinstance(raw, dict):
        return 0
    try:
        return int(raw.get("id") or 0)
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True)
class Candidate:
    """One product the shop says exists — the thing a button is pressed on."""

    product_id: int
    title: str
    sku: str
    status: str
    type: str
    price: int

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Candidate:
        return cls(
            product_id=int(row.get("id") or 0),
            title=str(row.get("name") or "").strip(),
            sku=str(row.get("sku") or "").strip(),
            status=str(row.get("status") or ""),
            type=str(row.get("type") or "simple"),
            price=_money(row, "price") or _money(row, "regular_price"),
        )

    def label(self) -> str:
        """The one line the seller reads before tapping — including what we do *not* know."""
        status = {"draft": "پیش‌نویس", "publish": "منتشر", "private": "خصوصی",
                  "pending": "در انتظار", "trash": "سطل بازیافت"}.get(self.status, self.status or "—")
        bits = [f"#{self.product_id}", self.title[:48] or "(بدون عنوان)",
                f"SKU: {self.sku}" if self.sku else "SKU ندارد",
                f"{self.price:,} تومان" if self.price else "بدون قیمت", status]
        return " · ".join(bits)


#: WooCommerce's stock statuses in the words the seller uses. One table, so the diff screen
#: and a variation's own summary never disagree about what «onbackorder» means.
STATUS_FA = {"instock": "موجود", "outofstock": "ناموجود", "onbackorder": "پیش‌فروش"}


def status_text(status: str) -> str:
    return STATUS_FA.get(str(status or ""), str(status or "—"))


@dataclass(frozen=True)
class ShopVariation:
    """A variation as the store has it — attributes first, because that is the only key we can match typed words against."""

    variation_id: int
    attributes: tuple[tuple[str, str], ...]
    price: int
    regular_price: int
    sale_price: int
    stock: int | None
    manage_stock: bool
    stock_status: str
    #: media id of the variation's own picture (0 = none); a replaced gallery never touches it
    image_id: int = 0
    #: the variation's post status: «publish», or «private» when the seller unticked «Enabled»
    status: str = ""

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> ShopVariation:
        pairs: list[tuple[str, str]] = []
        for item in row.get("attributes") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("variation") or item.get("name") or "").strip()
            value = str(item.get("option") or "").strip()
            if name or value:
                pairs.append((name, value))
        raw_stock = row.get("stock_quantity")
        return cls(
            variation_id=int(row.get("id") or 0),
            attributes=tuple(pairs),
            price=_money(row, "price"),
            regular_price=_money(row, "regular_price"),
            sale_price=_money(row, "sale_price"),
            stock=None if raw_stock in (None, "") else int(raw_stock),
            manage_stock=bool(row.get("manage_stock")),
            stock_status=str(row.get("stock_status") or ""),
            image_id=_image_id(row.get("image")),
            status=str(row.get("status") or ""),
        )

    def value_of(self, wanted_name: str) -> str:
        for name, value in self.attributes:
            if name == wanted_name:
                return value
        return ""

    def named(self, *needles: str) -> str:
        """The value of the first attribute whose name contains one of ``needles``."""
        lowered = [needle.casefold() for needle in needles]
        for name, value in self.attributes:
            if any(needle in name.casefold() for needle in lowered):
                return value
        return ""

    @property
    def model(self) -> str:
        for name, value in self.attributes:
            if is_model_attribute(name):
                return value
        return ""

    @property
    def color(self) -> str:
        for name, value in self.attributes:
            if is_color_attribute(name):
                return value
        return ""

    def label(self) -> str:
        """The colour/model the shop has, in the order the seller reads them."""
        return " · ".join(value for _name, value in self.attributes if value) or f"#{self.variation_id}"

    def stock_text(self) -> str:
        if self.stock_status == "outofstock":
            return "ناموجود"
        if self.stock is None:
            return "بدون شمارش"
        return f"{self.stock:,}"


@dataclass
class ShopProduct:
    """The product plus its variations, as read from the store in one go."""

    product_id: int
    title: str
    sku: str
    status: str
    type: str
    regular_price: int
    sale_price: int
    stock: int | None
    manage_stock: bool
    stock_status: str
    variations: list[ShopVariation] = field(default_factory=list)
    attribute_names: dict[str, str] = field(default_factory=dict)
    #: the product's ``attributes`` exactly as the shop sent them (name, options, variation,
    #: visible, position, id…). An update has to send the *whole* list back — the shop replaces
    #: it rather than merging — so what it does not change must travel unchanged.
    attributes: list[dict[str, Any]] = field(default_factory=list)
    #: media ids of the gallery, featured image first
    image_ids: list[int] = field(default_factory=list)
    #: non-empty when a step of the read could not be done; the caller must say it, not
    #: assume «there are no variations».
    notes: list[str] = field(default_factory=list)
    #: False when the variation list is missing or cut short. A diff against half a list would
    #: «create» what exists and «delete» what it never saw, so a writer must refuse to plan on it.
    variations_complete: bool = True

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> ShopProduct:
        raw_stock = row.get("stock_quantity")
        names: dict[str, str] = {}
        attributes: list[dict[str, Any]] = []
        for item in row.get("attributes") or []:
            if isinstance(item, dict):
                attributes.append({**item, "options": [str(v) for v in (item.get("options") or [])]})
                name = str(item.get("name") or "").strip()
                if name:
                    names[name] = " ".join(str(value) for value in (item.get("options") or [])[:12])
        image_ids = [_image_id(image) for image in (row.get("images") or []) if isinstance(image, dict)]
        return cls(
            product_id=int(row.get("id") or 0),
            title=str(row.get("name") or "").strip(),
            sku=str(row.get("sku") or "").strip(),
            status=str(row.get("status") or ""),
            type=str(row.get("type") or "simple"),
            regular_price=_money(row, "regular_price"),
            sale_price=_money(row, "sale_price"),
            stock=None if raw_stock in (None, "") else int(raw_stock),
            manage_stock=bool(row.get("manage_stock")),
            stock_status=str(row.get("stock_status") or ""),
            attribute_names=names,
            attributes=attributes,
            image_ids=[value for value in image_ids if value],
        )

    @property
    def is_variable(self) -> bool:
        return self.type == "variable" or bool(self.variations)

    def model_values(self) -> list[str]:
        return sorted({v.model for v in self.variations if v.model})

    def color_values(self) -> list[str]:
        return sorted({v.color for v in self.variations if v.color})

    def candidate_text(self) -> str:
        return f"{self.title} {self.sku}".strip()


def matches_model(typed: str, value: str) -> bool:
    """«17promax» is the same model as «iPhone 17 Pro Max» — by signature, not by string."""
    left, right = model_signature(typed), model_signature(value)
    if not left or not right:
        return False
    if left == right:
        return True
    # A seller who writes «13pm» for «iPhone 13 Pro Max» is understood by a person; here the
    # typed signature has to be a whole prefix-ish run of the shop one, never a 2-letter fog.
    return len(left) >= 4 and (right.startswith(left) or left in right.split())


def matches_color(typed: str, value: str) -> bool:
    left, right = color_key(typed), color_key(value)
    if not left or not right:
        return False
    return left == right or (len(left) >= 3 and (left in right or right in left))


async def find(
    query: str,
    *,
    limit: int = 8,
    audit: Audit | None = None,
    transport: Any | None = None,
    dry_run: bool = False,
) -> list[Candidate]:
    """Candidates for ``query``: an exact SKU lookup when it looks like a SKU, then a title search."""
    wanted = " ".join(str(query or "").split())[:80]
    trace = audit or Audit()
    if not wanted:
        return []
    async with WooClient(audit=trace, dry_run=dry_run, transport=transport) as client:
        base = products_base()
        if _SKU_LIKE_RE.match(wanted):
            response = await client.get(f"{base}/sku/{quote(wanted)}")
            if response.is_success:
                row = _body(response)
                if isinstance(row, dict) and row.get("id"):
                    trace.log(f"[match] SKU دقیق «{wanted}» → محصول {row['id']}")
                    return [Candidate.from_row(row)]
            trace.log(
                f"[match] SKU «{wanted}» پیدا نشد (HTTP {response.status_code}: "
                f"{error_message(response)[:80]}); با جستجوی عنوان ادامه می‌دهیم."
            )
        params: dict[str, Any] = {
            "search": wanted, "per_page": max(1, min(int(limit), 20)),
            "orderby": "date", "order": "desc", "status": "any",
        }
        response = await client.get(base, params=params)
        if response.status_code == 400 and "status" in params:
            # Some hosts refuse `status=any` on the collection. Drafts are exactly what a
            # restock is for, so dropping the filter is the failure mode to report, not hide.
            params.pop("status")
            trace.log("[match] هاست `status=any` را نپذیرفت؛ بدون آن جستجو می‌شود "
                      "(پیش‌نویس‌ها ممکن است در نتیجه نباشند).")
            response = await client.get(base, params=params)
        check(response)
        rows = _body(response)
        if not isinstance(rows, list):
            raise WooCommerceAPIError(response.status_code, "پاسخ ووکامرس فهرست نبود")
        return [Candidate.from_row(row) for row in rows if isinstance(row, dict) and row.get("id")]


#: One page of variations. WooCommerce caps ``per_page`` at 100, and a phone-case product is
#: easily 30 models × 4 colours — reading only the first page would make a diff think 20
#: variations were missing and create them a second time.
VARIATIONS_PAGE = 100
MAX_VARIATION_PAGES = 10


async def read(
    product_id: int,
    *,
    include_variations: bool = True,
    audit: Audit | None = None,
    transport: Any | None = None,
    dry_run: bool = False,
) -> ShopProduct:
    """The product and its variations, with every failed step recorded in ``notes``.

    A simple product has no variations, so asking for them is a request spent on nothing; only a
    ``variable`` product is followed up. The variation list is read page by page — and when any
    page fails, ``variations_complete`` says so instead of handing back a plausible half.
    """
    trace = audit or Audit()
    async with WooClient(audit=trace, dry_run=dry_run, transport=transport) as client:
        base = products_base()
        response = await client.get(f"{base}/{int(product_id)}")
        check(response)
        product = ShopProduct.from_row(_body(response))
        if not include_variations or product.type != "variable":
            # Variations that were deliberately not read are not «none»: a writer must not plan
            # on them, so a variable product read without them says it is incomplete.
            product.variations_complete = product.type != "variable"
            return product
        rows, problem = await read_variation_rows(client, base, int(product_id), trace)
        product.variations = [ShopVariation.from_row(row) for row in rows]
        if problem:
            product.notes.append(problem)
            product.variations_complete = False
            trace.log(f"[match] {problem}")
            return product
        trace.log(f"[match] محصول {product.product_id}: {len(product.variations)} واریژن خوانده شد")
        return product


async def read_variation_rows(client: WooClient, base: str, product_id: int,
                               trace: Audit) -> tuple[list[dict[str, Any]], str]:
    """Every variation row of a product, or whatever was read plus the reason it is not all."""
    url = f"{base}/{product_id}/variations"
    params: dict[str, Any] = {"per_page": VARIATIONS_PAGE, "page": 1, "status": "any"}
    rows: list[dict[str, Any]] = []
    for page in range(1, MAX_VARIATION_PAGES + 1):
        params["page"] = page
        response = await client.get(url, params=params)
        if response.status_code == 400 and "status" in params:
            # Some hosts refuse `status=any` here as they do on the product list. Disabled
            # variations are still variations, so say it in the trace instead of hiding it.
            params.pop("status")
            trace.log("[match] هاست `status=any` را برای واریژن‌ها نپذیرفت؛ بدون آن خوانده می‌شود.")
            response = await client.get(url, params=params)
        if not response.is_success:
            return rows, (f"واریژن‌ها خوانده نشدند (HTTP {response.status_code}: "
                          f"{error_message(response)[:80]})")
        try:
            page_rows = _body(response)
        except WooCommerceAPIError as exc:
            return rows, f"واریژن‌ها خوانده نشدند ({exc})"
        if not isinstance(page_rows, list):
            return rows, "پاسخ واریژن‌ها فهرست نبود"
        rows.extend(row for row in page_rows if isinstance(row, dict))
        if len(page_rows) < VARIATIONS_PAGE:
            return rows, ""
    return rows, f"واریژن‌ها بیشتر از {VARIATIONS_PAGE * MAX_VARIATION_PAGES} است و کامل خوانده نشد"


__all__ = [
    "STATUS_FA",
    "Candidate",
    "ShopProduct",
    "ShopVariation",
    "WooCommerceAPIError",
    "find",
    "matches_color",
    "matches_model",
    "read",
    "read_variation_rows",
    "status_text",
]
