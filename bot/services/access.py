"""Authorization at the point of use, including old keyboards and open flows."""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any

from bot.buttons import feature_allowed
from bot.services import metrics

logger = logging.getLogger(__name__)


async def require_feature(update: Any, key: str, checker: Callable[[int | None, str], bool] | None = None) -> bool:
    # A synthetic/channel update may carry no user at all: deny it, but do not explode on
    # the attribute access (the caller then has nothing to answer either).
    user = getattr(update, "effective_user", None)
    if user is not None and (checker or feature_allowed)(user.id, key):
        return True
    metrics.note_denial(key)
    denied = "⛔ دسترسی ندارید یا دسترسی این بخش برداشته شده است."
    try:
        query = getattr(update, "callback_query", None)
        if query is not None:
            await query.answer(denied, show_alert=True)
        else:
            message = getattr(update, "effective_message", None)
            if message is not None:
                await message.reply_text(denied)
    except Exception:
        # Telling the user is best-effort: their tap may be old, the bot blocked, the
        # network gone. Raising here turned a *refusal* into a crash of the handler —
        # the denial already happened (metric + no callback), so log and move on.
        logger.debug("could not deliver the denial message", exc_info=True)
    return False


def guard_feature(
    key: str | Callable[[Any, Any], str],
    *,
    on_denial: Callable[[int], Any] | None = None,
    checker: Callable[[int | None, str], bool] | None = None,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Recheck the current role and button policy on *every* handler invocation."""
    def decorate(callback: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @wraps(callback)
        async def guarded(update: Any, context: Any, *args: Any, **kwargs: Any) -> Any:
            feature = key(update, context) if callable(key) else key
            if not await require_feature(update, feature, checker):
                user = getattr(update, "effective_user", None)
                if user is not None and on_denial is not None:
                    on_denial(user.id)
                return -1  # ConversationHandler.END; ignored by ordinary handlers
            return await callback(update, context, *args, **kwargs)
        return guarded
    return decorate
