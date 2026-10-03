"""The Telegram half of the outbox (plan 4.8): retry a refused publish, then report it.

Kept apart from :mod:`bot.services.outbox` for the rule the whole bot follows — a service
never imports a module: the service stores and schedules, this module talks to Telegram and
calls the *real* publish path. The queue holds a product, not a callback, so nothing here can
drift from what a human pressing «تأیید و ساخت» does: same ``create_draft``, same batch id,
same ledger card, and (in dry-run) no draining at all.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, ContextTypes

from bot import __version__ as _BOT_VERSION
from bot.config import settings
from bot.buttons import feature_allowed
from bot.services.validation import validate_draft
from bot.keyboards import result_card, result_keyboard
from bot.services import outbox, product_journal, products_ledger, publish_batch
from bot.services.woo_client import WooCommerceAPIError, describe_exception
from bot.services.woocommerce_direct import create_draft

logger = logging.getLogger(__name__)

#: How often the queue is knocked on. Short enough that a 429 from a rate limit is over
#: within a minute; long enough that a restart of the bot cannot turn into a publish storm.
DRAIN_INTERVAL_SECONDS = 60


def enqueue_after_failure(
    *,
    user_id: int | str,
    chat_id: int,
    thread_id: int | None,
    mode: str,
    data: Any,
    files: list[Any],
    batch_id: str,
    ledger_key: str = "",
    error: str,
    retry_after: float = 0.0,
) -> bool:
    """Put a refused publish back in line. ``False`` means it is *not* queued — said out loud.

    Only a transient failure reaches this function (the caller asks
    :func:`bot.services.outbox.is_transient`), and only the direct REST mode: a ZIP is a file
    the seller uploads themselves, so queueing it would be a promise nobody can keep.
    """
    if settings.woo_dry_run or mode != "new":
        return False
    payload = dict(data.to_dict())
    # The retry has to finish the card that is already in the chat, not start a second story.
    payload["ledger_key"] = ledger_key
    return outbox.enqueue(
        batch_id=batch_id, chat_id=chat_id, user_id=user_id, payload=payload,
        images=list(files), thread_id=thread_id, mode=mode, error=error,
        delay=max(outbox.backoff_seconds(1), retry_after),
    )


async def _notify(app: Application, entry: outbox.QueuedPublish, text: str,
                  markup: InlineKeyboardMarkup | None = None) -> None:
    kwargs: dict[str, Any] = {"chat_id": entry.chat_id or entry.user_id, "text": text}
    if entry.thread_id:
        kwargs["message_thread_id"] = entry.thread_id
    if markup is not None:
        kwargs["reply_markup"] = markup
    try:
        await app.bot.send_message(**kwargs)
    except Exception as exc:                                # a lost chat must not undo a publish
        logger.warning("outbox: پیام به %s نرفت: %s", kwargs["chat_id"], exc)


def _finish_card(entry: outbox.QueuedPublish, **fields: Any) -> dict[str, Any]:
    """Close the intent card the failed attempt left open, and return the card shown.

    Two things this has to get right: ``fields`` always carries the title (the history keeps
    a card per attempt, not per field), and an aged-out key must not lose the result — the
    ledger holds 20 cards, and «the product was built but we cannot say which card to close»
    is a reason to write a new one, not a reason to say nothing. Returning the entry is why
    the success message is not built from ``recent(1)``: another chat's publish can be the
    newest card, and then this chat would be told the wrong id and the wrong link.
    """
    fields.setdefault("title", str(entry.payload.get("title") or ""))
    key = entry.ledger_key
    finished = products_ledger.update(key, **fields) if key else None
    return finished if finished is not None else products_ledger.record(user_id=entry.user_id, **fields)


def _describe_missing(entry: outbox.QueuedPublish) -> str:
    if not entry.missing_images:
        return ""
    return f"\n🖼 {len(entry.missing_images)} تصویر از فایل‌های موقت پیدا نشد و ارسال نشد."


async def _attempt(app: Application, entry: outbox.QueuedPublish) -> None:
    report: list[str] = []
    try:
        if outbox.expired(entry):
            raise WooCommerceAPIError(410, "مهلت ۲۴ ساعته/تعداد تلاش این بسته تمام شده؛ بدون ارسال به فروشگاه رها شد.")
        try:
            actor = int(entry.user_id)
        except (TypeError, ValueError):
            actor = None
        if not feature_allowed(actor, "product_new"):
            raise WooCommerceAPIError(403, "دسترسی ناشر برداشته شده؛ انتشار خودکار انجام نشد.")
        expected = entry.payload.get("_queued_image_count", len(entry.images) + len(entry.missing_images))
        if entry.missing_images or type(expected) is not int or expected != len(entry.images):
            raise WooCommerceAPIError(422, "همهٔ تصاویر بسته در spool موجود نیستند؛ انتشار ناقص انجام نشد.")
        issues = validate_draft(entry.payload, mode=entry.mode, image_count=len(entry.images),
                                price_min=settings.price_min, price_max=settings.price_max,
                                require_models=settings.require_models)
        if issues.blocking:
            raise WooCommerceAPIError(422, issues.errors[0].message)
        product_id, edit_url = await create_draft(
            entry.payload, list(entry.images), report=report, batch_id=entry.batch_id,
            resume_existing=True,
            meta=publish_batch.source_meta(
                entry.batch_id, chat_id=entry.chat_id or entry.user_id, thread_id=entry.thread_id,
                images=len(entry.images),
                variations=int(entry.payload.get("variation_count") or 0),
                bot_version=_BOT_VERSION,
            ),
        )
    except Exception as exc:
        trace = report or list(getattr(exc, "diagnostics", []) or [])
        if trace:
            await product_journal.send_publish_trace(app.bot, trace)
        reason = f"HTTP {exc.status_code}: {exc}" if isinstance(exc, WooCommerceAPIError) \
            else describe_exception(exc)
        if outbox.is_transient(exc):
            if entry.attempts + 1 >= outbox.MAX_ATTEMPTS or outbox.expired(entry):
                await asyncio.to_thread(_finish_card, entry, status="failed", error=reason)
            updated = await asyncio.to_thread(outbox.note_failure, entry, reason)
            if updated.status == outbox.STATUS_DROPPED:
                await _notify(
                    app, entry,
                    f"❌ بعد از {updated.attempts} تلاش این محصول در صف ماند و رها شد:\n{reason}\n"
                    "هرچه در پیش‌نمایش تأیید کرده بودی ذخیره شده؛ دوباره «تأیید و ساخت» را بزن.",
                )
            else:
                wait = max(0, int(updated.next_at - time.time()))
                logger.info("outbox: تلاش %s/%s برای %s پس از %ss", updated.attempts,
                            outbox.MAX_ATTEMPTS, updated.batch_id, wait)
            return
        # A 400 will answer the same way tomorrow: saying why now is kinder than a silent queue.
        await asyncio.to_thread(_finish_card, entry, status="failed", error=reason)
        await asyncio.to_thread(outbox.abandon, entry.batch_id, reason, expected_updated_at=entry.updated_at or None,
                                expected_generation=entry.generation or None, claim_token=entry.claim_token or None)
        await _notify(
            app, entry,
            f"❌ تلاشِ صف‌شده نشد، چون خطای تکراری است (HTTP {getattr(exc, 'status_code', '?')}):\n{exc}\n"
            "این را باید دستی درست کنی؛ صف دیگر برایش تلاش نمی‌کند.",
        )
        return

    if report:
        await product_journal.send_publish_trace(app.bot, report)
    entry_payload = entry.payload
    card = await asyncio.to_thread(_finish_card,
        entry,
        status="created",
        product_id=product_id,
        edit_url=edit_url,
        title=str(entry_payload.get("title") or ""),
        price=int(entry_payload.get("price") or 0),
        price_groups=dict(entry_payload.get("prices") or {}),
        model_prices=dict(entry_payload.get("model_prices") or {}),
        sale_price=int(entry_payload.get("sale_price") or 0),
        stock=entry_payload.get("stock"),
        stock_status=str(entry_payload.get("stock_status") or ""),
        sku_prefix=str(entry_payload.get("sku_prefix") or ""),
        images=len(entry.images),
        categories=list(entry_payload.get("categories") or []),
        variations=int(entry_payload.get("variation_count") or 0),
        warnings=[
            "🐇 این محصول از صفِ تلاش مجدد ساخته شد (سایت قبلاً جواب نمی‌داد)."
            + _describe_missing(entry)
        ],
    )
    await asyncio.to_thread(outbox.succeed, entry.batch_id, expected_updated_at=entry.updated_at or None,
                                expected_generation=entry.generation or None, claim_token=entry.claim_token or None)
    buttons = [[InlineKeyboardButton("🌐 ویرایش در سایت", url=edit_url)]] if edit_url else []
    await _notify(
        app, entry,
        "✅ صفِ ارسال انجام شد — محصول ساخته شد.\n\n" + result_card(card),
        InlineKeyboardMarkup(buttons) if buttons else result_keyboard(card),
    )


async def _claimed_attempt(app: Application, entry: outbox.QueuedPublish) -> None:
    current = asyncio.current_task()

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(30)
            try:
                valid = await asyncio.to_thread(outbox.renew_claim, entry)
            except Exception:
                valid = False
                logger.exception("outbox: could not renew claim")
            if not valid:
                if current is not None:
                    current.cancel()
                return

    pulse = asyncio.create_task(heartbeat())
    try:
        await _attempt(app, entry)
    finally:
        pulse.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pulse
        await asyncio.to_thread(outbox.release_claim, entry)


_drain_lock: asyncio.Lock | None = None
_drain_loop: asyncio.AbstractEventLoop | None = None


async def drain_once(app: Application) -> int:
    """Try what is due, one item at a time. Returns how many were attempted."""
    if settings.woo_dry_run:
        # A rehearsal must not write anything, and a queue would be no exception: in dry-run
        # the store is unreachable by design, so retrying is only noise.
        return 0
    global _drain_lock, _drain_loop
    loop = asyncio.get_running_loop()
    if _drain_lock is None or _drain_loop is not loop:
        _drain_loop = loop
        _drain_lock = asyncio.Lock()
    if _drain_lock.locked():
        return 0
    attempted = 0
    async with _drain_lock:
        for _ in range(outbox.DRAIN_LIMIT):
            entry = await asyncio.to_thread(outbox.claim_due)
            if entry is None:
                break
            attempted += 1
            try:
                await _claimed_attempt(app, entry)
            except Exception:
                logger.exception("outbox: durable acknowledgement failed; generation retained")
        await asyncio.to_thread(outbox.prune)
    return attempted



async def drain(context: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue callback: never let one bad item stop the scheduler."""
    app = getattr(context, "application", None)
    if app is None:                                          # pragma: no cover - PTB wiring
        return
    try:
        await drain_once(app)
    except Exception as exc:
        logger.exception("outbox: drain ناموفق بود: %s", exc)


async def start(app: Application) -> int:
    """Schedule a post-start pass; never block polling startup on network retries."""
    if settings.woo_dry_run:
        logger.info("outbox: خاموش (حالت آزمایشی)")
        return 0
    pending = await asyncio.to_thread(outbox.stats)
    if app.job_queue is not None:
        app.job_queue.run_once(drain, when=0, name="outbox_drain_boot")
        app.job_queue.run_repeating(
            drain, interval=DRAIN_INTERVAL_SECONDS, first=DRAIN_INTERVAL_SECONDS, name="outbox_drain"
        )
    else:                                                     # pragma: no cover - no APScheduler
        logger.warning("outbox: JobQueue نصب نیست؛ تخلیهٔ خودکار صف زمان‌بندی نشد")
    if pending["pending"]:
        logger.info("outbox: %s مورد در صف است (آخرین خطا: %s)", pending["pending"],
                    pending.get("last_error") or "—")
    return 0
