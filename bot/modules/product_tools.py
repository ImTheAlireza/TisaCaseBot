"""🧾 آخرین محصولات و 🔍 تست پارسر — the two screens that make the bot checkable.

After a publish the owner wants the id, the link and the numbers (the result card
in :mod:`bot.keyboards.cards`), and the day after that they want to know what was
shipped at all. Before a rule change is trusted, they want to see why the bot
read a text the way it did.

Both screens are read-only: neither creates a product, nor touches a file, so an
admin can use them without a sudo gate. The parser test deliberately calls
:func:`bot.modules.product_flow.analyze`, i.e. the flow's own pipeline — a sandbox
that ran a second, simpler parser would answer the wrong question.
"""

from __future__ import annotations

import html
import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot.constants import CB
from bot.keyboards.cards import result_card
from bot.services import flow_guard, learning
from bot.services.access import guard_feature
from bot.services.conversations import FlowConversationHandler
from bot.services import postmodel as ev
from bot.services import products_ledger
from bot.utils.text import clip_html

logger = logging.getLogger(__name__)

#: one-shot flag for «تست پارسر» — a text message that arrives while it is set is
#: read as the sample instead of going to the main menu.
_PENDING_KEY = "parser_test_until"
_PENDING_TTL_SECONDS = 300
_MAX_REPORT = 3500
PARSER_WAIT = 0
_active_data: dict[int, dict] = {}


def close_for(user_id: int) -> bool:
    data = _active_data.pop(user_id, None)
    return data is not None and data.pop(_PENDING_KEY, None) is not None


flow_guard.register("parser_test", "تست پارسر", close_for)


def _clip(text: str, parse_mode: str | None = "HTML") -> tuple[str, str | None]:
    return clip_html(text, limit=_MAX_REPORT)


@guard_feature("recent_products")
async def cb_recent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The last few cards, newest first, each one openable."""
    query = update.callback_query
    await query.answer()
    entries = products_ledger.recent(10)
    if not entries:
        await query.edit_message_text(
            "🧾 هنوز محصولی از این ربات ساخته نشده است.\n"
            "بعد از هر ساخت موفق (یا ناموفق)، همین‌جا قابل دیدن خواهد بود."
        )
        return
    lines = ["🧾 <b>آخرین محصولات</b>", ""]
    rows: list[list[InlineKeyboardButton]] = []
    for index, entry in enumerate(entries, 1):
        lines.append(f"{index}. " + html.escape(products_ledger.summary(entry), quote=False))
        label = str(entry.get("title") or "—")[:32]
        rows.append([InlineKeyboardButton(f"{index}. {label}", callback_data=f"{CB.PRODUCTS_OPEN}:{entry.get('key')}")])
    rows.append([InlineKeyboardButton("↩️ منو", callback_data=CB.MAIN_MENU)])
    await query.edit_message_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows)
    )


@guard_feature("recent_products")
async def cb_open(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """One stored card: the numbers as they were approved, not as they read today."""
    query = update.callback_query
    await query.answer()
    key = (query.data or "").rsplit(":", 1)[-1]
    entry = products_ledger.find(key)
    if entry is None:
        await query.message.reply_text("این کارت پیدا نشد؛ احتمالاً تاریخچه پاک شده است.")
        return
    markup = None
    if entry.get("report"):
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("👁 پیش‌نمایش", callback_data=f"products:report:{key}")
        ]])
    await query.message.reply_text(result_card(entry), parse_mode="HTML", reply_markup=markup)


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
    await query.answer()
    text, mode = _clip(report)
    await query.edit_message_text(
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
    await query.answer()
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("👁 پیش‌نمایش", callback_data=f"products:report:{key}")
    ]]) if entry.get("report") else None
    await query.edit_message_text(result_card(entry), parse_mode="HTML", reply_markup=markup)


@guard_feature("parser_test", on_denial=close_for)
async def cb_parser_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user = update.effective_user
    flow_guard.close_others("parser_test", user.id)
    _active_data[user.id] = context.user_data
    await query.answer()
    context.user_data[_PENDING_KEY] = time.time() + _PENDING_TTL_SECONDS
    await query.message.reply_text(
        "🔍 متن نمونهٔ پست را بفرست (قیمت، مدل‌ها، رنگ‌ها…).\n\n"
        "همان مسیری اجرا می‌شود که یک پست واقعی می‌رود — با این تفاوت که هیچ "
        "محصولی ساخته نمی‌شود و هیچ فایلی نوشته نمی‌شود.\n\n"
        "اگر قاعدهٔ فعالی داشته باشی، متن را یک بار با قواعد و یک بار بدون آن‌ها "
        "می‌خوانم و فرقی که می‌کند را نشان می‌دهم.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("↩️ انصراف", callback_data=CB.PARSER_TEST_CANCEL)
        ]]),
    )

    return PARSER_WAIT

async def cb_parser_test_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data.pop(_PENDING_KEY, None)
    if update.effective_user:
        close_for(update.effective_user.id)
    await query.message.reply_text("↩️ تست پارسر لغو شد.")
    return ConversationHandler.END


def _fmt(value: object) -> str:
    """One readable line for a field value, whatever its shape is."""
    if isinstance(value, dict):
        parts = []
        for key, item in value.items():
            inner = "، ".join(str(x) for x in item) if isinstance(item, (list, tuple)) else str(item)
            parts.append(f"{key}: {inner}")
        return " | ".join(parts) or "—"
    if isinstance(value, (list, tuple)):
        return "، ".join(str(x) for x in value) or "—"
    if isinstance(value, bool):
        return "بله" if value else "نه"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


# The fields a learned rule can plausibly change. Anything else (image files,
# categories) is not what the rules touch, and listing them as "بدون تغییر" would
# just be noise.
_DIFF_FIELDS: tuple[tuple[str, str], ...] = (
    ("عنوان", "title"),
    ("قیمت", "price"),
    ("قیمت گروهی", "prices"),
    ("مدل‌ها", "models"),
    ("ویژگی‌ها", "attributes"),
    ("محدودیت رنگ هر مدل", "model_colors"),
    ("واریژن", "variation_count"),
)


def _rules_block(with_rules: object, without: object | None) -> list[str]:
    """What the owner's learned rules did to *this* text.

    A test that quietly applies the memory answers «چرا این‌طور خواندی؟» with a
    lie: the answer is «به‌خاطر قاعده‌ای که خودت یک‌بار به من دادی». So the report
    names the rules and shows the fields they moved, produced by running the same
    pipeline a second time with the memory switched off.
    """
    active = [rule for rule in learning.rules_sorted() if rule.is_active]
    pending = len(learning.pending_rules())
    lines = ["", "⚙️ <b>قواعد یادگرفته‌شده روی این متن</b>", ""]
    if not active:
        lines.append("هیچ قاعدهٔ فعالی ندارم؛ این متن را خودِ پارسر خوانده.")
    else:
        lines.append(f"{len(active)} قاعدهٔ فعال در نظر گرفته شد:")
        for rule in active[:8]:
            usage = " — روی این متن: اعمال شد" if _fired(rule, with_rules) else ""
            lines.append(f"• {html.escape(rule.describe(), quote=False)}{usage}")
        if len(active) > 8:
            lines.append(f"… و {len(active) - 8} قاعدهٔ دیگر")
    if pending:
        lines.append(f"⏳ {pending} قاعده در انتظار تأیید است و در این تست اعمال نمی‌شود.")
    if without is None:
        return lines
    payload_a = with_rules.to_dict() if hasattr(with_rules, "to_dict") else {}
    payload_b = without.to_dict() if hasattr(without, "to_dict") else {}
    diffs = []
    for label, key in _DIFF_FIELDS:
        before, after = payload_b.get(key), payload_a.get(key)
        if before == after:
            continue
        diffs.append(f"• {label}: «{html.escape(_fmt(before), quote=False)}» ← "
                     f"«{html.escape(_fmt(after), quote=False)}»")
    lines.append("")
    if diffs:
        lines.append("<b>فرقِ «با قواعد» و «بدون قواعد»:</b>")
        lines += diffs
    else:
        lines.append("هیچ فرقی نکرد — قواعد فعال روی این متن اثری نداشتند.")
    return lines


def _fired(rule, data: object) -> bool:
    """Does the draft carry this rule's fingerprint in its provenance?

    The extractor records every learned rewrite as evidence quoting the old and
    the new value, so "did it fire" is answered from the draft itself instead of
    from a second bookkeeping channel that could drift.
    """
    if rule.kind != "term":
        return False                  # a numeric rule leaves no quote to look for
    evidence = getattr(data, "evidence", None) or {}
    for item in evidence.values():
        note = getattr(item, "note", "") or ""
        if getattr(item, "source", "") == ev.LEARNED and rule.key in note:
            return True
    return False


def _data_report(data: object, extra: list[str] | None = None) -> tuple[str, str | None]:
    """The extracted draft as (text, parse_mode), rendered like the preview."""
    payload = data.to_dict() if hasattr(data, "to_dict") else {}
    lines = ["🔍 <b>خروجی پارسر</b>", ""]

    def field(label: str, value: object) -> None:
        if value in (None, "", [], {}, 0):
            return
        if isinstance(value, (list, tuple)):
            shown = " | ".join(str(x) for x in value) or "—"
        elif isinstance(value, dict):
            shown = " | ".join(f"{k}: {v}" for k, v in value.items()) or "—"
        else:
            shown = str(value)
        lines.append(f"<b>{label}:</b> {html.escape(shown, quote=False)}")

    field("عنوان", payload.get("title"))
    field("قیمت", f"{payload.get('price'):,}" if payload.get("price") else "")
    field("قیمت گروهی", payload.get("prices"))
    field("مدل‌ها", payload.get("models"))
    field("رنگ‌ها", (payload.get("attributes") or {}).get("رنگ"))
    for name, values in (payload.get("attributes") or {}).items():
        if name != "رنگ":
            field(f"ویژگی {name}", values)
    field("محدودیت رنگ هر مدل", payload.get("model_colors"))
    field("پیشوند SKU", payload.get("sku_prefix"))
    field("دسته‌ها", payload.get("categories"))
    field("واریژن", payload.get("variation_count"))
    field("هشدار کاتالوگ", payload.get("warnings"))
    suggestions = payload.get("suggestions") or []
    if suggestions:
        field(
            "پیشنهاد",
            [f"{x.get('word')} ← {x.get('target')}؟" for x in suggestions if isinstance(x, dict)],
        )
    evidence = getattr(data, "evidence", None) or {}
    notes = list(payload.get("notes") or [])
    if evidence:
        lines.append("")
        lines.append(ev.preview_html(evidence, notes))
    elif notes:
        lines.append("")
        lines.append("<b>نکته‌ها:</b> " + html.escape("؛ ".join(notes), quote=False))
    if extra:
        lines += extra
    return _clip("\n".join(lines))


@guard_feature("parser_test", on_denial=close_for)
async def on_parser_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Read the sample with the flow's own pipeline. Never creates anything."""
    message = update.effective_message
    if not message or not message.text:
        return ConversationHandler.END
    until = float(context.user_data.get(_PENDING_KEY) or 0)
    if time.time() > until:
        return ConversationHandler.END
    context.user_data.pop(_PENDING_KEY, None)
    if update.effective_user:
        _active_data.pop(update.effective_user.id, None)
    from bot.modules.product_flow import analyze  # local: keeps this module importable alone

    # The second run is what makes the diff real, and it is skipped when there is
    # nothing to compare against: no active rules means no possible difference —
    # and an AI-configured shop should not pay two model calls to learn that.
    needs_comparison = any(rule.is_active for rule in learning.rules_sorted())
    try:
        data = await analyze(message.text)
        without = await analyze(message.text, apply_rules=False) if needs_comparison else None
    except Exception as exc:                       # pragma: no cover — parser bug
        logger.exception("parser test failed")
        await message.reply_text(f"⚠️ پارسر خطا داد: {type(exc).__name__}: {exc}")
        return ConversationHandler.END
    text, mode = _data_report(data, _rules_block(data, without))
    await message.reply_text(text, **({"parse_mode": mode} if mode else {}))
    return ConversationHandler.END


async def parser_exit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop(_PENDING_KEY, None)
    if update.effective_user:
        close_for(update.effective_user.id)
    from bot.modules.start import cmd_start
    await cmd_start(update, context)
    return ConversationHandler.END


async def parser_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user:
        close_for(update.effective_user.id)
    from bot.modules.start import cb_main_menu
    await cb_main_menu(update, context)
    return ConversationHandler.END


def register(app: Application) -> None:
    app.add_handler(CallbackQueryHandler(cb_recent, pattern=f"^{CB.PRODUCTS_RECENT}$"))
    app.add_handler(CallbackQueryHandler(cb_open, pattern=f"^{CB.PRODUCTS_OPEN}:\\w+$"))
    app.add_handler(CallbackQueryHandler(cb_report, pattern=r"^products:report:\w+$"))
    app.add_handler(CallbackQueryHandler(cb_summary, pattern=r"^products:summary:\w+$"))
    app.add_handler(FlowConversationHandler(
        flow="parser_test",
        entry_points=[CallbackQueryHandler(cb_parser_test, pattern=f"^{CB.PARSER_TEST}$")],
        states={PARSER_WAIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_parser_text)]},
        fallbacks=[
            CallbackQueryHandler(cb_parser_test_cancel, pattern=f"^{CB.PARSER_TEST_CANCEL}$"),
            CallbackQueryHandler(parser_menu, pattern=f"^{CB.MAIN_MENU}$"),
            CommandHandler(["cancel", "start", "menu"], parser_exit),
        ],
        conversation_timeout=_PENDING_TTL_SECONDS,
        name="parser_test",
    ))


__all__ = ["register"]
