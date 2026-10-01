"""متن‌های کوتاه‌شده برای تلگرام — یک قانون، همهٔ صفحه‌ها.

تلگرام ۴۰۹۶ کاراکتر تحمل می‌کند و پیامِ بلندتر از آن **رد می‌شود**؛ صفحه‌هایی که
گزارشِ ۵۰ واریژن یا کارتِ محصولِ پرحرف را می‌فرستند باید بتوانند کوتاه
کنند، و کوتاه‌کردن باید بگوید چه اتفاقی افتاده — نه اینکه متن وسطِ جمله بریده شود و
تمام. پس حدِّ پیام و جملهٔ «ادامه‌اش کجاست» یک‌جا تعریف شده‌اند (کارت محصول، گزارش‌ها
و «📊 وضعیت» همان یک تابع را صدا می‌زنند).
"""

from __future__ import annotations

import re

#: تلگرام ۴۰۹۶ است؛ کمی فضا می‌گذاریم برای خودِ جملهٔ «مخفف شد» و برچسب‌های HTML.
MESSAGE_LIMIT = 3900


def clip(text: str, limit: int = MESSAGE_LIMIT, note: str = "\n… (مخفف شد؛ کاملش در لاگ ربات)") -> str:
    """Cut `text` to `limit`, saying so — a silent cut looks like missing data.

    The note is part of the budget, so the result always fits: a screen that trims its
    own message must not be the thing that breaks the limit.
    """
    body = text or ""
    if len(body) <= limit:
        return body
    room = max(0, limit - len(note))
    return body[:room] + note


def clip_html(
    text: str,
    limit: int = MESSAGE_LIMIT,
    note: str = "\n… (میان‌بر برای رسیدن به حد تلگرام)",
) -> tuple[str, str | None]:
    """Fit an HTML message into Telegram's limit without breaking its markup.

    A raw slice can cut an HTML tag in half, and Telegram answers that with a
    400 — the failure would look like «the bot is broken» while it is one
    character of truncation. Past the limit we therefore drop the tags and send
    plain text, which cannot be mis-nested.
    """
    body = text or ""
    if len(body) <= limit:
        return body, "HTML"
    plain = re.sub(r"<[^>]+>", "", body)[:limit]
    room = max(0, limit - len(note))
    return plain[:room] + note, None


__all__ = ["MESSAGE_LIMIT", "clip", "clip_html"]

