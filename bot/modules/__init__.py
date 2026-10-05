"""Feature modules.

Each module is self-contained and exposes exactly one function:

    def register(app: telegram.ext.Application) -> None

which attaches its handlers (CommandHandler, CallbackQueryHandler,
ConversationHandler, ...) to the application.

To add a new feature:
  1. create bot/modules/<feature>.py with a register() function
  2. add its callback-data constants to bot/constants.py
  3. add its button to the relevant keyboard in bot/keyboards/
  4. append the module to ALL_MODULES below

Order matters: `fallback` must stay last, since it catches anything
the other modules didn't handle.
"""

from __future__ import annotations

from telegram.ext import Application

from bot.modules import (
    admins,
    fallback,
    image_compress,
    learning_panel,
    ops,
    outbox_flow,
    ping,
    product_flow,
    product_tools,
    restart,
    settings,
    shop_watch,
    start,
    tracking_converter,
)

# Registration order — conversations first (so their /start & /cancel
# fallbacks win while a flow is active), fallback always last.
ALL_MODULES = (
    tracking_converter,
    product_flow,
    outbox_flow,
    product_tools,
    image_compress,
    admins,
    learning_panel,
    ops,
    settings,
    start,
    ping,
    shop_watch,
    restart,
    fallback,
)


def register_all(app: Application) -> None:
    for module in ALL_MODULES:
        module.register(app)
