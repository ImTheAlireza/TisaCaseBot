"""پاسخِ فوری به تپ و صفحهٔ تازه، هم‌زمان.

هر تپ دکمه دو تماس با تلگرام دارد: ``answerCallbackQuery`` (خاموش‌کردنِ اسپینر) و
``editMessageText`` (عوض‌شدن صفحه). اگر این دو پشت‌سرهم اجرا شوند، کاربر دو
رفت‌وبرگشتِ کامل انتظار می‌کشد تا صفحه عوض شود؛ با اجرای هم‌زمان، صفحه یک
رفت‌وبرگشت زودتر می‌آید (اندازه‌گیری‌شده با همان ``telegram.Bot`` پروژه و RTT=۱۰۰ms:
۲۹۲ → ۱۴۶ میلی‌ثانیه در هر تپ).

قراردادها:

* هر تپ **دقیقاً یک بار** جواب می‌گیرد — حتی اگر ویرایش صفحه شکست بخورد؛
* خطای ویرایش مثل قبل بالا می‌رود (به error handler می‌رسد) و خطای پاسخ هم اگر
  ویرایش سالم بوده باشد؛
* هیچ task بی‌صاحبی جا نمی‌ماند: تا تسویهٔ پاسخ صبر می‌شود.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from typing import Any, TypeVar

from telegram import CallbackQuery
from telegram.error import BadRequest

T = TypeVar("T")

logger = logging.getLogger(__name__)

#: تلگرام با «چیزی عوض نشده» جواب می‌دهد وقتی محتوا و دکمه‌ها همان قبلی‌اند؛
#: این یک خطای عملیاتی نیست (مثلاً وقتی همان صفحه دوباره فرستاده می‌شود).
_UNCHANGED = "message is not modified"


async def _answer_call(query: CallbackQuery, toast: str | None, show_alert: bool) -> bool:
    kwargs: dict[str, Any] = {}
    if toast:
        kwargs["text"] = toast
    if show_alert:
        kwargs["show_alert"] = True
    return await query.answer(**kwargs)


async def answer_and(
    query: CallbackQuery,
    work: Awaitable[T],
    *,
    toast: str | None = None,
    show_alert: bool = False,
) -> T:
    """Answer ``query`` now, run ``work`` (the screen update) in parallel, return its result."""
    answered: asyncio.Task[bool] = asyncio.create_task(_answer_call(query, toast, show_alert))
    # One loop tick so the answer is *started* first: callbacks have always been
    # answered before the screen update goes out, and the fake queries in the tests
    # (and anything else watching the wire) rely on that order.
    await asyncio.sleep(0)
    try:
        result = await work
    except BaseException:
        # Never leave a tap unanswered just because the screen update failed.
        await asyncio.gather(answered, return_exceptions=True)
        raise
    await answered
    return result


async def edit_or_ignore(query: CallbackQuery, text: str, **kwargs: Any) -> bool:
    """``edit_message_text`` that treats Telegram's idempotent "not modified" as a no-op.

    Returns ``True`` when the screen was replaced, ``False`` when Telegram said it was
    already exactly that. Every other error stays visible to the caller.
    """
    try:
        await query.edit_message_text(text, **kwargs)
        return True
    except BadRequest as exc:
        if _UNCHANGED in str(exc).casefold():
            logger.debug("Telegram edit skipped: message content and markup are unchanged")
            return False
        raise


async def answer_and_edit(
    query: CallbackQuery,
    text: str,
    *,
    toast: str | None = None,
    show_alert: bool = False,
    quiet: bool = False,
    **kwargs: Any,
) -> bool:
    """The common callback shape: answer the tap and replace the screen, overlapped.

    ``quiet=True`` swallows «message is not modified» (a re-delivered tap on an
    unchanged screen); it does not hide any other error.
    """
    if quiet:
        return await answer_and(
            query, edit_or_ignore(query, text, **kwargs), toast=toast, show_alert=show_alert
        )
    # ``edit_message_text`` returns the edited Message; this function's contract is bool
    # («was the screen replaced»), so say that instead of pretending a Message is one.
    await answer_and(query, query.edit_message_text(text, **kwargs), toast=toast, show_alert=show_alert)
    return True


__all__ = ["answer_and", "answer_and_edit", "edit_or_ignore"]
