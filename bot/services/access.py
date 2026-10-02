"""Authorization at the point of use, including old keyboards and open flows."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any

from bot.buttons import feature_allowed
from bot.services import metrics


async def require_feature(update: Any, key: str, checker: Callable[[int | None, str], bool] | None = None) -> bool:
    user = update.effective_user
    if user is not None and (checker or feature_allowed)(user.id, key):
        return True
    metrics.note_denial(key)
    query = getattr(update, "callback_query", None)
    if query is not None:
        await query.answer("⛔ دسترسی ندارید یا دسترسی این بخش برداشته شده است.", show_alert=True)
    elif update.effective_message is not None:
        await update.effective_message.reply_text("⛔ دسترسی ندارید یا دسترسی این بخش برداشته شده است.")
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
                user = update.effective_user
                if user is not None and on_denial is not None:
                    on_denial(user.id)
                return -1  # ConversationHandler.END; ignored by ordinary handlers
            return await callback(update, context, *args, **kwargs)
        return guarded
    return decorate
