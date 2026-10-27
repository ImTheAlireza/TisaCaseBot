"""One Telegram file download for every media flow.

Compression and auto-crop both start by pulling the same bytes out of Telegram, and
the rules for that are not obvious enough to keep in two places:

* the size guard fires **before** the download (a 2 GB document must not reach the
  disk of a shared host to be refused afterwards);
* only idempotent *reads* are retried — a failed attempt leaves a partial file behind,
  and a retry that treats those bytes as a whole image is a silent corruption;
* the retry counts, and who counts it, belong to the caller.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

from telegram.error import NetworkError

logger = logging.getLogger(__name__)

#: Called as ``on_retry(attempt, exc)`` after a transient failure was cleaned up.
RetryHook = Callable[[int, BaseException], None]


def too_large(max_mb: float) -> ValueError:
    """The one refusal message, so both flows say the same thing."""
    return ValueError(f"فایل بزرگ‌تر از سقف مجاز ({max_mb:g} MB) است.")


async def download_to(
    bot,
    file_id: str,
    target: Path,
    *,
    max_mb: float,
    attempts: int = 3,
    on_retry: RetryHook | None = None,
) -> int:
    """Download ``file_id`` to ``target`` and return its size in bytes.

    Raises :class:`ValueError` when the file is over ``max_mb``, and the last network
    error when every attempt failed. A partial file never survives a failure.
    """
    maximum = int(max_mb * 1024 * 1024)
    for attempt in range(1, attempts + 1):
        try:
            tg_file = await bot.get_file(file_id)
            if tg_file.file_size and tg_file.file_size > maximum:
                raise too_large(max_mb)
            await tg_file.download_to_drive(custom_path=target)
            actual_size = target.stat().st_size if target.exists() else 0
            if actual_size > maximum:
                target.unlink(missing_ok=True)
                raise too_large(max_mb)
            return actual_size or tg_file.file_size or 0
        except (NetworkError, TimeoutError) as exc:
            # A failed download may have left a partial file behind. Never let a
            # retry treat that partial response as a complete image.
            target.unlink(missing_ok=True)
            if on_retry is not None:
                on_retry(attempt, exc)
            if attempt >= attempts:
                raise
            delay = 0.5 * (2 ** (attempt - 1))
            logger.warning(
                "Telegram download failed (%s/%s); retrying in %.1fs: %s",
                attempt,
                attempts,
                delay,
                exc,
            )
            await asyncio.sleep(delay)
    raise RuntimeError("Telegram download failed without an exception")  # pragma: no cover


__all__ = ["RetryHook", "download_to", "too_large"]
