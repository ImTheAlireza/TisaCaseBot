"""✂️ اتو کراپ ۱:۱ — every photo comes back square, and nothing else changes.

The shop wants square images (thumbnails, grid cards, marketplace rules). The
seller does not want a tool that quietly decides how much quality to lose. Those
wishes only agree if the tool does exactly one thing: **crop to a centred
square**, with the source's own encoder settings — see
:mod:`bot.services.image_cropper` for what "unchanged quality" means here.

Two consequences that show up in this file:

* the file is sent back as a **document**, never as a photo: Telegram recompresses
  photos, which would undo the whole promise;
* the reply card says the dimensions it turned (``۱۲۰۰×۸۰۰ → ۸۰۰×۸۰۰``) and, when the
  picture was already 1:1, says so instead of re-sending it.

Read-only towards the shop: this module never writes a product, a category, or a
price. Admins can use it while the sudo owner leaves the button visible in
«⚙️ تنظیمات».
"""

from __future__ import annotations

import html
import logging
import re
import time
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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
from bot.config import settings
from bot.constants import CB
from bot.services import flow_guard, metrics, worker, workspace
from bot.services.access import guard_feature
from bot.services.conversations import FlowConversationHandler
from bot.services.image_cropper import UnsupportedImage, crop_square
from bot.services.media_download import download_to
from bot.keyboards import main_menu_keyboard, main_menu_text
from bot.utils.ui import answer_and_edit

logger = logging.getLogger(__name__)

WAITING = 0
TEMP_DIR = settings.temp_dir / "crop"
FLOW_NAME = "auto_crop"

_active_data: dict[int, dict] = {}
#: work directories that must survive a `close_for`: an in-flight crop may still be
#: writing into them, and deleting the file under a worker is how a "square" arrives
#: half-written. They are removed by their own `finally` as usual.
active_roots: dict[int, set[Path]] = {}

#: همان‌طور که یک آدم توی چت می‌گوید، نه آن‌طور که یک راهنما می‌نویسد: یک خط
#: «چه بفرست»، یک خط «چطور تمومش کنی». هر چیزِ دیگری که به کار می‌آید کنارِ خودِ
#: نتیجه گفته می‌شود (ابعاد روی فایل، «دست نزدم») — نه این‌جا.
INSTRUCTION = (
    "✂️ <b>اتو کراپ ۱:۱</b>\n\n"
    "عکس بفرست، مربعش می‌کنم.\n"
    "هر وقت تموم شد /cancel."
)


def close_for(user_id: int) -> bool:
    """Stop this flow for ``user_id`` and drop its leftover workspace."""
    data = _active_data.pop(user_id, None)
    if data is not None:
        data["crop_closed"] = True
    changed = data is not None
    if TEMP_DIR.exists():
        prefix = f"{user_id}_"
        protected = active_roots.get(user_id, set())
        for path in TEMP_DIR.iterdir():
            if path.is_dir() and path.name.startswith(prefix) and path not in protected:
                workspace.remove(path)
                changed = True
    return changed


# Registered at import time (not only in register(app)): the guard must know how
# to close this flow even if a test or tool imports the module directly.
flow_guard.register(FLOW_NAME, "اتو کراپ ۱:۱", close_for)


def _can_crop(user_id: int | None) -> bool:
    """True for sudo, and for admins only while the button is visible to them."""
    return feature_allowed(user_id, FLOW_NAME)


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


async def _crop_and_send(
    message,
    context: ContextTypes.DEFAULT_TYPE,
    file_id: str,
    name: str,
    root: Path,
) -> str:
    """Download, crop, send. Returns a one-line outcome for the log or the user."""
    source = root / f"{message.message_id:09d}_{_safe_name(name, 'image.jpg')}"
    download_started = time.perf_counter()
    size = await download_to(context.bot, file_id, source, max_mb=settings.max_download_mb)
    download_ms = (time.perf_counter() - download_started) * 1000

    crop_started = time.perf_counter()
    result = await worker.run(
        crop_square, source, root / "out", timeout=settings.process_timeout_seconds
    )
    crop_ms = (time.perf_counter() - crop_started) * 1000
    metrics.observe("crop_download_ms", download_ms)
    metrics.observe("crop_cpu_ms", crop_ms)

    if not result.changed:
        await message.reply_text(
            f"✅ از قبل مربع بود ({result.size_text}) — دست نزدم."
        )
        return f"{name}: {result.change_text} · بدون تغییر"

    sent_started = time.perf_counter()
    with Path(result.path).open("rb") as handle:
        await context.bot.send_document(
            chat_id=message.chat_id,
            document=handle,
            filename=Path(result.path).name,
            caption=f"✂️ {result.change_text}",
        )
    upload_ms = (time.perf_counter() - sent_started) * 1000
    return (
        f"{name}: {result.change_text}؛ "
        f"{size:,} بایت؛ دریافت {download_ms / 1000:.1f}s، "
        f"برش {crop_ms / 1000:.1f}s، ارسال {upload_ms / 1000:.1f}s"
    )


async def _handle_media(
    user_id: int, message, context: ContextTypes.DEFAULT_TYPE, media: tuple[str, str]
) -> None:
    """One message, one crop; failures are reported per image and never swallow the next."""
    user_data = getattr(context, "user_data", {}) or {}
    try:
        root = workspace.new_dir(TEMP_DIR, user_id)
    except OSError:
        logger.exception("Could not create auto-crop workspace for user %s", user_id)
        await message.reply_text("❌ الان نمی‌تونم پردازش کنم؛ یه بار دیگه بفرست.")
        return
    active_roots.setdefault(user_id, set()).add(root)
    workspace.protect(root)
    file_id, name = media
    try:
        status = None
        try:
            status = await message.reply_text("✂️ دارم می‌برم…")
        except Exception:  # pragma: no cover - a failed progress note must not stop the work
            logger.info("Could not send crop progress for user %s", user_id, exc_info=True)
        if user_data.get("crop_closed"):
            await _drop(status)
            return
        try:
            summary = await _crop_and_send(message, context, file_id, name, root)
        except UnsupportedImage as exc:
            await message.reply_text(f"❌ نشد: {html.escape(str(exc))}")
            logger.info("auto-crop refused %s for user %s: %s", name, user_id, exc)
            await _drop(status)
            return
        except Exception:
            logger.exception("auto-crop failed for user %s (%s)", user_id, name)
            await message.reply_text("❌ یه چیزی خراب شد؛ توی لاگ نوشتم.")
            await _drop(status)
            return
        metrics.incr("crop_images")
        logger.info("✂️ [crop:%s] %s", user_id, summary)
        await _drop(status)
    finally:
        roots = active_roots.get(user_id)
        if roots is not None:
            roots.discard(root)
            if not roots:
                active_roots.pop(user_id, None)
        workspace.remove(root)


async def _drop(status) -> None:
    """Progress notes are helpful; a failed delete must not fail the crop."""
    if status is None:
        return
    try:
        await status.delete()
    except Exception:
        logger.info("Could not delete crop status message", exc_info=True)


async def entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """«✂️ اتو کراپ ۱:۱» pressed → wait for photos."""
    query = update.callback_query
    user = update.effective_user
    if not user or not _can_crop(user.id):
        metrics.note_denial(FLOW_NAME)
        await query.answer("⛔ دسترسی ندارید.", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    context.user_data["crop_closed"] = False
    _active_data[user.id] = context.user_data
    # «one thing at a time»: any other open flow of this user is closed first.
    closed = flow_guard.close_others(FLOW_NAME, user.id)
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


@guard_feature(FLOW_NAME, on_denial=close_for, checker=lambda uid, key: feature_allowed(uid, key))
async def on_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Crop every photo/document that arrives while the tool is open."""
    message = update.effective_message
    user = update.effective_user
    if not user or not message:
        return WAITING
    media = _media(message)
    if not media:
        await message.reply_text("❗ عکسی توی این پیام نبود.")
        return WAITING
    await _handle_media(user.id, message, context, media)
    return WAITING


@guard_feature(FLOW_NAME, on_denial=close_for, checker=lambda uid, key: feature_allowed(uid, key))
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Text is not a photo: say what is needed instead of silently ignoring it."""
    message = update.effective_message
    if not message or not (message.text or "").strip():
        return WAITING
    await message.reply_text("📷 عکس بفرست.")
    return WAITING


async def cb_back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """The in-flow «بازگشت به منو» button."""
    query = update.callback_query
    user = update.effective_user
    if user:
        close_for(user.id)
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
    await update.effective_message.reply_html(
        main_menu_text(user.id if user else None, user),
        reply_markup=main_menu_keyboard(user.id if user else None),
    )
    return ConversationHandler.END


async def shutdown() -> None:
    """Close every open crop flow (called from the application's shutdown list)."""
    for user_id in set(_active_data) | set(active_roots):
        close_for(user_id)


def register(app: Application) -> None:
    conv = FlowConversationHandler(
        flow=FLOW_NAME,
        entry_points=[CallbackQueryHandler(entry, pattern=f"^{CB.CROP}$")],
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
        name="auto_crop",
    )
    app.add_handler(conv)


__all__ = ["close_for", "register", "shutdown"]
