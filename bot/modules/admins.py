"""مدیریت ادمین‌ها — sudo only.

Dedicated screen (button «👥 مدیریت ادمین‌ها» in the sudo main menu) to
**see** the current admins and **add / remove** them at runtime, without
touching .env.

* Adding: the sudo user forwards any message from the person to add, or
  sends that person's numeric Telegram user ID.
* Removing: each admin row has a «حذف» button → confirm → gone.

Admins are stored in data/roles.json (see bot/rbac.py). The sudo owner is
never listed/removable here — it always comes from SUDO_IDS in .env, so sudo
can't accidentally lock itself out.
"""

from __future__ import annotations

import html
import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, MessageOriginUser, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot import rbac
from bot.services.conversations import FlowConversationHandler
from bot.services import flow_guard, metrics
from bot.services.access import guard_feature
from bot.constants import (
    ADMIN_REMOVE_BACK_PREFIX,
    ADMIN_REMOVE_CONFIRM_PREFIX,
    ADMIN_REMOVE_PREFIX,
    ADMIN_REVOKE_PREFIX,
    CB,
)
from bot.keyboards import main_menu_keyboard, main_menu_text

logger = logging.getLogger(__name__)

# Conversation state
ADDING = 0

# user_data key remembering the instruction message to refresh into the list.
_EDIT_KEY = "admin_add_edit_target"
_active_data: dict[int, dict] = {}


def close_for(user_id: int) -> bool:
    data = _active_data.pop(user_id, None)
    return data is not None and data.pop(_EDIT_KEY, None) is not None


flow_guard.register("admin_add", "افزودن ادمین", close_for)

ADD_INSTRUCTION = (
    "➕ <b>افزودن ادمین</b>\n\n"
    "هر پیامی از آن شخص را <b>فوروارد</b> کن — او مستقیم ادمین می‌شود.\n\n"
    "یا آیدی عددی تلگرام او را بفرست (مثلاً <code>123456789</code>\n"
    "یا <code>@username</code>)؛ در این حالت یک <b>کد دعوت</b> ساخته می‌شود و\n"
    "فقط وقتی آن شخص کد را برای ربات بفرستد (<code>/start KOD</code>) ادمین می‌شود.\n\n"
    "چرا؟ چون یک عدد تایپ‌شده (یا اشتباه تایپی) نباید به دسترسی تبدیل شود."
)


def _back_to_list_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ بازگشت به منوی ادمین‌ها", callback_data=CB.ADMINS_ADD_CANCEL)]]
    )


# --- Rendering ---------------------------------------------------------------

def _admin_rows() -> str:
    """One line per stored admin, from rbac storage."""
    stored = rbac.admins()
    if not stored:
        return "   هنوز ادمینی اضافه نشده است."
    lines = []
    for i, (uid, rec) in enumerate(sorted(stored.items(), key=lambda kv: int(kv[0])), 1):
        name = rec.get("name") or ""
        handle = rec.get("username") or ""
        label = html.escape(str(name or (f"@{handle}" if handle else uid)))
        lines.append(f"{i}. {label}  (<code>{uid}</code>)")
    return "\n".join(lines)


def _invite_rows() -> str:
    pending = rbac.pending_invites()
    if not pending:
        return ""
    lines = ["", "<b>در انتظار تأیید خودِ کاربر</b> (کد را برایش بفرست):"]
    for uid, record in sorted(pending.items(), key=lambda kv: int(kv[0]), reverse=True):
        lines.append(
            f"⏳ <code>{uid}</code> — کد <code>{record.get('code', '')}</code>"
        )
    lines.append("<i>کاربر باید در چت خصوصی «/start &lt;کد&gt;» را بفرستد (۲۴ ساعت مهلت).</i>")
    return "\n".join(lines)


def _list_text(owner_ids: str) -> str:
    return (
        "👥 <b>مدیریت ادمین‌ها</b>\n\n"
        f"👑 مالک (سودو): <code>{owner_ids}</code>\n\n"
        "<b>ادمین‌ها</b> — فقط دکمه‌هایی را می‌بینند که در «⚙️ تنظیمات» برایشان روشن شده:\n"
        f"{_admin_rows()}"
        f"{_invite_rows()}\n\n"
        "با «➕ افزودن ادمین» اضافه کن و با «حذف» هر ردیف، دسترسی همان ادمین "
        "را بردار."
    )


def _list_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton("➕ افزودن ادمین", callback_data=CB.ADMINS_ADD)],
    ]
    for uid in sorted((int(u) for u in rbac.pending_invites()), reverse=True):
        rows.append([
            InlineKeyboardButton(
                f"لغو دعوت {uid}", callback_data=f"{ADMIN_REVOKE_PREFIX}{uid}"
            )
        ])
    for uid in sorted((int(u) for u in rbac.admins()), reverse=True):
        rows.append(
            [
                InlineKeyboardButton(
                    f"حذف ادمین {uid}", callback_data=f"{ADMIN_REMOVE_PREFIX}{uid}"
                )
            ]
        )
    rows.append([InlineKeyboardButton("⬅️ بازگشت به منو", callback_data=CB.MAIN_MENU)])
    return InlineKeyboardMarkup(rows)


def _confirm_remove_keyboard(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ بله، حذف شود", callback_data=f"{ADMIN_REMOVE_CONFIRM_PREFIX}{uid}"
                ),
                InlineKeyboardButton(
                    "انصراف", callback_data=f"{ADMIN_REMOVE_BACK_PREFIX}{uid}"
                ),
            ]
        ]
    )


# --- Helpers ---------------------------------------------------------------

def _owner_display() -> str:
    return ", ".join(str(i) for i in sorted(rbac.sudo_ids()))


async def _refresh_list(context: ContextTypes.DEFAULT_TYPE, *, chat_id: int,
                        message_id: int) -> None:
    """Rewrite a known message with the current admin list."""
    await context.bot.edit_message_text(
        chat_id=chat_id,
        message_id=message_id,
        text=_list_text(_owner_display()),
        reply_markup=_list_keyboard(),
        parse_mode="HTML",
    )


def _describe(user) -> dict:
    """Build a display record from a telegram User/Chat."""
    first = (getattr(user, "first_name", None) or "").strip()
    last = (getattr(user, "last_name", None) or "").strip()
    return {
        "username": getattr(user, "username", "") or "",
        "name": f"{first} {last}".strip(),
    }


# --- Main list (callback) ---------------------------------------------------

async def cb_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open the admin-management screen. Sudo only."""
    query = update.callback_query
    user = update.effective_user
    if not user or not rbac.is_sudo(user.id):
        metrics.note_denial("admins")
        await query.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await query.answer()
    await query.edit_message_text(
        _list_text(_owner_display()), reply_markup=_list_keyboard(), parse_mode="HTML"
    )


# --- Remove flow (plain callbacks) ------------------------------------------

async def cb_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«حذف» pressed → ask for confirmation."""
    query = update.callback_query
    user = update.effective_user
    if not user or not rbac.is_sudo(user.id):
        metrics.note_denial("admins")
        await query.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    m = re.search(rf"{re.escape(ADMIN_REMOVE_PREFIX)}(\d+)$", query.data or "")
    uid = int(m.group(1)) if m else 0
    await query.answer()
    await query.edit_message_text(
        f"❌ <b>حذف ادمین</b>\n\n"
        f"مطمئنی که دسترسی ادمین <code>{uid}</code> برداشته شود؟",
        reply_markup=_confirm_remove_keyboard(uid),
        parse_mode="HTML",
    )


async def cb_remove_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Confirmation → remove the admin and show the refreshed list."""
    query = update.callback_query
    user = update.effective_user
    if not user or not rbac.is_sudo(user.id):
        metrics.note_denial("admins")
        await query.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    m = re.search(rf"{re.escape(ADMIN_REMOVE_CONFIRM_PREFIX)}(\d+)$", query.data or "")
    uid = int(m.group(1)) if m else 0
    if uid:
        rbac.remove_admin(uid)
    await query.answer()
    await query.edit_message_text(
        _list_text(_owner_display()), reply_markup=_list_keyboard(), parse_mode="HTML"
    )


async def cb_revoke_invite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel a pending invite without touching any existing access."""
    query = update.callback_query
    user = update.effective_user
    if not user or not rbac.is_sudo(user.id):
        metrics.note_denial("admins")
        await query.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    match = re.search(rf"{re.escape(ADMIN_REVOKE_PREFIX)}(\d+)$", query.data or "")
    uid = int(match.group(1)) if match else 0
    cancelled = rbac.cancel_invite(uid) if uid else False
    await query.answer("🗑️ دعوت لغو شد." if cancelled else "دعوتی پیدا نشد.")
    await query.edit_message_text(
        _list_text(_owner_display()), reply_markup=_list_keyboard(), parse_mode="HTML"
    )


async def cb_remove_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«انصراف» → back to the list."""
    query = update.callback_query
    user = update.effective_user
    if not user or not rbac.is_sudo(user.id):
        metrics.note_denial("admins")
        await query.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await query.answer()
    await query.edit_message_text(
        _list_text(_owner_display()), reply_markup=_list_keyboard(), parse_mode="HTML"
    )


# --- Add flow (conversation) ------------------------------------------------

async def entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """«افزودن ادمین» pressed → show instructions, wait for id/forward."""
    query = update.callback_query
    user = update.effective_user
    if not user or not rbac.is_sudo(user.id):
        metrics.note_denial("admins")
        await query.answer("⛔ دسترسی ندارید.", show_alert=True)
        return ConversationHandler.END
    flow_guard.close_others("admin_add", user.id)
    _active_data[user.id] = context.user_data
    await query.answer()
    await query.edit_message_text(
        ADD_INSTRUCTION, reply_markup=_back_to_list_keyboard(), parse_mode="HTML"
    )
    # Remember this message so we can refresh it into the list when done.
    context.user_data[_EDIT_KEY] = (query.message.chat_id, query.message.message_id)
    return ADDING


async def _finish_add(update: Update, context: ContextTypes.DEFAULT_TYPE,
                      uid: int, name: str) -> int:
    user = update.effective_user
    if not user or not rbac.is_sudo(user.id):
        await update.effective_message.reply_text("⛔ دسترسی ندارید.")
        return ConversationHandler.END
    target = context.user_data.pop(_EDIT_KEY, None)
    if user:
        _active_data.pop(user.id, None)

    if rbac.is_sudo(uid):
        await update.effective_message.reply_text(
            "👑 این کاربر همان مالک (سودو) است و از قبل دسترسی کامل دارد. "
            "نیازی به افزودن نیست."
        )
    else:
        rbac.add_admin(uid, name=name, added_by=user.id if user else None)
        await update.effective_message.reply_text(
            f"✅ ادمین <code>{uid}</code> با موفقیت اضافه شد.", parse_mode="HTML"
        )

    # Refresh the instruction message into the current list.
    if target is not None:
        try:
            await _refresh_list(context, chat_id=target[0], message_id=target[1])
        except Exception:
            logger.exception("Could not refresh admin list after add")
    return ConversationHandler.END


@guard_feature("admins")
async def on_forwarded(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """A forwarded message → the original sender becomes the admin."""
    origin = getattr(update.effective_message, "forward_origin", None)
    sender = origin.sender_user if isinstance(origin, MessageOriginUser) else None
    if sender is None:
        await update.effective_message.reply_text(
            "❗ نتوانستم فرستنده‌ی اصلی این پیام را پیدا کنم. "
            "یا پیام واقعی یک کاربر را فوروارد کن، یا آیدی عددی او را بفرست."
        )
        return ADDING
    rec = _describe(sender)
    return await _finish_add(update, context, sender.id, rec["name"])


@guard_feature("admins")
async def on_id_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Text → a numeric id or @username; creates an invite, never direct access.

    Typing digits is not proof that the person wants (or exists as) an admin: a
    typo created a junk record and a guessed id created a real account with
    access. So the id only reserves a slot, and the slot activates when the
    user itself sends the code.
    """
    text = (update.effective_message.text or "").strip()
    uid: int | None = None
    name = ""
    handle = re.search(r"@([A-Za-z][A-Za-z0-9_]{3,31})", text)
    if handle:
        name = "@" + handle.group(1)
    match = re.search(r"\d{5,15}", text)
    if match:
        uid = int(match.group(0))
    elif handle:
        try:
            chat = await context.bot.get_chat(name)
            uid = int(getattr(chat, "id", 0)) or None
        except Exception:
            uid = None
    if uid is None:
        await update.effective_message.reply_text(
            "❗ یا یک پیام از آن شخص را فوروارد کن، یا آیدی عددی (۵ تا ۱۵ رقم) "
            "یا @username او را بفرست."
        )
        return ADDING

    if rbac.is_sudo(uid):
        await update.effective_message.reply_text(
            "👑 این کاربر همان مالک (سودو) است و از قبل دسترسی کامل دارد."
        )
        return ADDING
    if rbac.is_admin(uid):
        await update.effective_message.reply_text("✅ این کاربر از قبل ادمین است.")
        return ADDING

    try:
        chat = await context.bot.get_chat(uid)
        rec = _describe(chat)
        name = rec["name"] or name or f"کاربر {uid}"
    except Exception:
        name = name or f"کاربر {uid}"

    code = rbac.issue_invite(uid, name=name, added_by=update.effective_user.id)
    context.user_data.pop(_EDIT_KEY, None)
    await update.effective_message.reply_html(
        f"⏳ دعوت برای <code>{uid}</code> ({html.escape(name)}) ساخته شد.\n\n"
        f"به او بگو این را در چت خصوصی ربات بفرستد:\n<code>/start {code}</code>\n\n"
        "تا او نفرستد، ادمین نمی‌شود (مهلت: ۲۴ ساعت).",
        reply_markup=_back_to_list_keyboard(),
    )
    return ConversationHandler.END


async def cb_add_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """«بازگشت به منوی ادمین‌ها» → back to the list without adding."""
    query = update.callback_query
    user = update.effective_user
    if not user or not rbac.is_sudo(user.id):
        metrics.note_denial("admins")
        await query.answer("⛔ دسترسی ندارید.", show_alert=True)
        return ConversationHandler.END
    context.user_data.pop(_EDIT_KEY, None)
    await query.answer()
    await query.edit_message_text(
        _list_text(_owner_display()), reply_markup=_list_keyboard(), parse_mode="HTML"
    )
    return ConversationHandler.END


async def cmd_exit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/cancel, /start or /menu during add flow → back to main menu."""
    context.user_data.pop(_EDIT_KEY, None)
    user = update.effective_user
    await update.effective_message.reply_html(
        main_menu_text(user.id if user else None, user),
        reply_markup=main_menu_keyboard(user.id if user else None),
    )
    return ConversationHandler.END


# --- Registration -----------------------------------------------------------

def register(app: Application) -> None:
    app.add_handler(CallbackQueryHandler(cb_list, pattern=f"^{CB.ADMINS_LIST}$"))

    # Remove-flow callbacks (no conversation state).
    app.add_handler(
        CallbackQueryHandler(cb_remove, pattern=rf"^{re.escape(ADMIN_REMOVE_PREFIX)}\d+$")
    )
    app.add_handler(
        CallbackQueryHandler(
            cb_remove_confirm, pattern=rf"^{re.escape(ADMIN_REMOVE_CONFIRM_PREFIX)}\d+$"
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            cb_remove_back, pattern=rf"^{re.escape(ADMIN_REMOVE_BACK_PREFIX)}\d+$"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            cb_revoke_invite, pattern=rf"^{re.escape(ADMIN_REVOKE_PREFIX)}\d+$"
        )
    )

    # Add-flow conversation.
    add_conv = FlowConversationHandler(
        flow="admin_add",
        entry_points=[
            CallbackQueryHandler(entry, pattern=f"^{CB.ADMINS_ADD}$"),
        ],
        states={
            ADDING: [
                MessageHandler(filters.FORWARDED, on_forwarded),
                CallbackQueryHandler(cb_add_cancel, pattern=f"^{CB.ADMINS_ADD_CANCEL}$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_id_text),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_exit),
            CommandHandler("start", cmd_exit),
            CommandHandler("menu", cmd_exit),
        ],
        name="admin_add",
    )
    app.add_handler(add_conv)
