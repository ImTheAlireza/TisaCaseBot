"""The last products the bot read, kept so a rule can be tested before it is used.

Self-learning is a promise about the *future*: «از این به بعد این‌طور می‌خوانم».
The only honest way to check that promise before it takes effect is to run the
new rule on the products that were just read and show what would have happened —
which is what :mod:`bot.services.learning_impact` does with this corpus.

What is stored per product is the smallest thing that makes a replay possible: the
text, what the parser read out of it (title, price, models, attribute values,
per-model colour limits) and the resulting variation count. Nothing else is kept,
and nothing is uploaded:
the same product text already lives in the ledger cards' report, in the same
git-ignored ``data/`` directory.

The corpus is a ring buffer of ``MAX_ENTRIES`` items. It is deliberately *not* a
history of everything: a preview that replays 500 products would be slow, and the
question the owner is asking is «آیا این قاعده روی محصولاتِ این روزهای من
خرابکاری می‌کند؟».
"""

from __future__ import annotations

import copy
import logging
import threading
import time
from typing import Any

from bot.config import data_dir
from bot.services import jsonstore, plan

logger = logging.getLogger(__name__)

DATA_DIR = data_dir()
CORPUS_FILE = DATA_DIR / "learning_corpus.json"

MAX_ENTRIES = 20
# One product must not be able to grow the file without limit; the first lines are
# where prices, models and colors are written anyway.
MAX_TEXT_CHARS = 64_000


def _lock_for() -> threading.Lock:
    """The store's per-path lock, looked up on use (tests repatch the path)."""
    return jsonstore.lock_for(CORPUS_FILE)


def record(text: str, data: Any) -> bool:
    """Add one extraction to the replay corpus.

    ``data`` is a ``ProductData`` (anything with the same attributes is fine);
    only the fields the projection needs are copied out, so the file stays a
    plain JSON snapshot and never pins the session object.
    """
    payload = data.to_dict() if hasattr(data, "to_dict") else {}
    title = str(payload.get("title") or "").strip()
    body = (text or "").strip()
    if not title and not body:
        return True
    entry = {
        "ts": time.time(),
        "text": body[:MAX_TEXT_CHARS],
        "replay_complete": len(body) <= MAX_TEXT_CHARS,
        "title": title[:160],
        "price": int(payload.get("price") or 0),
        "models": [str(x) for x in (payload.get("models") or [])],
        "attributes": {
            str(name): [str(v) for v in (values or [])]
            for name, values in (payload.get("attributes") or {}).items()
            if isinstance(values, (list, tuple))
        },
        "categories": [str(x) for x in (payload.get("categories") or [])][:4],
        # Per-model colour limits decide how many variations the product really
        # has, so a replay that wanted the card's number needs them.
        "model_colors": {
            str(name): [str(v) for v in (values or [])]
            for name, values in (payload.get("model_colors") or {}).items()
            if isinstance(values, (list, tuple))
        },
        # Not the number the session happened to carry: the same builder that
        # fills the preview card, so the corpus and the card cannot drift apart.
        "variation_count": plan.plan_from_dict(payload).count,
        "stock_matrix": copy.deepcopy(payload.get("stock_matrix") or {}),
        "stock_matrix_errors": copy.deepcopy(payload.get("stock_matrix_errors") or []),
        "prices": copy.deepcopy(payload.get("prices") or {}),
        "model_prices": copy.deepcopy(payload.get("model_prices") or {}),
    }
    with _lock_for():
        entries = _read()
        # A product is edited several times in one session, and every edit is an
        # extraction. Without this, the "last 20 products" would be six snapshots
        # of the same draft, and the preview would over-count it.
        if entries and entries[-1].get("title") == entry["title"] and entry["title"]:
            entries[-1] = entry
        else:
            entries.append(entry)
        del entries[:-MAX_ENTRIES]
        if not jsonstore.write_json(CORPUS_FILE, entries):
            # A corpus that cannot be written means impact previews will be empty.
            # It must not break a publish, so this only says so in the log.
            logger.warning("learning corpus could not be written to %s", CORPUS_FILE)
            return False
        jsonstore.invalidate(CORPUS_FILE)
        return True


def _read() -> list[dict[str, Any]]:
    raw = jsonstore.read_json(CORPUS_FILE, [])
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def entries(limit: int = MAX_ENTRIES) -> list[dict[str, Any]]:
    """The recorded products, oldest first (what the replay iterates)."""
    with _lock_for():
        found = _read()
    return found[-limit:] if limit else found


def count() -> int:
    with _lock_for():
        return len(_read())


def clear() -> None:
    """Forget the corpus (the rules themselves are untouched)."""
    with _lock_for():
        jsonstore.checked_write(CORPUS_FILE, [])


__all__ = ["CORPUS_FILE", "MAX_ENTRIES", "clear", "count", "entries", "record"]
