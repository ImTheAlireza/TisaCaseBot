"""Central registry of the bot's main-menu buttons.

The main menu and the «⚙️ تنظیمات» screen are both generated from this list,
so adding/removing a button only has to happen here. Visibility flags are
stored via bot.services.preferences (key = ``Button.key``).
"""

from __future__ import annotations

from dataclasses import dataclass

from bot import rbac
from bot.constants import CB
from bot.services import preferences


@dataclass(frozen=True)
class Button:
    key: str           # stable id used as the preference key
    label: str         # emoji + Persian label
    callback: str      # callback_data of the button
    admin_eligible: bool  # True if it can ever be shown to admins


BUTTONS: tuple[Button, ...] = (
    Button("tracking", "📦 تبدیل فایل کد رهگیری", CB.TRACKING_CONVERT, True),
    Button("compress", "🗜️ فشرده‌سازی عکس‌ها", CB.COMPRESS, True),
    Button("product_new", "🆕 محصول جدید", CB.PHONE_NEW, True),
    Button("product_restock", "🔄 اپدیت محصول موجود", CB.PHONE_RESTOCK, True),
    Button("recent_products", "🧾 آخرین محصولات", CB.PRODUCTS_RECENT, True),
    Button("parser_test", "🔍 تست پارسر", CB.PARSER_TEST, True),
    Button("ping", "🏓 Ping", CB.PING, False),
    Button("ops_status", "📊 وضعیت", CB.OPS_STATUS, False),
    Button("restart", "🔄 ری‌استارت", CB.RESTART_ASK, False),
    Button("admins", "👥 مدیریت ادمین‌ها", CB.ADMINS_LIST, False),
    Button("learning", "🧠 یادگیری‌ها", CB.LEARNING, False),
    Button("settings", "⚙️ تنظیمات", CB.SETTINGS, False),
)

BY_KEY: dict[str, Button] = {button.key: button for button in BUTTONS}


def feature_allowed(user_id: int | None, key: str) -> bool:
    """Whether ``user_id`` may currently use the feature behind ``key``.

    Sudo always may. Admins may use only admin-eligible buttons that are
    currently visible — a hidden button is denied here too, so a stale
    keyboard can't bypass the toggle.
    """
    role = rbac.role(user_id)
    if role == rbac.SUDO:
        return True
    if role != rbac.ADMIN:
        return False
    button = BY_KEY.get(key)
    if button is None or not button.admin_eligible:
        return False
    return preferences.button_visible(key)
