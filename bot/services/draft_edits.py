"""Editing one field of a product draft — on purpose, and it sticks.

The flow only accepted free text («قیمت 698000») and re-ran the whole
extraction. That is wrong in two ways: the seller has to relearn how to talk to
the parser, and a manual fix could be overwritten by the next AI round or by
their next message. So this module does two things:

* **typed parsing per field** — a price is read as a price, colors are split as
  colors, a category is checked against the store's real taxonomy; a rejected
  value comes back with the reason instead of being stored wrongly;
* **locks** — what the owner typed by hand is recorded in ``user_edits`` and
  re-applied after every later extraction (:func:`apply_locks`), so the bot
  never quietly undoes a deliberate edit.

Everything here is Telegram-free on purpose: the buttons are a thin shell over
these functions, and the rules are testable without a bot.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from typing import Any

from bot.config import settings
from bot.services import brand_suggest, money, vocabulary
from bot.services import postmodel as ev
from bot.services.category_taxonomy import (
    FORBIDDEN,
    TAXONOMY,
    apply_sku_category_policy,
    is_printed_category,
    sku_uses_printed_category,
)
from bot.services.color_matrix import color_key
from bot.services.phone_parser import extract_phone_models, fold_variant_words

logger = logging.getLogger(__name__)

CLEAR_WORDS = {"-", "—", "خالی", "حذف", "none", "null"}

GroupPrice = dict[str, int]


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _is_clear(text: str) -> bool:
    return (text or "").strip().casefold() in CLEAR_WORDS


#: separators a person uses for a hand-typed list
_SPLIT_RE = re.compile(r"\s*(?:\||,|،|؛|;|/|\n| و )\s*")
#: categories and paths must keep their « و » («قاب و کاور گوشی و تبلت») — the
#: only sane separators there are the explicit list ones.
_PATH_SPLIT_RE = re.compile(r"\s*(?:\||,|،|؛|;|\n)\s*")


def _split_items(text: str, *, paths: bool = False) -> list[str]:
    """Split a hand-typed list on the separators people actually use."""
    pattern = _PATH_SPLIT_RE if paths else _SPLIT_RE
    parts = pattern.split((text or "").strip())
    return list(dict.fromkeys(p.strip(" -–—•*:") for p in parts if p.strip(" -–—•*:")))


# ---------------------------------------------------------------------------
# parsers — each returns the value, or raises ValueError with a Persian reason
# ---------------------------------------------------------------------------
def parse_title(text: str) -> str:
    value = _one_line(text)
    if _is_clear(value):
        raise ValueError("عنوان را نمی‌توان خالی گذاشت؛ محصول بی‌نام در فروشگاه پیدا نمی‌شود.")
    if len(value) < 3:
        raise ValueError("عنوان خیلی کوتاه است (حداقل ۳ حرف).")
    if len(value) > 200:
        raise ValueError("عنوان بیشتر از ۲۰۰ کاراکتر است؛ کوتاه‌اش کن تا در گوگل هم کامل دیده شود.")
    if re.search(r"\d{4,}", value) and not re.search(r"[A-Za-z]+\s*\d+", value):
        raise ValueError(
            "این خط بیشتر شبیه مبلغ/کد است تا عنوان. اگر قیمت است، فیلد «قیمت» را پر کن."
        )
    return value


def parse_price(text: str) -> int:
    raw = _one_line(text)
    if _is_clear(raw):
        return 0
    value = money.parse_line_amount(raw)
    if not value:
        digits_only = money.digits(raw).replace(",", "").replace("،", "")
        value = int(digits_only) if re.fullmatch(r"\d{3,12}", digits_only) else 0
    if not value:
        raise ValueError("عدد قیمت را بنویس؛ مثلاً «698000»، «698» یا «698t» (همه ۶۹۸٬۰۰۰).")
    if not money.in_accepted_range(value):
        raise ValueError(
            f"قیمت {money.format_toman(value)} خارج از بازهٔ مجاز فروشگاه "
            f"({settings.price_min:,} تا {settings.price_max:,} تومان) است. "
            "اگر واقعاً همین است، PRICE_MIN/PRICE_MAX را در تنظیمات عوض کن."
        )
    return value


def parse_group_prices(text: str) -> GroupPrice:
    """«ایفون 698 اندروید 598» → {\"iphone\": 698000, \"android\": 598000}."""
    raw = (text or "").strip()
    if _is_clear(raw):
        return {}
    out: GroupPrice = {}
    for line in raw.splitlines() or [raw]:
        for group, value in money.group_amounts(line).items():
            if money.in_accepted_range(value):
                out[group] = value
        for label, amount in re.findall(
            r"(?i)(iphone|android|ایفون|آیفون|اندروید|سامسونگ|شیائومی)\s*[:=]?\s*([\d,،.]{3,12})", line
        ):
            key = "iphone" if "iphone" in label.lower() or "یفون" in label else (
                "android" if "اندروید" in label or label.lower() in {"samsung", "xiaomi", "سامسونگ", "شیائومی"} else ""
            )
            value = money.parse_line_amount(amount) or 0
            if key and value and money.in_accepted_range(value):
                out[key] = value
    if not out:
        raise ValueError(
            "هیچ گروهی با مبلغ نفهمیدم. شکل درست: «ایفون 698 اندروید 598» یا «iphone: 698000»."
        )
    missing = [g for g in ("iphone", "android") if g not in out]
    if missing and len(out) == 1:
        raise ValueError(
            "فقط یک گروه قیمت دارد؛ اگر واقعاً فقط یک گروه می‌فروشی «حذف» را بزن تا قیمت "
            "مشترک استفاده شود، وگرنه گروه دیگر ۰ تومان ساخته می‌شود."
        )
    return out


def parse_sale_price(text: str) -> int:
    """«قیمت ویژه 498» → 498000 — same reading rules as the price, so both speak toman.

    «کمتر بودن از قیمت اصلی» اینجا چک نمی‌شود: این تابع قیمت فعلی را نمی‌داند. آن قاعده
    در `bot/services/validation.py` است تا یک مسیر دو جای مختلف نیمی از آن را نگیرد.
    """
    raw = _one_line(text)
    if _is_clear(raw):
        return 0
    match = re.match(r"(?i)^\s*(?:قیمت\s*)?(?:فروش\s*)?ویژه\s*[:=\-]?\s*", raw)
    body = raw[match.end():] if match else raw
    if not body.strip():
        raise ValueError("عدد قیمت ویژه را بنویس؛ مثلاً «498000» یا «498t».")
    return parse_price(body)


def parse_stock(text: str) -> int | None:
    """«۲۰» / «موجودی 20 عدد» → 20. «حذف» → None یعنی «هیچ موجودی‌ای ارسال نشود»."""
    raw = _one_line(text)
    if _is_clear(raw):
        return None
    digits = re.sub(r"\D", "", money.digits(raw))
    if not digits:
        raise ValueError("عدد موجودی را بنویس؛ مثلاً «20» یا «موجودی 20».")
    value = int(digits)
    if value > 100_000:
        raise ValueError(
            f"موجودی {value:,} غیرواقعی به نظر می‌رسد. اگر واقعاً همین است، بدان که "
            "این عدد روی هر واریژن نوشته می‌شود."
        )
    return value


def parse_stock_status(text: str) -> str:
    raw = _one_line(text).casefold()
    if _is_clear(raw):
        return ""
    values = {
        "موجود": "instock", "موجود است": "instock", "instock": "instock",
        "ناموجود": "outofstock", "outofstock": "outofstock",
        "پیش‌فروش": "onbackorder", "پیش فروش": "onbackorder", "onbackorder": "onbackorder",
    }
    if raw not in values:
        raise ValueError("وضعیت را بنویس: موجود، ناموجود یا پیش‌فروش.")
    return values[raw]


def parse_colors(text: str) -> list[str]:
    items = _split_items(text)
    if _is_clear((text or "").strip()):
        return []
    if len(items) < 2:
        raise ValueError(
            "برای ویژگی رنگ حداقل دو مقدار لازم است (با «|» یا «،» جدا کن). "
            "اگر رنگی نیست، «حذف» را بزن تا محصول ساده ساخته شود."
        )
    if len(items) > 20:
        raise ValueError("بیش از ۲۰ رنگ منطقی نیست؛ احتمالاً متن دیگری را اینجا چسباندی.")
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = color_key(item)
        if key and key not in seen:
            seen.add(key)
            out.append(item)
    if len(out) < 2:
        raise ValueError("بعد از حذف تکرارها کمتر از دو رنگ ماند.")
    return out


def parse_models(text: str) -> list[str]:
    items = _split_items(text)
    if not items:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        # Canonicalise what we can («13 پرو مکس» → «iPhone 13 Pro Max») and keep
        # the rest verbatim: the owner may name a model the parser does not know,
        # and their word beats our absence of a rule.
        folded = fold_variant_words(item)
        parsed = extract_phone_models(folded)
        if not parsed:
            # A bare «13 پرو مکس» under no header: the same reading rule the
            # caption parser uses (an Apple section) canonicalises it instead of
            # storing a label no variation will ever match.
            parsed = extract_phone_models(f"Apple\n{folded}")
        label = parsed[0].label if len(parsed) == 1 else item
        key = re.sub(r"\s+", " ", label).casefold()
        if key in seen:
            raise ValueError(f"«{label}» دو بار نوشته شده؛ یکی را حذف کن.")
        seen.add(key)
        out.append(label)
    if not out:
        raise ValueError("هیچ مدلی نفهمیدم؛ هر مدل را در یک خط بنویس (مثلاً «13 پرو مکس»).")
    return out


def parse_sku_prefix(text: str) -> str:
    raw = _one_line(text)
    if _is_clear(raw):
        return ""
    value = re.sub(r"[^A-Za-z0-9]", "", money.digits(raw)).upper()
    if not value:
        raise ValueError("پیشوند SKU فقط حرف و عدد لاتین است (مثلاً «BO»).")
    if len(value) > 12:
        raise ValueError("پیشوند SKU بلندتر از ۱۲ نویسه نشود؛ روی هر SKU فضا کم می‌آید.")
    return value


def taxonomy_paths() -> list[str]:
    """Every category path in the store's taxonomy, as «پدر > فرزند»."""
    out: list[str] = []
    stack: list[str] = []
    for line in TAXONOMY.splitlines():
        if not line.strip():
            continue
        depth = (len(line) - len(line.lstrip(" "))) // 2
        name = line.strip()
        stack = [*stack[:depth], name]
        if depth >= 1:
            out.append(" > ".join(stack))
    return out


def parse_categories(text: str) -> list[str]:
    items = _split_items(text, paths=True)
    if _is_clear((text or "").strip()):
        return []
    valid = taxonomy_paths()
    by_leaf: dict[str, list[str]] = {}
    for path in valid:
        by_leaf.setdefault(path.split(" > ")[-1].casefold(), []).append(path)
    out: list[str] = []
    for item in items:
        clean = _one_line(item)
        if clean in FORBIDDEN or clean.casefold() in {x.casefold() for x in FORBIDDEN}:
            raise ValueError(f"«{clean}» دستهٔ ممنوع است (فروش ویژه/بلک‌فرایدی/عمده).")
        match = [p for p in valid if p.casefold() == clean.casefold()]
        if not match:
            leaf = clean.split(" > ")[-1].casefold()
            match = by_leaf.get(leaf, [])
        if not match:
            raise ValueError(
                f"«{clean}» در دسته‌بندی فروشگاه نیست. چند نمونه: " + "، ".join(valid[:3])
            )
        if len(match) > 1:
            raise ValueError(
                f"«{clean}» در بیش از یک شاخه هست؛ مسیر کامل را بنویس: " + " | ".join(match[:4])
            )
        for path in match:
            if path not in out:
                out.append(path)
    return out


def parse_attribute(text: str) -> list[str]:
    if _is_clear(text):
        return []
    items = _split_items(text)
    if len(items) < 2:
        raise ValueError("حداقل دو مقدار بنویس یا ویژگی را حذف کن.")
    return items


# ---------------------------------------------------------------------------
# field table
# ---------------------------------------------------------------------------
def _fields() -> dict[str, dict[str, Any]]:
    """The editable fields, in the order a seller checks them."""
    return {
        "title": {"label": "عنوان", "hint": "یک خط؛ نه قیمت، نه مدل", "parse": parse_title},
        "price": {"label": "قیمت", "hint": "مثلاً 698000 یا 698t", "parse": parse_price},
        "prices": {
            "label": "قیمت گروه‌ها",
            "hint": "«ایفون 698 اندروید 598» — برای حذف بنویس «حذف»",
            "parse": parse_group_prices,
        },
        "sale_price": {
            "label": "قیمت ویژه",
            "hint": "مثلاً 498000 یا 498t — باید از قیمت اصلی کمتر باشد؛ برای حذف بنویس «حذف»",
            "parse": parse_sale_price,
        },
        "stock": {
            "label": "موجودی",
            "hint": "یک عدد، مثلاً 20؛ برای حذف بنویس «حذف»",
            "parse": parse_stock,
        },
        "stock_status": {
            "label": "وضعیت موجودی",
            "hint": "موجود، ناموجود یا پیش‌فروش",
            "parse": parse_stock_status,
        },
        "colors": {"label": "رنگ‌ها", "hint": "با | یا ، جدا کن (دو تا به بالا)", "parse": parse_colors},
        "models": {"label": "مدل‌ها", "hint": "هر مدل در یک خط", "parse": parse_models},
        "sku_prefix": {"label": "پیشوند SKU", "hint": "حروف لاتین، مثلاً BO", "parse": parse_sku_prefix},
        "categories": {"label": "دسته‌بندی", "hint": "مسیر کامل یا نام آخر دسته", "parse": parse_categories},
    }


def editable_fields(data: Any) -> list[tuple[str, str, str]]:
    """``(key, label, current value)`` for the picker keyboard.

    Attribute axes the parser created (طرح، جنس، …) are editable too, with
    ``attr:<name>`` keys, because those are exactly the fields a seller usually
    wants to trim.
    """
    out: list[tuple[str, str, str]] = []
    specs = _fields()
    for key in ("title", "price", "sale_price", "prices", "stock", "stock_status", "colors", "models", "sku_prefix", "categories"):
        out.append((key, specs[key]["label"], display_value(data, key)))
    for name, values in (getattr(data, "attributes", None) or {}).items():
        if name == "رنگ":
            continue
        out.append((f"attr:{name}", f"ویژگی {name}", " | ".join(values)))
    return out


def snapshot(data: Any) -> dict[str, tuple[str, str]]:
    """``key -> (label, rendered value)`` for every editable field.

    The preview used to re-render the whole card after a one-field edit, which
    turned «چه چیزی عوض شد؟» into a game of spot-the-difference. The flow takes a
    snapshot before the edit and diffs it against the one after — see :func:`diff`.
    """
    return {key: (label, value) for key, label, value in editable_fields(data)}


def diff(
    before: dict[str, tuple[str, str]],
    after: dict[str, tuple[str, str]],
    *,
    variations: tuple[int, int] | None = None,
) -> str:
    """The «تغییرات» line for a manual edit; "" when nothing actually moved."""
    parts: list[str] = []
    for key, (label, value) in after.items():
        previous = before.get(key)
        if previous is not None and previous[1] != value:
            parts.append(f"{label}: {previous[1]} ← {value}")
        elif previous is None:
            parts.append(f"{label}: +{value}")
    for key, (label, value) in before.items():
        if key not in after:
            parts.append(f"{label}: −{value}")
    if variations and variations[0] != variations[1]:
        delta = variations[1] - variations[0]
        sign = "+" if delta > 0 else "−"
        parts.append(f"{sign}{abs(delta)} واریژن ({variations[0]} ← {variations[1]})")
    return " · ".join(parts)


def display_value(data: Any, key: str) -> str:
    if key == "price":
        value = getattr(data, "price", 0)
        return money.format_toman(value) if value else "—"
    if key == "sale_price":
        value = getattr(data, "sale_price", 0)
        return money.format_toman(value) if value else "—"
    if key == "stock":
        value = getattr(data, "stock", None)
        return "—" if value is None else f"{value:,} عدد"
    if key == "stock_status":
        return {
            "instock": "موجود", "outofstock": "ناموجود", "onbackorder": "پیش‌فروش",
        }.get(str(getattr(data, "stock_status", "") or ""), "—")
    if key == "prices":
        groups = getattr(data, "prices", None) or {}
        return " | ".join(f"{g}: {v:,}" for g, v in groups.items()) or "—"
    if key == "colors":
        colors = (getattr(data, "attributes", None) or {}).get("رنگ") or []
        return " | ".join(colors) or "—"
    if key == "categories":
        cats = getattr(data, "categories", None) or []
        return " | ".join(cats) or "—"
    value = getattr(data, key, "")
    if isinstance(value, list):
        return " | ".join(str(x) for x in value) or "—"
    return str(value) if value else "—"


def prompt_for(key: str, data: Any) -> str:
    """The message shown when a field is picked: current value + accepted shape."""
    specs = _fields()
    if key.startswith("attr:"):
        name = key.split(":", 1)[1]
        return (
            f"✏️ <b>ویژگی {name}</b>\n"
            f"فعلی: {display_value(data, key)}\n"
            "مقدارها را با «|» جدا کن؛ برای حذف بنویس «حذف»."
        )
    spec = specs[key]
    scope = _scope_note(key, data)
    return (
        f"✏️ <b>{spec['label']}</b>\n"
        f"فعلی: {display_value(data, key)}\n"
        f"{spec['hint']}\n"
        + (f"{scope}\n" if scope else "")
        + "مقدار جدید را بفرست."
    )


def _scope_note(key: str, data: Any) -> str:
    """Where the number will actually land — said, not implied.

    The same :class:`bot.services.plan.VariationPlan` the preview and the writer read decides,
    so the picker cannot promise «روی هر ۴ واریژن» for a product that will be built simple.
    """
    if key not in ("stock", "sale_price"):
        return ""
    from bot.services.plan import plan_from_dict

    raw = data.to_dict() if hasattr(data, "to_dict") else {}
    plan = plan_from_dict(raw)
    if not plan.is_variable:
        return "محصول ساده است: روی خودِ محصول نوشته می‌شود."
    return f"محصول متغیر است: روی هر {plan.count} واریژن نوشته می‌شود."


def apply_edit(data: Any, key: str, raw: str) -> str | None:
    """Set one field from typed text. Returns a Persian error, or None on success."""
    specs = _fields()
    try:
        if key.startswith("attr:"):
            name = key.split(":", 1)[1]
            values = parse_attribute(raw)
            attributes = dict(getattr(data, "attributes", None) or {})
            if values:
                attributes[name] = values
            else:
                attributes.pop(name, None)
            data.attributes = attributes
            stored: Any = values
            evidence_key = name
        else:
            spec = specs.get(key)
            if spec is None:
                return f"فیلد «{key}» قابل ویرایش نیست."
            stored = spec["parse"](raw)
            if key == "colors":
                attributes = dict(getattr(data, "attributes", None) or {})
                if stored:
                    attributes["رنگ"] = stored
                else:
                    attributes.pop("رنگ", None)
                data.attributes = attributes
                data.model_colors = _prune_matrix(getattr(data, "model_colors", None) or {}, stored)
                evidence_key = "colors"
            elif key == "categories":
                categories = stored or []
                if (
                    any(is_printed_category(category) for category in categories)
                    and not sku_uses_printed_category(getattr(data, "sku_prefix", ""))
                ):
                    return "دستهٔ «چاپی» فقط برای شناسه‌های CH و SB مجاز است."
                data.categories = apply_sku_category_policy(
                    categories, getattr(data, "sku_prefix", "")
                )
                stored = data.categories
                evidence_key = "category"
            else:
                setattr(data, key, stored)
                if key == "sku_prefix":
                    data.categories = apply_sku_category_policy(
                        getattr(data, "categories", None) or [], str(stored or "")
                    )
                evidence_key = key
    except ValueError as exc:
        return str(exc)

    edited = dict(getattr(data, "user_edits", None) or {})
    edited[key] = stored
    data.user_edits = edited
    ev.merge(data.evidence, evidence_key, ev.USER, quote="ویرایش دستی شما", overwrite=True)
    if not any(n.startswith("ویرایش دستی") for n in data.notes):
        data.notes.append("ویرایش دستی تو بعد از هر استخراج دوباره اعمال می‌شود")
    return None


def _prune_matrix(matrix: dict[str, list[str]], colors: Sequence[str]) -> dict[str, list[str]]:
    """Keep per-model restrictions inside the color list the owner just typed.

    A restriction naming a color that no longer exists would silently delete a
    whole model's variations; pruning keeps the product buildable and honest.
    """
    if not matrix or not colors:
        return {}
    allowed = {color_key(c) for c in colors}
    out: dict[str, list[str]] = {}
    for model, values in matrix.items():
        kept = [c for c in values if color_key(c) in allowed]
        if len(kept) >= 1:
            out[model] = kept
    return out


def apply_locks(data: Any) -> dict[str, Any]:
    """Re-apply the owner's manual edits after an extraction; returns what moved.

    This is the point of the whole module: a deliberate fix must outlive the
    next AI round, the next photo, the next correction message.
    """
    restored: dict[str, Any] = {}
    edits = dict(getattr(data, "user_edits", None) or {})
    for key, value in edits.items():
        try:
            if key == "colors":
                attributes = dict(getattr(data, "attributes", None) or {})
                if value:
                    attributes["رنگ"] = value
                else:
                    attributes.pop("رنگ", None)
                data.attributes = attributes
            elif key == "categories":
                data.categories = value or []
            elif key.startswith("attr:"):
                name = key.split(":", 1)[1]
                attributes = dict(getattr(data, "attributes", None) or {})
                if value:
                    attributes[name] = value
                else:
                    attributes.pop(name, None)
                data.attributes = attributes
            else:
                setattr(data, key, value)
        except Exception as exc:               # a stale lock must never break a draft
            logger.warning("اعمال قفل دستی روی «%s» ناموفق بود: %s", key, exc)
            if hasattr(data, "notes") and isinstance(data.notes, list):
                data.notes.append(f"⚠️ بازگردانی ویرایش دستیِ «{key}» ناموفق بود: {exc}")
            continue
        restored[key] = value
        ev.merge(data.evidence, key if not key.startswith("attr:") else key.split(":", 1)[1],
                 ev.USER, quote="ویرایش دستی شما", overwrite=True)
    if hasattr(data, "categories"):
        before_categories = list(getattr(data, "categories", None) or [])
        data.categories = apply_sku_category_policy(
            before_categories, getattr(data, "sku_prefix", "")
        )
        if data.categories != before_categories:
            restored["categories"] = data.categories
    return restored


# ---------------------------------------------------------------------------
# per-message color sourcing (the P1-11 leak, and the button that fixes it)
# ---------------------------------------------------------------------------
def colors_by_message(blocks: Sequence[Any]) -> dict[str, list[str]]:
    """Which colors each message stated — the list the picker shows."""
    from bot.services.color_matrix import extract_colors

    out: dict[str, list[str]] = {}
    for block in blocks:
        if not block.has(ev.ROLE_COLORS):
            continue
        for color in extract_colors(block.text(), allow_unknown=False):
            bucket = out.setdefault(block.message or "متن", [])
            if color not in bucket:
                bucket.append(color)
    return out


def suppress_colors(blocks: Sequence[Any], suppressed: Iterable[str]) -> list[Any]:
    """Drop the ``colors`` role from the suppressed messages.

    The lines stay (their titles and prices are still wanted) — only the color
    harvest stops, which is exactly what «این پیام محصول دیگری بود» means.
    """
    skip = set(suppressed)
    if not skip:
        return list(blocks)
    out: list[Any] = []
    for block in blocks:
        if block.message in skip and block.has(ev.ROLE_COLORS):
            roles = tuple(r for r in block.roles if r != ev.ROLE_COLORS)
            out.append(type(block)(raw=block.raw, line_no=block.line_no, message=block.message, roles=roles))
        else:
            out.append(block)
    return out


# ---------------------------------------------------------------------------
# suggestions (the typo path)
# ---------------------------------------------------------------------------
def brand_suggestions(text: str) -> list[dict[str, str]]:
    """«Nubia Z60» → a one-tap correction to Nokia, if only one brand fits."""
    out: list[dict[str, str]] = []
    for word in model_catalog_unknown(text):
        found = brand_suggest.suggest_brand(word)
        if not found:
            continue
        key, label = found
        out.append({"kind": "brand", "word": word, "target": label, "brand": key})
    return out


def model_catalog_unknown(text: str) -> list[str]:
    from bot.services import model_catalog

    return model_catalog.unknown_brand_words(text)


def accept_suggestion(data: Any, suggestion: dict[str, str]) -> str:
    """Teach the shop dictionary the typo; the next post will not ask again."""
    word, target = brand_suggest.correction_rule(suggestion["word"], suggestion["target"])
    vocabulary.set_rule(word, target)
    vocabulary.invalidate()
    applied = vocabulary.apply(data.title if data is not None else "")
    if data is not None and applied != data.title:
        data.title = applied
        ev.merge(data.evidence, "title", ev.VOCAB, quote=f"«{word}» → «{target}»", overwrite=True)
    return f"✅ از این به بعد «{word}» را «{target}» می‌خوانم"


__all__ = [
    "accept_suggestion", "apply_edit", "apply_locks", "brand_suggestions", "colors_by_message",
    "diff", "display_value", "editable_fields", "prompt_for", "snapshot", "suppress_colors",
    "taxonomy_paths",
]
