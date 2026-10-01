"""«🔄 اپدیت محصول»: what the seller's draft changes on the product the shop already has.

The seller sends photos, a caption and a few lines exactly as they would for a new product; the
builder reads them into a draft. This module compares **that draft with the shop's product** and
answers one question: *what has to be written so the shop matches what was said — and nothing
else*. Five rules carry the whole design:

* **silence is not an instruction.** A field the draft says nothing about (no price, no stock, no
  models, no photos) is never touched. A price that is not given stays the old price;
* **equal is not a change.** A value the draft repeats and the shop already has is left out of
  every request — the card says it is untouched and the shop is not asked to rewrite it;
* **a list the seller gives is the whole list.** New models replace the old ones: models the shop
  has and the list does not are deleted, models the list has and the shop does not are created,
  models in both stay (same variation, only the fields that differ are written). Colours and
  other attributes follow the same rule. An axis that was not mentioned keeps its options;
* **new photos replace the gallery.** Nothing is deleted from the media library — only the
  product stops showing the old pictures;
* **the baseline is the shop, not our memory.** Everything is computed from what
  :func:`bot.services.product_match.read` returned, so «0 → 20» is a fact about the shop.

The result is a pure value (:class:`UpdatePlan`): no network, no Telegram. The card renders it
(:meth:`UpdatePlan.rows`), the confirm step re-computes it against a fresh read and compares
:meth:`UpdatePlan.signature`, and :mod:`bot.services.update_apply` sends exactly that.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from bot.services import pricing
from bot.services.color_matrix import (
    build_combinations,
    color_key,
    is_color_attribute,
    is_model_attribute,
    model_signature,
)
from bot.services.plan import clean_values
from bot.services.product_match import ShopProduct, ShopVariation, status_text

MODEL, COLOR, OTHER = "model", "color", "other"

#: More deletions than this (or more than half of the variations) earns a warning: it is the
#: shape of «I sent two models and the product lost forty».
BIG_DELETE = 5

#: How many price/stock groups one card row lists before it says «و N گروه دیگر».
MAX_GROUPS = 4
#: Names listed in one «➕ / 🗑» line.
MAX_NAMES = 8


# — small helpers —

def _norm(text: object) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(text or ""))).strip()


def _kind(name: str) -> str:
    if is_model_attribute(name):
        return MODEL
    if is_color_attribute(name):
        return COLOR
    return OTHER


def _key(kind: str, value: object) -> str:
    """Two spellings of one option compare equal here — «iPhone 15 ProMax» and «۱۵ پرو مکس»."""
    text = _norm(value)
    if kind == MODEL:
        return model_signature(text) or text.casefold()
    if kind == COLOR:
        return color_key(text) or text.casefold()
    return text.casefold()


def _toman(value: int | None) -> str:
    return f"{value:,}" if value else "—"


def _stock_text(value: int | None) -> str:
    return "بدون شمارش" if value is None else f"{value:,}"


def _cut(text: str, limit: int = 120) -> str:
    """A title on a card is a label, not a document: a Telegram message has 4096 characters in all."""
    text = _norm(text)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _names(values: Sequence[str]) -> str:
    shown = [html.escape(value) for value in values[:MAX_NAMES]]
    if len(values) > MAX_NAMES:
        shown.append(f"… و {len(values) - MAX_NAMES} مورد دیگر")
    return " · ".join(shown)


# — what the shop has —

@dataclass(frozen=True)
class Axis:
    """One attribute the product's variations are built from."""

    name: str
    kind: str
    options: tuple[str, ...]

    def has(self, value: str) -> bool:
        wanted = _key(self.kind, value)
        return any(_key(self.kind, option) == wanted for option in self.options)

    def canonical(self, value: str) -> str:
        """The shop's own spelling when it has this option, the typed one when it does not."""
        wanted = _key(self.kind, value)
        for option in self.options:
            if _key(self.kind, option) == wanted:
                return option
        return _norm(value)


def shop_axes(product: ShopProduct) -> list[Axis]:
    """The variation axes of ``product``, in the order the shop lists them."""
    axes: list[Axis] = []
    for item in product.attributes:
        if not item.get("variation"):
            continue
        name = _norm(item.get("name"))
        options = tuple(str(option).strip() for option in (item.get("options") or []) if str(option).strip())
        if name:
            axes.append(Axis(name, _kind(name), options))
    if axes or not product.variations:
        return axes
    # A host that sends no `attributes` still sends variations; their own pairs are the axes.
    seen: dict[str, list[str]] = {}
    for variation in product.variations:
        for name, value in variation.attributes:
            if name and value and value not in seen.setdefault(name, []):
                seen[name].append(value)
    return [Axis(_norm(name), _kind(name), tuple(values)) for name, values in seen.items()]


def _value_for(variation: ShopVariation, axis: Axis) -> str:
    wanted = _norm(axis.name).casefold()
    for name, value in variation.attributes:
        if _norm(name).casefold() == wanted:
            return value
    if axis.kind == MODEL:
        return variation.model
    if axis.kind == COLOR:
        return variation.color
    return ""


def _variation_key(variation: ShopVariation, axes: Sequence[Axis]) -> tuple[str, ...]:
    return tuple(_key(axis.kind, _value_for(variation, axis)) for axis in axes)


def _combo_key(combo: Mapping[str, str], axes: Sequence[Axis]) -> tuple[str, ...]:
    return tuple(_key(axis.kind, combo.get(axis.name, "")) for axis in axes)


# — what the seller said —

@dataclass(frozen=True)
class Draft:
    """The fields of ``ProductData.to_dict()`` an update reads — with «not said» spelled out."""

    title: str = ""
    sku_prefix: str = ""
    price: int = 0
    prices: dict[str, int] = field(default_factory=dict)
    model_prices: dict[str, int] = field(default_factory=dict)
    sale: int = 0
    wholesale: bool = False
    stock: int | None = None
    stock_status: str = ""
    models: list[str] = field(default_factory=list)
    attributes: dict[str, list[str]] = field(default_factory=dict)
    restrictions: dict[str, list[str]] = field(default_factory=dict)
    stock_matrix: bool = False

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> Draft:
        raw_stock = data.get("stock")
        attributes: dict[str, list[str]] = {}
        for name, values in (data.get("attributes") or {}).items():
            options = clean_values(values if isinstance(values, (list, tuple, set)) else [values])
            if options and not is_model_attribute(str(name)):
                attributes[_norm(name)] = options
        restrictions: dict[str, list[str]] = {}
        for model, colors in (data.get("model_colors") or {}).items():
            if isinstance(colors, (list, tuple)) and clean_values(colors):
                restrictions[str(model)] = clean_values(colors)
        return cls(
            title=_norm(data.get("title")),
            sku_prefix=str(data.get("sku_prefix") or "").strip(),
            price=int(data.get("price") or 0),
            prices={str(k): int(v) for k, v in (data.get("prices") or {}).items() if v},
            model_prices={str(k): int(v) for k, v in (data.get("model_prices") or {}).items()
                          if int(v or 0) > 0},
            sale=int(data.get("sale_price") or 0),
            wholesale=bool(data.get("wholesale_price") or data.get("wholesale_model_prices")),
            stock=None if raw_stock in (None, "") else int(raw_stock),
            stock_status=str(data.get("stock_status") or "").strip(),
            models=clean_values(data.get("models") or []),
            attributes=attributes,
            restrictions=restrictions,
            stock_matrix=bool(data.get("stock_matrix") or data.get("stock_matrix_errors")),
        )

    @property
    def common_price(self) -> int:
        """What a model with no price of its own gets — the same rule the publisher uses."""
        return self.price or next(iter(self.prices.values()), 0)

    @property
    def says_price(self) -> bool:
        return bool(self.price or self.prices or self.model_prices)

    @property
    def says_structure(self) -> bool:
        return bool(self.models or self.attributes)

    def price_for(self, model: str) -> int:
        """The regular price this draft gives ``model`` (0 = the draft does not say)."""
        if not self.says_price:
            return 0
        return pricing.price_for_model(model, self.common_price, self.prices, self.model_prices)

    def status_for(self, quantity: int | None) -> str:
        """The stock status that goes with a stated quantity (an explicit one wins)."""
        if self.stock_status:
            return self.stock_status
        if quantity is None:
            return ""
        return "instock" if quantity > 0 else "outofstock"


# — the plan —

@dataclass(frozen=True)
class VariationChange:
    """One variation that exists, what it holds now, and what it will hold."""

    variation_id: int
    label: str
    model: str
    price: tuple[int, int] | None = None
    sale: tuple[int, int] | None = None
    stock: tuple[int | None, int] | None = None
    status: tuple[str, str] | None = None

    def payload(self) -> dict[str, Any]:
        """The ``update`` row for ``variations/batch``: the id and only what moves.

        Prices travel as strings because WooCommerce answers them that way; a number here would
        come back as ``"700000"`` and the write would be reported as unconfirmed.
        """
        row: dict[str, Any] = {"id": self.variation_id}
        if self.price:
            row["regular_price"] = str(self.price[1])
        if self.sale:
            row["sale_price"] = str(self.sale[1])
        if self.stock:
            row["manage_stock"] = True
            row["stock_quantity"] = self.stock[1]
        if self.status:
            row["stock_status"] = self.status[1]
        return row


@dataclass(frozen=True)
class VariationCreate:
    """A variation the shop does not have yet — a new model, a new colour."""

    combo: tuple[tuple[str, str], ...]
    label: str
    model: str
    price: int
    sale: int = 0
    stock: int | None = None
    status: str = ""
    order: int = 0

    def payload(self, *, image_id: int = 0) -> dict[str, Any]:
        """The ``create`` row — the same shape :mod:`bot.services.woocommerce_direct` builds."""
        row: dict[str, Any] = {
            "regular_price": str(self.price),
            "status": "publish",
            "visible": True,
            "menu_order": self.order,
            "attributes": [{"name": name, "option": value} for name, value in self.combo],
        }
        if self.sale:
            row["sale_price"] = str(self.sale)
        if self.stock is not None:
            row["manage_stock"] = True
            row["stock_quantity"] = self.stock
        if self.stock is not None or self.status:
            row["stock_status"] = self.status or "instock"
        if image_id:
            row["image"] = {"id": image_id}
        return row


@dataclass(frozen=True)
class AxisChange:
    """How the list behind one attribute («مدل»، «رنگ») changes."""

    name: str
    kind: str
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    kept: int = 0
    new_axis: bool = False


@dataclass
class UpdatePlan:
    """The whole answer to «چی عوض می‌شود» — including what will *not* be touched."""

    product_id: int
    title_now: str = ""
    sku: str = ""
    is_variable: bool = False
    #: the new title, or ``""`` when the title stays
    new_title: str = ""
    #: ``PUT products/<id>`` fields for a simple product (price, sale, stock); strings for money
    product_fields: dict[str, Any] = field(default_factory=dict)
    product_before: dict[str, Any] = field(default_factory=dict)
    #: the full ``attributes`` array to send, or ``None`` when no axis changes
    attributes: list[dict[str, Any]] | None = None
    axis_changes: list[AxisChange] = field(default_factory=list)
    images_old: int = 0
    images_new: int = 0
    updates: list[VariationChange] = field(default_factory=list)
    creates: list[VariationCreate] = field(default_factory=list)
    deletes: list[ShopVariation] = field(default_factory=list)
    #: variations the draft matches that already hold every value it states
    kept: int = 0
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    # — state —
    @property
    def variations_after(self) -> int:
        """How many variations the product has once this is applied."""
        return len(self.updates) + len(self.creates) + self.kept

    @property
    def empty(self) -> bool:
        """Nothing at all would be written."""
        return not (self.new_title or self.product_fields or self.attributes is not None
                    or self.images_new or self.updates or self.creates or self.deletes)

    @property
    def blocking(self) -> bool:
        return bool(self.errors)

    @property
    def can_apply(self) -> bool:
        return not self.empty and not self.blocking

    @property
    def changes_variations(self) -> bool:
        return bool(self.updates or self.creates or self.deletes)

    # — what is sent —
    def product_body(self, media_ids: Sequence[int] = ()) -> dict[str, Any]:
        """The body for ``PUT products/<id>``: title, gallery, attribute lists, simple-product fields."""
        body: dict[str, Any] = {}
        if self.new_title:
            body["name"] = self.new_title
        if media_ids:
            body["images"] = [{"id": media_id} for media_id in media_ids]
        if self.attributes is not None:
            body["attributes"] = self.attributes
        body.update(self.product_fields)
        return body

    def signature(self) -> str:
        """A fingerprint of everything this plan would send.

        The confirm step builds the plan a second time against a fresh read of the shop; if the
        two signatures differ the shop changed under the seller's feet and the card is shown again
        instead of writing something nobody approved.

        It covers what is *sent*, not the old values the card showed beside it: a live shop's stock
        moves with every order, and asking the seller to approve again because «5» became «4»
        under a «→ 20» they already approved would make the banner noise.
        """
        body = [
            self.new_title, self.product_fields, self.attributes, self.images_new,
            [row.payload() for row in self.updates],
            [row.payload() for row in self.creates],
            sorted(variation.variation_id for variation in self.deletes),
        ]
        return hashlib.sha1(json.dumps(body, ensure_ascii=False, sort_keys=True,
                                       default=str).encode()).hexdigest()

    # — what is shown —
    def rows(self, stage: str = "full") -> list[str]:
        """The card body (HTML): only what changes, then what stays.

        ``stage`` keeps the card's fill-in order — title, price and stock first (known from the
        seller's own words), the model lists once the models are read, the full picture last.
        Rows are only ever added as the stages advance, so nothing jumps while the AI works.
        """
        lines: list[str] = []
        lines += self._title_rows()
        lines += self._price_rows()
        lines += self._sale_rows()
        lines += self._stock_rows()
        lines += self._image_rows()
        if stage in ("models", "full"):
            lines += self._structure_rows()
        if stage == "full":
            if self.empty and not self.errors:
                lines.append("ℹ️ هیچ تفاوتی با محصول فعلی پیدا نشد؛ قیمت، موجودی، مدل یا عکس تازه بفرست.")
            lines += [f"ℹ️ {html.escape(note)}" for note in self.notes]
            lines += [f"⚠️ {html.escape(warning)}" for warning in self.warnings]
            lines += [f"⛔ {html.escape(error)}" for error in self.errors]
            lines += self._untouched_rows()
        return lines

    def change_lines(self) -> list[str]:
        """The same changes as short plain lines — the result card and the log group read these."""
        lines: list[str] = []
        if self.new_title:
            lines.append(f"✏️ عنوان: {_cut(self.title_now)} ← {_cut(self.new_title)}")
        for emoji, label, pairs in (
            ("💰", "قیمت", self._price_pairs()),
            ("🏷", "قیمت ویژه", self._sale_pairs()),
            ("📦", "موجودی", self._stock_pairs()),
            ("📦", "وضعیت", self._status_pairs()),
        ):
            for text in _group_texts(pairs):
                lines.append(f"{emoji} {label}: {text}")
        if self.images_new:
            lines.append(f"🖼 تصاویر: {self.images_new} تصویر جایگزین گالری شد")
        for change in self.axis_changes:
            bits = []
            if change.added:
                bits.append(f"➕ {len(change.added)}")
            if change.removed:
                bits.append(f"🗑 {len(change.removed)}")
            lines.append(f"🎨 {change.name}: " + " · ".join(bits))
        if self.creates or self.deletes:
            lines.append(f"🧩 واریژن: ➕ {len(self.creates)} · 🗑 {len(self.deletes)}")
        return lines

    def summary(self) -> str:
        """One line for the log: what kind of changes, how many."""
        parts = []
        if self.new_title:
            parts.append("عنوان")
        for label, pairs in (("قیمت", self._price_pairs()), ("ویژه", self._sale_pairs()),
                             ("موجودی", self._stock_pairs() or self._status_pairs())):
            if pairs:
                parts.append(f"{label}({len(pairs)})")
        if self.images_new:
            parts.append(f"تصویر({self.images_new})")
        if self.creates or self.deletes:
            parts.append(f"واریژن(+{len(self.creates)} −{len(self.deletes)})")
        return " · ".join(parts) or "بدون تغییر"

    # — pairs: (before, after) text for every variation / the simple product that moves —
    def _price_pairs(self) -> list[tuple[str, str]]:
        pairs = [(_toman(row.price[0]), _toman(row.price[1])) for row in self.updates if row.price]
        if "regular_price" in self.product_fields:
            pairs.append((_toman(self.product_before.get("regular_price")),
                          _toman(int(self.product_fields["regular_price"]))))
        return pairs

    def _sale_pairs(self) -> list[tuple[str, str]]:
        pairs = [(_toman(row.sale[0]), _toman(row.sale[1])) for row in self.updates if row.sale]
        if "sale_price" in self.product_fields:
            pairs.append((_toman(self.product_before.get("sale_price")),
                          _toman(int(self.product_fields["sale_price"]))))
        return pairs

    def _stock_pairs(self) -> list[tuple[str, str]]:
        pairs = [(_stock_text(row.stock[0]), _stock_text(row.stock[1])) for row in self.updates if row.stock]
        if "stock_quantity" in self.product_fields:
            pairs.append((_stock_text(self.product_before.get("stock")),
                          _stock_text(int(self.product_fields["stock_quantity"]))))
        return pairs

    def _status_pairs(self) -> list[tuple[str, str]]:
        pairs = [(status_text(row.status[0]), status_text(row.status[1])) for row in self.updates
                 if row.status and not row.stock]
        if "stock_status" in self.product_fields and "stock_quantity" not in self.product_fields:
            pairs.append((status_text(str(self.product_before.get("stock_status") or "")),
                          status_text(str(self.product_fields["stock_status"]))))
        return pairs

    # — rows —
    def _title_rows(self) -> list[str]:
        if not self.new_title:
            return []
        return [f"✏️ <b>عنوان:</b> {html.escape(_cut(self.title_now))} ← {html.escape(_cut(self.new_title))}"]

    def _price_rows(self) -> list[str]:
        return _pair_rows("💰", "قیمت", self._price_pairs(), unit="تومان")

    def _sale_rows(self) -> list[str]:
        return _pair_rows("🏷", "قیمت ویژه", self._sale_pairs(), unit="تومان")

    def _stock_rows(self) -> list[str]:
        return (_pair_rows("📦", "موجودی", self._stock_pairs(), unit="عدد")
                + _pair_rows("📦", "وضعیت", self._status_pairs()))

    def _image_rows(self) -> list[str]:
        if not self.images_new:
            return []
        if self.images_old:
            return [f"🖼 <b>تصاویر:</b> {self.images_new} تصویر جدید جای {self.images_old} تصویر فعلی می‌نشیند"]
        return [f"🖼 <b>تصاویر:</b> {self.images_new} تصویر به محصول اضافه می‌شود"]

    def _structure_rows(self) -> list[str]:
        lines: list[str] = []
        for change in self.axis_changes:
            title = {MODEL: "مدل‌ها", COLOR: "رنگ‌ها"}.get(change.kind, html.escape(change.name))
            if change.new_axis:
                lines.append(f"🎨 <b>{title}:</b> محور تازه — همهٔ واریژن‌های فعلی با ترکیب‌های تازه جایگزین می‌شوند")
            else:
                lines.append(f"🎨 <b>{title}:</b>")
            if change.added:
                lines.append(f"   ➕ {_names(change.added)}")
            if change.removed:
                lines.append(f"   🗑 {_names(change.removed)}")
            if change.kept:
                lines.append(f"   ✓ {change.kept} مورد بدون تغییر")
        if self.changes_variations and (self.creates or self.deletes):
            bits = [f"➕ {len(self.creates)}", f"🗑 {len(self.deletes)}"]
            if self.updates:
                bits.append(f"✏️ {len(self.updates)}")
            if self.kept:
                bits.append(f"✓ {self.kept}")
            lines.append("🧩 <b>واریژن‌ها:</b> " + " · ".join(bits))
        return lines

    def _untouched_rows(self) -> list[str]:
        """The promise made visible: what the seller did not change stays as it is."""
        stays = ["SKU", "دسته‌بندی", "توضیحات"]
        if not self.new_title:
            stays.insert(0, "عنوان")
        if not self._price_pairs():
            stays.append("قیمت")
        if not self._sale_pairs():
            stays.append("قیمت ویژه")
        if not (self._stock_pairs() or self._status_pairs()):
            stays.append("موجودی")
        if not self.images_new:
            stays.append("تصاویر")
        if not self.axis_changes:
            stays.append("مدل‌ها و رنگ‌ها")
        return ["✓ <b>دست‌نخورده:</b> " + "، ".join(stays)]


def _group_text(before: str, after: str, count: int, *, many: bool, unit: str = "") -> str:
    """«698,000 ← 720,000 تومان (8 واریژن)» — the count only when more than one thing moves."""
    text = f"{before} ← {after}" + (f" {unit}" if unit else "")
    return text + (f" ({count} واریژن)" if many else "")


def _group_texts(pairs: Sequence[tuple[str, str]], *, unit: str = "") -> list[str]:
    """One line per distinct (before, after), the biggest group first, capped."""
    groups = Counter(pairs).most_common()
    many = len(pairs) > 1
    texts = [_group_text(before, after, count, many=many, unit=unit)
             for (before, after), count in groups[:MAX_GROUPS]]
    if len(groups) > MAX_GROUPS:
        texts.append(f"… و {len(groups) - MAX_GROUPS} گروه دیگر")
    return texts


def _pair_rows(emoji: str, label: str, pairs: Sequence[tuple[str, str]], *, unit: str = "") -> list[str]:
    """One line when everything moves the same way, an indented list when it does not."""
    if not pairs:
        return []
    texts = [html.escape(text) for text in _group_texts(pairs, unit=unit)]
    if len(texts) == 1:
        return [f"{emoji} <b>{label}:</b> {texts[0]}"]
    return [f"{emoji} <b>{label}:</b>", *(f"   {text}" for text in texts)]


# — building the plan —

def _dedupe(kind: str, values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        key = _key(kind, value)
        if key and key not in seen:
            seen.add(key)
            out.append(value)
    return out


def build(product: ShopProduct, data: Mapping[str, Any], *, image_count: int = 0) -> UpdatePlan:
    """Compare the seller's draft (``ProductData.to_dict()``) with ``product`` as the shop has it."""
    draft = Draft.from_mapping(data)
    plan = UpdatePlan(
        product_id=product.product_id,
        title_now=_norm(product.title),
        sku=product.sku,
        is_variable=product.is_variable,
        images_old=len(product.image_ids),
        images_new=max(0, int(image_count)),
    )
    if not _readable(plan, product):
        return plan
    if draft.title and draft.title != plan.title_now:
        plan.new_title = draft.title
    if draft.sku_prefix and product.sku and not product.sku.upper().startswith(draft.sku_prefix.upper()):
        # The SKU is the product's identity and is never rewritten here, but a prefix that
        # disagrees is the clearest sign the wrong product was picked.
        plan.warnings.append(
            f"پیشوند SKU نوشته‌شده ({draft.sku_prefix}) با SKU محصول انتخاب‌شده ({product.sku}) "
            "فرق دارد؛ محصول درست را انتخاب کرده‌ای؟ (SKU عوض نمی‌شود)")
    if plan.is_variable:
        _plan_variable(plan, product, draft)
    else:
        _plan_simple(plan, product, draft)
    if draft.wholesale:
        plan.notes.append("قیمت همکاری فقط ثبت می‌شود؛ روی سایت اعمال نمی‌شود.")
    return plan


def _readable(plan: UpdatePlan, product: ShopProduct) -> bool:
    """Refuse to plan on a product the shop did not describe completely."""
    if product.type not in ("simple", "variable"):
        plan.errors.append(f"نوع محصول «{product.type}» را نمی‌توانم اپدیت کنم (فقط ساده و متغیر).")
        return False
    if product.type == "variable" and not product.variations_complete:
        reason = f" ({product.notes[-1]})" if product.notes else ""
        plan.errors.append(
            "واریژن‌های محصول کامل خوانده نشد" + reason + "؛ بدون آن نمی‌توانم تغییر را با محصول فعلی "
            "مقایسه کنم و چیزی نمی‌نویسم.")
        return False
    return True


# — a simple product: the line goes on the product itself —

def _plan_simple(plan: UpdatePlan, product: ShopProduct, draft: Draft) -> None:
    if len(draft.models) >= 2 or draft.attributes:
        plan.errors.append(
            "این محصول ساده است (مدل و رنگ ندارد)؛ اضافه‌کردن مدل یا رنگ به آن را پشتیبانی نمی‌کنم. "
            "مدل و رنگ را از متن بردار تا بقیه (قیمت، موجودی، عکس) اعمال شود.")
    elif draft.models:
        plan.notes.append("فقط یک مدل نوشته شده؛ یک مدل تنها محور مدل نمی‌سازد و نادیده گرفته شد.")
    plan.product_before = {
        "regular_price": product.regular_price, "sale_price": product.sale_price,
        "stock": product.stock, "manage_stock": product.manage_stock,
        "stock_status": product.stock_status,
    }
    fields: dict[str, Any] = {}
    common = draft.common_price
    if common and common != product.regular_price:
        fields["regular_price"] = str(common)
    effective = common or product.regular_price
    if draft.sale and draft.sale != product.sale_price:
        if effective and draft.sale >= effective:
            plan.errors.append(_sale_error(draft.sale, effective, ""))
        else:
            fields["sale_price"] = str(draft.sale)
    if draft.stock is not None:
        status = draft.status_for(draft.stock)
        if not product.manage_stock or product.stock != draft.stock or product.stock_status != status:
            fields.update({"manage_stock": True, "stock_quantity": draft.stock, "stock_status": status})
    elif draft.stock_status and draft.stock_status != product.stock_status:
        fields["stock_status"] = draft.stock_status
    plan.product_fields = fields


def _sale_error(sale: int, price: int, where: str) -> str:
    at = f" ({where})" if where else ""
    return f"قیمت ویژهٔ {sale:,} از قیمت {price:,} کمتر نیست{at}؛ فروشگاه آن را تخفیف نمی‌شمارد."


# — a variable product: models, colours, per-variation price and stock —

def _plan_variable(plan: UpdatePlan, product: ShopProduct, draft: Draft) -> None:
    axes = shop_axes(product)
    new_axes, stated = _target_axes(plan, product, axes, draft)
    if plan.errors:
        return
    # A draft that repeats the shop's own lists changes no structure — and must not rebuild a
    # deliberately restricted grid. But a shop that disagrees *with itself* (a variation on a
    # model its list no longer has, a listed model with no variation) is a structure change that
    # stopped half-way, and asking again has to finish it.
    structural = (bool(plan.axis_changes) or bool(draft.restrictions)
                  or (stated and not _consistent(product, axes)))
    price_gaps: list[str] = []
    sale_clash: list[tuple[str, int]] = []
    inherited_from: set[int] = set()

    def update_for(variation: ShopVariation) -> VariationChange | None:
        target_price = draft.price_for(variation.model)
        price = ((variation.regular_price, target_price)
                 if target_price and target_price != variation.regular_price else None)
        sale = (variation.sale_price, draft.sale) if draft.sale and draft.sale != variation.sale_price else None
        stock: tuple[int | None, int] | None = None
        status: tuple[str, str] | None = None
        if draft.stock is not None:
            if not variation.manage_stock or variation.stock != draft.stock:
                stock = (variation.stock, draft.stock)
            wanted = draft.status_for(draft.stock)
            if wanted != variation.stock_status:
                status = (variation.stock_status, wanted)
        elif draft.stock_status and draft.stock_status != variation.stock_status:
            status = (variation.stock_status, draft.stock_status)
        effective = target_price or variation.regular_price
        if sale is not None and effective and draft.sale >= effective:
            sale_clash.append((variation.label(), effective))
        if not (price or sale or stock or status):
            return None
        return VariationChange(variation.variation_id, variation.label(), variation.model,
                               price=price, sale=sale, stock=stock, status=status)

    if not structural:
        for variation in product.variations:
            change = update_for(variation)
            if change is None:
                plan.kept += 1
            else:
                plan.updates.append(change)
    else:
        restrictions = draft.restrictions or _inherited_restrictions(product, axes, new_axes, draft)
        combos = build_combinations([(axis.name, list(axis.options)) for axis in new_axes], restrictions)
        by_key: dict[tuple[str, ...], list[ShopVariation]] = {}
        for variation in product.variations:
            by_key.setdefault(_variation_key(variation, new_axes), []).append(variation)
        fresh: list[tuple[int, dict[str, str]]] = []
        for order, combo in enumerate(combos):
            bucket = by_key.get(_combo_key(combo, new_axes))
            if bucket:
                change = update_for(bucket.pop(0))
                if change is None:
                    plan.kept += 1
                else:
                    plan.updates.append(change)
            else:
                fresh.append((order, combo))
        left = {id(variation) for bucket in by_key.values() for variation in bucket}
        plan.deletes = [variation for variation in product.variations if id(variation) in left]
        model_axis = next((axis for axis in new_axes if axis.kind == MODEL), None)
        for order, combo in fresh:
            model = combo.get(model_axis.name, "") if model_axis else ""
            price = draft.price_for(model)
            if not price:
                price = _inherited_price(product.variations, model, product.regular_price)
                if price:
                    inherited_from.add(price)
            if draft.sale and price and draft.sale >= price:
                sale_clash.append((" · ".join(combo.values()), price))
            label = " · ".join(combo.values())
            if not price:
                price_gaps.append(label)
            quantity = draft.stock
            plan.creates.append(VariationCreate(
                combo=tuple(combo.items()), label=label, model=model, price=price, sale=draft.sale,
                stock=quantity, status=draft.status_for(quantity) if quantity is not None
                else draft.stock_status, order=order))
    plan.warnings += _variable_warnings(plan, product, draft, inherited_from)
    if price_gaps:
        sample = "، ".join(price_gaps[:3])
        plan.errors.append(
            f"برای {len(price_gaps)} واریژن تازه ({sample}) قیمتی پیدا نکردم؛ قیمت را هم بنویس.")
    if sale_clash:
        label, price = sale_clash[0]
        more = f" و {len(sale_clash) - 1} واریژن دیگر" if len(sale_clash) > 1 else ""
        plan.errors.append(_sale_error(draft.sale, price, label + more))


def _consistent(product: ShopProduct, axes: Sequence[Axis]) -> bool:
    """Does the product agree with itself: every variation sits on listed options, every option is used?"""
    if not product.variations:
        return not any(axis.options for axis in axes)
    for axis in axes:
        used = {_key(axis.kind, _value_for(variation, axis)) for variation in product.variations}
        if any(value and not axis.has(value) for value in
               (_value_for(variation, axis) for variation in product.variations)):
            return False
        if any(_key(axis.kind, option) not in used for option in axis.options):
            return False
    return True


def _variable_warnings(plan: UpdatePlan, product: ShopProduct, draft: Draft,
                       inherited_from: set[int]) -> list[str]:
    out: list[str] = []
    total = len(product.variations)
    if len(plan.deletes) >= BIG_DELETE or (total and len(plan.deletes) * 2 > total):
        out.append(f"{len(plan.deletes)} واریژن از {total} حذف می‌شود؛ اگر فهرست مدل‌ها ناقص است، "
                   "همین حالا کامل‌ترش را بفرست.")
    if plan.creates and draft.stock is None and not draft.stock_status \
            and any(variation.manage_stock for variation in product.variations):
        out.append("موجودی واریژن‌های تازه را ننوشتی؛ بدون شمارش (همیشه موجود) ساخته می‌شوند.")
    if plan.creates and not draft.says_price and inherited_from:
        out.append("قیمت واریژن‌های تازه را ننوشتی؛ از واریژن‌های فعلیِ همان گروه برداشته شد ("
                   + "، ".join(f"{price:,}" for price in sorted(inherited_from)) + " تومان).")
    return out


def _inherited_price(variations: Sequence[ShopVariation], model: str, fallback: int) -> int:
    """What a model like this one costs in the shop today, when the seller did not say.

    The most common regular price among the variations of the same price group (iPhone / Android),
    else among all of them, else the parent's own price — never a guess from outside the shop.
    """
    group = pricing.price_group(model)
    same = [row.regular_price for row in variations
            if row.regular_price and pricing.price_group(row.model) == group]
    pool = same or [row.regular_price for row in variations if row.regular_price]
    if pool:
        return Counter(pool).most_common(1)[0][0]
    return fallback


def _inherited_restrictions(product: ShopProduct, old_axes: Sequence[Axis], axes: Sequence[Axis],
                            draft: Draft) -> dict[str, list[str]]:
    """Keep «this model only comes in these colours» for models that stay, when only models changed.

    A new model list with no colour list says nothing about colours; rebuilding the full
    model × colour grid would create combinations the shop deliberately never had.
    """
    if draft.attributes:
        return {}
    model_axis = next((axis for axis in axes if axis.kind == MODEL), None)
    color_axis = next((axis for axis in axes if axis.kind == COLOR), None)
    old_model = next((axis for axis in old_axes if axis.kind == MODEL), None)
    old_color = next((axis for axis in old_axes if axis.kind == COLOR), None)
    if model_axis is None or color_axis is None or old_model is None or old_color is None:
        return {}
    seen: dict[str, set[str]] = {}
    for variation in product.variations:
        model = _key(MODEL, _value_for(variation, old_model))
        color = _key(COLOR, _value_for(variation, old_color))
        if model and color:
            seen.setdefault(model, set()).add(color)
    everything = {color for colors in seen.values() for color in colors}
    out: dict[str, list[str]] = {}
    for option in model_axis.options:
        colors = seen.get(_key(MODEL, option))
        if colors and colors != everything:
            out[option] = [value for value in color_axis.options if _key(COLOR, value) in colors]
    return out


def _target_axes(plan: UpdatePlan, product: ShopProduct, axes: Sequence[Axis],
                 draft: Draft) -> tuple[list[Axis], bool]:
    """The axes after the draft's lists replace the shop's; records what changed on ``plan``.

    The flag says whether any list of the draft *landed on an axis* — a lone model that makes no
    axis is read and ignored, and must not set the structure in motion.
    """
    result = list(axes)
    stated = False
    changed: dict[str, tuple[str, ...]] = {}
    added: list[Axis] = []
    changes: list[AxisChange] = []

    def find(kind: str, name: str) -> int | None:
        for index, axis in enumerate(result):
            if axis.kind == kind and (kind != OTHER or _norm(axis.name).casefold() == _norm(name).casefold()):
                return index
        return None

    def replace(index: int, wanted: Sequence[str]) -> None:
        nonlocal stated
        stated = True
        axis = result[index]
        options = _dedupe(axis.kind, [axis.canonical(value) for value in wanted])
        keep = {_key(axis.kind, option) for option in options}
        new = tuple(option for option in options if not axis.has(option))
        gone = tuple(option for option in axis.options if _key(axis.kind, option) not in keep)
        if not new and not gone:
            return
        result[index] = Axis(axis.name, axis.kind, tuple(options))
        changed[axis.name] = tuple(options)
        changes.append(AxisChange(axis.name, axis.kind, new, gone, len(axis.options) - len(gone)))

    def attach(axis: Axis, *, first: bool = False) -> None:
        nonlocal stated
        stated = True
        result.insert(0, axis) if first else result.append(axis)
        added.append(axis)
        changes.append(AxisChange(axis.name, axis.kind, added=axis.options, new_axis=True))

    if draft.models:
        index = find(MODEL, "")
        if index is not None:
            replace(index, draft.models)
        elif len(draft.models) >= 2:
            attach(Axis("مدل", MODEL, tuple(draft.models)), first=True)
        else:
            plan.notes.append("فقط یک مدل نوشته شده؛ یک مدل تنها محور مدل نمی‌سازد و نادیده گرفته شد.")
    for name, values in draft.attributes.items():
        kind = _kind(name)
        index = find(kind, name)
        if index is not None:
            replace(index, values)
        elif _plain_attribute(product, kind, name):
            plan.errors.append(
                f"ویژگی «{name}» در این محصول برای واریژن فعال نیست؛ از پیشخوان وردپرس فعالش کن یا "
                "از متن بردارش.")
        else:
            attach(Axis(name, kind, tuple(values)))
    plan.axis_changes = changes
    if changes:
        plan.attributes = _attributes_payload(product, result, changed, added)
    return result, stated


def _plain_attribute(product: ShopProduct, kind: str, name: str) -> bool:
    """The product has an attribute like this one, but it is not used for variations."""
    for item in product.attributes:
        if item.get("variation"):
            continue
        shop_name = _norm(item.get("name"))
        if kind in (MODEL, COLOR) and _kind(shop_name) == kind:
            return True
        if kind == OTHER and shop_name.casefold() == _norm(name).casefold():
            return True
    return False


def _attributes_payload(product: ShopProduct, axes: Sequence[Axis], changed: Mapping[str, tuple[str, ...]],
                        added: Sequence[Axis]) -> list[dict[str, Any]]:
    """The whole ``attributes`` array: the shop replaces it, so what stays must travel with it."""
    if not product.attributes:
        rows = [{"name": axis.name, "visible": True, "variation": True, "options": list(axis.options)}
                for axis in axes]
    else:
        rows = []
        for raw in product.attributes:
            item = dict(raw)
            name = _norm(raw.get("name"))
            if raw.get("variation") and name in changed:
                item["options"] = list(changed[name])
            rows.append(item)
        fresh = [{"name": axis.name, "visible": True, "variation": True, "options": list(axis.options)}
                 for axis in added]
        # The model axis leads, as the publisher builds it; anything else follows the old ones.
        lead = [row for row, axis in zip(fresh, added, strict=True) if axis.kind == MODEL]
        rest = [row for row, axis in zip(fresh, added, strict=True) if axis.kind != MODEL]
        rows = lead + rows + rest
    for position, item in enumerate(rows):
        item["position"] = position
    return rows


__all__ = [
    "Axis", "AxisChange", "Draft", "UpdatePlan", "VariationChange", "VariationCreate",
    "build", "shop_axes",
]
