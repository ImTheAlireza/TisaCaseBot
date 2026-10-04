"""Sudo-only runtime preferences screen (⚙️ تنظیمات).

Lists every main-menu button with its visibility status for admins, as a
toggle: ✅ = shown to admins, ❌ = hidden from admins. Clicking a button
flips it. Sudo-only buttons are listed with a 👑 badge and cannot be granted
to admins. Values persist in data/preferences.json (bot/services/preferences).
"""

from __future__ import annotations

import asyncio
import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

from bot import rbac
from bot.buttons import BUTTONS
from bot.constants import CB
from bot.services import preferences
from bot.utils.ui import answer_and_edit

logger = logging.getLogger(__name__)


def _text() -> str:
    lines = [
        "⚙️ <b>تنظیمات</b>",
        "",
        "<b>نمایش دکمه‌ها برای ادمین‌ها:</b>",
        "",
    ]
    for button in BUTTONS:
        if button.admin_eligible:
            state = "✅" if preferences.button_visible(button.key) else "❌"
            lines.append(f"{state} {button.label}")
        else:
            lines.append(f"👑 {button.label} <i>(فقط سودو)</i>")
    lines += ["", "روی هر دکمه بزن تا وضعیت نمایش آن برای ادمین‌ها عوض شود."]
    return "\n".join(lines)


def _keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for button in BUTTONS:
        if button.admin_eligible:
            state = "✅" if preferences.button_visible(button.key) else "❌"
            rows.append(
                [
                    InlineKeyboardButton(
                        f"{state} {button.label}",
                        callback_data=f"{CB.SETTINGS_TOGGLE_PREFIX}{button.key}",
                    )
                ]
            )
        else:
            rows.append(
                [
                    InlineKeyboardButton(
                        f"👑 {button.label}", callback_data=CB.SETTINGS_LOCKED
                    )
                ]
            )
    rows.append([InlineKeyboardButton("⬅️ بازگشت به منو", callback_data=CB.MAIN_MENU)])
    return InlineKeyboardMarkup(rows)


async def cb_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open the settings screen. Sudo only."""
    query = update.callback_query
    user = update.effective_user
    if not user or not rbac.is_sudo(user.id):
        await query.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await answer_and_edit(query, _text(), reply_markup=_keyboard(), parse_mode="HTML", quiet=True)


async def cb_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Flip the admin-visibility of one button and re-render."""
    query = update.callback_query
    user = update.effective_user
    if not user or not rbac.is_sudo(user.id):
        await query.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    match = re.search(re.escape(CB.SETTINGS_TOGGLE_PREFIX) + r"([a-z_]+)$", query.data or "")
    key = match.group(1) if match else ""
    if not key:
        await query.answer()
        return
    # Off the loop: three fsyncs on a shared host would stall every other user.
    await asyncio.to_thread(preferences.set_button_visible, key, not preferences.button_visible(key))
    logger.info(
        "Sudo %s toggled button %s -> %s",
        user.id, key, preferences.button_visible(key),
    )
    await answer_and_edit(
        query,
        _text(),
        reply_markup=_keyboard(),
        parse_mode="HTML",
        toast="✅ به‌روزرسانی شد.",
        quiet=True,
    )


async def cb_locked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A sudo-only button was tapped — explain it can't be granted to admins."""
    query = update.callback_query
    user = update.effective_user
    if not user or not rbac.is_sudo(user.id):
        await query.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await query.answer(
        "👑 این دکمه مخصوص سودو است و همیشه فقط برای شما نمایش داده می‌شود.",
        show_alert=True,
    )


def register(app: Application) -> None:
    app.add_handler(CallbackQueryHandler(cb_settings, pattern=f"^{CB.SETTINGS}$"))
    app.add_handler(
        CallbackQueryHandler(
            cb_toggle, pattern=rf"^{re.escape(CB.SETTINGS_TOGGLE_PREFIX)}[a-z_]+$"
        )
    )
    app.add_handler(CallbackQueryHandler(cb_locked, pattern=f"^{CB.SETTINGS_LOCKED}$"))
