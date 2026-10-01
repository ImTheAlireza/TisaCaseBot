"""One shared gate for every product, used by both output paths.

Before this module the two writers disagreed: the REST path (``mode=new``) did
not require phone models at all, while the ZIP importer does — so a post whose
models were never detected was published as an attribute-less simple product.
A second problem was invisible degradation: a price read off a weight line, a
model list silently merged into one, colours pruned to nothing.

Every check returns an :class:`Issue` with a level:

``error``   the flow must not publish; the user fixes it or overrides explicitly
``warn``    publishable, but shown in the preview so nobody is surprised later
"""
from __future__ import annotations

import html
from dataclasses import dataclass, field
from typing import Any
from collections.abc import Iterable

LEVEL_ERROR = "error"
LEVEL_WARN = "warn"


@dataclass(frozen=True)
class Issue:
    level: str
    code: str
    message: str          # Persian, shown to the user as-is
    hint: str = ""         # what to do about it

    @property
    def is_blocking(self) -> bool:
        return self.level == LEVEL_ERROR

    def as_html(self) -> str:
        icon = "⛔" if self.is_blocking else "⚠️"
        line = f"{icon} {html.escape(self.message)}"
        if self.hint:
            line += f"\n   ↳ {html.escape(self.hint)}"
        return line


@dataclass
class IssueList:
    issues: list[Issue] = field(default_factory=list)

    def add(self, level: str, code: str, message: str, hint: str = "") -> None:
        if not any(issue.code == code and issue.message == message for issue in self.issues):
            self.issues.append(Issue(level, code, message, hint))

    @property
    def errors(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.is_blocking]

    @property
    def warnings(self) -> list[Issue]:
        return [issue for issue in self.issues if not issue.is_blocking]

    @property
    def blocking(self) -> bool:
        return bool(self.errors)

    def as_html(self) -> str:
        if not self.issues:
            return ""
        return "\n".join(issue.as_html() for issue in self.issues)

    def to_manifest(self) -> list[dict[str, str]]:
        return [{"level": i.level, "code": i.code, "message": i.message, "hint": i.hint}
                for i in self.issues]


def _values(data: dict[str, Any]) -> dict[str, Any]:
    return data


def validate_draft(
    data: dict[str, Any],
    *,
    mode: str = "new",
    image_count: int = 0,
    price_min: int = 1_000,
    price_max: int = 500_000_000,
    require_models: bool = False,
    unapplied_model_words: Iterable[tuple[str, Iterable[str]]] = (),
) -> IssueList:
    """Check a product draft. ``data`` is ``ProductData.to_dict()``."""
    report = IssueList()
    data = _values(data or {})
    title = str(data.get("title") or "").strip()
    price = int(data.get("price") or 0)
    prices = {str(k): int(v) for k, v in (data.get("prices") or {}).items() if v}
    models = [str(x).strip() for x in (data.get("models") or []) if str(x).strip()]
    attributes = data.get("attributes") or {}
    sku_prefix = str(data.get("sku_prefix") or "").strip()
    variations = int(data.get("variation_count") or 0)
    restrictions = data.get("model_colors") or {}

    if mode == "new":
        if not title:
            report.add(LEVEL_ERROR, "E_NO_TITLE",
                       "عنوان محصول پیدا نشد.",
                       "یک خط بنویس: «عنوان: قاب سیلیکونی مگنتی»")
        if not sku_prefix:
            report.add(LEVEL_ERROR, "E_NO_SKU",
                       "پیشوند SKU پیدا نشد.",
                       "حروف بزرگِ تنها در متن (مثلاً «BO») پیشوند SKU است.")
        if not models:
            if require_models:
                report.add(LEVEL_ERROR, "E_NO_MODELS",
                           "هیچ مدل گوشی/لوازم تشخیص داده نشد.",
                           "اگر محصول واقعاً مدل ندارد، REQUIRE_MODELS را در .env روی no بگذار.")
            else:
                report.add(LEVEL_WARN, "W_NO_MODELS",
                           "مدلی تشخیص داده نشد؛ محصول بدون محور مدل ساخته می‌شود.",
                           "اگر ویژگی متغیر دیگری ندارد، محصول به‌صورت ساده در ووکامرس ساخته خواهد شد.")
        if image_count == 0:
            report.add(LEVEL_ERROR, "E_NO_IMAGES", "هیچ عکسی برای این محصول دریافت نشد.")
        if not price and not prices:
            report.add(LEVEL_ERROR, "E_NO_PRICE", "هیچ قیمتی پیدا نشد.",
                       "یک خط قیمت بنویس: «قیمت 698000 تومان»")
    else:
        if not price and not prices and not models and not attributes:
            report.add(LEVEL_ERROR, "E_NOTHING_TO_APPLY",
                       "هیچ تغییری برای اعمال وجود ندارد (قیمت، مدل یا ویژگی جدید بفرست).")

    stock = data.get("stock")
    stock = None if stock in (None, "") else int(stock)
    stock_status = str(data.get("stock_status") or "").strip()
    sale = int(data.get("sale_price") or 0)

    checked: list[tuple[str, int]] = [("قیمت", price)]
    checked += [(f"قیمت {group}", value) for group, value in prices.items()]
    if sale:
        checked.append(("قیمت ویژه", sale))
    for label, value in checked:
        if value and not price_min <= value <= price_max:
            report.add(
                LEVEL_ERROR, "E_PRICE_RANGE",
                f"{label} ({value:,}) خارج از بازهٔ منطقی {price_min:,} تا {price_max:,} است.",
                "اگر واقعاً همین است، بازه را در .env (PRICE_MIN/PRICE_MAX) تغییر بده.",
            )

    # «قیمت ویژه» that is not cheaper is not a discount: WooCommerce stores both
    # numbers and shows the bigger one, so the admin would publish a sale nobody sees.
    if sale:
        for label, base in [("قیمت اصلی", price), *[(f"قیمت {group}", value) for group, value in prices.items()]]:
            if base and sale >= base:
                report.add(
                    LEVEL_ERROR, "E_SALE_NOT_CHEAPER",
                    f"قیمت ویژه ({sale:,}) از {label} ({base:,}) کمتر نیست.",
                    "قیمت ویژه را پایین‌تر بنویس، یا با «✏️ قیمت ویژه → حذف» بردارش.",
                )
                break

    if stock is not None:
        if stock < 0:
            report.add(LEVEL_ERROR, "E_STOCK_NEGATIVE", "موجودی منفی معنایی ندارد.",
                       "یک عدد بدون علامت بنویس، یا «حذف» تا ربات اصلاً موجودی نفرستد.")
        elif stock > 100_000:
            report.add(LEVEL_WARN, "W_STOCK_HUGE",
                       f"موجودی {stock:,} برای یک محصول غیرعادی است.",
                       "این عدد روی هر واریژن نوشته می‌شود؛ اگر اشتباه است با «✏️ موجودی» عوضش کن.")
        if stock_status == "outofstock" and stock > 0:
            report.add(LEVEL_WARN, "W_STOCK_CONTRADICTION",
                       f"هم «ناموجود» نوشته شده و هم موجودی {stock:,}.",
                       "ناموجود یعنی فروشگاه آن را تمام‌شده نشان می‌دهد، فارغ از عدد.")
    if stock_status and stock_status not in ("instock", "outofstock", "onbackorder"):
        report.add(LEVEL_ERROR, "E_STOCK_STATUS",
                   f"وضعیت موجودی «{stock_status}» را ووکامرس نمی‌شناسد.",
                   "مقادیر مجاز: instock، outofstock، onbackorder.")

    # A price list that covers only one group is dangerous: the other group
    # silently gets the fallback price.
    if prices and len(prices) == 1 and len(models) > 1:
        only = next(iter(prices))
        other = "android" if only == "iphone" else "iphone"
        report.add(LEVEL_WARN, "W_ONE_GROUP_PRICE",
                   f"فقط قیمت {only} نوشته شده؛ بقیهٔ مدل‌ها همین قیمت را می‌گیرند.",
                   f"اگر {other} قیمت دیگری دارد، بنویس: «قیمت {other} 598000»".replace(
                       "android", "اندروید").replace("iphone", "آیفون"))

    # A model line holding a word we could not apply means a model was either
    # dropped or replaced by a neighbouring one — the worst kind of silent loss.
    for line, words in unapplied_model_words or ():
        left = "، ".join(str(word) for word in (words or ()))
        report.add(LEVEL_WARN, "W_MODEL_WORD_UNAPPLIED",
                   f"در خطِ مدل «{line}» کلمهٔ «{left}» اعمال نشد.",
                   "اگر این واقعاً یک مدل جداست (مثلاً Pro Plus) آن را در یک خطِ کامل بنویس.")

    # Attribute axes that collapsed to a single value cannot be variations.
    for name, values in attributes.items():
        cleaned = list(dict.fromkeys(str(v).strip() for v in (values or []) if str(v).strip()))
        if len(values or []) >= 2 and len(cleaned) < 2:
            report.add(LEVEL_ERROR, "E_AXIS_COLLAPSED",
                       f"ویژگی «{name}» فقط یک مقدار یکتا دارد و واریژن نمی‌سازد.",
                       "مقدارهای تکراری را حذف کن یا ویژگی جدید اضافه کن.")

    if models and len(models) >= 2 and attributes and variations == 1:
        report.add(LEVEL_ERROR, "E_NO_VARIATIONS",
                   "ویژگی وجود دارد اما هیچ ترکیب معتبری از آن درنمی‌آید.")

    # A model whose listed colours match none of the real colour options stays
    # unrestricted — that is safe, but usually means a spelling mismatch.
    color_options = {
        str(value).strip().casefold()
        for name, values in attributes.items()
        if _is_color_name(name)
        for value in (values or [])
    }
    for model, colors in (restrictions or {}).items():
        unknown = [
            color for color in (colors or [])
            if color_options and str(color).strip().casefold() not in color_options
        ]
        if unknown and len(unknown) == len(list(colors or [])):
            report.add(LEVEL_WARN, "W_COLOR_MISMATCH",
                       f"رنگ‌های «{model}» با گزینه‌های ویژگی رنگ جور نشد؛ این مدل محدود نشد.",
                       f"نوشته‌های: {'، '.join(str(x) for x in unknown)}")

    # Category assignment is best-effort; say so instead of hiding it.
    for category in data.get("rejected_categories") or []:
        report.add(LEVEL_WARN, "W_CATEGORY_NOT_FOUND",
                   f"دستهٔ «{category}» در فروشگاه پیدا نشد و اعمال نمی‌شود.")

    return report


def _is_color_name(name: str) -> bool:
    from bot.services.color_matrix import is_color_attribute

    return is_color_attribute(str(name))


__all__ = ["LEVEL_ERROR", "LEVEL_WARN", "Issue", "IssueList", "validate_draft"]
