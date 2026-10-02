"""Which order files the bot has already converted — so a second copy is noticed.

The warehouse habit that motivates this file: the same export is sent twice (someone
else already ran it, or the chat scrolled away). A silent re-run is not harmless —
the owner ends up with two identical import files and luck decides which one reaches
the tracking system. So every processed file leaves a fingerprint here, and a repeat
is answered with the earlier result plus a button that says «باز هم انجامش بده».

Only the fingerprint and the counts are stored: no barcodes, no names, no addresses.
The file's content belongs to the shop's own system, and a JSON under ``data/`` is not
a database to copy an order table into.

Like the product ledger, this is a bounded ring (:data:`MAX_ENTRIES`): a file that was
processed before the window simply stops being recognised as a repeat, which is the
accepted trade for a file the owner can delete without breaking anything.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
import json
from pathlib import Path
from typing import Any

from bot.config import data_dir
from bot.services.jsonstore import lock_for, read_json, write_json

logger = logging.getLogger(__name__)

DATA_DIR = data_dir()
FILE = DATA_DIR / "tracking_ledger.json"

#: How many processed files we remember (~one shift of warehouse work).
MAX_ENTRIES = 30
#: Reading more than this to hash it would cost more than the check is worth; a
#: file that big is refused by MAX_FILE_MB before we ever get here.
MAX_HASH_BYTES = 64 * 1024 * 1024


def _lock() -> threading.Lock:
    """The store's per-path lock, looked up on use (tests repatch ``FILE``)."""
    return lock_for(FILE)


def fingerprint(path: str | Path) -> str:
    """Content hash of the downloaded file (streamed, capped).

    The hash is of the *bytes*, so renaming a file does not hide a repeat, and
    one re-exported with an extra row does not look like the same file.
    """
    digest = hashlib.sha1()
    read = 0
    try:
        with open(path, "rb") as handle:
            while chunk := handle.read(65536):
                digest.update(chunk)
                read += len(chunk)
                if read >= MAX_HASH_BYTES:
                    break
    except OSError:
        return ""
    return f"{digest.hexdigest()[:16]}"


def _load() -> list[dict[str, Any]]:
    data = read_json(FILE, None)
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def _owner_id(value: object) -> int | None:
    """The chat that ran it, stored only so a repeat can say who ran it first."""
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def remember(fingerprint_value: str, *, user_id: object, fname: str, report: dict[str, Any],
             dry_run: bool = False) -> dict[str, Any] | None:
    """Store one processed file. ``report`` is :meth:`processor.Report.as_dict`'s shape."""
    if not fingerprint_value:
        return None
    entry = {
        "fp": fingerprint_value,
        "ts": time.time(),
        "user_id": _owner_id(user_id),
        "file": fname,
        "rows": int(report.get("rows") or 0),
        "usable": int(report.get("usable") or 0),
        "errors": int(report.get("errors") or 0),
        "warnings": int(report.get("warnings") or 0),
        "dry_run": bool(dry_run),
    }
    with _lock():
        entries = [e for e in _load() if e.get("fp") != fingerprint_value]
        entries.append(entry)
        del entries[:-MAX_ENTRIES]
        if not write_json(FILE, {"entries": entries}):
            # A ledger that cannot be written only loses the duplicate warning; it
            # must never fail the conversion the user is waiting for.
            logger.warning("tracking ledger could not be written to %s", FILE)
            return None
        return entry


def find(fingerprint_value: str) -> dict[str, Any] | None:
    """The earlier record of this exact file, if we have one."""
    if not fingerprint_value:
        return None
    for entry in reversed(_load()):
        if entry.get("fp") == fingerprint_value:
            return entry
    return None


def recent(limit: int = 5) -> list[dict[str, Any]]:
    """The last processed files, newest last (what a status screen lists)."""
    return _load()[-limit:]


def clear() -> int:
    with _lock():
        count = len(_load())
        write_json(FILE, {"entries": []})
    return count


def probe() -> tuple[int, str]:
    """(registered files, problem) — what ``--check-config`` prints.

    A write that fails costs nothing but the duplicate warning, and a flow must not stop
    for that. Which is exactly why the start-up check is where an unreadable or corrupt
    ledger has to be said out loud: after that, no screen would ever mention it.
    """
    if not FILE.exists():
        return 0, ""
    try:
        raw = FILE.read_text(encoding="utf-8")
    except OSError as exc:
        return 0, f"دفتر فایل‌های ردیابی خوانده نشد: {exc}"
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        payload = None
    entries = _load()
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
        return 0, f"{FILE} قابل‌خواندن نیست؛ هشدار «این فایل قبلاً پردازش شده» از کار می‌افتد."
    return len(entries), ""


def describe(entry: dict[str, Any]) -> str:
    """One line a human can decide on: when, what, how clean."""
    moment = time.strftime("%Y/%m/%d %H:%M", time.localtime(float(entry.get("ts") or 0)))
    bits = [
        f'🕒 {moment}',
        f'📄 {entry.get("file") or "?"}',
        f'🔢 {entry.get("rows", 0)} ردیف',
        f'✅ {entry.get("usable", 0)} ردیف در tracking.csv',
    ]
    if entry.get("errors"):
        bits.append(f'❌ {entry["errors"]}')
    if entry.get("warnings"):
        bits.append(f'⚠️ {entry["warnings"]}')
    if entry.get("dry_run"):
        bits.append("🧪 dry-run")
    return "\n".join(bits)


__all__ = ["FILE", "MAX_ENTRIES", "clear", "describe", "find", "fingerprint", "probe", "recent", "remember"]
