"""SKU policy for the shop: local high-water mark → plugin → catalog scan (plan 4.4).

Split out of :mod:`bot.services.woocommerce_direct` because "what number may this shop use
next" is a rule of its own, with three sources of truth (the optional `next-sku` plugin,
the catalog scan, and the store's own insert-time rejection) and one hostile special case:
ghost rows in ``wc_product_meta_lookup`` that no API can see. Keeping it here also means
the dry-run fake transport and the writer agree on the same set of requests without either
side importing the other.

Nothing in this module creates or mutates a product; :func:`next_free` only *chooses* a
string. The collision handling that has to re-POST lives in the writer
(:func:`bot.services.woocommerce_direct.create_draft`), because only it knows whether a
product was already created in this attempt.

The high-water mark per prefix is remembered in ``data/sku_state.json``. A fresh local hint
avoids both the plugin request and the catalog walk. A stale hint is safe: WooCommerce's
product POST enforces uniqueness, and the writer advances after a duplicate response instead
of sending a redundant preflight GET that cannot reserve the number.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from bot.config import data_dir, settings
from bot.services import jsonstore
from bot.services.woo_client import Sink, WooClient, WooCommerceAPIError, app_password, check

#: How many times the writer may bump the SKU number before giving up.
MAX_SKU_RETRIES = 100

#: A sane ceiling for how far past the suggested SKU a ghost block may extend.
#: When every candidate up to this ceiling collides, the store is not hitting a
#: finite ghost block — something else is rejecting product creation (broken
#: lookup table, a plugin/WAF, or lost write permission).
MAX_GHOST_SPAN = 100_000

#: The plugin's own REST route (a WordPress route, not ``wc/v3``). It lives here so the
#: writer and the 🩺 diagnostic probe the *same* string: a renamed route must never be
#: able to make the diagnostic say «نصب است» while the writer gets a 404.
PLUGIN_ROUTE = "/wp-json/wcspb/v1/next-sku"


DATA_DIR = data_dir()
STATE_FILE = DATA_DIR / "sku_state.json"

#: How long a remembered high-water mark may be trusted. Long enough to cover a run of
#: products entered one after another, short enough that an SKU added by hand in the WP
#: admin is not ignored forever.
CACHE_TTL_SECONDS = 600


def _read_state() -> dict[str, dict[str, Any]]:
    state = jsonstore.read_json(STATE_FILE, {}) or {}
    prefixes = state.get("prefixes") if isinstance(state, dict) else None
    return prefixes if isinstance(prefixes, dict) else {}


def cached_max(prefix: str) -> int | None:
    """The last number this bot used for ``prefix``, while the hint is fresh."""
    entry = _read_state().get(prefix)
    if not isinstance(entry, dict):
        return None
    age = time.time() - float(entry.get("ts") or 0)
    if age > CACHE_TTL_SECONDS:
        return None
    value = entry.get("max")
    return int(value) if isinstance(value, (int, float)) and int(value) > 0 else None


def remember(prefix: str, last_used: int) -> None:
    """Record the highest number used for ``prefix`` (monotonic, never walks backwards).

    Written even when the create later fails: the number only says "start probing at
    least this high", and skipping a free SKU is harmless while reusing a taken one is not.
    """
    if not prefix or last_used <= 0:
        return
    with jsonstore.lock_for(STATE_FILE):
        state = jsonstore.read_json(STATE_FILE, {}) or {}
        if not isinstance(state, dict) or state.get("version") != 1:
            state = {"version": 1, "prefixes": {}}
        prefixes = state.setdefault("prefixes", {})
        entry = prefixes.get(prefix) if isinstance(prefixes.get(prefix), dict) else {}
        previous = int(entry.get("max") or 0) if entry else 0
        prefixes[prefix] = {"max": max(previous, int(last_used)), "ts": time.time()}
        jsonstore.write_json(STATE_FILE, state)


# Test-only: clears the per-prefix counter so two tests cannot inherit each other's state.
def forget_for_tests() -> None:
    """Drop the cache (used by the suite and by «history is wrong, rescan» debugging)."""
    jsonstore.invalidate(STATE_FILE)
    Path(STATE_FILE).unlink(missing_ok=True)


def is_collision(message: str) -> bool:
    """True when a WooCommerce 400 means "this SKU is already taken".

    Matches both the standard "Invalid or duplicated SKU." and WooCommerce's
    "already present in the lookup table" error, which fires when a deleted
    (trashed) product left a ghost SKU row in ``wc_product_meta_lookup``.
    """
    lowered = (message or "").casefold()
    if "lookup table" in lowered:
        return True
    if "sku" not in lowered:
        return False
    return any(token in lowered for token in ("duplicate", "duplicated", "already", "present", "exists"))


def number(value: str, prefix: str) -> int | None:
    """Return the numeric suffix of a SKU like ``BO147`` / ``BO-147`` / ``BO 147``."""
    match = re.fullmatch(re.escape(prefix) + r"[\s._-]*(\d+)\s*", value, re.I)
    return int(match.group(1)) if match else None


async def from_plugin(client: WooClient, prefix: str, audit: Sink) -> str | None:
    """Ask the SKU-prefix plugin for the next SKU; ``None`` means "not available".

    The plugin is optional — if its route is missing (404) or the Application Password
    can't use it (401/403), we fall back to scanning the catalog instead of aborting the
    whole product-creation flow. Note this is a WordPress REST route, not a ``wc/v3`` one:
    it is authenticated with the application password (``basic=True``), because a
    WooCommerce consumer key cannot satisfy ``current_user_can('edit_products')`` — and the
    secrets must never be put in that URL or in an error log.
    """
    if not prefix or app_password() is None:
        audit.log("[sku] افزونهٔ next-sku بررسی نشد (اطلاعات WordPress ناقص یا پیشوند خالی).")
        return None
    endpoint = f"{settings.wordpress_url.rstrip('/')}{PLUGIN_ROUTE}"
    response = await client.get(endpoint, params={"prefix": prefix}, basic=True)
    if response.status_code in (401, 403, 404):
        audit.log(f"[sku] افزونهٔ next-sku در دسترس نیست (HTTP {response.status_code})؛ به اسکن دستی می‌رویم.")
        return None
    check(response)
    value = response.json().get("sku")
    sku = str(value).strip() if value else None
    audit.log(f"[sku] افزونهٔ next-sku پاسخ داد: «{sku or '(خالی)'}»")
    return sku


async def exists(client: WooClient, base: str, sku: str, include_trash: bool = False) -> bool:
    """True if a product with this exact SKU already exists in the store.

    By default this is a single, cheap request. A trashed product keeps its SKU
    in WooCommerce's ``wc_product_meta_lookup`` table and would reject a new
    product with the same SKU, but the POST retry loop catches that regardless,
    so the extra Trash request is opt-in via ``include_trash`` and only used for
    the diagnostic log — keeping the hot probing path fast.
    """
    statuses = (None, "trash") if include_trash else (None,)
    for status in statuses:
        params: dict[str, str | int] = {"sku": sku, "per_page": 1}
        if status:
            params["status"] = status
        response = await client.get(base, params=params)
        if response.status_code == 400:
            # The store does not support this filter scope; assume it is free.
            return False
        check(response)
        if response.json():
            return True
    return False


async def scan_max(client: WooClient, base: str, prefix: str, audit: Sink) -> int:
    """Find the highest numeric suffix for ``prefix`` among existing SKUs.

    Tries WooCommerce's `search` first (cheap, but many stores only search
    titles), then walks the catalog newest-first — SKU numbers grow over time,
    so the newest products hold the highest suffixes. The exact-SKU collision
    check in :func:`next_free` covers anything a truncated scan misses.
    """

    def scan(items: list[dict[str, Any]]) -> int:
        top = 0
        for item in items:
            value = number(str(item.get("sku", "")), prefix)
            if value:
                top = max(top, value)
        return top

    maximum = 0
    search_pages = 0
    for page in range(1, 51):
        response = await client.get(base, params={"search": prefix, "per_page": 100, "page": page})
        if response.status_code == 400:
            break
        check(response)
        items = response.json()
        search_pages = page
        maximum = max(maximum, scan(items))
        if len(items) < 100:
            break
    audit.log(f"[sku] اسکن با search=«{prefix}»: {search_pages} صفحه، بیشترین شماره={maximum}")

    if maximum == 0:
        full_pages = 0
        for page in range(1, 51):
            response = await client.get(
                base, params={"per_page": 100, "page": page, "orderby": "id", "order": "desc"}
            )
            if response.status_code == 400 or not response.json():
                break
            check(response)
            items = response.json()
            full_pages = page
            maximum = max(maximum, scan(items))
            if len(items) < 100:
                break
        audit.log(f"[sku] اسکن کامل کاتالوگ (جدید→قدیم): {full_pages} صفحه، بیشترین شماره={maximum}")
    return maximum


async def next_free(
    client: WooClient,
    base: str,
    prefix: str,
    audit: Sink,
    *,
    verify_candidate: bool = True,
) -> str:
    """Choose a starting SKU for ``prefix`` (e.g. BO -> BO148).

    A fresh local high-water mark is cheapest, followed by the optional site plugin and a
    catalog scan. ``verify_candidate`` is retained for diagnostic callers; the product writer
    leaves it off because a standalone GET cannot reserve a SKU. WooCommerce's product POST
    is the final uniqueness check and its bounded collision retry advances stale hints safely.
    """
    prefix = (prefix or "").strip().upper()
    if not prefix:
        audit.log("[sku] پیشوند SKU خالی است؛ بدون SKU ادامه می‌دهیم.")
        return ""

    start = 0
    cached = cached_max(prefix)
    if cached:
        start = cached + 1
        audit.log(f"[sku] کش محلی: آخرین {prefix}{cached}؛ ادامه از {prefix}{start} (بدون API خواندنی)")
    else:
        plugin_sku = await from_plugin(client, prefix, audit)
        if plugin_sku:
            start = number(plugin_sku, prefix) or 0
    if not start:
        start = await scan_max(client, base, prefix, audit) + 1
        audit.log(f"[sku] شروع جستجو از: {prefix}{start}")

    if not verify_candidate:
        candidate = f"{prefix}{start}"
        remember(prefix, start)
        audit.log(f"[sku] کاندید «{candidate}» انتخاب شد؛ یکتایی هنگام POST ووکامرس کنترل می‌شود.")
        return candidate

    for attempt in range(200):
        candidate = f"{prefix}{start + attempt}"
        if not await exists(client, base, candidate):
            audit.log(f"[sku] SKU کاندید آزاد است (API): {candidate}")
            remember(prefix, start + attempt)
            return candidate
        audit.log(f"[sku] SKU کاندید اشغال است (API): {candidate}")
    raise WooCommerceAPIError(500, f"یافتن SKU آزاد برای پیشوند «{prefix}» ممکن نشد.")
