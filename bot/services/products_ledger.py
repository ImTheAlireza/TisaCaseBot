"""What the bot published recently — and the card that explains it.

A publish used to end in one line of «✅ ساخته شد» that scrolled away: no id, no
link, no variation count, and no way back to the draft afterwards. This ledger is
the small durable store of those result cards (the last few), read by the card
itself («🧾 گزارش») and by «🧾 آخرین محصولات» in the main menu.

Only facts go in here — values that were really sent to WooCommerce, never what
the bot hoped for. A failed publish is recorded too (``status="failed"`` with the
error), because «سایت که خالی است» is the most common question after a crash, and
the honest answer is in this file.

Statuses: ``created`` / ``zip`` / ``failed`` / ``dry`` (a rehearsal, plan 4.1) /
``pending`` — the intent written before the first request, which is what makes a
crash mid-publish recoverable instead of duplicated (plan 4.3).

The store is bounded (:data:`MAX_ENTRIES`): when a very long stretch of publishes
pushes an old card out, the duplicate check for that product simply forgets it.
That is the accepted trade — history deep enough to matter lives in the shop's own
database, not in a JSON file the owner may delete.


The store is plain JSON on purpose (see :mod:`bot.services.jsonstore`): it is
small, human-readable, and deleting it costs nothing but history.
"""

from __future__ import annotations

import time
from typing import Any
from collections.abc import Iterable

from bot.services.jsonstore import lock_for, read_json, write_json
from bot.config import data_dir

DATA_DIR = data_dir()
FILE = DATA_DIR / "recent_products.json"

#: How many cards we keep. A longer history belongs in the shop's own database.
MAX_ENTRIES = 20
#: The preview text stored per card, so «گزارش» works after a restart too.
REPORT_LIMIT = 6000

_lock = lock_for(FILE)


def _load() -> list[dict[str, Any]]:
    data = read_json(FILE, None)
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def new_key(user_id: int | str) -> str:
    """Identifier used by ``products:open:<key>`` — time-based, unique per second."""
    return f"{int(time.time())}{abs(hash(str(user_id))) % 997:03d}"


def record(
    *,
    user_id: int | str,
    status: str = "created",
    product_id: int | str | None = None,
    edit_url: str = "",
    mode: str = "new",
    title: str = "",
    variations: int = 0,
    price: int = 0,
    price_groups: dict[str, int] | None = None,
    model_prices: dict[str, int] | None = None,
    sale_price: int = 0,
    stock: int | None = None,
    stock_status: str = "",
    stock_matrix: dict[str, dict[str, int | None]] | None = None,
    sku_prefix: str = "",
    images: int = 0,
    categories: Iterable[str] = (),
    warnings: Iterable[str] = (),
    error: str = "",
    report: str = "",
    key: str | None = None,
    batch_id: str = "",
) -> dict[str, Any]:
    """Append one result card and return it (also used directly as the message).

    ``report`` is the preview text at the moment of publishing: the numbers a
    card shows must be the ones the owner approved, not what the parser thinks
    today — so it is stored, not recomputed.
    """
    entry: dict[str, Any] = {
        "key": key or new_key(user_id),
        "user_id": int(user_id) if str(user_id).lstrip("-").isdigit() else str(user_id),
        "status": status,
        "product_id": product_id,
        "edit_url": edit_url,
        "mode": mode,
        "title": (title or "").strip()[:120],
        "variations": max(0, int(variations)),
        "price": int(price or 0),
        "price_groups": {str(k): int(v) for k, v in (price_groups or {}).items()},
        "model_prices": {
            str(k): int(v) for k, v in (model_prices or {}).items() if int(v or 0) > 0
        },
        # Stored so the card can say what the shop was told. ``None`` is a real state here:
        # «the text never mentioned stock» must not be recorded as 0.
        "sale_price": int(sale_price or 0),
        "stock": None if stock in (None, "") else int(stock),
        "stock_status": str(stock_status or ""),
        "stock_matrix": {
            str(design): {str(model): quantity for model, quantity in row.items()}
            for design, row in (stock_matrix or {}).items()
        },
        "sku_prefix": sku_prefix,
        "images": int(images or 0),
        "categories": [str(x) for x in categories][:6],
        "warnings": [str(x) for x in warnings][:8],
        "error": (error or "")[:400],
        "report": (report or "")[:REPORT_LIMIT],
        "batch_id": batch_id,
        "ts": time.time(),
    }
    with _lock:
        entries = _load()
        entries.insert(0, entry)
        write_json(FILE, {"version": 1, "entries": entries[:MAX_ENTRIES]})
    _count(entry)
    return entry


def _count(entry: dict[str, Any], *, previous: str = "") -> None:
    """The same write feeds the operation counters — one choke point, no double count.

    «created» is the only status that means the shop made something, so a rehearsal
    (``dry``), a ZIP (``zip``) and an intent still open (``pending``) never inflate the
    numbers; ``queued`` and ``failed`` do, because both are things an operator asks about.

    ``previous`` is what this card said *before* an edit. The publish path opens a card
    as ``pending`` and closes it with :func:`update`, so counting must happen there too —
    and must not happen twice when the outbox re-finalizes the same key.
    """
    from bot.services import metrics

    status = entry.get("status")
    if not status or status == previous:
        return
    if status == "created":
        metrics.incr("products_created")
        if entry.get("variations"):
            metrics.incr("variations_created", int(entry["variations"]))
    elif status == "restocked":
        metrics.incr("restocks_applied")
    elif status == "queued":
        metrics.incr("publish_queued")
    elif status == "failed":
        metrics.incr("publish_failed")


def update(key: str, **fields: Any) -> dict[str, Any] | None:
    """Finish an intent: merge ``fields`` into the card with this key.

    A publish writes a ``pending`` card *before* the first request goes out (see
    :mod:`bot.services.publish_batch`), so a crash leaves something to find. This is
    the other half of that: the same key, now with the real outcome.
    """
    with _lock:
        entries = _load()
        for index, entry in enumerate(entries):
            if str(entry.get("key")) == str(key):
                previous = str(entry.get("status") or "")
                entry.update({k: v for k, v in fields.items() if v is not None or k == "product_id"})
                entry["done_ts"] = time.time()
                entries[index] = entry
                write_json(FILE, {"version": 1, "entries": entries[:MAX_ENTRIES]})
                _count(entry, previous=previous)
                return entry
    return None


def batch_history(batch_id: str) -> list[dict[str, Any]]:
    """Every retained attempt for this content, newest first (including dry runs)."""
    if not batch_id:
        return []
    return [entry for entry in _load() if str(entry.get("batch_id") or "") == str(batch_id)]


def find_batch(batch_id: str) -> dict[str, Any] | None:
    """The newest card built from this content (``None`` if we never tried)."""
    history = batch_history(batch_id)
    return history[0] if history else None


def recent(limit: int = 10) -> list[dict[str, Any]]:
    """The newest cards first (the stored order is already newest-first)."""
    return _load()[: max(1, limit)]


def find(key: str) -> dict[str, Any] | None:
    for entry in _load():
        if str(entry.get("key")) == str(key):
            return entry
    return None


def clear() -> int:
    """Forget the history (sudo panel). Returns how many cards were dropped."""
    with _lock:
        count = len(_load())
        write_json(FILE, {"version": 1, "entries": []})
    return count


def summary(entry: dict[str, Any]) -> str:
    """One line for the list: what happened, to which product, when."""
    moment = time.strftime("%Y/%m/%d %H:%M", time.localtime(float(entry.get("ts") or 0)))
    status = str(entry.get("status"))
    mark = {
        "created": "✅", "zip": "📦", "failed": "❌", "dry": "🧪", "pending": "⏳",
        "queued": "🐇", "restocked": "🔄",
    }.get(status, "•")
    title = str(entry.get("title") or "(بدون عنوان)")
    bits = [f"{mark} {title[:38]}"]
    if entry.get("product_id"):
        bits.append(f"#{entry['product_id']}")
    if entry.get("variations"):
        bits.append(f"{entry['variations']} واریژن")
    if entry.get("mode") in ("update", "restock"):
        bits.append("شارژ")
    if entry.get("error"):
        error = str(entry["error"])
        bits.append(error.partition(":")[0] if status == "queued" else error[:40])
    if status == "queued":
        # A queued attempt is not a failure and not a success: it is a promise the bot has
        # made for later, and the list has to be readable as that.
        bits.append("در صف تلاش مجدد")
    if status == "pending":
        # «⏳ در جریان بود» must not look like a finished card: it is the state a
        # crash leaves behind, and the owner has to be able to tell it apart.
        bits.append("نیمه‌کاره (ربات وسط کار خاموش شده؟)")
    return " · ".join(bits) + f"\n    🕒 {moment}"


def price_range(entry: dict[str, Any]) -> str:
    """The price(s) a card was built with, as one honest string."""
    models = {str(k): int(v) for k, v in (entry.get("model_prices") or {}).items() if int(v or 0) > 0}
    groups = {str(k): int(v) for k, v in (entry.get("price_groups") or {}).items() if int(v or 0) > 0}
    if models:
        # Model-specific prices are the real range on the storefront; the base price
        # is only what the remaining models get, so say it that way.
        base = int(entry.get("price") or 0)
        parts = [f"{name}: {value:,}" for name, value in models.items()]
        parts += [f"{name}: {value:,}" for name, value in groups.items()]
        if base:
            parts.append(f"پایه: {base:,}")
        return " | ".join(parts)
    if groups:
        return " | ".join(f"{name}: {value:,}" for name, value in groups.items())
    value = int(entry.get("price") or 0)
    return f"{value:,} تومان" if value else "—"


__all__ = [
    "FILE",
    "MAX_ENTRIES",
    "clear",
    "find",
    "new_key",
    "price_range",
    "recent",
    "record",
    "summary",
]
