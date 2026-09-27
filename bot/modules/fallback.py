"""Fallback handlers — catch anything no other module handled.

Registered LAST (see bot/modules/__init__.py) in group 0, so real features always win.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot.services import product_journal
from bot.utils.logging import redact

logger = logging.getLogger(__name__)


async def cb_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """An inline button whose callback data no module recognizes
    (e.g. a stale keyboard from an older bot version)."""
    query = update.callback_query
    logger.warning("Unknown callback data: %r", query.data)
    await query.answer("این دکمه دیگر فعال نیست. با /start منو را باز کن.", show_alert=True)


async def msg_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Free text outside of any conversation — nudge the user to the menu."""
    await update.effective_message.reply_text(
        "من با دکمه‌ها کار می‌کنم — برای دیدن منو /start را بفرست."
    )


async def doc_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A document sent outside of any flow — point to the converter button."""
    await update.effective_message.reply_text(
        "برای تبدیل فایل، اول از منو دکمه «📦 تبدیل فایل کد رهگیری» را بزن.\n"
        "منو: /start"
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log an unhandled error locally and to the configured audit chat."""
    error = context.error
    exc_info = (type(error), error, error.__traceback__) if error is not None else None
    logger.error("Unhandled exception while processing update", exc_info=exc_info)

    callback = getattr(update, "callback_query", None)
    user = getattr(update, "effective_user", None)
    update_id = getattr(update, "update_id", "—")
    details = [
        "⚠️ خطای مدیریت‌نشدهٔ بات",
        f"update_id={update_id}",
        f"user_id={user.id if user else '—'}",
    ]
    if callback is not None:
        details.append(f"callback={str(callback.data or '')[:120]}")
    if error is not None:
        message = redact(str(error).strip())[:500] or "(بدون پیام خطا)"
        details.append(f"{type(error).__name__}: {message}")
    reported = False
    try:
        reported = await product_journal.send_log_message(context.bot, "\n".join(details))
    except Exception:
        # Diagnostics must never stop the user-facing fallback.
        logger.exception("Could not report an unhandled error to the log chat")

    effective_message = getattr(update, "effective_message", None)
    if effective_message:
        notice = (
            "⚠️ خطایی رخ داد و جزئیات برای بررسی ثبت شد. با /start دوباره تلاش کن."
            if reported else "⚠️ خطایی رخ داد. با /start دوباره تلاش کن."
        )
        try:
            await effective_message.reply_text(notice)
        except Exception:
            logger.debug("Could not send the fallback error message", exc_info=True)


def register(app: Application) -> None:
    # Same group (0) as everything else — order of registration decides,
    # and this module is registered last.
    app.add_handler(CallbackQueryHandler(cb_unknown))
    app.add_handler(MessageHandler(filters.Document.ALL, doc_unknown))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_unknown))
    app.add_error_handler(on_error)
