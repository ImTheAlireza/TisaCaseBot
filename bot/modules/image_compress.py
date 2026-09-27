"""Image compression-only utility (فشرده‌سازی عکس‌ها).

A single button starts a short conversation: the user forwards (or sends)
messages containing photos / image documents, the bot downloads them, runs
them through ``compress_image``, and sends the compressed files back. No
WordPress / product involvement — just local image processing.

Admins can use it only while the sudo owner has it enabled for them (see the
«⚙️ تنظیمات» screen and bot/services/preferences.py).
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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
from bot.services import metrics, flow_guard, product_journal
from bot.config import settings
from bot.constants import CB
from bot.keyboards import main_menu_keyboard, main_menu_text
from bot.services.image_compressor import compress_image
from bot.services.product_text_summary import format_product_summary

logger = logging.getLogger(__name__)

WAITING = 0
TEMP_DIR = Path("/tmp/tisaposttowp-compress")


def close_for(user_id: int) -> bool:
    """Drop this user's leftover workspaces; True when something was on disk.

    Registered with :mod:`bot.services.flow_guard` so that starting a product
    flow does not leave a compression batch half-downloaded in /tmp.
    """
    if not TEMP_DIR.exists():
        return False
    removed = False
    prefix = f"{user_id}_"
    for path in TEMP_DIR.iterdir():
        if path.is_dir() and path.name.startswith(prefix):
            shutil.rmtree(path, ignore_errors=True)
            removed = True
    return removed

INSTRUCTION = (
    "🗜️ <b>فشرده‌سازی عکس‌ها</b>\n\n"
    "پیام‌های عکس‌دار را فوروارد کن؛ عکس فشرده می‌شود و مدل‌ها و ویژگی‌ها از کپشن/متن همراه استخراج می‌شوند.\n"
    "مدل‌ها به شکل <code>model | model | ...</code> نمایش داده می‌شوند.\n\n"
    "برای پایان: /cancel"
)

ANALYSIS_CAPTIONS_KEY = "compress_analysis_captions"
ANALYSIS_INFO_KEY = "compress_analysis_info"
ANALYSIS_REPORT_KEY = "compress_analysis_report"
ANALYSIS_SOURCE_KEY = "compress_analysis_source"
ANALYSIS_PROMPT_KEY = "compress_analysis_prompt_sent"


async def _log_to_group(context: ContextTypes.DEFAULT_TYPE, text: str, *, parse_mode: str | None = None) -> None:
    logger.info("%s", text)
    if settings.log_chat_id:
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
    # Reuse the product-creation flow verbatim: its parser/AI normalization,
    # learned vocabulary, color matrix, and accessory handling all stay in sync.
    from bot.modules.product_flow import extract_product_metadata

    models, attributes = await extract_product_metadata(caption, info)
    report = format_product_summary(models, attributes)
    if report == context.user_data.get(ANALYSIS_REPORT_KEY):
        return
    await context.bot.send_message(chat_id=user_id, text=report, parse_mode="HTML")
    await _log_to_group(
        context,
        f"🗜️ [compress:{user_id}] خروجی همان پارسرِ ساخت محصول:\n{report}",
        parse_mode="HTML",
    )
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


async def _download(context: ContextTypes.DEFAULT_TYPE, file_id: str, target: Path) -> int:
    tg_file = await context.bot.get_file(file_id)
    if tg_file.file_size and tg_file.file_size > settings.max_download_mb * 1024 * 1024:
        raise ValueError(f"فایل بزرگ‌تر از سقف مجاز ({settings.max_download_mb:g} MB) است.")
    await tg_file.download_to_drive(custom_path=target)
    return tg_file.file_size or 0


async def entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """«فشرده‌سازی عکس‌ها» pressed → wait for media messages."""
    query = update.callback_query
    user = update.effective_user
    if not user or not _can_compress(user.id):
        metrics.note_denial("image_compress")
        await query.answer("⛔ دسترسی ندارید.", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    _clear_analysis(context)
    # «one thing at a time»: any other open flow of this user is closed first.
    closed = flow_guard.close_others("compress", user.id)
    if closed:
        await query.message.reply_text(  # type: ignore[union-attr]
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


async def on_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Download, compress and send back every image in the incoming message."""
    message = update.effective_message
    user = update.effective_user
    if not user or not message:
        return WAITING

    media = _media(message)
    if not media:
        await message.reply_text("❗ عکسی در این پیام پیدا نکردم. پیامی با عکس فوروارد کن.")
        return WAITING

    file_id, name = media
    _append_analysis_text(context, ANALYSIS_CAPTIONS_KEY, message.caption or "")
    root = TEMP_DIR / f"{user.id}_{int(time.time() * 1000)}"
    root.mkdir(parents=True, exist_ok=True)
    src = root / _safe_name(name, "image.jpg")
    status = await message.reply_text("⬇️ در حال دانلود عکس...")
    send_started = False
    delivered = False
    try:
        original_size = await _download(context, file_id, src)
        await status.edit_text("🗜️ در حال فشرده‌سازی...")
        compressed = await asyncio.to_thread(compress_image, src, root / "out")
        await status.edit_text("📤 در حال ارسال...")
        send_started = True
        with compressed.open("rb") as handle:
            await context.bot.send_document(
                user.id,
                document=handle,
                filename=compressed.name,
                caption=f"🗜️ فشرده شد: {name}",
            )
        delivered = True
        try:
            await status.delete()
        except Exception:
            # The photo was already sent. A timed-out cleanup must not turn success
            # into a red «compression failed» message.
            logger.info("Could not delete compression status for user %s", user.id, exc_info=True)
        compressed_size = compressed.stat().st_size if compressed.exists() else 0
        await _log_to_group(
            context,
            f"🗜️ [compress:{user.id}] عکس ارسال شد: {name}؛ "
            f"{original_size} → {compressed_size} بایت.",
        )
        await _try_send_analysis(context, user.id)
    except Exception as exc:
        logger.exception("Compress flow failed for user %s", user.id)
        await _log_to_group(
            context,
            f"🗜️ [compress:{user.id}] خطای فشرده‌سازی/ارسال: {type(exc).__name__}: {str(exc)[:240]}",
        )
        if delivered:
            # Any post-send failure is ancillary; the file is already in the admin chat.
            try:
                await status.edit_text("✅ عکس فشرده و ارسال شد.")
            except Exception:
                pass
        elif send_started and isinstance(exc, NetworkError):
            # Telegram may have accepted the document even though its response timed out.
            # Do not suggest an automatic retry that could send a duplicate file.
            try:
                await status.edit_text("⚠️ پاسخ تلگرام نرسید؛ ممکن است عکس ارسال شده باشد. قبل از تکرار، پیام‌ها را بررسی کن.")
            except Exception:
                pass
        else:
            try:
                await status.edit_text(f"❌ خطا: {type(exc).__name__}: {exc}")
            except Exception:
                pass
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return WAITING


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
    await query.answer()
    _clear_analysis(context)
    await query.edit_message_text(
        main_menu_text(user.id if user else None, user),
        reply_markup=main_menu_keyboard(user.id if user else None),
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def cmd_exit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/cancel, /start or /menu during the flow → back to the main menu."""
    user = update.effective_user
    _clear_analysis(context)
    await update.effective_message.reply_html(
        main_menu_text(user.id if user else None, user),
        reply_markup=main_menu_keyboard(user.id if user else None),
    )
    return ConversationHandler.END


# Registered at import time (not only in register(app)): the guard must know
# how to close this flow even if a test or tool imports the module directly.
flow_guard.register("compress", "فشرده‌سازی عکس‌ها", close_for)


def register(app: Application) -> None:
    conv = ConversationHandler(
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
