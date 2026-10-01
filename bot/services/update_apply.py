"""Applying an update plan: the only place in this feature that writes.

:mod:`bot.services.product_match` reads, :mod:`bot.services.update_plan` decides, and this
module sends. One rule keeps it honest: **the plan is sent exactly as it was shown**. Nothing is
re-parsed here and no field is improved on the way out.

The order is chosen so that a failure half-way leaves the product usable:

1. **pictures** are uploaded first — nothing on the product has changed yet, so an upload that
   fails costs nothing;
2. **the product** is written once (title, gallery, the attribute lists, a simple product's
   price and stock). Variations depend on the attribute lists, so a refusal here stops everything;
3. **variations to create and to update** go out in one ``variations/batch`` request per hundred;
4. **variations to delete** go last, and only when every create and update was confirmed — a
   product that lost its old models *and* failed to get the new ones is worse than either alone.

After each step the write is **verified from the shop's own answer**: a row counts as written
only when the response carries the value that was sent. A row the batch swallowed without an
echo is written again on its own; a deletion the shop did not echo is checked with one re-read.
Whatever is still unconfirmed is reported as unconfirmed, never as success.

Nothing here is transactional, and nothing needs to be: the plan is *a diff against the shop*,
so after a failure the seller only has to ask again — what already landed is no longer a
difference, and only the rest is sent.
"""
from __future__ import annotations

import html
import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from bot.config import settings
from bot.services import product_match, update_plan, woocommerce_direct
from bot.services.color_matrix import is_color_attribute
from bot.services.woo_client import (
    Audit,
    WooClient,
    WooCommerceAPIError,
    describe_exception,
    error_message,
    products_base,
)

logger = logging.getLogger(__name__)

#: Rows per ``variations/batch`` request (WooCommerce's own default ceiling).
BATCH_SIZE = 100
#: The variation fields a verification compares — everything this module can write.
WRITTEN = ("regular_price", "sale_price", "manage_stock", "stock_quantity", "stock_status")
#: A batch the host rejects with one of these is retried row by row. A batch that *creates* is
#: only retried on «no such route»: a 400 there may mean some rows were already made, and a
#: second attempt would make them twice.
SPLIT_ON = (400, 404, 405, 501)
SPLIT_ON_CREATE = (404, 405, 501)


@dataclass
class ApplyResult:
    """What the shop confirmed. The card the seller sees is built from these numbers only."""

    product_updated: bool = False
    images: int = 0
    created: int = 0
    updated: list[int] = field(default_factory=list)
    deleted: int = 0
    #: sent, but the shop's answer did not show the new value (neither echo nor re-read)
    unconfirmed: list[int] = field(default_factory=list)
    #: one short line per row the shop refused or never made
    failed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    #: old variations left in place because something before them was not confirmed
    skipped_deletes: int = 0
    dry_run: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.product_updated or self.images or self.created or self.updated or self.deleted)

    @property
    def ok(self) -> bool:
        return not (self.failed or self.unconfirmed or self.errors)

    def summary(self) -> str:
        bits: list[str] = []
        if self.product_updated:
            bits.append("محصول")
        if self.images:
            bits.append(f"{self.images} تصویر")
        if self.created:
            bits.append(f"{self.created} واریژن ساخته شد")
        if self.updated:
            bits.append(f"{len(self.updated)} واریژن به‌روز شد")
        if self.deleted:
            bits.append(f"{self.deleted} واریژن حذف شد")
        head = ("✅ " + " · ".join(bits)) if bits else "❌ چیزی اعمال نشد"
        if self.dry_run:
            head += " (dry-run)"
        problems: list[str] = []
        if self.unconfirmed:
            problems.append(f"{len(self.unconfirmed)} واریژن فرستاده شد ولی فروشگاه مقدار جدید را برنگرداند")
        problems += self.failed[:3]
        problems += self.errors[:2]
        if self.skipped_deletes:
            problems.append(f"{self.skipped_deletes} واریژن قدیمی هنوز حذف نشده")
        return head + (("\n⚠️ " + "؛ ".join(problems)) if problems else "")


async def apply(plan: update_plan.UpdatePlan, files: Sequence[Path] = (), *,
                audit: Audit | None = None, transport: Any | None = None,
                dry_run: bool = False) -> ApplyResult:
    """Send ``plan`` to the shop and say what it confirmed. ``files`` are the new photos."""
    if plan.empty or plan.blocking:
        raise ValueError("این طرح قابل اعمال نیست")
    paths = list(files)
    if plan.images_new and not paths:
        raise ValueError("تصویری برای آپلود نیست")
    if paths and not all((settings.wordpress_url, settings.wordpress_username,
                          settings.wordpress_app_password)):
        raise RuntimeError("اطلاعات WordPress Media API برای آپلود عکس کامل نیست.")
    trace = audit or Audit()
    result = ApplyResult(dry_run=dry_run)
    base = products_base()
    # Paced like a publish: one request in flight, a short gap between them — a shared host
    # that is asked for twenty things at once answers some of them with a 500.
    async with WooClient(audit=trace, dry_run=dry_run, transport=transport,
                         min_request_interval=woocommerce_direct.PUBLISH_MIN_REQUEST_INTERVAL_SECONDS,
                         max_concurrent_requests=1) as client:
        uploads: list[tuple[int, Path]] = []
        if plan.images_new:
            try:
                uploads = await woocommerce_direct.upload_images(client, paths, trace)
            except (WooCommerceAPIError, httpx.HTTPError) as exc:
                result.errors.append(f"آپلود تصویر ناموفق بود: {describe_exception(exc)}")
                trace.log(f"[update] آپلود تصویر شکست خورد؛ هیچ چیزی در محصول عوض نشد: {exc}")
                return result
        media_ids = [media_id for media_id, _path in uploads]
        if not await _write_product(client, base, plan, media_ids, result, trace):
            return result
        if plan.is_variable and plan.changes_variations:
            await _write_variations(client, base, plan, uploads, result, trace)
    trace.log(
        f"[update] product {plan.product_id}: product={result.product_updated} images={result.images} "
        f"created={result.created} updated={len(result.updated)} deleted={result.deleted} "
        f"unconfirmed={len(result.unconfirmed)} failed={len(result.failed)}")
    return result


# — the product —

async def _write_product(client: WooClient, base: str, plan: update_plan.UpdatePlan,
                         media_ids: Sequence[int], result: ApplyResult, trace: Audit) -> bool:
    body = plan.product_body(media_ids)
    if not body:
        return True
    try:
        response = await client.put(f"{base}/{plan.product_id}", json=body)
    except (WooCommerceAPIError, httpx.HTTPError) as exc:
        result.errors.append(f"نوشتن خود محصول ناموفق بود: {describe_exception(exc)}")
        trace.log(f"[update] PUT products/{plan.product_id} ERROR {exc}")
        return False
    if not response.is_success:
        # Without the attribute lists the new variations would have nowhere to stand, so a
        # refused parent stops the whole apply.
        result.errors.append(_human(response))
        trace.log(f"[update] product {plan.product_id} FAILED {response.status_code}")
        return False
    bad = _contradictions(body, _json(response))
    if bad:
        result.errors.append("فروشگاه این را نپذیرفت یا برنگرداند: " + "، ".join(bad))
        trace.log(f"[update] product {plan.product_id}: جواب فروشگاه با چیزی که فرستادیم نمی‌خواند ({bad})")
        return False
    result.product_updated = True
    result.images = len(media_ids)
    trace.log(f"[update] PUT products/{plan.product_id} {sorted(body)}")
    return True


def _num(raw: object) -> int | None:
    try:
        return int(float(str(raw).replace(",", "")))
    except (TypeError, ValueError):
        return None


def _norm(text: object) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().casefold()


def _contradictions(sent: dict[str, Any], got: Any) -> list[str]:
    """What the shop's answer disagrees with. An answer that does not carry a field proves nothing,
    so only fields that came back are compared — but a field that came back different is a no."""
    if not isinstance(got, dict):
        return []
    bad: list[str] = []
    if ("name" in sent and "name" in got
            and _norm(html.unescape(str(sent["name"]))) != _norm(html.unescape(str(got["name"])))):
        bad.append("عنوان")
    for key, label in (("regular_price", "قیمت"), ("sale_price", "قیمت ویژه"),
                       ("stock_quantity", "موجودی")):
        if key in sent and key in got and _num(sent[key]) != _num(got[key]):
            bad.append(label)
    if "stock_status" in sent and "stock_status" in got and sent["stock_status"] != got["stock_status"]:
        bad.append("وضعیت موجودی")
    if "images" in sent and isinstance(got.get("images"), list):
        want = [int(image.get("id") or 0) for image in sent["images"]]
        have = [int(image.get("id") or 0) for image in got["images"] if isinstance(image, dict)]
        if want != have:
            bad.append("تصاویر")
    if "attributes" in sent and isinstance(got.get("attributes"), list):
        theirs = {_norm(item.get("name")): {_norm(option) for option in item.get("options") or []}
                  for item in got["attributes"] if isinstance(item, dict)}
        for item in sent["attributes"]:
            if not item.get("variation"):
                continue
            options = {_norm(option) for option in item.get("options") or []}
            if theirs.get(_norm(item.get("name"))) != options:
                bad.append(f"گزینه‌های «{item.get('name')}»")
    return bad


# — the variations —

async def _write_variations(client: WooClient, base: str, plan: update_plan.UpdatePlan,
                            uploads: Sequence[tuple[int, Path]], result: ApplyResult,
                            trace: Audit) -> None:
    colors = sorted({value for create in plan.creates for name, value in create.combo
                     if is_color_attribute(name)})
    by_color = woocommerce_direct.images_by_color(list(uploads), colors) if uploads and colors else {}
    creates: list[dict[str, Any]] = []
    for create in plan.creates:
        color = next((value for name, value in create.combo if is_color_attribute(name)), "")
        creates.append(create.payload(image_id=by_color.get(color, 0)))
    work: list[tuple[str, dict[str, Any]]] = (
        [("create", row) for row in creates] + [("update", row.payload()) for row in plan.updates])
    for start in range(0, len(work), BATCH_SIZE):
        chunk = work[start:start + BATCH_SIZE]
        await _write_chunk(client, base, plan.product_id,
                           [row for kind, row in chunk if kind == "create"],
                           [row for kind, row in chunk if kind == "update"], result, trace)
    if not plan.deletes:
        return
    if result.failed or result.unconfirmed:
        result.skipped_deletes = len(plan.deletes)
        result.errors.append("واریژن‌های قدیمی حذف نشدند چون ساخت یا به‌روزرسانی کامل تأیید نشد؛ "
                             "دوباره اپدیت را بزن.")
        trace.log(f"[update] {len(plan.deletes)} حذف عمداً انجام نشد: قبلش چیزی تأیید نشد")
        return
    ids = [variation.variation_id for variation in plan.deletes]
    for start in range(0, len(ids), BATCH_SIZE):
        await _delete_chunk(client, base, plan.product_id, ids[start:start + BATCH_SIZE], result, trace)


async def _write_chunk(client: WooClient, base: str, product_id: int, creates: list[dict[str, Any]],
                       updates: list[dict[str, Any]], result: ApplyResult, trace: Audit) -> None:
    body: dict[str, Any] = {}
    if creates:
        body["create"] = creates
    if updates:
        body["update"] = updates
    try:
        response = await client.post(f"{base}/{product_id}/variations/batch", json=body)
    except (WooCommerceAPIError, httpx.HTTPError) as exc:
        result.errors.append(describe_exception(exc))
        result.failed.extend(_names(creates, updates))
        trace.log(f"[update] batch ERROR {exc}")
        return
    if response.status_code in (SPLIT_ON_CREATE if creates else SPLIT_ON):
        # `variations/batch` is not universal; per-row calls are slower but the plan still
        # gets applied instead of asking the seller to try again.
        trace.log(f"[update] batch rejected (HTTP {response.status_code})؛ تک‌تک")
        for row in creates:
            await _single_create(client, base, product_id, row, result, trace)
        for row in updates:
            await _single_update(client, base, product_id, row, result, trace)
        return
    if not response.is_success:
        result.errors.append(_human(response))
        result.failed.extend(_names(creates, updates))
        trace.log(f"[update] batch FAILED {response.status_code}")
        return
    data = _json(response)
    made = [row for row in _rows(data, "create") if _was_made(row)]
    result.created += len(made)
    missing = len(creates) - len(made)
    if missing > 0:
        reasons = [_row_error(row) for row in _rows(data, "create") if not _was_made(row)]
        result.failed.append(f"{missing} واریژن تازه ساخته نشد" + (f" ({reasons[0]})" if reasons and reasons[0] else ""))
    echoed = {int(row.get("id") or 0): row for row in _rows(data, "update")}
    for row in updates:
        variation_id = int(row["id"])
        got = echoed.get(variation_id)
        if got is not None and got.get("error"):
            # The shop named this row and refused it (usually: it no longer exists).
            result.failed.append(f"واریژن {variation_id}: {_row_error(got) or 'رد شد'}")
        elif got is None:
            await _single_update(client, base, product_id, row, result, trace)
        elif _same_value(row, got):
            result.updated.append(variation_id)
        else:
            result.unconfirmed.append(variation_id)
    if result.unconfirmed:
        await _recheck(client, base, product_id, updates, result, trace)


async def _single_create(client: WooClient, base: str, product_id: int, row: dict[str, Any],
                         result: ApplyResult, trace: Audit) -> None:
    try:
        response = await client.post(f"{base}/{product_id}/variations", json=row)
    except (WooCommerceAPIError, httpx.HTTPError) as exc:
        result.errors.append(describe_exception(exc))
        result.failed.append(f"ساخت {_name(row)} ناموفق")
        return
    got = _json(response)
    if response.is_success and isinstance(got, dict) and int(got.get("id") or 0) > 0:
        result.created += 1
        trace.log(f"[update] POST products/{product_id}/variations → {got.get('id')}")
        return
    result.failed.append(f"ساخت {_name(row)} ناموفق: {_human(response)}")
    trace.log(f"[update] create {_name(row)} FAILED {response.status_code}")


async def _single_update(client: WooClient, base: str, product_id: int, row: dict[str, Any],
                         result: ApplyResult, trace: Audit) -> None:
    variation_id = int(row["id"])
    body = {key: value for key, value in row.items() if key != "id"}
    try:
        response = await client.put(f"{base}/{product_id}/variations/{variation_id}", json=body)
    except (WooCommerceAPIError, httpx.HTTPError) as exc:
        result.errors.append(describe_exception(exc))
        result.failed.append(f"واریژن {variation_id} به‌روز نشد")
        return
    if not response.is_success:
        result.failed.append(f"واریژن {variation_id}: {_human(response)}")
        trace.log(f"[update] variation {variation_id} FAILED {response.status_code}")
        return
    trace.log(f"[update] PUT products/{product_id}/variations/{variation_id}")
    got = _json(response)
    if isinstance(got, dict) and _same_value(row, got):
        result.updated.append(variation_id)
    else:
        result.unconfirmed.append(variation_id)


async def _recheck(client: WooClient, base: str, product_id: int, updates: list[dict[str, Any]],
                   result: ApplyResult, trace: Audit) -> None:
    """One read of the variation list, used only when the shop's echo was unusable."""
    rows, problem = await product_match.read_variation_rows(client, base, product_id, trace)
    if problem and not rows:
        trace.log(f"[update] recheck ناممکن: {problem}")
        return
    wanted = {int(row["id"]): row for row in updates}
    pending = set(result.unconfirmed)
    fixed = {int(row.get("id") or 0) for row in rows
             if int(row.get("id") or 0) in pending and _same_value(wanted[int(row["id"])], row)}
    if fixed:
        result.updated.extend(sorted(fixed))
        result.unconfirmed = [vid for vid in result.unconfirmed if vid not in fixed]
        trace.log(f"[update] با یک خواندن مجدد، {len(fixed)} واریژن تأیید شد")


async def _delete_chunk(client: WooClient, base: str, product_id: int, ids: list[int],
                        result: ApplyResult, trace: Audit) -> None:
    try:
        response = await client.post(f"{base}/{product_id}/variations/batch", json={"delete": ids})
    except (WooCommerceAPIError, httpx.HTTPError) as exc:
        result.errors.append(describe_exception(exc))
        result.failed.append(f"{len(ids)} واریژن حذف نشد")
        trace.log(f"[update] delete batch ERROR {exc}")
        return
    if response.status_code in SPLIT_ON:
        trace.log(f"[update] delete batch rejected (HTTP {response.status_code})؛ تک‌تک")
        for variation_id in ids:
            await _single_delete(client, base, product_id, variation_id, result, trace)
        return
    if not response.is_success:
        result.errors.append(_human(response))
        result.failed.append(f"{len(ids)} واریژن حذف نشد")
        trace.log(f"[update] delete batch FAILED {response.status_code}")
        return
    gone = {int(row.get("id") or 0) for row in _rows(_json(response), "delete") if _was_made(row)}
    left = [variation_id for variation_id in ids if variation_id not in gone]
    if left:
        # The echo is missing or short: ask the shop once what it still has.
        rows, problem = await product_match.read_variation_rows(client, base, product_id, trace)
        if not problem:
            alive = {int(row.get("id") or 0) for row in rows}
            gone |= {variation_id for variation_id in left if variation_id not in alive}
            left = [variation_id for variation_id in left if variation_id in alive]
    result.deleted += len(ids) - len(left)
    if left:
        result.failed.append(f"{len(left)} واریژن قدیمی حذف نشد")
    trace.log(f"[update] variations/batch delete: {len(ids) - len(left)} از {len(ids)} حذف شد")


async def _single_delete(client: WooClient, base: str, product_id: int, variation_id: int,
                         result: ApplyResult, trace: Audit) -> None:
    try:
        response = await client.delete(f"{base}/{product_id}/variations/{variation_id}",
                                       params={"force": "true"})
    except (WooCommerceAPIError, httpx.HTTPError) as exc:
        result.errors.append(describe_exception(exc))
        result.failed.append(f"واریژن {variation_id} حذف نشد")
        return
    if response.is_success:
        result.deleted += 1
        trace.log(f"[update] DELETE products/{product_id}/variations/{variation_id}")
        return
    result.failed.append(f"حذف واریژن {variation_id}: {_human(response)}")
    trace.log(f"[update] delete {variation_id} FAILED {response.status_code}")


# — reading the shop's answers —

def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _rows(data: Any, key: str) -> list[dict[str, Any]]:
    """The ``create`` / ``update`` / ``delete`` list of a batch answer; some hosts send a bare list."""
    if isinstance(data, dict):
        rows = data.get(key) or []
    elif key == "update" and isinstance(data, list):
        rows = data
    else:
        rows = []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _was_made(row: dict[str, Any]) -> bool:
    """A batch row that carries an id and no error object is a row the shop really handled."""
    return int(row.get("id") or 0) > 0 and not row.get("error")


def _row_error(row: dict[str, Any]) -> str:
    error = row.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or "")[:80]
    return ""


def _same_value(sent: dict[str, Any], got: dict[str, Any]) -> bool:
    """Did the shop give back what we asked for? WooCommerce answers prices as strings."""
    for key in WRITTEN:
        if key not in sent:
            continue
        have = got.get(key)
        if str(sent[key]) != str(have if have is not None else ""):
            return False
    return True


def _name(row: dict[str, Any]) -> str:
    values = [str(item.get("option")) for item in row.get("attributes") or [] if isinstance(item, dict)]
    return " · ".join(values) or f"#{row.get('id', '؟')}"


def _names(creates: Sequence[dict[str, Any]], updates: Sequence[dict[str, Any]]) -> list[str]:
    out = [f"ساخت {_name(row)} ناموفق" for row in creates]
    if updates:
        out.append(f"{len(updates)} واریژن به‌روز نشد")
    return out


def _human(response: httpx.Response) -> str:
    """The shop's own message when it is usable, the status code when it isn't."""
    text = error_message(response).strip()
    if text.startswith("{"):
        try:
            text = str(json.loads(text).get("message") or "").strip() or text
        except ValueError:
            pass
    return text or f"خطای {response.status_code}"


__all__ = ["BATCH_SIZE", "ApplyResult", "apply"]
