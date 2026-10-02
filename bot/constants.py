"""Central namespace for callback data and shared constants.

Every inline button carries a callback-data string. To keep modules from
colliding, each feature gets its own prefix (e.g. "ping", "case:new").
ConversationHandler entry points can pattern-match on these prefixes.
"""

# --- Callback data -----------------------------------------------------------

class CB:
    """Callback-data identifiers. One constant per button action."""

    # Navigation
    MAIN_MENU = "nav:main"

    # Diagnostics
    PING = "ping"
    WOO_PING = "ping:woocommerce"
    WP_MEDIA_PING = "ping:wordpress-media"
    WOO_PRODUCT_PING = "ping:woocommerce-product"
    # 📊 وضعیت / 🩺 عیب‌یابی / 📥 متریک‌ها (bot/modules/ops.py)
    OPS_STATUS = "ops:status"
    OPS_DIAGNOSE = "ops:diagnose"
    OPS_METRICS = "ops:metrics"

    # Restart via supervisor
    RESTART_ASK = "restart:ask"
    RESTART_CONFIRM = "restart:confirm"

    # Tracking-file converter (تبدیل فایل کد رهگیری)
    TRACKING_CONVERT = "tracking:convert"
    TRACKING_CANCEL = "tracking:cancel"
    # The same file a second time: process it again on purpose, or move on.
    TRACKING_RETRY = "tracking:retry"
    TRACKING_MORE = "tracking:more"

    # Phone/accessories post processor (migrated from OPTION)
    # Product builder modes
    PHONE_POST = "phone:post"
    PHONE_NEW = "phone:new"
    PHONE_RESTOCK = "phone:restock"
    PHONE_POST_CANCEL = "phone:post:cancel"
    # «اپدیت محصول موجود»: the search step (the rest is the product builder's own screens)
    RESTOCK_PICK = "restock:pick"                # data = f"{...}:{product_id}"
    RESTOCK_CANCEL = "restock:cancel"
    RESTOCK_RETRY_SEARCH = "restock:search"

    # Image compression-only utility (فشرده‌سازی عکس‌ها)
    COMPRESS = "compress:start"

    # Sudo runtime preferences (تنظیمات نمایش دکمه‌ها برای ادمین‌ها)
    SETTINGS = "settings"
    SETTINGS_TOGGLE_PREFIX = "settings:toggle:"
    SETTINGS_LOCKED = "settings:locked"

    # Self-learning memory (یادگیری از اصلاحات) — see bot/modules/learning_panel.py
    LEARNING = "learning:list"
    LEARNING_CORRECTIONS = "learning:corrections"
    LEARNING_DELETE = "learning:delete"        # data = f"{...}:{short_id(rule_id)}"
    LEARNING_PENDING = "learning:pending"      # proposals waiting for the owner
    LEARNING_CONFIRM = "learning:confirm"      # data = f"{...}:{short_id(rule_id)}"
    LEARNING_DISABLE = "learning:disable"      # data = f"{...}:{short_id(rule_id)}"
    LEARNING_ENABLE = "learning:enable"        # data = f"{...}:{short_id(rule_id)}"
    LEARNING_SCOPE = "learning:scope"          # data = f"{...}:{short_id(rule_id)}"
    LEARNING_CLEAR_ASK = "learning:clear:ask"
    LEARNING_CLEAR_YES = "learning:clear:yes"
    LEARNING_CLEAR_NO = "learning:clear:no"

    # Product history + parser sandbox — see bot/modules/product_tools.py
    PRODUCTS_RECENT = "products:recent"
    PRODUCTS_OPEN = "products:open"          # data = f"{...}:{ledger_key}"
    PARSER_TEST = "tools:parser"
    PARSER_TEST_CANCEL = "tools:parser:cancel"

    # Product flow — the result card's own buttons
    PRODUCT_NEXT_PREFIX = "product:next"     # data = f"{...}:{new|update}"
    PRODUCT_PREVIEW = "product:preview"
    PRODUCT_CONFIRM_GUESSED = "product:guess:ok"   # «بله، این‌ها درست است»

    # Admin management (sudo only) — see bot/modules/admins.py
    ADMINS_LIST = "admins:list"
    ADMINS_ADD = "admins:add"
    ADMINS_ADD_CANCEL = "admins:add:cancel"
    ADMINS_REVOKE = "admins:invite:revoke"    # data = f"{...}:{user_id}"
    ADMINS_REMOVE = "admins:remove"          # data = f"{...}:{user_id}"
    ADMINS_REMOVE_CONFIRM = "admins:remove:confirm"
    ADMINS_REMOVE_BACK = "admins:remove:back"


# Prefix for per-admin remove buttons. Full data: f"{ADMINS_REMOVE}:{user_id}".
ADMIN_REVOKE_PREFIX = f"{CB.ADMINS_REVOKE}:"
ADMIN_REMOVE_PREFIX = f"{CB.ADMINS_REMOVE}:"
ADMIN_REMOVE_CONFIRM_PREFIX = f"{CB.ADMINS_REMOVE_CONFIRM}:"
ADMIN_REMOVE_BACK_PREFIX = f"{CB.ADMINS_REMOVE_BACK}:"


# --- Misc --------------------------------------------------------------------

BOT_NAME = "تیساکیس"

# Role badge shown in the main-menu greeting.
ROLE_BADGE = {
    "sudo": "👑 مالک (سودو)",
    "admin": "🛡️ ادمین",
    "user": "👤 کاربر",
}

DENIED_TEXT = "⛔ شما اجازه‌ی استفاده از این ربات را ندارید."
