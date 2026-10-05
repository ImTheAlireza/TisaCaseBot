"""Direct WooCommerce draft creation through Woo REST + WordPress Media API."""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
import time
from urllib.parse import quote
from pathlib import Path
from typing import Any
from collections.abc import Sequence

import httpx

from bot.config import settings
from bot.services import metrics, pricing, publish_batch, publish_lease
from bot.services import woo_fencing
from bot.services.image_tags import colors_for_file
from bot.services.color_matrix import is_color_attribute
from bot.services.woo_contract import human_fields, field_errors, json_body, positive_id
from bot.services.color_matrix import build_combinations
from bot.services.plan import VariationPlan, plan_from_dict
from bot.services.sku import (
    MAX_GHOST_SPAN,
    MAX_SKU_RETRIES,
    is_collision as is_sku_collision,
    next_free as next_sku,
    number as sku_number,
    remember as remember_sku,
)
from bot.services.woo_client import (
    Audit,
    Sink,
    WooClient,
    WooCommerceAPIError,
    body_snippet,
    check,
    describe_exception,
    error_message,
    media_base,
    products_base,
)

logger = logging.getLogger(__name__)

# A publish uses one in-flight shop request at a time and leaves a small gap between
# very fast responses. Normal shared-host latency is already much longer than this;
# the limit mainly prevents concurrent image/variation fallbacks from bursting.
PUBLISH_MIN_REQUEST_INTERVAL_SECONDS = 0.35
_CATEGORY_CACHE_TTL_SECONDS = 600
_CATEGORY_ID_CACHE: dict[tuple[str, int, str], tuple[float, int]] = {}


# Test-only: drops the category-name cache between two shops/tests.
def clear_category_cache() -> None:
    """Clear the short-lived category-ID cache (also useful for isolated tests)."""
    _CATEGORY_ID_CACHE.clear()


# WooCommerceAPIError is raised here and caught by bot.modules.product_flow, which imports it
# from this module; it lives in :mod:`bot.services.woo_client` because every HTTP layer failure
# — not just this one — is reported with it.
__all__ = [
    "PUBLISH_MIN_REQUEST_INTERVAL_SECONDS",
    "WooCommerceAPIError",
    "create_draft",
    "images_by_color",
    "product_description",
    "upload_images",
]

def product_description(data: dict[str, Any]) -> str:
    """Return the controlled WooCommerce description for this product."""
    prefix = str(data.get("sku_prefix", "")).strip().upper()
    title = str(data.get("title", "")).casefold()
    if prefix in {"CH", "SB"}:
        return (
            '<p>برای مشاهده محصولات چاپی بیشتر به سایت '
            '<strong><a href="https://TISACHAP.COM">TISACHAP.COM</a></strong> '
            'مراجعه کنید.</p>'
            '<p>آماده سازی و تولید محصولات چاپی 7 تا 18 روزکاری زمان بر خواهد بود؛ '
            'از صبوری شما متشکریم</p>'
        )
    if "قاب" in title:
        return (
            '<p><strong>⚠️ توجه: تصاویر صرفاً برای نمایش رنگ و طرح محصول هستند. '
            'ظاهر نهایی قاب (گرد یا تخت بودن لبه‌ها، میزان برجستگی محافظ دوربین، '
            'محل دکمه‌ها و...) متناسب با مدل گوشی انتخابی شما تولید و ارسال می‌شود</strong></p>'
        )
    return ""


def _price_for_model(
    model: str,
    common: int,
    prices: dict[str, int],
    model_prices: dict[str, int] | None = None,
) -> int:
    """Backwards-compatible wrapper around the shared resolver.

    ``bot.services.pricing`` is also what the preview and validation read, so the
    number on the card is the number in the payload.
    """
    return pricing.price_for_model(model, common, prices, model_prices)


def _clean_options(values: Sequence[Any]) -> list[str]:
    """Deduplicate options, drop empties, and normalize whitespace."""
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _attributes(data: dict[str, Any]) -> list[dict[str, Any]]:
    """The attribute axes for WooCommerce — delegated to :mod:`bot.services.plan`.

    The preview, the REST payload and the ZIP manifest all come from
    ``plan.build_plan`` now, so the number shown in Telegram is the number of
    variations that will exist. Keeping this function as a thin wrapper means the
    existing tests (and any other caller) keep working.
    """
    return plan_from_dict(data).woo_attributes()


def _combinations(attrs: list[dict[str, Any]], restrictions: dict[str, list[str]] | None = None) -> list[dict[str, str]]:
    """Every attribute combination that is actually sellable.

    The full cartesian product minus the model↔color pairs the seller did not
    list: the رنگ attribute still shows every color, but iPhone 17 Pro only
    gets a variation for the colors that exist for it.
    """
    pairs = [(str(attr["name"]), list(attr["options"])) for attr in attrs]
    return build_combinations(pairs, restrictions or {})


# Test-only: exposes the restriction mapping the preview builder reads.
def _model_color_restrictions(data: dict[str, Any]) -> dict[str, list[str]]:
    """Read ``model_colors`` from the product data, defensively cleaned."""
    raw = data.get("model_colors") or {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[str]] = {}
    for model, colors in raw.items():
        if not isinstance(colors, (list, tuple)):
            continue
        values = _clean_options(colors)
        if values:
            out[str(model)] = values
    return out


def _media_type(path: Path, head: bytes) -> str:
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"BM"):
        return "image/bmp"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


async def _verify_write(
    client: WooClient, endpoint: str, sent: dict[str, Any], response: httpx.Response,
    *, variation: bool = False, expected_id: int | None = None,
) -> dict[str, Any]:
    check(response)
    got = json_body(response)
    identity = expected_id or positive_id(got)
    errors = field_errors(sent, got, expected_id=expected_id, variation=variation)
    if errors and identity:
        reread = await client.get(f"{endpoint}/{identity}")
        if reread.is_success:
            got = json_body(reread)
            errors = field_errors(sent, got, expected_id=identity, variation=variation)
    if errors:
        raise WooCommerceAPIError(502, "نوشتن تأیید نشد؛ پاسخ فروشگاه ناقص/متفاوت بود: " + human_fields(errors))
    return got


async def _delete_confirmed(client: WooClient, endpoint: str, *, basic: bool = False) -> bool:
    response = await client.delete(endpoint, params={"force": "true"}, basic=basic)
    if response.status_code in (404, 410):
        return True
    body = json_body(response)
    if not response.is_success or not isinstance(body, dict) or body.get("error"):
        return False
    identity = int(endpoint.rsplit("/", 1)[-1])
    return body.get("deleted") is True or positive_id(body) == identity


async def _upload_media(client: WooClient, path: Path, audit: Sink) -> int:
    # HTTP headers are ASCII, so the seller's own file name goes out percent-encoded in the
    # RFC 5987 field; an ASCII ``filename=`` stays as the fallback for servers that ignore it.
    # Sending the raw name used to raise UnicodeEncodeError inside publish — for a document
    # called «قاب‌مشکی.jpg» that meant a red card with nothing wrong in the product.
    ascii_name = path.name.encode("ascii", "ignore").decode().strip() or "image.jpg"
    image_bytes = await asyncio.to_thread(path.read_bytes)
    content_type = _media_type(path, image_bytes[:32])
    audit.log(f"[media:start] آپلود {path.name}؛ حجم {len(image_bytes):,} بایت")
    try:
        response = await client.post(
            media_base(),
            content=image_bytes,
            basic=True,
            headers={
                "Content-Type": content_type,
                "Content-Disposition": (
                    f'attachment; filename="{ascii_name}"; '
                    f"filename*=UTF-8''{quote(path.name)}"
                ),
            },
        )
    except httpx.TransportError as exc:
        audit.log(f"[media:error] آپلود {path.name} ({len(image_bytes):,} بایت): {describe_exception(exc)}")
        raise
    if not response.is_success:
        audit.log(f"[media] آپلود {path.name} ناموفق: HTTP {response.status_code}: {error_message(response)} | body={body_snippet(response)}")
    check(response)
    media_id = positive_id(json_body(response))
    if not media_id:
        raise WooCommerceAPIError(502, "آپلود تصویر تأیید نشد؛ پاسخ JSON با media id مثبت انتظار می‌رفت.")
    metrics.incr_shop("images_uploaded")
    audit.log(f"[media] آپلود شد: {path.name} → media id {media_id}")
    return media_id


async def _upload_media_many(client: WooClient, paths: list[Path], audit: Sink) -> list[tuple[int, Path]]:
    """Upload images one at a time, keeping each ``(media id, source file)`` pair.

    The pair, not just the id, because a seller who names the file after the colour
    («01_مشکی.jpg») has already done the mapping work: the variation of that colour gets
    that picture. Serial uploads also avoid a burst of WordPress media writes on shared hosts.
    """
    uploads: list[tuple[int, Path]] = []
    try:
        for path in paths:
            uploads.append((await _upload_media(client, path, audit), path))
        return uploads
    except BaseException:
        # No parent exists yet. Only these positively identified, unattached
        # uploads are safe to remove; an ambiguous upload cannot be guessed.
        for media_id, _path in uploads:
            try:
                deleted = await _delete_confirmed(client, f"{media_base()}/{media_id}", basic=True)
                audit.log(f"[media:cleanup] media {media_id}: {'حذف تأیید شد' if deleted else 'حذف تأیید نشد'}")
            except Exception as exc:
                audit.log(f"[media:cleanup] media {media_id}: {describe_exception(exc)}")
        raise


def _images_by_color(uploads: list[tuple[int, Path]], colors: Sequence[str]) -> dict[str, int]:
    """``colour -> media id`` for files whose name says that colour. Nothing else."""
    out: dict[str, int] = {}
    for media_id, path in uploads:
        for color in colors_for_file(path, colors):
            out.setdefault(color, media_id)

    return out


# The two media helpers below are what an *update* needs as well (see bot.services.update_apply):
# one upload policy, one colour-by-file-name rule. Public names, same functions.
async def upload_images(client: WooClient, paths: list[Path], audit: Sink) -> list[tuple[int, Path]]:
    """Upload ``paths`` one at a time; ``(media id, source file)`` per image, in order."""
    return await _upload_media_many(client, paths, audit)


def images_by_color(uploads: list[tuple[int, Path]], colors: Sequence[str]) -> dict[str, int]:
    """``colour -> media id`` for uploads whose file name says that colour."""
    return _images_by_color(uploads, colors)


async def _create_without_sku_then_set(
    client: WooClient, base: str, payload: dict[str, Any], sku: str, audit: Sink
) -> httpx.Response | None:
    """Bypass the broken WooCommerce SKU lock: create without SKU, then update.

    WooCommerce only takes the ``wc_product_meta_lookup`` SKU lock inside the
    product data store's ``create()`` path, and only for REST requests that
    carry a non-empty SKU. When that lock INSERT fails (a known WooCommerce bug,
    see issue #57312), every REST create with a SKU is rejected with "already
    present in the lookup table" even though the SKU is genuinely free.

    Creating without a SKU skips the lock entirely, and the follow-up PUT runs
    through ``update()`` + ``set_sku()`` — the ordinary uniqueness check, which
    works. Returns the update response on success, or ``None`` after logging
    (and deleting the temporary no-SKU draft) when it fails, so the caller can
    fall back to the normal bump/jump loop.
    """
    no_sku_payload = {key: value for key, value in payload.items() if key != "sku"}
    response = await client.post(base, json=no_sku_payload)
    if not response.is_success:
        audit.log(
            f"[sku] دور زدن قفل SKU: ساخت بدون SKU ناموفق بود (HTTP {response.status_code}: "
            f"{error_message(response)} | body={body_snippet(response)})."
        )
        return None
    product = await _verify_write(client, base, no_sku_payload, response)
    product_id = positive_id(product)
    audit.log(f"[sku] دور زدن قفل SKU: محصول بدون SKU ساخته شد (id={product_id})؛ اکنون SKU را ثبت می‌کنیم.")
    response = await client.put(f"{base}/{product_id}", json=payload)
    if not response.is_success:
        audit.log(
            f"[sku] ثبت SKU با به‌روزرسانی ناموفق بود (HTTP {response.status_code}: "
            f"{error_message(response)} | body={body_snippet(response)})."
        )
        if any(row.get("key") == publish_batch.META_BATCH for row in payload.get("meta_data", [])):
            raise WooCommerceAPIError(409, f"پیش‌نویس {product_id} بدون SKU باقی ماند؛ ابتدا SKU را در سایت بررسی کن (محصول مشترک حذف نشد)")
        try:
            deleted = await _delete_confirmed(client, f"{base}/{product_id}")
            audit.log(f"[sku] حذف پیش‌نویس موقت {product_id}: {deleted}")
            if not deleted:
                raise WooCommerceAPIError(502, "پیش‌نویس بدون SKU باقی مانده است؛ ساخت دیگری مجاز نیست.")
        except Exception as exc:
            raise WooCommerceAPIError(502, f"حذف پیش‌نویس موقت {product_id} تأیید نشد؛ ساخت متوقف شد.") from exc
        return None
    await _verify_write(client, base, payload, response, expected_id=product_id)
    audit.log(f"[sku] SKU «{sku}» با به‌روزرسانی روی محصول {product_id} ثبت شد.")
    return response


async def _create_with_sku_retry(
    client: WooClient, base: str, payload: dict[str, Any], prefix: str, sku: str, audit: Sink
) -> httpx.Response:
    """POST the product, bumping the SKU if WooCommerce reports a collision.

    A "free" SKU can still be rejected at insert time: a deleted product may
    have left a ghost row in WooCommerce's ``wc_product_meta_lookup`` table
    that is invisible to the REST product list, the Trash, and even the
    "Regenerate lookup tables" tool. The retry strategy walks linearly for the
    first few collisions (so single ghost rows cost a single SKU) and then
    switches to doubling jumps, which escapes large contiguous ghost blocks in
    O(log n) attempts instead of failing after a fixed budget.
    """
    number = sku_number(sku, prefix) if (sku and prefix) else None
    candidate_num = number if number is not None else 1
    start_num = candidate_num
    ceiling = start_num + MAX_GHOST_SPAN
    last_sku = sku or ""
    jump = 1
    linear_attempts = 5
    hit_ceiling = False
    tried_lock_fallback = False

    for attempt in range(1, MAX_SKU_RETRIES + 1):
        if prefix and candidate_num > ceiling:
            hit_ceiling = True
            audit.log(
                f"[sku] توقف: کاندید از سقف معقول «{prefix}{ceiling}» گذشت؛ "
                f"این دیگر بلوک شبح عادی نیست و پرش بیشتر بی‌فایده است."
            )
            break
        candidate = f"{prefix}{candidate_num}" if prefix else None
        if candidate:
            payload["sku"] = candidate
            last_sku = candidate
        response = await client.post(base, json=payload)
        existing_id = woo_fencing.conflict_id(json_body(response), "tisa_batch_exists", "existing_product_id")
        if existing_id:
            raise woo_fencing.ExistingBatch(existing_id)
        if response.is_success:
            await _verify_write(client, base, payload, response)
            if prefix:
                remember_sku(prefix, candidate_num)
            audit.log(f"[attempt {attempt}] POST موفق با SKU «{candidate}» → HTTP {response.status_code}")
            return response
        message = error_message(response)
        audit.log(
            f"[attempt {attempt}] POST با SKU «{candidate}» → HTTP {response.status_code}: {message} "
            f"| body={body_snippet(response)}"
        )
        if response.status_code == 400 and prefix and is_sku_collision(message):
            metrics.incr_shop("sku_collisions")
            # The SKU lock (obtain_lock_on_sku_for_concurrent_requests) can fail
            # spuriously and reject every SKU. Try the no-SKU bypass exactly once
            # on the first "lookup table" collision; if it works we are done.
            if not tried_lock_fallback and "lookup table" in (message or "").casefold():
                tried_lock_fallback = True
                fallback_response = await _create_without_sku_then_set(
                    client, base, payload, candidate or last_sku, audit
                )
                if fallback_response is not None:
                    return fallback_response
            if attempt <= linear_attempts:
                # The POST response is authoritative. A follow-up product/Trash read only
                # labelled the collision; it did not make the next candidate safer.
                candidate_num += 1
                audit.log(f"[sku] برخورد SKU در زمان ساخت؛ ادامه از «{prefix}{candidate_num}» بدون درخواست تشخیصی.")
            else:
                jump *= 2
                candidate_num += jump
                audit.log(f"[sku] عبور از بلوک رکوردهای شبح: پرش +{jump} → کاندید بعدی {prefix}{candidate_num}")
            if prefix:
                remember_sku(prefix, candidate_num - 1)
            continue
        check(response)

    if hit_ceiling:
        raise WooCommerceAPIError(
            400,
            f"ووکامرس همهٔ SKUها از «{prefix}{start_num}» تا «{last_sku}» را اشغال می‌داند "
            f"(بیش از {MAX_GHOST_SPAN:,} عدد پشت‌سرهم). این «رکورد شبح» عادی نیست و پاک‌سازی جدول lookup حلش نمی‌کند.\n\n"
            "این علامتِ باگِ شناخته‌شدهٔ «قفل SKU» ووکامرس است (obtain_lock_on_sku_for_concurrent_requests، از WC 9.3): "
            "INSERT قفل برای هر SKU شکست می‌خورد و همهٔ ساخت‌های REST را با «already present in the lookup table» رد می‌کند "
            "هرچند SKU واقعاً آزاد است.\n\n"
            "راه‌حل (در سرور وردپرس، نه ربات): یک فایل mu-plugin بساز تا این قفل معیوب را دور بزند:\n\n"
            "فایل wp-content/mu-plugins/disable-sku-lock.php:\n"
            "<?php\n/**\n * Plugin Name: Disable WC SKU lock\n */\nadd_filter( 'wc_product_pre_lock_on_sku', '__return_true', 10 );\n\n"
            "یا اگر ووکامرس 9.7.x / 9.8.x است، آن را به آخرین نسخه به‌روزرسانی کن (باگ در نسخه‌های بعدی رفع شده است).\n\n"
            "برای دیدن خطای دقیق MySQL، در لاگ‌های ووکامرس (WooCommerce → Status → Logs یا پوشهٔ wp-content/uploads/wc-logs) دنبال "
            "عبارت «Failed to obtain SKU lock» بگرد؛ فیلد error همان پیام واقعی دیتابیس است.",
        )

    raise WooCommerceAPIError(
        400,
        f"ربات {MAX_SKU_RETRIES} تلاش برای یافتن SKU آزاد انجام داد اما همه در جدول lookup ووکامرس اشغال بودند "
        f"(آخرین مورد: «{last_sku}»). این «رکوردهای شبح» متعلق به محصولاتی هستند که حذف شده‌اند ولی ردیف SKU آن‌ها "
        "در جدول wc_product_meta_lookup باقی مانده است. این رکوردها از هیچ API دیده نمی‌شوند و Regenerate یا خالی کردن "
        "زباله‌دان هم طبق باگ شناخته‌شدهٔ ووکامرس آن‌ها را پاک نمی‌کند.\n\n"
        "راه‌حل کم‌خطر: فقط رکوردهای شبح حذف می‌شوند؛ به محصولات واقعی دست زده نمی‌شود "
        "(پیشوند wp_ را با پیشوند واقعی جدول‌هایت جایگزین کن و قبلش بکاپ بگیر):\n\n"
        "۱) اول پیش‌نمایش — این کوئری چیزی حذف نمی‌کند:\n"
        "SELECT l.product_id, l.sku, p.post_type, p.post_status, p.post_parent, pp.post_status AS parent_status\n"
        "FROM wp_wc_product_meta_lookup l\n"
        "LEFT JOIN wp_posts p ON p.ID = l.product_id\n"
        "LEFT JOIN wp_posts pp ON pp.ID = p.post_parent\n"
        "WHERE p.ID IS NULL\n"
        "   OR p.post_type NOT IN ('product','product_variation')\n"
        "   OR p.post_status IN ('trash','auto-draft')\n"
        "   OR (p.post_type = 'product_variation' AND (pp.ID IS NULL OR pp.post_type <> 'product' OR pp.post_status IN ('trash','auto-draft')));\n\n"
        "۲) بعد حذفِ دقیقاً همان ردیف‌ها:\n"
        "DELETE l\n"
        "FROM wp_wc_product_meta_lookup l\n"
        "LEFT JOIN wp_posts p ON p.ID = l.product_id\n"
        "LEFT JOIN wp_posts pp ON pp.ID = p.post_parent\n"
        "WHERE p.ID IS NULL\n"
        "   OR p.post_type NOT IN ('product','product_variation')\n"
        "   OR p.post_status IN ('trash','auto-draft')\n"
        "   OR (p.post_type = 'product_variation' AND (pp.ID IS NULL OR pp.post_type <> 'product' OR pp.post_status IN ('trash','auto-draft')));\n\n"
        "این DELETE فقط ردیف‌هایی را حذف می‌کند که به یک محصول/وارییشن زنده اشاره ندارند؛ "
        "محصولات منتشرشده، پیش‌نویس و خصوصی و وارییشن‌های سالم دست‌نخورده می‌مانند.",
    )


async def _create_variations_individually(
    client: WooClient, base: str, product_id: int, payloads: list[dict[str, Any]], audit: Sink
) -> None:
    """Fallback: create variations one at a time if the batch route is unavailable."""
    semaphore = asyncio.Semaphore(1)

    async def one(payload: dict[str, Any]) -> None:
        async with semaphore:
            combination = " / ".join(
                f"{item.get('name')}={item.get('option')}"
                for item in payload.get("attributes", [])
                if isinstance(item, dict)
            ) or "ترکیب نامشخص"
            audit.log(f"[variation:start] محصول {product_id}؛ {combination}")
            try:
                response = await client.post(
                    f"{base}/{product_id}/variations",
                    json=payload
                )
            except httpx.TransportError as exc:
                audit.log(f"[variation:error] محصول {product_id}؛ {combination}: {describe_exception(exc)}")
                raise
            if not response.is_success:
                audit.log(
                    f"[variation] ساخت variation ناموفق: HTTP {response.status_code}: "
                    f"{error_message(response)} | body={body_snippet(response)}"
                )
            recovered = await woo_fencing.recover_variation(client, f"{base}/{product_id}/variations", payload, json_body(response))
            if recovered is None:
                await _verify_write(client, f"{base}/{product_id}/variations", payload, response, variation=True)

    for payload in payloads:
        await one(payload)
    audit.log(f"[variation] {len(payloads)} variation ساخته شد (تکی موازی).")


async def _find_resumable(
    client: WooClient, base: str, title: str, batch_id: str, audit: Sink
) -> dict[str, Any] | None:
    """Did an earlier attempt already create this product? Find it instead of doubling it.

    The store cannot filter products by meta, so the hunt is: search the title (ours, and
    WooCommerce does search titles), then read *our own* ``tisa_batch_id`` back off each
    hit. A hit is therefore this exact publish — not merely a similar product — which is
    the only property that makes resuming safe instead of lucky.
    """
    if not batch_id:
        return None
    params: dict[str, Any] = {
        "tisa_batch_id": batch_id, "status": "any",
        "per_page": 100, "orderby": "date", "order": "desc",
    }
    for page in range(1, 51):
        params["page"] = page
        response = await client.get(base, params=params)
        if response.status_code == 400 and "status" in params:
            params.pop("status")
            response = await client.get(base, params=params)
        check(response)
        items = json_body(response)
        if not isinstance(items, list) or any(not positive_id(row) or row.get("error") for row in items):
            raise WooCommerceAPIError(502, "جستجوی تلاش قبلی پاسخ معتبر نداد؛ برای جلوگیری از محصول تکراری ساخت متوقف شد.")
        matches = [row for row in items if publish_batch.meta_batch_of(row) == batch_id]
        if len(matches) > 1:
            raise WooCommerceAPIError(409, "چند محصول با شناسهٔ این تلاش وجود دارد؛ انتخاب خودکار ایمن نیست.")
        if matches:
            return matches[0]
        total_pages = _total_pages(response)
        if (total_pages is not None and page >= total_pages) or (total_pages is None and len(items) < 100):
            audit.log(f"[resume] {page} صفحه بررسی شد؛ تلاش {batch_id} قبلاً ساخته نشده است.")
            return None
    raise WooCommerceAPIError(503, "جستجوی تلاش قبلی از سقف ایمن گذشت؛ نبودن محصول ثابت نشد و ساخت انجام نشد.")


def _total_pages(response: httpx.Response) -> int | None:
    raw = response.headers.get("X-WP-TotalPages", "")
    return int(raw) if re.fullmatch(r"[0-9]+", raw) else None


async def _existing_combos(
    client: WooClient, base: str, product_id: int, audit: Sink
) -> list[dict[str, Any]] | None:
    """Every variation the product already has (``None`` = endpoint missing).

    Unknown or malformed reads stop the publish. Raising on a real error is deliberate: double variations are not a
    cosmetic problem, they are a product whose price/stock is now ambiguous. A *missing* endpoint (404/405/501) is
    not a read error though: some hosts disable that route while the batch endpoint still works, so the caller
    gets ``None`` and creates the combinations the preview asked for.
    """
    found: list[dict[str, Any]] = []
    page = 1
    while True:
        params: dict[str, Any] = {"per_page": 100, "page": page, "status": "any"}
        response = await client.get(
            f"{base}/{product_id}/variations", params=params
        )
        if response.status_code == 400:
            params.pop("status", None)
            response = await client.get(
                f"{base}/{product_id}/variations", params=params
            )
        if response.status_code in (404, 405, 501):
            audit.log(
                f"[resume] endpoint واریژن‌های قبلی در این فروشگاه نیست (HTTP {response.status_code})؛ "
                "همهٔ ترکیب‌های پیش‌نمایش تازه ساخته می‌شوند."
            )
            return None
        if not response.is_success:
            raise WooCommerceAPIError(
                response.status_code,
                "خواندن واریژن‌های موجود ناموفق بود؛ ساخت دوبارهٔ آن‌ها قیمت/موجودی محصول را "
                "دوپاره می‌کند، پس کار متوقف شد (محصول پاک نشد).",
            )
        items = json_body(response)
        if not isinstance(items, list) or any(not positive_id(row) or not isinstance(row.get("attributes"), list) for row in items):
            raise WooCommerceAPIError(502, "پاسخ واریژن‌های موجود ناقص است؛ ساخت دوباره ممنوع است.")
        found.extend(items)
        total_pages = _total_pages(response)
        if (total_pages is not None and page >= total_pages) or (total_pages is None and len(items) < 100):
            break
        if page >= 100:
            raise WooCommerceAPIError(503, "خواندن واریژن‌ها از سقف ایمن گذشت؛ ساخت متوقف شد.")
        page += 1
    return found


async def _create_variations(
    client: WooClient,
    base: str,
    product_id: int,
    attrs: list[dict[str, Any]],
    common_price: int,
    prices: dict[str, int],
    audit: Sink,
    restrictions: dict[str, list[str]] | None = None,
    combos: list[dict[str, str]] | None = None,
    existing_combos: list[dict[str, Any]] | None = None,
    sale_price: int = 0,
    stock: int | None = None,
    stock_status: str = "",
    images_by_color: dict[str, int] | None = None,
    variation_plan: VariationPlan | None = None,
    model_prices: dict[str, int] | None = None,
) -> None:
    """Create every variation in bulk via the batch endpoint, with a fallback.

    The WooCommerce ``variations/batch`` route builds all variations in a few
    requests (chunked by 100). If the store blocks or lacks that route, we fall
    back to concurrent individual POSTs so behaviour is preserved.

    ``restrictions`` maps a model to the colors that are actually in stock for
    it; combinations outside that list are never created.
    """
    # The plan's combos are authoritative: they are exactly what the preview
    # counted. Recomputing here is only a fallback for direct callers.
    if combos is None:
        combos = _combinations(attrs, restrictions)
    if not combos:
        return
    if restrictions:
        full = _combinations(attrs)
        audit.log(
            f"[variation] ماتریس رنگ هر مدل اعمال شد: {len(combos)} ترکیب معتبر "
            f"از {len(full)} ترکیب کامل ({len(restrictions)} مدل محدود شد)."
        )
    images_by_color = images_by_color or {}
    payloads: list[dict[str, Any]] = []
    for index, combo in enumerate(combos):
        model = combo.get("مدل", "")
        variation: dict[str, Any] = {
            "regular_price": str(_price_for_model(model, common_price, prices, model_prices)),
            "status": "publish",
            # visible + menu_order are what the seller actually judges: a variation that is
            # created but hidden, or listed in hash order instead of the order the message
            # listed the colours in, reads as «۲ تا رنگ گم شده».
            "visible": True,
            "menu_order": index,
            "attributes": [{"name": name, "option": value} for name, value in combo.items()],
            "tisa_fence": True,
        }
        if sale_price:
            variation["sale_price"] = str(sale_price)
        if variation_plan is not None and variation_plan.stock_matrix:
            matrix_quantity = variation_plan.matrix_stock_for(combo)
            if matrix_quantity is None:
                raise ValueError(
                    "برای این ترکیب مقدار ماتریس موجودی پیدا نشد: "
                    + " × ".join(combo.values())
                )
            variation["manage_stock"] = True
            variation["stock_quantity"] = matrix_quantity
            variation["stock_status"] = "instock" if matrix_quantity > 0 else "outofstock"
        else:
            if stock is not None:
                variation["manage_stock"] = True
                variation["stock_quantity"] = stock
            if stock is not None or stock_status:
                variation["stock_status"] = stock_status or "instock"
        image_id = images_by_color.get(str(combo.get("رنگ") or ""))
        if image_id:
            variation["image"] = {"id": image_id}
        if existing_combos is not None:
            wanted_attrs = variation["attributes"]
            matches = [row for row in existing_combos
                       if not field_errors({"attributes": wanted_attrs}, row, variation=True)]
            if len(matches) > 1:
                raise WooCommerceAPIError(409, "ترکیب واریژن تکراری در محصول قبلی هست؛ تکمیل خودکار متوقف شد.")
            if matches:
                # Money and stock decide whether the existing row really is what this
                # preview asked for. ``menu_order``/``visible`` are cosmetics of an
                # earlier run (its combination list may have been shorter) and must not
                # block finishing what that run started.
                substance = {
                    key: value for key, value in variation.items()
                    if key in {"regular_price", "sale_price", "status",
                               "manage_stock", "stock_quantity", "stock_status"}
                }
                errors = field_errors(substance, matches[0], variation=True)
                if errors:
                    raise WooCommerceAPIError(409, "واریژن قبلی با پیش‌نمایش جور نیست: " + "، ".join(errors))
                continue
        payloads.append(variation)

    if existing_combos:
        audit.log(
            f"[variation] {len(combos) - len(payloads)} واریژن از تلاش قبلی موجود بود؛ "
            f"{len(payloads)} ترکیب جاافتاده ساخته می‌شود."
        )
    endpoint = f"{base}/{product_id}/variations/batch"
    created = 0
    failed = 0
    total_chunks = (len(payloads) + 99) // 100
    seen_ids: set[int] = set()
    for start in range(0, len(payloads), 100):
        chunk = payloads[start:start + 100]
        audit.log(
            f"[variation:batch] محصول {product_id}؛ بسته {start // 100 + 1}/{total_chunks}؛ "
            f"{len(chunk)} واریژن (از ردیف {start + 1} تا {start + len(chunk)})"
        )
        response = await client.post(
            endpoint,
            json={"create": chunk}
        )
        if response.status_code in (404, 405, 501):
            audit.log(
                f"[variation] endpoint بچ پشتیبانی نمی‌شود (HTTP {response.status_code})؛ "
                "فقط در این حالت ساخت تکی و سریالی انجام می‌شود."
            )
            await _create_variations_individually(client, base, product_id, payloads[start:], audit)
            return
        if not response.is_success:
            audit.log(
                f"[variation] بچ ناموفق بود (HTTP {response.status_code})؛ "
                "برای جلوگیری از درخواست/ساخت تکراری، ساخت تکی شروع نمی‌شود."
            )
            check(response)
        body = json_body(response)
        items = body.get("create") if isinstance(body, dict) else None
        if not isinstance(items, list) or len(items) != len(chunk):
            raise WooCommerceAPIError(502, "پاسخ بچ کوتاه/نامعتبر است؛ ساخت واریژن‌ها تأیید نشد (POST تکرار نمی‌شود).")
        for sent, got in zip(chunk, items, strict=True):
            recovered = await woo_fencing.recover_variation(client, f"{base}/{product_id}/variations", sent, got)
            if recovered is not None:
                got = recovered
            identity = positive_id(got)
            if not identity or identity in seen_ids or not isinstance(got, dict) or got.get("error"):
                raise WooCommerceAPIError(502, "بچ یک واریژن را رد کرد یا id معتبر/یکتا نداد؛ ساخت کامل تأیید نشد.")
            if field_errors(sent, got, variation=True):
                reread = await client.get(f"{base}/{product_id}/variations/{identity}")
                got = json_body(reread) if reread.is_success else None
                if field_errors(sent, got, expected_id=identity, variation=True):
                    raise WooCommerceAPIError(502, "قیمت/گزینه/موجودی واریژن با مقدار تأییدشده یکی نیست.")
            seen_ids.add(identity)
            created += 1
    audit.log(f"[variation] {created} variation ساخته و تأیید شد (بچ)؛ ناموفق: {failed}")


async def _rollback(
    client: WooClient, base: str, product_id: int, media_ids: list[int], audit: Sink
) -> None:
    """Delete attached media only after deletion of our own parent was confirmed."""
    try:
        deleted = await _delete_confirmed(client, f"{base}/{product_id}")
    except Exception as exc:
        audit.log(f"[rollback] حذف محصول {product_id} ناموفق بود: {describe_exception(exc)}")
        return
    if not deleted:
        audit.log(f"[rollback] حذف محصول {product_id} تأیید نشد؛ تصاویرش حفظ شدند.")
        return
    audit.log(f"[rollback] محصول {product_id} حذف شد (تأییدشده).")
    removed = 0
    for media_id in media_ids or []:
        try:
            if await _delete_confirmed(client, f"{media_base()}/{media_id}", basic=True):
                removed += 1
            else:
                audit.log(f"[rollback] حذف media {media_id} تأیید نشد.")
        except Exception as exc:
            audit.log(f"[rollback] حذف media {media_id} ناموفق بود: {describe_exception(exc)}")
    audit.log(f"[rollback] حذف {removed} از {len(media_ids)} تصویر تأیید شد.")


async def _resolve_categories(client: WooClient, base: str, categories: list[str], audit: Sink) -> list[dict[str, int]]:
    endpoint = f"{base}/categories"
    category_ids: list[dict[str, int]] = []
    seen_ids: set[int] = set()
    # Repeated product categories are stable for long stretches. Keep positive ID matches
    # for ten minutes so adjacent product entries do not repeat the same taxonomy reads.
    # Dry-run IDs are synthetic and must never enter the real-shop cache.
    cacheable = not bool(getattr(client, "dry_run", False))
    now = time.monotonic()
    for key, (saved_at, _identifier) in list(_CATEGORY_ID_CACHE.items()):
        if now - saved_at > _CATEGORY_CACHE_TTL_SECONDS:
            _CATEGORY_ID_CACHE.pop(key, None)
    while len(_CATEGORY_ID_CACHE) > 1024:
        _CATEGORY_ID_CACHE.pop(next(iter(_CATEGORY_ID_CACHE)))
    resolved: dict[tuple[int, str], int | None] = {}
    for raw_path in categories:
        parts = [part.strip() for part in str(raw_path).replace("&gt;", ">").split(">") if part.strip()]
        parent_id = 0
        for part in parts:
            cache_key = (parent_id, part.casefold())
            if cache_key in resolved:
                category_id = resolved[cache_key]
                if category_id is None:
                    break
                if category_id not in seen_ids:
                    category_ids.append({"id": category_id})
                    seen_ids.add(category_id)
                parent_id = category_id
                continue

            global_key = (base, parent_id, part.casefold())
            category_id = None
            cached = _CATEGORY_ID_CACHE.get(global_key) if cacheable else None
            if cached is not None:
                cached_at, cached_id = cached
                if time.monotonic() - cached_at <= _CATEGORY_CACHE_TTL_SECONDS:
                    category_id = cached_id
                    audit.log(f"[cat] کش محلی: «{part}» → {category_id}")
                else:
                    _CATEGORY_ID_CACHE.pop(global_key, None)

            if category_id is None:
                response = await client.get(endpoint, params={"search": part, "per_page": 100})
                if not response.is_success:
                    resolved[cache_key] = None
                    audit.log(f"[cat] جستجوی دستهٔ «{part}» ناموفق: HTTP {response.status_code}")
                    break
                items = json_body(response)
                matches = [
                    item for item in items if isinstance(item, dict)
                    and str(item.get("name", "")).casefold() == part.casefold()
                ] if isinstance(items, list) else []
                exact = next(
                    (item for item in matches if int(item.get("parent", 0)) == parent_id and positive_id(item)),
                    None,
                )
                if exact:
                    category_id = int(exact["id"])
                    if cacheable:
                        _CATEGORY_ID_CACHE[global_key] = (time.monotonic(), category_id)
                else:
                    resolved[cache_key] = None
                    audit.log(f"[cat] دستهٔ «{part}» در فروشگاه پیدا نشد؛ نادیده گرفته شد.")
                    break

            resolved[cache_key] = category_id
            if category_id not in seen_ids:
                category_ids.append({"id": category_id})
                seen_ids.add(category_id)
            parent_id = category_id
    audit.log(f"[cat] دسته‌های نهایی: {[c['id'] for c in category_ids] if category_ids else '(هیچ)'}")
    return category_ids


def _reuse_parent_images(parent: dict[str, Any], data: dict[str, Any], attrs: list[dict[str, Any]],
                        image_paths: list[Path], batch: str) -> dict[str, int]:
    expected = {"name": data["title"], "type": "variable" if attrs else "simple", "status": "draft", "attributes": attrs}
    if field_errors(expected, parent) or not any(isinstance(row, dict) and row.get("key") == publish_batch.META_BATCH
                                              and row.get("value") == batch for row in parent.get("meta_data", [])):
        raise WooCommerceAPIError(409, "محصول بستهٔ قبلی پس از پیش‌نمایش تغییر کرده؛ بدون دستکاری/ساخت تکراری متوقف شد")
    images = parent.get("images")
    if not isinstance(images, list) or len(images) != len(image_paths) or any(not positive_id(row) for row in images):
        raise WooCommerceAPIError(409, "گالری محصول قبلی با بسته یکسان نیست؛ تصاویر ناقص منتشر نمی‌شوند")
    metadata = {row.get("key"): row.get("value") for row in parent.get("meta_data", []) if isinstance(row, dict)}
    ids = [positive_id(row) for row in images]
    if "tisa_gallery_ids" in metadata and metadata["tisa_gallery_ids"] != ids:
        raise WooCommerceAPIError(409, "گالری بسته در سایت جابه‌جا/عوض شده؛ تأیید تازه لازم است")
    colors = [str(value) for attribute in attrs if is_color_attribute(attribute.get("name", ""))
              for value in attribute.get("options", [])]
    return _images_by_color(list(zip(ids, image_paths, strict=True)), colors)


async def _create_draft_unlocked(
    data: dict[str, Any],
    image_paths: list[Path],
    *,
    dry_run: bool = False,
    report: list[str] | None = None,
    batch_id: str = "",
    resume_existing: bool = True,
    meta: Sequence[dict[str, str]] = (),
    transport: httpx.BaseTransport | None = None,
) -> tuple[int, str]:
    """Create a WooCommerce draft and its variations; return ID and edit URL.

    ``dry_run`` keeps every step — media payloads, SKU scan, category lookup, the
    variation batch — and only replaces the network (see :func:`_dry_run_transport`).
    The ID it returns is then a fake one and the edit URL is empty, on purpose: a
    link to a product that does not exist would be worse than no link.
    ``report`` receives the audit lines so the caller can show them.

    ``batch_id`` (see :mod:`bot.services.publish_batch`) makes retries idempotent. When
    ``resume_existing`` is true, the writer searches for a matching partial product before
    creating anything; the interactive flow disables that extra read on a brand-new attempt
    (its local ledger proves there was no earlier request) and enables it for retries. ``meta``
    is written verbatim into the product.
    """
    if not all((settings.woocommerce_url, settings.woocommerce_key, settings.woocommerce_secret)):
        raise RuntimeError("اطلاعات WooCommerce API در .env کامل نیست.")
    if image_paths and not all((settings.wordpress_url, settings.wordpress_username, settings.wordpress_app_password)):
        raise RuntimeError("اطلاعات WordPress Media API برای آپلود عکس کامل نیست.")

    base = products_base()
    prices = {str(k): int(v) for k, v in (data.get("prices") or {}).items() if v}
    model_prices = {
        str(k): int(v) for k, v in (data.get("model_prices") or {}).items() if int(v or 0) > 0
    }
    pricing_errors = [str(item) for item in (data.get("pricing_errors") or []) if str(item).strip()]
    if pricing_errors:
        # An unmapped series/model price table would otherwise put one amount on
        # every variation. Refuse before the first request, like the stock matrix.
        raise ValueError("قیمت‌ها قابل‌اعمال نیستند: " + "؛ ".join(pricing_errors[:3]))
    common_price = int(data.get("price") or (next(iter(prices.values())) if prices else 0))
    raw_stock_matrix = data.get("stock_matrix") or {}
    if not isinstance(raw_stock_matrix, dict) or any(
        not isinstance(row, dict) for row in raw_stock_matrix.values()
    ):
        raise ValueError("ساختار ماتریس موجودی معتبر نیست.")
    plan = plan_from_dict(data)
    attrs = plan.woo_attributes()
    restrictions = plan.restrictions
    if plan.dropped:
        audit_note = "؛ ".join(
            f"«{name}» {had}→{left}" for name, had, left in plan.dropped
        )
    else:
        audit_note = ""
    prefix = str(data.get("sku_prefix", "")).strip().upper()
    # What the extractor/edit put in the draft — the card only ever states these numbers.
    raw_stock = data.get("stock")
    stock = None if raw_stock in (None, "") else int(raw_stock)
    stock_status = str(data.get("stock_status") or "").strip()
    sale_price = int(data.get("sale_price") or 0)
    stock_matrix_errors = [
        str(item) for item in (data.get("stock_matrix_errors") or []) if str(item).strip()
    ]
    if stock_matrix_errors:
        raise ValueError("ماتریس موجودی نامعتبر است: " + "؛ ".join(stock_matrix_errors[:3]))
    if plan.stock_matrix:
        if plan.matrix_axis_errors or plan.matrix_missing:
            detail = "; ".join(plan.matrix_axis_errors[:2])
            if plan.matrix_missing:
                detail += ("؛ " if detail else "") + f"{len(plan.matrix_missing)} خانه خالی است"
            raise ValueError("ماتریس موجودی ناقص/ناسازگار است: " + detail)
        if stock is not None or stock_status:
            raise ValueError("ماتریس موجودی با موجودی یا وضعیت کلی هم‌زمان مجاز نیست.")
        if not plan.combos:
            raise ValueError("ماتریس موجودی هیچ ترکیب قابل‌ساختی ندارد.")

    audit = Audit()
    if audit_note:
        audit.log(f"[plan] محورهای حذف‌شده: {audit_note}")
    audit.log(f"[plan] {plan.summary()}")
    audit.log(f"[config] WooCommerce: {settings.woocommerce_url or '(تنظیم نشده)'} (نسخه API: {settings.woocommerce_version})")
    audit.log(f"[config] WordPress media: {settings.wordpress_url or '(تنظیم نشده)'}")
    audit.log(f"[config] عنوان: {data.get('title', '(خالی)')} | پیشوند SKU: {prefix or '(خالی)'} | قیمت پایه: {common_price} | قیمت‌های گروهی: {prices or '(هیچ)'}")
    if model_prices:
        audit.log(
            f"[price:model] {len(model_prices)} قیمت مدل‌محور روی واریژن همان مدل می‌نشیند: "
            f"{model_prices}"
        )
    audit.log(
        "[config] موجودی: "
        + (f"{stock} عدد" if stock is not None else "ارسال نمی‌شود")
        + (f" | وضعیت: {stock_status}" if stock_status else "")
        + (f" | قیمت ویژه: {sale_price}" if sale_price else "")
    )
    audit.log(f"[config] ویژگی‌ها: {[a['name'] for a in attrs] or '(هیچ)'} | تعداد تصاویر: {len(image_paths)}")
    if plan.stock_matrix:
        matrix_quantities = [plan.matrix_stock_for(combo) for combo in plan.combos]
        known_quantities = [value for value in matrix_quantities if value is not None]
        audit.log(
            f"[stock:matrix] {len(known_quantities)} ترکیب؛ "
            f"جمع موجودی {sum(known_quantities):,}؛ هر تعداد روی واریژن متناظر ثبت می‌شود"
        )
    if restrictions:
        audit.log(
            f"[config] ماتریس رنگ هر مدل: {len(restrictions)} مدل محدود شد "
            f"(نمونه: {next(iter(restrictions.items()))})"
        )

    try:
        # One client, one policy (auth, timeout, redirect, retry, redaction): see
        # :mod:`bot.services.woo_client`. Publish requests are deliberately serialized and
        # paced; ``dry_run`` still wins over an injected transport and cannot reach the shop.
        async with WooClient(
            audit=audit,
            dry_run=dry_run,
            transport=transport,
            min_request_interval=PUBLISH_MIN_REQUEST_INTERVAL_SECONDS,
            max_concurrent_requests=1,
        ) as client:
            await woo_fencing.require(client, base)
            resumed: dict[str, Any] | None = None
            # A resumed attempt uploads nothing, so it has no new media ids to attach: the
            # variations it still lacks are created without a per-colour image, and saying
            # so beats silently pretending the pictures were re-used.
            images_by_color: dict[str, int] = {}
            colors_for_images: list[str] = []
            if batch_id and not dry_run and resume_existing:
                resumed = await _find_resumable(client, base, str(data.get("title") or ""), batch_id, audit)
            elif batch_id and not dry_run:
                audit.log("[resume] جستجوی محصول نیمه‌کاره انجام نشد؛ دفتر محلی این را تلاش اول می‌داند.")
            if resumed is not None:
                # Nothing is re-created on purpose: the previous attempt may have attached
                # images and categories already, and re-uploading would leave the old
                # media orphaned in the library rather than fix anything.
                product_id = int(resumed["id"])
                media_ids: list[int] = []
                category_ids: list[dict[str, int]] = []
                sku = str(resumed.get("sku") or "")
                images_by_color = _reuse_parent_images(resumed, data, attrs, image_paths, batch_id)
                audit.log(
                    f"[resume] محصول {product_id} از تلاش قبلی (batch {batch_id}) پیدا شد؛ "
                    "عنوان، تصویر و دسته‌ها دست‌نخورده می‌مانند و فقط واریژن‌های جاافتاده ساخته می‌شوند."
                )
            else:
                category_ids = await _resolve_categories(client, base, data.get("categories") or [], audit)
                # The product POST enforces SKU uniqueness. Avoid a separate exact-SKU GET;
                # a collision is handled by the bounded POST retry loop below.
                sku = await next_sku(client, base, prefix, audit, verify_candidate=False)
                uploads = await _upload_media_many(client, image_paths, audit)
                media_ids = [media_id for media_id, _path in uploads]
                colors_for_images = [
                    str(value)
                    for attribute in attrs
                    if attribute.get("name") == "رنگ"
                    for value in (attribute.get("options") or [])
                ]
                images_by_color = _images_by_color(uploads, colors_for_images)

                # Only send fields we actually have values for. WooCommerce returns
                # HTTP 400 for some empty/zero placeholders (e.g. a "0" regular_price
                # or a null SKU), so omitting them is safer than defaulting them.
                payload: dict[str, Any] = {
                    "name": data["title"],
                    "type": "variable" if attrs else "simple",
                    "status": "draft",
                    "description": product_description(data),
                    "attributes": attrs,
                }
                if sku:
                    payload["sku"] = sku
                if common_price and not attrs:
                    payload["regular_price"] = str(common_price)
                if sale_price and not attrs:
                    # Never replaces regular_price: the strikethrough price has to survive
                    # the day the sale is removed, and it does if we only add a sale.
                    payload["sale_price"] = str(sale_price)
                if plan.stock_matrix and not attrs:
                    matrix_quantity = plan.matrix_stock_for({})
                    if matrix_quantity is not None:
                        payload["manage_stock"] = True
                        payload["stock_quantity"] = matrix_quantity
                        payload["stock_status"] = "instock" if matrix_quantity > 0 else "outofstock"
                elif stock is not None or stock_status:
                    if attrs:
                        # A variable product owns no stock of its own — WooCommerce computes
                        # the parent from its variations — so only the status goes here and
                        # the number is written where it is true: on every variation.
                        if stock_status:
                            payload["stock_status"] = stock_status
                    else:
                        if stock is not None:
                            payload["manage_stock"] = True
                            payload["stock_quantity"] = stock
                        payload["stock_status"] = stock_status or "instock"
                if category_ids:
                    payload["categories"] = category_ids
                if media_ids:
                    payload["images"] = [{"id": image_id} for image_id in media_ids]
                meta_rows = list(data.get("meta") or []) + list(meta)
                if batch_id:
                    meta_rows.append({"key": publish_batch.META_BATCH, "value": batch_id})
                    meta_rows.append({"key": "tisa_gallery_ids", "value": media_ids})
                if meta_rows:
                    payload["meta_data"] = meta_rows
                if stock is not None:
                    audit.log(
                        f"[stock] موجودی {stock} → "
                        + (f"روی هر {len(plan.combos)} واریژن" if attrs else "روی خود محصول")
                    )
                if sale_price:
                    audit.log(
                        f"[price] قیمت ویژه {sale_price} → "
                        + (f"روی هر {len(plan.combos)} واریژن" if attrs else "روی خود محصول")
                    )
                if images_by_color:
                    missing = len(colors_for_images) - len(images_by_color)
                    audit.log(
                        f"[variation] تصویر رنگ: {len(images_by_color)} از {len(colors_for_images)} رنگ "
                        f"تصویر هم‌نام داشت" + (f"؛ {missing} رنگ بدون تصویر رنگ" if missing else "")
                    )
                audit.log(f"[payload] {payload}")

                try:
                    response = await _create_with_sku_retry(client, base, payload, prefix, sku, audit)
                    product = json_body(response)
                except woo_fencing.ExistingBatch as conflict:
                    response = await client.get(f"{base}/{conflict.product_id}")
                    check(response)
                    product = json_body(response)
                    images_by_color = _reuse_parent_images(product, data, attrs, image_paths, batch_id)
                    resumed = product
                    # Our competing POST was fenced before attachment. These are
                    # only this attempt's newly uploaded, unattached files.
                    await _rollback(client, base, 0, media_ids, audit)
                    media_ids = []
                product_id = positive_id(product)
                if not product_id:
                    raise WooCommerceAPIError(502, "ساخت محصول id معتبر برنگرداند؛ نتیجه نامعلوم است.")
                audit.log(f"[product] محصول ساخته شد: id={product_id}, sku={product.get('sku')}")

            try:
                if attrs:
                    existing = await _existing_combos(client, base, product_id, audit) if resumed else None
                    await _create_variations(
                        client, base, product_id, attrs, common_price, prices, audit,
                        restrictions, plan.combos, existing_combos=existing,
                        sale_price=sale_price, stock=stock, stock_status=stock_status,
                        images_by_color=images_by_color,
                        variation_plan=plan,
                        model_prices=model_prices,
                    )
            except Exception as exc:
                # Half-built is worse than not built: a product with a missing
                # colour cannot be ordered, and nobody knows which ones are
                # missing. Remove it (and its media) and report the real error —
                # but never a product we did not create in this attempt.
                if resumed is not None:
                    audit.log(
                        f"[rollback] انجام نشد: محصول {product_id} از تلاش قبلی است و "
                        "ممکن است کسی رویش کار کرده باشد. "
                        "پیش‌نویسِ نیمه‌کاره در وردپرس باقی می‌ماند."
                    )
                    raise
                if dry_run:
                    # Rehearsal: nothing exists on the shop to delete.
                    raise
                audit.log(f"[rollback] ساخت واریژن ناموفق بود ({describe_exception(exc)})؛ محصول در حال حذف است.")
                await _rollback(client, base, product_id, media_ids, audit)
                raise
            if plan.dropped:
                audit.log(
                    "[plan] هشدار: بعضی ویژگی‌ها بعد از حذف مقادیر تکراری از بین رفتند؛ "
                    "تعداد واریژن با پیش‌نمایش یکی است چون هر دو همین نقشه را می‌خوانند."
                )

        if dry_run:
            audit.log(f"[dry-run] جمع‌بندی: محصول ساختگی id={product_id}، {plan.count} واریژن، "
                      f"{len(image_paths)} تصویر (آپلود ساختگی). هیچ داده‌ای در سایت نوشته نشد.")
        elif resumed is not None:
            audit.log(
                f"[resume] جمع‌بندی: محصول {product_id} از تلاش قبلی بود و تکمیل شد؛ "
                "محصول دومی ساخته نشد. اگر واریژنی کم بود، فقط همان‌ها اضافه شدند."
            )
        # The caller always gets the trace when it asks for one — the resume note above
        # is exactly the kind of thing the result card has to admit to.
        if report is not None:
            report.extend(audit.lines)
        if dry_run:
            return product_id, ""
        return product_id, f"{settings.woocommerce_url.rstrip('/')}/wp-admin/post.php?post={product_id}&action=edit"
    except WooCommerceAPIError as exc:
        if not exc.diagnostics:
            exc.diagnostics = audit.lines
        raise
    except Exception as exc:
        # Preserve the successful steps and the final HTTP path on transport failures too.
        # Otherwise the user sees an empty ``ReadTimeout:`` despite a useful audit trail.
        # ``RuntimeError`` already accepts arbitrary attributes but built-ins (ValueError,
        # OSError, …) do not; guard with setattr so a non-standard exception cannot crash
        # the audit attachment itself.
        if hasattr(exc, "diagnostics"):
            exc.diagnostics = audit.lines
        else:
            try:
                object.__setattr__(exc, "diagnostics", audit.lines)
            except Exception:
                logger.debug(
                    "could not attach the audit trail to %s", type(exc).__name__, exc_info=True,
                )
        logger.exception("create_draft failed unexpectedly")
        raise


async def create_draft(
    data: dict[str, Any], image_paths: list[Path], *, dry_run: bool = False,
    report: list[str] | None = None, batch_id: str = "", resume_existing: bool = True,
    meta: Sequence[dict[str, str]] = (), transport: httpx.BaseTransport | None = None,
) -> tuple[int, str]:
    """Serialize identical live attempts across tasks/processes on the state volume."""
    async with publish_lease.hold(products_base(), batch_id if not dry_run else ""):
        return await _create_draft_unlocked(
            data, image_paths, dry_run=dry_run, report=report, batch_id=batch_id,
            # Honoured as the caller sent it: the local ledger knows whether an earlier
            # live attempt exists for this batch (it drove ``resume_existing``), and a
            # first publish must not pay for a recovery search it does not need.
            resume_existing=resume_existing, meta=meta, transport=transport,
        )
