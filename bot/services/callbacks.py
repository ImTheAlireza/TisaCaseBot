"""Compact session/revision-bound callback payloads (Telegram's 64-byte limit)."""
from __future__ import annotations

import re
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler

from bot.constants import CB

_SUFFIX = re.compile(r"\|([0-9a-f]{16})\.([0-9a-f]+)$")


def split(data: Any) -> tuple[str, str, int | None]:
    text = data if isinstance(data, str) else ""
    match = _SUFFIX.search(text)
    if match is None:
        return text, "", None
    return text[:match.start()], match[1], int(match[2], 16)


def action(query: Any) -> str:
    return split(getattr(query, "data", ""))[0]


def bind(markup: InlineKeyboardMarkup | None, nonce: str, revision: int) -> InlineKeyboardMarkup | None:
    if markup is None:
        return None
    rows = []
    for row in markup.inline_keyboard:
        buttons = []
        for button in row:
            data = button.callback_data
            if isinstance(data, str) and ((((data.startswith("product:") and not data.startswith("product:next:")) or data.startswith("restock:"))) or data == CB.MAIN_MENU):
                signed = f"{split(data)[0]}|{nonce}.{revision:x}"
                if len(signed.encode("utf-8")) > 64:
                    raise ValueError("Callback exceeds Telegram's 64-byte limit")
                buttons.append(InlineKeyboardButton(button.text, callback_data=signed))
            else:
                buttons.append(button)
        rows.append(buttons)
    return InlineKeyboardMarkup(rows)


class SessionCallbackHandler(CallbackQueryHandler):
    """Match the original action while allowing its optional proof suffix."""
    def __init__(self, callback: Any, pattern: str | None = None, **kwargs: Any) -> None:
        if pattern is not None and pattern.endswith("$"):
            pattern = pattern[:-1] + r"(?:\|[0-9a-f]{16}\.[0-9a-f]+)?$"
        super().__init__(callback, pattern=pattern, **kwargs)
