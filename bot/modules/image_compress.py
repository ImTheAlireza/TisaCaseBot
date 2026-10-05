"""Image compression-only utility (فشرده‌سازی عکس‌ها).

A single button starts a short conversation: the user forwards (or sends)
messages containing photos / image documents, the bot downloads them, runs
them through ``compress_image``, and sends the compressed files back. When
caption or product text is present, metadata is also extracted through the
product-creation parser; this utility never publishes a product.

Admins can use it only while the sudo owner has it enabled for them (see the
«⚙️ تنظیمات» screen and bot/services/preferences.py).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import re
import time
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.error import NetworkError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot.buttons import feature_allowed
from bot.services.access import guard_feature
from bot.services import metrics, flow_guard, product_journal, worker, workspace
from bot.config import settings
from bot.constants import CB
from bot.services.conversations import FlowConversationHandler
from bot.keyboards import main_menu_keyboard, main_menu_text
from bot.services.image_compressor import compress_image
from bot.utils.ui import answer_and_edit

logger = logging.getLogger(__name__)

WAITING = 0
TEMP_DIR = settings.temp_dir / "compress"


def close_for(user_id: int) -> bool:
    """Stop waiting albums and mark in-flight work to stop after a safe boundary.

    A Telegram upload already in flight is never force-cancelled: it may have been
    accepted remotely, so interrupting it would leave an ambiguous user-visible result.
    """
    data = _active_data.pop(user_id, None)
    if data is not None:
        data["compress_closed"] = True
        data.pop("compress_generation", None)
    changed = data is not None
    for key, task in list(album_tasks.items()):
        if key[0] != user_id:
            continue
        album_buffers.pop(key, None)
        if key in processing_albums:
            cancelled_albums.add(key)
        else:
            task.cancel()
            album_tasks.pop(key, None)
        changed = True
    if TEMP_DIR.exists():
        prefix = f"{user_id}_"
        protected = active_roots.get(user_id, set())
        for path in TEMP_DIR.iterdir():
            if path.is_dir() and path.name.startswith(prefix) and path not in protected:
                workspace.remove(path)
                changed = True
    return changed

INSTRUCTION = (
    "🗜️ <b>فشرده‌سازی عکس‌ها</b>\n\n"
    "پیام‌های عکس‌دار را فوروارد کن؛ عکس فشرده می‌شود و مدل‌ها و ویژگی‌ها از کپشن/متن همراه استخراج می‌شوند.\n"
    "کارت همان پارسرِ «📦 ساخت محصول» است، ولی فقط <i>مقادیر</i> را می‌گوید: مدل‌ها و ویژگی‌ها، "
    "و هر جا چیزی را نخوانده باشد. جزئیاتِ واریژن در لاگِ گروه ثبت می‌شود.\n\n"
    "برای پایان: /cancel"
)

ANALYSIS_CAPTIONS_KEY = "compress_analysis_captions"
ANALYSIS_INFO_KEY = "compress_analysis_info"
ANALYSIS_REPORT_KEY = "compress_analysis_report"
ANALYSIS_SOURCE_KEY = "compress_analysis_source"
ANALYSIS_PROMPT_KEY = "compress_analysis_prompt_sent"
COMPRESS_RETRIES_KEY = "compress_network_retries"

# Telegram sends each album photo as a separate update. Buffering that group lets
# downloads/compression overlap and runs text extraction once, without concurrent
# ConversationHandler callbacks or an unbounded task per image.
album_buffers: dict[tuple[int, str], list[Message]] = {}
album_tasks: dict[tuple[int, str], asyncio.Task[None]] = {}
processing_albums: set[tuple[int, str]] = set()
cancelled_albums: set[tuple[int, str]] = set()
active_roots: dict[int, set[Path]] = {}
_active_data: dict[int, dict] = {}
_global_media_semaphore: asyncio.Semaphore | None = None
_global_media_loop: asyncio.AbstractEventLoop | None = None
MAX_PARALLEL_MEDIA = 4


async def _log_to_group(context: ContextTypes.DEFAULT_TYPE, text: str, *, parse_mode: str | None = None) -> None:
    logger.info("%s", text)
    # The shared sender also reports an unset destination, so misconfiguration is
    # visible locally instead of silently skipping the requested audit trail.
    await product_journal.send_log_message(context.bot, text, parse_mode=parse_mode)


def _append_analysis_text(context: ContextTypes.DEFAULT_TYPE, key: str, text: str) -> None:
    text = (text or "").strip()
    if not text:
        return
    values = context.user_data.setdefault(key, [])
    if text not in values:
        values.append(text)


async def _send_analysis(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    """Analyze the text accompanying compressed photos and send a concise admin report."""
    generation = context.user_data.get("compress_generation")
    if context.user_data.get("compress_closed"):
        return
    captions = context.user_data.get(ANALYSIS_CAPTIONS_KEY, [])
    info_parts = context.user_data.get(ANALYSIS_INFO_KEY, [])
    caption = "\n".join(captions)
    info = "\n".join(info_parts)
    source = "\n".join(part for part in (caption, info) if part.strip()).strip()
    if not source:
        if not context.user_data.get(ANALYSIS_PROMPT_KEY):
            context.user_data[ANALYSIS_PROMPT_KEY] = True
            await context.bot.send_message(
                chat_id=user_id,
                text="📝 برای تشخیص مدل و ویژگی‌ها، کپشن عکس یا متن محصول را هم بفرست؛ عکسِ بدون متن قابل‌تشخیص نیست.",
            )
            await _log_to_group(
                context,
                f"🗜️ [compress:{user_id}] تشخیص اجرا نشد: کپشن یا متن همراه عکس دریافت نشده است.",
            )
        return

    report_source = "\n".join(part for part in (caption, info) if part.strip())
    if report_source == context.user_data.get(ANALYSIS_SOURCE_KEY):
        return
    context.user_data[ANALYSIS_SOURCE_KEY] = report_source
    # Reuse the product-creation flow verbatim: its parser/AI normalization, learned vocabulary,
    # color matrix and accessory handling all stay in sync. The card then states the *values* and
    # what the parser refused — enough to answer «درست خواند؟» at a glance — while the variation
    # plan and the rest go to the log below, because this screen writes nothing to the shop.
    from bot.modules.product_flow import analyze_text

    extraction_started = time.perf_counter()
    analysis = await analyze_text(caption, info)
    extraction_ms = (time.perf_counter() - extraction_started) * 1000
    report = analysis.report()
    if report == context.user_data.get(ANALYSIS_REPORT_KEY):
        return
    if (context.user_data.get("compress_closed") or context.user_data.get("compress_generation") != generation
            or not _can_compress(user_id)):
        return
    await context.bot.send_message(chat_id=user_id, text=report, parse_mode="HTML")
    audit_text = (
        f"🗜️ [compress:{user_id}] خروجی همان پارسرِ ساخت محصول "
        f"(استخراج {extraction_ms:.0f} ms):\n{report}"
    )
    folded = analysis.detail()
    if folded:
        # The card is written to be skimmed in three seconds; the log is written to be audited.
        # Whatever the card leaves out — the variation count, the palette of every model, the AI
        # chatter, a note about a field this screen cannot act on — lands here instead.
        audit_text += "\nجزئیات (فشرده‌شده در کارت):\n" + folded
    await _log_to_group(context, audit_text, parse_mode="HTML")
    context.user_data[ANALYSIS_REPORT_KEY] = report


async def _try_send_analysis(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    try:
        await _send_analysis(context, user_id)
    except Exception as exc:
        # Compression should still succeed if extraction/AI is unavailable.
        context.user_data.pop(ANALYSIS_SOURCE_KEY, None)
        logger.exception("Image-compression metadata extraction failed for user %s", user_id)
        await _log_to_group(
            context,
            f"🗜️ [compress:{user_id}] خطای تشخیص: {type(exc).__name__}: {str(exc)[:240]}",
        )
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="⚠️ عکس فشرده شد؛ تشخیص مدل و ویژگی از متن انجام نشد.",
            )
        except Exception:
            logger.exception("Could not notify user %s about metadata extraction failure", user_id)


def _clear_analysis(context: ContextTypes.DEFAULT_TYPE) -> None:
    for key in (ANALYSIS_CAPTIONS_KEY, ANALYSIS_INFO_KEY, ANALYSIS_REPORT_KEY, ANALYSIS_SOURCE_KEY, ANALYSIS_PROMPT_KEY):
        context.user_data.pop(key, None)


def _can_compress(user_id: int | None) -> bool:
    """True for sudo, and for admins only while the button is visible to them."""
    return feature_allowed(user_id, "compress")


def _safe_name(name: str, fallback: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).name).strip("._")
    return name or fallback


def _media(message) -> tuple[str, str] | None:
    """Return (file_id, filename) for a photo or image document, else None."""
    if message.photo:
        return message.photo[-1].file_id, f"image_{message.message_id}.jpg"
    if message.document and (message.document.mime_type or "").startswith("image/"):
        return (
            message.document.file_id,
            _safe_name(
                message.document.file_name or "image.jpg",
                f"image_{message.message_id}.jpg",
            ),
        )
    return None


async def _download(
    context: ContextTypes.DEFAULT_TYPE, file_id: str, target: Path, attempts: int = 3
) -> int:
    """Download safely, retrying only idempotent Telegram read operations."""
    for attempt in range(1, attempts + 1):
        try:
            tg_file = await context.bot.get_file(file_id)
            if tg_file.file_size and tg_file.file_size > settings.max_download_mb * 1024 * 1024:
                raise ValueError(f"فایل بزرگ‌تر از سقف مجاز ({settings.max_download_mb:g} MB) است.")
            await tg_file.download_to_drive(custom_path=target)
            actual_size = target.stat().st_size if target.exists() else 0
            maximum = int(settings.max_download_mb * 1024 * 1024)
            if actual_size > maximum:
                target.unlink(missing_ok=True)
                raise ValueError(f"فایل بزرگ‌تر از سقف مجاز ({settings.max_download_mb:g} MB) است.")
            return actual_size or tg_file.file_size or 0
        except (NetworkError, TimeoutError) as exc:
            # A failed download may have left a partial file behind. Never let a
            # retry treat that partial response as a complete image.
            target.unlink(missing_ok=True)
            user_data = getattr(context, "user_data", None)
            if isinstance(user_data, dict):
                user_data[COMPRESS_RETRIES_KEY] = int(user_data.get(COMPRESS_RETRIES_KEY, 0)) + 1
            if attempt >= attempts:
                raise
            delay = 0.5 * (2 ** (attempt - 1))
            logger.warning(
                "Telegram image download failed (%s/%s); retrying in %.1fs: %s",
                attempt,
                attempts,
                delay,
                exc,
            )
            await asyncio.sleep(delay)
    raise RuntimeError("Telegram image download failed without an exception")


async def entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """«فشرده‌سازی عکس‌ها» pressed → wait for media messages."""
    query = update.callback_query
    user = update.effective_user
    if not user or not _can_compress(user.id):
        metrics.note_denial("image_compress")
        await query.answer("⛔ دسترسی ندارید.", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    context.user_data["compress_closed"] = False
    context.user_data["compress_generation"] = secrets.token_hex(8)
    _active_data[user.id] = context.user_data
    _clear_analysis(context)
    context.user_data[COMPRESS_RETRIES_KEY] = 0
    # «one thing at a time»: any other open flow of this user is closed first.
    closed = flow_guard.close_others("compress", user.id)
    if closed and query.message is not None:
        await query.message.reply_text(
            "↩️ جریان «" + "»، «".join(closed) + "» قبلی‌ات بسته شد."
        )
    await query.edit_message_text(
        INSTRUCTION,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ بازگشت به منو", callback_data=CB.MAIN_MENU)]]
        ),
        parse_mode="HTML",
    )
    return WAITING


async def _update_status(status, text: str, user_id: int) -> None:
    """Progress messages are helpful, but a failed edit must not cancel image work."""
    if status is None:
        return
    try:
        await asyncio.wait_for(status.edit_text(text), timeout=5.0)
    except Exception:
        logger.info("Could not update compression status for user %s", user_id, exc_info=True)


def _media_semaphore() -> asyncio.Semaphore:
    """One process-wide cap, recreated only when a test starts a new event loop."""
    global _global_media_semaphore, _global_media_loop
    loop = asyncio.get_running_loop()
    if _global_media_semaphore is None or _global_media_loop is not loop:
        _global_media_semaphore = asyncio.Semaphore(MAX_PARALLEL_MEDIA)
        _global_media_loop = loop
    return _global_media_semaphore


async def _process_media_batch(
    user_id: int,
    messages: list[Message],
    context: ContextTypes.DEFAULT_TYPE,
    *,
    album_key: tuple[int, str] | None = None,
) -> None:
    """Bounded, failure-isolated download/compress/send for one Telegram batch."""
    user_data = getattr(context, "user_data", {})
    generation = user_data.get("compress_generation")
    if user_data.get("compress_closed"):
        return
    _active_data[user_id] = user_data

    def still_open() -> bool:
        return (not user_data.get("compress_closed")
                and user_data.get("compress_generation") == generation
                and _can_compress(user_id))

    media_items = [
        (message, _media(message))
        for message in sorted(messages, key=lambda item: item.message_id)
    ]
    media_items = [(message, media) for message, media in media_items if media is not None]
    if not media_items:
        return

    try:
        root = workspace.new_dir(TEMP_DIR, user_id)
    except OSError as exc:
        logger.exception("Could not create image-compression workspace for user %s", user_id)
        await _log_to_group(
            context,
            f"🗜️ [compress:{user_id}] ساخت فضای موقت ناموفق بود: "
            f"{type(exc).__name__}: {str(exc)[:200]}",
        )
        try:
            await media_items[0][0].reply_text("❌ فضای موقت پردازش در دسترس نیست؛ کمی بعد دوباره تلاش کن.")
        except Exception:
            logger.info("Could not notify user %s about workspace failure", user_id, exc_info=True)
        return
    active_roots.setdefault(user_id, set()).add(root)
    workspace.protect(root)
    first_message = media_items[0][0]
    status = None
    started_batch = time.perf_counter()
    failures: list[str] = []
    user_data = getattr(context, "user_data", {})
    retries_before = int(user_data.get(COMPRESS_RETRIES_KEY, 0)) if isinstance(user_data, dict) else 0
    try:
        try:
            status = await asyncio.wait_for(
                first_message.reply_text(
                    f"⏳ در حال پردازش {len(media_items)} عکس..."
                ),
                timeout=5.0,
            )
        except Exception:
            logger.info("Could not send compression progress for user %s", user_id, exc_info=True)

        batch_semaphore = asyncio.Semaphore(min(3, len(media_items)))
        process_semaphore = _media_semaphore()

        async def prepare(index: int, message: Message, media: tuple[str, str]) -> dict[str, object]:
            file_id, name = media
            source = root / f"{index:02d}_{_safe_name(name, 'image.jpg')}"
            download_started = time.perf_counter()
            async with batch_semaphore, process_semaphore:
                original_size = await _download(context, file_id, source)
            download_ms = (time.perf_counter() - download_started) * 1000
            compress_started = time.perf_counter()
            async with process_semaphore:
                compressed = await worker.run(compress_image, source, root / "out", timeout=settings.process_timeout_seconds)
            compress_ms = (time.perf_counter() - compress_started) * 1000
            compressed_size = compressed.stat().st_size if compressed.is_file() else 0
            metrics.observe("compress_download_ms", download_ms)
            metrics.observe("compress_cpu_ms", compress_ms)
            return {
                "message": message,
                "name": name,
                "compressed": compressed,
                "original_size": original_size,
                "compressed_size": compressed_size,
                "download_ms": download_ms,
                "compress_ms": compress_ms,
            }

        prepared = await asyncio.gather(
            *(prepare(index, message, media) for index, (message, media) in enumerate(media_items, 1)),
            return_exceptions=True,
        )
        successes: list[dict[str, object]] = []
        for index, result in enumerate(prepared, 1):
            if isinstance(result, BaseException):
                logger.error(
                    "Compression batch item %s/%s failed for user %s",
                    index,
                    len(media_items),
                    user_id,
                    exc_info=(type(result), result, result.__traceback__),
                )
                failures.append(f"عکس {index}: {type(result).__name__}: {str(result)[:160]}")
            else:
                successes.append(result)

        sent = 0
        for item in successes:
            if not still_open() or (album_key is not None and album_key in cancelled_albums):
                failures.append("ادامهٔ ارسال به‌دلیل بسته‌شدن جریان متوقف شد")
                break
            name = str(item["name"])
            compressed = Path(item["compressed"])
            send_started = time.perf_counter()
            try:
                with compressed.open("rb") as handle:
                    await context.bot.send_document(
                        user_id,
                        document=handle,
                        filename=compressed.name,
                        caption=f"🗜️ فشرده شد: {name}",
                    )
                upload_ms = (time.perf_counter() - send_started) * 1000
                metrics.observe("compress_upload_ms", upload_ms)
                item["upload_ms"] = upload_ms
                sent += 1
            except Exception as exc:
                # A timeout can mean Telegram accepted the file. Never retry a write
                # whose result is ambiguous; continue only with distinct files.
                logger.exception("Compressed image send failed for user %s (%s)", user_id, name)
                ambiguity = "؛ ممکن است ارسال پذیرفته شده باشد و خودکار تکرار نشد" if isinstance(
                    exc, (NetworkError, TimeoutError)
                ) else ""
                failures.append(
                    f"{name}: {type(exc).__name__}: {str(exc)[:160]}{ambiguity}"
                )

        cancelled = not still_open() or (album_key is not None and album_key in cancelled_albums)
        if status is not None:
            if failures:
                final_text = f"⚠️ {sent} از {len(media_items)} عکس ارسال شد؛ جزئیات در گروه لاگ ثبت شد."
            else:
                final_text = f"✅ {sent} عکس فشرده و ارسال شد."
            await _update_status(status, final_text, user_id)

        retry_total = int(user_data.get(COMPRESS_RETRIES_KEY, 0)) if isinstance(user_data, dict) else retries_before
        retry_count = max(0, retry_total - retries_before)
        summary = [
            f"🗜️ [compress:{user_id}] دستهٔ {len(media_items)} عکس: "
            f"{sent} ارسال شد، {len(failures)} ناموفق؛ "
            f"کل {time.perf_counter() - started_batch:.1f}s"
            + (f"؛ {retry_count} تلاش مجدد شبکه" if retry_count else "")
            + "."
        ]
        for item in successes:
            summary.append(
                f"{item['name']}: {int(item['original_size']):,} → "
                f"{int(item['compressed_size']):,} بایت؛ "
                f"دریافت {float(item['download_ms']) / 1000:.1f}s، "
                f"فشرده‌سازی {float(item['compress_ms']) / 1000:.1f}s، "
                f"ارسال {float(item.get('upload_ms', 0)) / 1000:.1f}s"
            )
        if failures:
            summary.append("خطاها: " + " | ".join(failures[:8]))
        await _log_to_group(context, "\n".join(summary))

        if not cancelled:
            metrics.incr("compress_batches")
            if sent:
                metrics.incr("compress_images", sent)
            await _try_send_analysis(context, user_id)
    finally:
        roots = active_roots.get(user_id)
        if roots is not None:
            roots.discard(root)
            if not roots:
                active_roots.pop(user_id, None)
        workspace.remove(root)


async def _flush_album(key: tuple[int, str], context: ContextTypes.DEFAULT_TYPE) -> None:
    current = asyncio.current_task()
    try:
        await asyncio.sleep(settings.album_wait_seconds)
        messages = album_buffers.pop(key, [])
        if not messages:
            return
        processing_albums.add(key)
        await _process_media_batch(key[0], messages, context, album_key=key)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("Compression album failed for user %s", key[0])
        await _log_to_group(
            context,
            f"🗜️ [compress:{key[0]}] پردازش دسته ناموفق بود: "
            f"{type(exc).__name__}: {str(exc)[:240]}",
        )
    finally:
        processing_albums.discard(key)
        cancelled_albums.discard(key)
        if album_tasks.get(key) is current:
            album_tasks.pop(key, None)
        # A Telegram album photo that arrived just after the collection window
        # gets a new bounded batch instead of being stranded in the buffer.
        if (album_buffers.get(key) and key not in album_tasks
                and not context.user_data.get("compress_closed")):
            album_tasks[key] = asyncio.create_task(_flush_album(key, context))


@guard_feature("compress", on_denial=close_for, checker=lambda uid, key: feature_allowed(uid, key))
async def on_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Queue albums immediately; process singles in a one-file batch."""
    message = update.effective_message
    user = update.effective_user
    if not user or not message:
        return WAITING

    media = _media(message)
    if not media:
        await message.reply_text("❗ عکسی در این پیام پیدا نکردم.")
        return WAITING

    _append_analysis_text(context, ANALYSIS_CAPTIONS_KEY, message.caption or "")
    media_group_id = getattr(message, "media_group_id", None)
    if media_group_id:
        key = (user.id, str(media_group_id))
        album_buffers.setdefault(key, []).append(message)
        task = album_tasks.get(key)
        if task is None or task.done():
            album_tasks[key] = asyncio.create_task(_flush_album(key, context))
        return WAITING

    await _process_media_batch(user.id, [message], context)
    return WAITING


@guard_feature("compress", on_denial=close_for, checker=lambda uid, key: feature_allowed(uid, key))
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Collect product info sent beside photos and show detected models/features."""
    message = update.effective_message
    user = update.effective_user
    text = (message.text or "").strip() if message else ""
    if not user or not text:
        return WAITING
    _append_analysis_text(context, ANALYSIS_INFO_KEY, text)
    status = await message.reply_text("🔎 در حال تشخیص مدل‌ها و ویژگی‌ها...")
    try:
        await _try_send_analysis(context, user.id)
        await status.delete()
    except Exception:
        logger.exception("Could not send image-compression analysis status for user %s", user.id)
    return WAITING


async def cb_back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """The in-flow «بازگشت به منو» button."""
    query = update.callback_query
    user = update.effective_user
    if user:
        close_for(user.id)
    _clear_analysis(context)
    await answer_and_edit(
        query,
        main_menu_text(user.id if user else None, user),
        reply_markup=main_menu_keyboard(user.id if user else None),
        parse_mode="HTML",
        quiet=True,
    )
    return ConversationHandler.END


async def cmd_exit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/cancel, /start or /menu during the flow → back to the main menu."""
    user = update.effective_user
    if user:
        close_for(user.id)
    _clear_analysis(context)
    await update.effective_message.reply_html(
        main_menu_text(user.id if user else None, user),
        reply_markup=main_menu_keyboard(user.id if user else None),
    )
    return ConversationHandler.END


# Registered at import time (not only in register(app)): the guard must know
# how to close this flow even if a test or tool imports the module directly.
flow_guard.register("compress", "فشرده‌سازی عکس‌ها", close_for)


async def shutdown() -> None:
    for user_id in set(_active_data) | {key[0] for key in album_tasks}:
        close_for(user_id)
    tasks = list(album_tasks.values())
    for task in tasks:
        if not task.done() and not task.cancelling():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def register(app: Application) -> None:
    conv = FlowConversationHandler(
        flow="compress",
        entry_points=[CallbackQueryHandler(entry, pattern=f"^{CB.COMPRESS}$")],
        states={
            WAITING: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_media),
                CallbackQueryHandler(cb_back_to_menu, pattern=f"^{CB.MAIN_MENU}$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_text),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_exit),
            CommandHandler("start", cmd_exit),
            CommandHandler("menu", cmd_exit),
        ],
        name="image_compress",
    )
    app.add_handler(conv)
