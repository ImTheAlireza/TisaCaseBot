"""/start command and main menu navigation."""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from bot import rbac
from bot.services import flow_guard
from bot.constants import CB, DENIED_TEXT
from bot.keyboards import main_menu_keyboard, main_menu_text

logger = logging.getLogger(__name__)


async def _deny(update: Update) -> None:
    """Log + reply to a user who is not allowed to use the bot."""
    user = update.effective_user
    if user:
        logger.warning(
            "Denied access to user %s (%s) — role=%s",
            user.id, user.username, rbac.role(user.id),
        )
    if update.callback_query:
        await update.callback_query.answer("⛔ دسترسی ندارید.", show_alert=True)
    elif update.effective_message:
        user_id = update.effective_user.id if update.effective_user else "نامشخص"
        await update.effective_message.reply_text(
            f"{DENIED_TEXT}\n\nشناسه تلگرام شما: {user_id}\n"
            "این عدد باید در SUDO_IDS فایل .env قرار داشته باشد."
        )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Entry point: /start shows the main menu (sudo/admin only).

    ``/start <code>`` additionally redeems a pending admin invite, so a numeric
    id typed by sudo can never grant access by itself.
    """
    user = update.effective_user
    message = update.effective_message
    if user and message and getattr(message, "text", ""):
        parts = message.text.split(maxsplit=1)
        if len(parts) == 2 and rbac.confirm_invite(user.id, parts[1]):
            await message.reply_html(
                "✅ تبریک! حالا <b>ادمین</b> هستی. از <code>/start</code> منو را باز کن."
            )
            return
    if not user or not rbac.is_allowed(user.id):
        await _deny(update)
        return

    flow_guard.close_others("", user.id)
    logger.info("User %s (%s) opened the main menu — role %s",
                user.id, user.username, rbac.role(user.id))
    await update.effective_message.reply_html(
        main_menu_text(user.id, user), reply_markup=main_menu_keyboard(user.id)
    )


async def cb_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Return to the main menu from any screen (callback button)."""
    query = update.callback_query
    user = update.effective_user
    if not user or not rbac.is_allowed(user.id):
        await _deny(update)
        return

    flow_guard.close_others("", user.id)
    await query.answer()
    await query.edit_message_text(
        main_menu_text(user.id, user),
        reply_markup=main_menu_keyboard(user.id),
        parse_mode="HTML",
    )


def register(app: Application) -> None:
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_start))
    app.add_handler(CallbackQueryHandler(cb_main_menu, pattern=f"^{CB.MAIN_MENU}$"))
