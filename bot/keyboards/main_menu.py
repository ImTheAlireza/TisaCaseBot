"""Main menu inline keyboard.

This is the bot's home screen, and it is **role-aware**: the buttons shown
depend on who is looking at it.

* **sudo** (owner)  → sees every button from the registry (bot/buttons.py).
* **admin**         → sees only admin-eligible buttons whose visibility flag
                       is on; the owner controls those flags in «⚙️ تنظیمات».
* everyone else     → never reaches the menu (blocked in bot/modules/start.py).

The button list itself lives in bot/buttons.py — this module just renders it.
"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.constants import BOT_NAME, ROLE_BADGE
from bot import rbac
from bot.buttons import BUTTONS
from bot.services import preferences


def _display_name(user) -> str:
    """A friendly display name for the greeting, or a fallback."""
    if user is None:
        return "عزیز"
    name = (user.first_name or "").strip()
    if not name and user.username:
        name = user.username
    return name or "عزیز"


def main_menu_text(user_id: int | None = None, user=None) -> str:
    """Personalised, role-aware welcome shown on the main-menu screen."""
    role = rbac.role(user_id)
    lines = [
        f"👋 سلام <b>{_display_name(user)}</b> عزیز!",
        f"به ربات مدیریت <b>{BOT_NAME}</b> خوش اومدی. 🌟",
        "",
        f"نقش شما: {ROLE_BADGE.get(role, '')}",
        "",
        "از دکمه‌های زیر یک گزینه رو انتخاب کن:",
    ]
    return "\n".join(lines)


def main_menu_keyboard(user_id: int | None = None) -> InlineKeyboardMarkup:
    role = rbac.role(user_id)

    # Only sudo and admin ever reach the menu (bot/modules/start.py blocks the
    # rest); this guard is purely defensive — a plain user gets no buttons.
    if role == rbac.USER:
        return InlineKeyboardMarkup([])

    # Grouped layout: related actions grouped side-by-side or in logical pairs.
    # 1. Products & Store: new + restock, then recent products
    # 2. Tools & Processing: tracking + compress, then ping (sudo)
    # 3. Learning (sudo)
    # 4. Management & Settings (sudo): admins + settings, then restart
    menu_groups: list[list[tuple[str, ...]]] = [
        [("product_new", "product_restock"), ("recent_products",)],
        [("tracking", "compress"), ("ping",)],
        [("learning",)],
        [("admins", "settings"), ("restart",)],
    ]

    by_key = {b.key: b for b in BUTTONS}
    rows: list[list[InlineKeyboardButton]] = []

    for section in menu_groups:
        for row_keys in section:
            row: list[InlineKeyboardButton] = []
            for key in row_keys:
                button = by_key.get(key)
                if not button:
                    continue
                if role == rbac.SUDO or (button.admin_eligible and preferences.button_visible(button.key)):
                    row.append(InlineKeyboardButton(button.label, callback_data=button.callback))
            if row:
                rows.append(row)

    return InlineKeyboardMarkup(rows)
