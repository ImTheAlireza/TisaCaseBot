"""The card a publish ends with — shared by the flow and the history screen.

Kept here (instead of inside ``bot.modules.product_flow``) because two screens
render it: right after a publish, and later from «🧾 آخرین محصولات». The card is
built from a :mod:`bot.services.products_ledger` entry, so both show the same
facts and the history view works after a restart.
"""

from __future__ import annotations

import html
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.constants import CB


def price_line(entry: dict[str, object]) -> str:
    from bot.services import products_ledger

    return products_ledger.price_range(entry)


def result_card(entry: dict[str, object]) -> str:
    """What really happened: id, link, counts, and the warnings that remain."""
    status = str(entry.get("status") or "")
    title = html.escape(str(entry.get("title") or "—"), quote=False)
    lines: list[str] = []
    if status == "dry":
        lines.append("🧪 تست موفق؛ هیچ محصولی در سایت ساخته نشد.")
    elif status == "failed":
        lines.append("❌ <b>ساخت ناموفق بود</b>")
        error = re.sub(r"\s+", " ", str(entry.get("error") or "").strip())
        if len(error) > 160:
            error = error[:159] + "…"
        if error:
            lines.append(f"⚠️ {html.escape(error, quote=False)}")
    elif status == "queued":
        lines.append("🐇 <b>در صف تلاش مجدد</b>")
        lines.append("✅ ذخیره شد؛ تلاش مجدد خودکار.")
        error = str(entry.get("error") or "").strip()
        if error:
            lines.append(f"⚠️ {html.escape(error.partition(':')[0], quote=False)}")
    elif status == "restocked":
        lines.append("🔄 <b>شارژ موجودی به‌روز شد.</b>")
    elif status == "zip":
        lines.append("📦 <b>فایل آمادهٔ آپلود است.</b>")
    else:
        lines.append("✅ <b>پیش‌نویس ساخته شد</b>")
        lines.append(f"🆔 #{html.escape(str(entry.get('product_id')), quote=False)}")
        if entry.get("edit_url"):
            lines.append(f"🔗 {html.escape(str(entry.get('edit_url')), quote=False)}")
    lines.append("")
    lines.append(f"عنوان: {title}")
    lines.append(
        f"🎨 {entry.get('variations', 0)} واریژن · 🖼 {entry.get('images', 0)} تصویر · "
        f"💰 {price_line(entry)}"
    )
    if int(entry.get("sale_price") or 0):
        lines.append(f"🏷 قیمت ویژه: {int(entry['sale_price']):,} تومان")
    model_prices = entry.get("model_prices")
    if isinstance(model_prices, dict) and model_prices:
        lines.append(
            "🧩 قیمت مدل‌های خاص: " + " | ".join(
                f"{html.escape(str(model), quote=False)}: {int(value):,}"
                for model, value in model_prices.items()
            )
        )
    if entry.get("stock") is not None:
        lines.append(f"📦 موجودی: {int(entry['stock']):,} عدد"
                     + (f" ({entry['stock_status']})" if entry.get("stock_status") else ""))
    matrix = entry.get("stock_matrix")
    if isinstance(matrix, dict) and matrix:
        quantities = [
            quantity
            for row in matrix.values() if isinstance(row, dict)
            for quantity in row.values()
            if isinstance(quantity, int) and not isinstance(quantity, bool)
        ]
        designs = len(matrix)
        models = len({str(model) for row in matrix.values() if isinstance(row, dict) for model in row})
        lines.append(
            f"📦 موجودی ماتریسی: {designs} طرح × {models} دسته · "
            f"جمع {sum(quantities):,} عدد"
        )
    if entry.get("sku_prefix"):
        lines.append(f"🏷 پیشوند SKU: <code>{html.escape(str(entry['sku_prefix']), quote=False)}</code>")
    warnings = [str(x) for x in (entry.get("warnings") or [])]
    if warnings:
        lines.append("")
        lines.append(f"📎 {len(warnings)} نکته‌ای که باید بدانی:")
        lines.extend(f"• {html.escape(text, quote=False)}" for text in warnings[:4])
    return "\n".join(lines)


def result_keyboard(entry: dict[str, object]) -> InlineKeyboardMarkup:
    """Act on the result — or start the next product without leaving the chat."""
    rows: list[list[InlineKeyboardButton]] = []
    if entry.get("edit_url"):
        rows.append([InlineKeyboardButton("🌐 ویرایش در سایت", url=str(entry["edit_url"]))])
    if str(entry.get("mode")) == "restock":
        # A restock card has no «next product with the same settings»: the settings are the
        # shop's own product, and the next one has to be looked up again.
        rows.append([InlineKeyboardButton("🔄 شارژ محصول بعدی", callback_data=CB.PHONE_RESTOCK)])
    else:
        mode = "update" if str(entry.get("mode")) == "update" else "new"
        rows.append([InlineKeyboardButton("📦 محصول بعدی (همان تنظیمات)",
                                         callback_data=f"product:next:{mode}")])
    rows.append([
        InlineKeyboardButton("🧾 گزارش همین محصول", callback_data=f"{CB.PRODUCTS_OPEN}:{entry.get('key')}")
    ])
    return InlineKeyboardMarkup(rows)
