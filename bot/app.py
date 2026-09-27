"""Application factory — builds the bot and wires up all modules."""

from __future__ import annotations

import logging

from telegram import BotCommand, Chat, Update
from telegram.ext import Application, ApplicationBuilder
from telegram.request import HTTPXRequest

from bot.config import settings
from bot.modules import register_all
from bot.modules.outbox_flow import start as start_outbox
from bot.modules.product_flow import notify_interrupted_flows
from bot.modules.restart import notify_restart_complete
from bot.utils.logging import set_current_user

logger = logging.getLogger(__name__)

BOT_COMMANDS = [
    BotCommand("start", "Open the main menu"),
    BotCommand("menu", "Open the main menu"),
    BotCommand("cancel", "لغو عملیات جاری"),
    # Only the owner can actually use it (the handler says so); Telegram does not have
    # per-role command lists, and an underscore is required — «/export-metrics» is not a
    # valid bot command name, however the upgrade plan spelled it.
    BotCommand("export_metrics", "خروجی CSV شمارنده‌های عملیاتی (فقط سودو)"),
]


def is_private_chat_update(update: object) -> bool:
    """True if the update comes from a private chat (or from no chat at all).

    Updates without a chat (inline queries, polls, …) pass through — the bot
    has no handlers for them anyway. Anything from a group, supergroup or
    channel is rejected, so no module can ever reply there.
    """
    if not isinstance(update, Update):
        return True
    chat = update.effective_chat
    return chat is None or chat.type == Chat.PRIVATE


class PrivateOnlyApplication(Application):
    """Application that silently drops every update from a non-private chat.

    Overriding :meth:`~telegram.ext.Application.process_update` is a single
    choke point that runs before ANY handler — commands, /start, button
    callbacks, conversation entries, fallbacks, everything. New modules
    therefore automatically inherit "no groups" behavior.
    """

    async def process_update(self, update: object) -> None:
        if isinstance(update, Update):
            user = update.effective_user
            set_current_user(user.id if user else None)
        if isinstance(update, Update) and not is_private_chat_update(update):
            chat = update.effective_chat
            logger.info(
                "Ignoring update from %s chat %s — bot only works in private chats.",
                chat.type,
                chat.id,
            )
            return
        await super().process_update(update)


async def _post_init(app: Application) -> None:
    """Runs once after the bot connects — set the command list shown in Telegram."""
    await app.bot.set_my_commands(BOT_COMMANDS)
    me = await app.bot.get_me()
    logger.info("Bot started as @%s (id=%s)", me.username, me.id)
    for problem in settings.problems:
        logger.warning("config: %s", problem)
    # If a supervisor restart was pending, confirm it in the chat that asked.
    await notify_restart_complete(app)
    # Flows that died with the previous process must be announced, not
    # silently forgotten (their temp files are swept by product_flow).
    await notify_interrupted_flows(app)
    # A publish the shop refused (429/5xx) waits in data/outbox.sqlite3 and is retried by
    # itself — including the ones left over from before this restart.
    await start_outbox(app)


def build_application() -> Application:
    # Telegram's defaults are only five seconds for reads/writes and one second
    # for connection-pool acquisition. That is brittle on shared hosting and
    # especially for multipart media uploads. Keep polling on its own small pool
    # so a slow upload cannot starve getUpdates.
    api_request = HTTPXRequest(
        connection_pool_size=16,
        connect_timeout=10.0,
        read_timeout=45.0,
        write_timeout=45.0,
        pool_timeout=20.0,
        media_write_timeout=90.0,
    )
    polling_request = HTTPXRequest(
        connection_pool_size=2,
        connect_timeout=10.0,
        read_timeout=40.0,
        write_timeout=15.0,
        pool_timeout=10.0,
    )
    app = (
        ApplicationBuilder()
        .token(settings.bot_token)
        .request(api_request)
        .get_updates_request(polling_request)
        .application_class(PrivateOnlyApplication)
        .post_init(_post_init)
        .build()
    )
    register_all(app)
    return app
