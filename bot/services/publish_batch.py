"""Identity of one publish attempt: the same input must never become two products.

Why a hash of the content and not a uuid: a uuid has to survive the crash, and a crash in
the middle of a publish (media uploaded → product POSTed → response lost) is exactly the
case this guards. Content-addressing means the retry recomputes the *same* id from the
same draft — there is nothing to persist and nothing to lose.

The id is written into the shop as product meta (``tisa_batch_id``) so that a half-finished
publish can be found again from the store's side too, not only from our JSON. The importer
plugin (``tisa-product-importer.zip``) reads the same id from ``product.json`` and refuses
to import a package it has already imported.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any
from collections.abc import Sequence

#: meta key on the WooCommerce product, written through the REST API. No leading underscore,
#: on purpose: WP hides underscore-prefixed keys from REST reads, and the resume hunt has to
#: read this back. The WordPress *importer* dedupes on its own post meta — the key there is a
#: detail of a codebase that is not in this repo, so it lives in docs/IMPORTER-CONTRACT.md.
META_BATCH = "tisa_batch_id"
#: provenance of the publish: which chat, which bot version, how big the package was.
META_SOURCE = "tisa_source"

#: Fields that define "the same product". Deliberately excludes anything the shop fills in
#: itself (ids, dates) and anything cosmetic (evidence, notes): if the owner edits a price,
#: it is a different product and a duplicate is allowed on purpose.
_CONTENT_KEYS = (
    "title",
    "price",
    "prices",
    "model_prices",
    "wholesale_price",
    "wholesale_model_prices",
    "sku_prefix",
    "models",
    "attributes",
    "categories",
    "model_colors",
    "stock_matrix",
    "variations",
    "summary",
    "description",
    "brand",
    "stock",
    "stock_status",
    "sale_price",
)


def _content(data: dict[str, Any]) -> str:
    """The canonical JSON of everything that makes this product *this* product."""
    picked: dict[str, Any] = {}
    for key in _CONTENT_KEYS:
        value = data.get(key)
        if value in (None, "", [], {}, 0):
            continue
        picked[key] = value
    return json.dumps(picked, ensure_ascii=False, sort_keys=True, default=str)


def batch_id(
    data: dict[str, Any],
    image_paths: Sequence[Path] | None = (),
    *,
    chat_id: int | str | None = None,
) -> str:
    """12 hex characters identifying this exact package.

    The chat is part of the signature on purpose: two admins publishing an
    identical-looking product in their own chats are two intentions, not one, and
    blocking the second one because the first succeeded would be its own bug.
    """
    images = []
    for path in image_paths or ():
        try:
            size = path.stat().st_size
        except OSError:
            size = -1
        images.append(f"{path.name}:{size}")
    payload = json.dumps(
        {"content": _content(data), "images": sorted(images), "chat": str(chat_id or "")},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def source_meta(
    batch: str,
    *,
    chat_id: int | str | None = None,
    thread_id: int | str | None = None,
    images: int = 0,
    variations: int = 0,
    bot_version: str = "",
) -> list[dict[str, str]]:
    """The ``meta_data`` entries to send with the product (plan item 4.7).

    Small and boring on purpose: enough to answer «این محصول از کدام پیام‌ها آمد و با
    چه نسخه‌ای ساخته شد» two months later, without copying the chat into the shop.
    """
    source = {
        "chat_id": chat_id,
        "thread_id": thread_id,
        "images": int(images or 0),
        "variations": int(variations or 0),
        "bot_version": bot_version,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    return [
        {"key": META_BATCH, "value": batch},
        {"key": META_SOURCE, "value": json.dumps(source, ensure_ascii=False, sort_keys=True)},
    ]


def meta_batch_of(product: dict[str, Any]) -> str:
    """The batch id stored on a product returned by the REST API ("" when absent)."""
    for item in product.get("meta_data") or []:
        if isinstance(item, dict) and str(item.get("key")) == META_BATCH:
            return str(item.get("value") or "")
    return ""
