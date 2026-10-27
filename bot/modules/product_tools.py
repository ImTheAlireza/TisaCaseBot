"""🧾 آخرین محصولات — the screen that makes the bot checkable after the fact.

After a publish the owner wants the id, the link and the numbers (the result
card in :mod:`bot.keyboards.cards`), and the day after that they want to know
what was shipped at all.

The screen is read-only: it neither creates a product nor touches a file, so an
admin can use it without a sudo gate.
"""

from __future__ import annotations

import html

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

from bot.constants import CB
from bot.keyboards.cards import result_card
from bot.services import products_ledger
from bot.services.access import guard_feature
from bot.utils.text import clip_html
from bot.utils.ui import answer_and, answer_and_edit

_MAX_REPORT = 3500


def _clip(text: str, parse_mode: str | None = "HTML") -> tuple[str, str | None]:
    return clip_html(text, limit=_MAX_REPORT)


@guard_feature("recent_products")
async def cb_recent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The last few cards, newest first, each one openable."""
    query = update.callback_query
    entries = products_ledger.recent(10)
    if not entries:
        await answer_and_edit(
            query,
            "🧾 هنوز محصولی از این ربات ساخته نشده است.\n"
            "بعد از هر ساخت موفق (یا ناموفق)، همین‌جا قابل دیدن خواهد بود.",
        )
        return
    lines = ["🧾 <b>آخرین محصولات</b>", ""]
    rows: list[list[InlineKeyboardButton]] = []
    for index, entry in enumerate(entries, 1):
        lines.append(f"{index}. " + html.escape(products_ledger.summary(entry), quote=False))
        label = str(entry.get("title") or "—")[:32]
        rows.append([InlineKeyboardButton(f"{index}. {label}", callback_data=f"{CB.PRODUCTS_OPEN}:{entry.get('key')}")])
    rows.append([InlineKeyboardButton("↩️ منو", callback_data=CB.MAIN_MENU)])
    await answer_and_edit(
        query, "\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows)
    )


@guard_feature("recent_products")
async def cb_open(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """One stored card: the numbers as they were approved, not as they read today."""
    query = update.callback_query
    key = (query.data or "").rsplit(":", 1)[-1]
    entry = products_ledger.find(key)
    if entry is None:
        await answer_and(
            query, query.message.reply_text("این کارت پیدا نشد؛ احتمالاً تاریخچه پاک شده است.")
        )
        return
    markup = None
    if entry.get("report"):
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("👁 پیش‌نمایش", callback_data=f"products:report:{key}")
        ]])
    await answer_and(
        query, query.message.reply_text(result_card(entry), parse_mode="HTML", reply_markup=markup)
    )


@guard_feature("recent_products")
async def cb_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the approved field-by-field preview only when someone asks for it."""
    query = update.callback_query
    key = (query.data or "").rsplit(":", 1)[-1]
    entry = products_ledger.find(key)
    report = str(entry.get("report") or "") if entry else ""
    if not report:
        await query.answer("پیش‌نمایش پیدا نشد.", show_alert=True)
        return
    text, mode = _clip(report)
    await answer_and_edit(
        query,
        text,
        **({"parse_mode": mode} if mode else {}),
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("↩️ خلاصه", callback_data=f"products:summary:{key}")
        ]]),
    )


@guard_feature("recent_products")
async def cb_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    key = (query.data or "").rsplit(":", 1)[-1]
    entry = products_ledger.find(key)
    if not entry:
        await query.answer("این کارت پیدا نشد.", show_alert=True)
        return
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("👁 پیش‌نمایش", callback_data=f"products:report:{key}")
    ]]) if entry.get("report") else None
    await answer_and_edit(query, result_card(entry), parse_mode="HTML", reply_markup=markup)


def register(app: Application) -> None:
    app.add_handler(CallbackQueryHandler(cb_recent, pattern=f"^{CB.PRODUCTS_RECENT}$"))
    app.add_handler(CallbackQueryHandler(cb_open, pattern=f"^{CB.PRODUCTS_OPEN}:\\w+$"))
    app.add_handler(CallbackQueryHandler(cb_report, pattern=r"^products:report:\w+$"))
    app.add_handler(CallbackQueryHandler(cb_summary, pattern=r"^products:summary:\w+$"))


__all__ = ["register"]
