"""Conversation-driven product package builder.

The bot never calls the shared-host WordPress installation. It prepares a ZIP
which the future WordPress importer can turn into a draft variable product.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.util
import html
import json
import logging
import re
import shutil
import time
import traceback
import zipfile
from contextlib import AsyncExitStack, asynccontextmanager
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ChatAction
from telegram.error import BadRequest, TimedOut, NetworkError
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, ConversationHandler, CommandHandler, MessageHandler, filters

from bot import rbac
from bot import __version__ as _BOT_VERSION
from bot.buttons import feature_allowed
from bot.config import settings
from bot.constants import CB
from bot.keyboards import main_menu_keyboard, main_menu_text, result_card, result_keyboard
from bot.modules import outbox_flow, restock_flow
from bot.services import (
    draft_edits,
    flow_guard,
    flow_state,
    learning,
    learning_corpus,
    learning_impact,
    metrics,
    outbox,
    products_ledger,
    publish_batch,
    workspace,
)
from bot.services import postmodel as ev, product_journal
from bot.services.ai_normalizer import ai_client_session, ai_normalize
from bot.services.category_taxonomy import FORBIDDEN, TAXONOMY, apply_sku_category_policy
from bot.services.color_matrix import (
    is_color_attribute,
    is_model_attribute,
    parse_color_matrix,
    prune_unused_colors,
)
from bot.services.phone_parser import normalize_caption, unmatched_model_words
from bot.services import vocabulary
from bot.services.plan import plan_from_dict
from bot.services.stock_matrix import parse_stock_matrix_sources, strip_stock_matrix_sections
from bot.services.validation import validate_draft
from bot.services.image_compressor import compress_image, compressed_size_savings
from bot.services.product_extractor import (
    ProductData,
    _fallback,
    _number_from_line,
    extract_accessory_models,
    extract_product,
)
from bot.services.woo_client import describe_exception
from bot.services.woocommerce_direct import WooCommerceAPIError, create_draft, product_description

# One session owns one persistent Telegram card. COLLECT accepts images and the
# first product text; REVIEW keeps accepting both, and every free-text message is
# appended to the product info and rendered back into that same card. Only the
# deliberate field editor interprets text as a field value.
COLLECT = 0
EDITING_FIELD = 1
REVIEW = 2
#: the collecting state, named the way the older handlers speak it
WAITING = COLLECT

TEMP_DIR = Path("/tmp/tisaposttowp-products")

#: How many steps back the card can go. Each snapshot is one draft plus two
#: short texts — small enough that a handful never matters, deep enough that a
#: seller who mistyped two messages in a row can still walk back.
_UNDO_DEPTH = 5


@dataclass
class _DraftSnapshot:
    """The draft exactly as it was before one user action.

    An undo restores the *parsed draft itself* rather than re-reading the text,
    so it is instant, offline, and cannot be broken again by the same misreading
    that made it necessary. ``files`` is the list only — the images themselves
    stay in the temp workspace and go away with it either way.
    """

    data: ProductData | None
    info_text: str
    model_text: str
    files: list[Path]
    suppressed_colors: list[str]
    verified_fields: list[str]
    dismissed: list[str]
    last_extract_hash: str
    color_summary: str


@dataclass
class ProductSession:
    files: list[Path] = field(default_factory=list)
    model_text: str = ""
    info_text: str = ""
    models: list[str] = field(default_factory=list)
    data: ProductData | None = None
    #: The live preview card. It is the card that fills in while the AI reads the
    #: product, and the one that is deleted and re-posted at the bottom after
    #: every incoming message.
    status_message_id: int | None = None
    #: The first instruction message («عکس‌ها و اطلاعات را بفرست»). It is written
    #: once and then never touched again, so the instructions stay readable while
    #: the preview card moves to the bottom of the chat.
    guide_message_id: int | None = None
    #: Called by :func:`_extract` at each phase boundary so the card can show what
    #: is known so far. ``None`` when nobody is watching (the parser test calls
    #: ``_extract`` directly).
    on_stage: Any = None
    #: Drafts as they were before each user action, oldest first. «↩️ برگرداندن
    #: به حالت قبل» pops one and repaints the card from it — no AI call, no site
    #: request, so undoing a misread correction can never make things worse.
    history: list[_DraftSnapshot] = field(default_factory=list)
    mode: str = "new"
    image_mode: str = "keep"
    processing_media: bool = False
    # Human-readable trace of the detected per-model color matrix (for the log group).
    color_summary: str = ""
    # Everything this session writes on disk lives under ONE directory so that a
    # cleanup can never leave the original downloads behind.
    workspace: Path | None = None
    # Set while a product is being published. Publishing takes several seconds
    # (media upload + SKU scan), and a second tap used to create a second draft.
    submitting: bool = False
    # Text fingerprint of the last extraction, to avoid a pointless AI rerun.
    last_extract_hash: str = ""
    # During media intake, the captions are for models; expensive product-detail
    # extraction can wait until PRODUCT INFO arrives.
    defer_details: bool = False
    # Where the flow was started (chat + forum thread). Every proactive message
    # has to go back there; ``user.id`` was a private-chat assumption that breaks
    # in a topic chat.
    chat_id: int = 0
    thread_id: int | None = None
    # Field the owner chose to edit by hand ("" outside an edit step).
    editing_field: str = ""
    # Picker indexes, in the order the buttons were rendered: callback_data may
    # not carry a Persian label inside its 64 bytes, so a tap carries a number.
    field_keys: list[str] = field(default_factory=list)
    color_sources: list[str] = field(default_factory=list)
    # Messages whose color list belongs to another product (P1-11).
    suppressed_colors: list[str] = field(default_factory=list)
    # Offers the owner declined for this product, so they stop nagging.
    dismissed: list[str] = field(default_factory=list)
    # Fields whose value the bot only *inferred* (AI, OCR, a file name) and the
    # owner has since confirmed. Kept on the session so a re-extraction does not
    # ask the same question again.
    verified_fields: list[str] = field(default_factory=list)
    # Who owns this session: the learned-rule queue is sudo-only, and the
    # keyboard has to know that without a user object on every render.
    user_id: int = 0
    #: Set by «🔁 با این حال دوباره بساز»: publish the same content a second time on
    #: purpose. One-shot — it is cleared as soon as the gate is passed, so the next
    #: tap has to be asked again.
    force_publish: bool = False
    # Sanitized AI outcomes collected during one extraction and drained to its log card.
    ai_diagnostics: list[str] = field(default_factory=list)


sessions: dict[int, ProductSession] = {}
album_buffers: dict[tuple[int, str], list[Message]] = {}
album_tasks: dict[tuple[int, str], asyncio.Task] = {}

logger = logging.getLogger(__name__)


async def _edit_message_if_changed(target: Any, *args: Any, **kwargs: Any) -> bool:
    """Treat Telegram's idempotent "message is not modified" response as a no-op.

    Callback updates can be delivered again after the preview was already replaced (or
    still contains the same validation text). That is not an operational failure; other
    Telegram errors remain visible to the caller.
    """
    try:
        await target.edit_message_text(*args, **kwargs)
        return True
    except BadRequest as exc:
        if "message is not modified" in str(exc).casefold():
            logger.debug("Telegram edit skipped: message content and markup are unchanged")
            return False
        raise


async def _send_chat_action(
    context: ContextTypes.DEFAULT_TYPE, user_id: int, session: ProductSession
) -> None:
    """Show Telegram's short-lived typing indicator without sending a message.

    Telegram has no text-message toast API: ``answerCallbackQuery`` only works
    for button taps. ``sendChatAction`` is the closest transient indicator for
    ordinary messages and expires automatically.
    """
    bot = getattr(context, "bot", None)
    send_action = getattr(bot, "send_chat_action", None)
    if send_action is None:
        return
    try:
        await send_action(action=ChatAction.TYPING, **_target(session, user_id))
    except Exception as exc:
        # A typing indicator is best-effort and must never break product intake.
        logger.debug("Telegram chat action unavailable (%s): %s", type(exc).__name__, exc)


async def _chat_action_heartbeat(
    context: ContextTypes.DEFAULT_TYPE, user_id: int, session: ProductSession
) -> None:
    """Refresh Telegram's five-second chat action while a long task is running."""
    try:
        while True:
            await asyncio.sleep(4)
            await _send_chat_action(context, user_id, session)
    except asyncio.CancelledError:
        raise


@asynccontextmanager
async def _typing_indicator(
    context: ContextTypes.DEFAULT_TYPE, user_id: int, session: ProductSession
) -> AsyncIterator[None]:
    """Maintain the transient typing indicator for the duration of one operation."""
    await _send_chat_action(context, user_id, session)
    task = asyncio.create_task(_chat_action_heartbeat(context, user_id, session))
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def _delete_card(bot: Any, session: ProductSession, target: dict[str, object]) -> bool:
    """Delete the previous card so its replacement can be posted at the bottom.

    A bot may only delete its own messages, and only for ~48 hours; when Telegram
    refuses, the caller falls back to editing. That keeps exactly one card in the
    chat either way — a second live preview with stale buttons is worse than an
    old card that simply stays where it was.
    """
    if session.status_message_id is None:
        return False
    delete = getattr(bot, "delete_message", None)
    if delete is None:
        return False
    try:
        await delete(chat_id=int(target["chat_id"]), message_id=session.status_message_id)
        return True
    except Exception as exc:
        logger.debug("Telegram refused to delete the old card (%s): %s", type(exc).__name__, exc)
        return False


async def _react_failure(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    session: ProductSession,
    message: Message | None,
) -> bool:
    """Put a transient ⚡ reaction on the message whose processing failed.

    Telegram's only real toast is ``answerCallbackQuery``, which works for button
    taps only; there is no way to pop up text for an ordinary message. A reaction
    comes closest: it is transient, adds nothing to the chat history, and leaves
    the preview card alone. The chat may have reactions disabled, so this is
    strictly best-effort.
    """
    bot = getattr(context, "bot", None)
    set_reaction = getattr(bot, "set_message_reaction", None)
    message_id = getattr(message, "message_id", None)
    if set_reaction is None or message_id is None:
        return False
    chat_id = getattr(message, "chat_id", None) or _target(session, user_id)["chat_id"]
    try:
        await set_reaction(
            chat_id=int(chat_id), message_id=int(message_id), reaction=["⚡"],
        )
        return True
    except Exception as exc:                                 # never escalate a signal
        logger.debug("could not set the failure reaction (%s): %s", type(exc).__name__, exc)
        return False


async def _report_failure(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    session: ProductSession,
    detail: str,
    *,
    message: Message | None = None,
) -> None:
    """Report a failure transiently — never as a message, never on the card.

    The seller asked for a toast, not extra chat traffic: a photo that fails to
    process gets a ⚡ reaction on itself, the preview card is left exactly as it
    was, and the technical trace goes to the log group. When Telegram cannot show
    even the reaction (reactions off, no message id), the failure stays in the
    logs rather than turning into a second message.
    """
    detail = re.sub(r"\s+", " ", (detail or "").strip())[:180]
    logger.warning("[product:%s] %s", user_id, detail)
    await _react_failure(context, user_id, session, message)


async def _render_card(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    session: ProductSession,
    text: str,
    *,
    parse_mode: str | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
    move_to_bottom: bool = False,
) -> int | None:
    """Create the flow card once, then edit that same message for every step.

    ``move_to_bottom`` is for renders that follow an **incoming user message**:
    the old card is deleted and a fresh one is sent below that message, so the
    newest preview is always the last thing in the chat. Button taps keep editing
    the card in place — it is already under the finger, and re-posting on every
    tap would only add traffic.
    """
    bot = getattr(context, "bot", None)
    if bot is None:
        raise RuntimeError("product card cannot be rendered without a Telegram bot")
    target = _target(session, user_id)
    kwargs: dict[str, Any] = {
        "text": text,
        "reply_markup": reply_markup if reply_markup is not None else InlineKeyboardMarkup([]),
    }
    if parse_mode:
        kwargs["parse_mode"] = parse_mode

    if move_to_bottom and await _delete_card(bot, session, target):
        # The old card is gone; forgetting its id keeps the send path below single.
        session.status_message_id = None

    if session.status_message_id is None:
        message = await bot.send_message(**kwargs, **target)
        session.status_message_id = getattr(message, "message_id", None)
        return session.status_message_id

    thread = (
        {"message_thread_id": target["message_thread_id"]}
        if "message_thread_id" in target else {}
    )
    await _edit_message_if_changed(
        bot,
        text,
        chat_id=int(target["chat_id"]),
        message_id=session.status_message_id,
        reply_markup=kwargs["reply_markup"],
        **({"parse_mode": parse_mode} if parse_mode else {}),
        **thread,
    )
    return session.status_message_id


async def _telegram_log(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Record one step of this product — the card at the end is what gets sent.

    Fourteen separate messages per product used to arrive at the log group, which read
    like nothing. Every line still exists (and reaches the chat as a second message when
    ``VERBOSE_LOG=1``); it simply no longer competes with itself for attention.
    """
    journal = product_journal.journal_for(context)
    if journal is not None:
        journal.line(text)
    logger.info("%s", text)


async def _flush_journal(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    status: str,
    data: ProductData,
    session: ProductSession,
    batch: str = "",
    product_id: int | str | None = None,
    edit_url: str = "",
    warnings: Sequence[str] = (),
    errors: Sequence[str] = (),
) -> None:
    """Close this product's card with the facts of its outcome, and send it.

    One place builds the fields for every ending (created / dry / zip / queued /
    failed), so a branch added later cannot quietly produce a half-empty card.
    """
    journal = product_journal.journal_for(context)
    for warning in warnings:
        if journal is not None:
            journal.warn(str(warning))
    for error in errors:
        if journal is not None:
            journal.fail(str(error))
    price = int(getattr(data, "price", 0) or 0)
    price_text = f"{price:,} تومان" if price else ""
    model_prices = dict(getattr(data, "model_prices", None) or {})
    if model_prices:
        shown = list(model_prices.items())[:3]
        detail = " | ".join(f"{model}: {value:,}" for model, value in shown)
        if len(model_prices) > len(shown):
            detail += f" | +{len(model_prices) - len(shown)} مدل"
        price_text = f"{price_text} | {detail}" if price_text else detail
    await product_journal.flush(
        context,
        status=status,
        title=(getattr(data, "title", "") or "").strip()[:80],
        product_id=product_id,
        edit_url=edit_url,
        batch_id=batch,
        variations=getattr(data, "variation_count", 0),
        images=len(session.files),
        price=price_text,
        mode=session.mode,
    )


def _audit_for_chat(lines: list[str]) -> str:
    """Condense the WooCommerce audit into a short diagnostic for operator text.

    The single most valuable line is the FIRST POST attempt: it carries the
    actual WooCommerce error message and body. The per-attempt SKU probes are
    collapsed into one-line counts so that repeated collisions do not bury the
    useful error (the complete, truthfully labeled trace belongs in the log group).
    """
    if not lines:
        return ""
    config = [line for line in lines if line.startswith("[config]")]
    attempts = [line for line in lines if line.startswith("[attempt")]
    sku_lines = [line for line in lines if line.startswith("[sku]")]

    if not attempts:
        # No POST happened (e.g. media upload or category lookup failed
        # earlier): show the non-payload steps verbatim.
        return "\n".join(line for line in lines if not line.startswith("[payload]"))

    parts: list[str] = list(config)
    parts.append(attempts[0])
    if len(attempts) > 2:
        parts.append(attempts[-1])

    # Keep the HTTP timeline concise but never drop the exact failed request. This is
    # especially useful when several image uploads or variations were in flight together.
    stage_lines = [
        line for line in lines
        if line.startswith(("[media:start]", "[media:error]", "[variation:start]", "[variation:error]", "[variation:batch]"))
    ]
    if stage_lines:
        parts.append("جزئیات رسانه/واریژن:")
        recent_stages = stage_lines[-5:]
        parts.extend(recent_stages)
        parts.extend(
            line for line in stage_lines
            if line.startswith(("[media:error]", "[variation:error]")) and line not in recent_stages
        )

    http_lines = [line for line in lines if line.startswith(("[http:start]", "[http:done]", "[http:error]", "[retry]"))]
    if http_lines:
        parts.append("گزارش HTTP (آخرین درخواست‌ها):")
        recent = http_lines[-6:]
        parts.extend(recent)
        parts.extend(
            line for line in http_lines
            if line.startswith("[http:error]") and line not in recent
        )

    plugin = [line for line in sku_lines if "next-sku" in line]
    free = [line for line in sku_lines if "آزاد است" in line]
    ghosts = [line for line in sku_lines if "رکورد شبح" in line]
    jumps = [line for line in sku_lines if "پرش" in line]
    stops = [line for line in sku_lines if "توقف" in line]

    if plugin:
        parts.append(plugin[-1])
    if free:
        parts.append(free[0])
    if ghosts:
        parts.append(f"→ {len(ghosts)} رکورد شبح پشت‌سرهم شناسایی شد.")
    if jumps:
        match = re.search(r"کاندید بعدی (\S+)", jumps[-1])
        last = match.group(1) if match else "؟"
        parts.append(f"→ {len(jumps)} پرش هندسی تا «{last}»؛ همه اشغال بودند.")
    parts.extend(stops)
    return "\n".join(parts)


def _dry_run_report(lines: Sequence[str], budget: int = 3600) -> str:
    """Compatibility wrapper for the dry-run trace format (sent to the log group)."""
    return product_journal.publish_trace_report(lines, dry_run=True, budget=budget)


def _already_published_note(entry: dict[str, object]) -> str:
    """Say *which* product already exists, and how to get to it.

    Refusing a duplicate is only useful if the owner can see the thing that was
    already made — otherwise the answer is «بزن دوباره تا درست شود» and a second
    product, which is the exact bug this gate exists to prevent.
    """
    when = time.strftime("%Y/%m/%d %H:%M", time.localtime(float(entry.get("ts") or 0)))
    title = html.escape(str(entry.get("title") or "—"), quote=False)
    ident = entry.get("product_id")
    url = str(entry.get("edit_url") or "")
    lines = [
        "♻️ <b>این محتوا پیش‌تر منتشر شده است</b>",
        f"«{title}» در {when} ساخته شد" + (f" (id: <code>{ident}</code>)" if ident else "") + ".",
        "اگر دوباره تأیید کنی، یک محصول <b>تکراری با SKU تازه</b> ساخته می‌شود — ووکامرس"
        " جلوی عنوان تکراری را نمی‌گیرد، پس اینجا ربات در را نگه داشته است.",
        "برای دیدن همان محصول: «🧾 آخرین محصولات». برای ساخت عمدیِ دومی: دکمهٔ زیر.",
    ]
    if url:
        lines.append(f'<a href="{html.escape(url, quote=True)}">🔗 ویرایش همان محصول در سایت</a>')
    return "\n".join(lines)


def _keyboard(session: ProductSession | None = None) -> InlineKeyboardMarkup:
    confirm_label = "✅ تأیید و ساخت پیش‌نویس" if not session or session.mode == "new" else "✅ تأیید و ساخت ZIP"
    rows = [[InlineKeyboardButton(confirm_label, callback_data="product:confirm")]]
    if session and session.mode == "update":
        keep = "✅ تصاویر فعلی" if session.image_mode == "keep" else "تصاویر فعلی"
        replace = "✅ جایگزینی تصاویر" if session.image_mode == "replace" else "جایگزینی تصاویر"
        rows.insert(0, [
            InlineKeyboardButton(keep, callback_data=CB.PHONE_IMAGE_KEEP),
            InlineKeyboardButton(replace, callback_data=CB.PHONE_IMAGE_REPLACE),
        ])
    data = session.data if session else None
    if data is not None:
        dismissed = set(session.dismissed)
        for index, item in enumerate(getattr(data, "suggestions", None) or []):
            if f"{item.get('kind')}:{item.get('word')}" in dismissed:
                continue
            rows.append([
                InlineKeyboardButton(
                    f"✅ بله، «{item.get('word')}» یعنی «{item.get('target')}»",
                    callback_data=f"product:sug:{index}",
                ),
                InlineKeyboardButton("⏭️ نه", callback_data=f"product:sug:no:{index}"),
            ])
        if _open_questions(session):
            rows.append([InlineKeyboardButton(
                "✅ تأیید حدس‌ها", callback_data=CB.PRODUCT_CONFIRM_GUESSED
            )])
        rows.append([InlineKeyboardButton("✏️ اصلاح فیلد خاص", callback_data="product:edit"),
                     InlineKeyboardButton("➕ افزودن عکس یا متن", callback_data="product:addmore")])
        if session.history:
            # Only offered when there is something to go back to: a permanent
            # «برگرداندن» button that answers «چیزی نیست» is just noise.
            rows.append([InlineKeyboardButton("↩️ برگرداندن به حالت قبل", callback_data="product:undo")])
        pending = learning.pending_rules() if rbac.is_sudo(session.user_id) else []
        if pending:
            # The rule was learned while this product was being built; sending the
            # owner to the memory screen is the shortest honest path to the
            # impact preview and the two buttons that act on it.
            rows.append([InlineKeyboardButton(
                f"⏳ {len(pending)} قاعدهٔ تازه در انتظار تأیید ({_pending_hint(pending)})",
                callback_data=CB.LEARNING_PENDING,
            )])
        sources = draft_edits.colors_by_message(
            ev.parse_sources([("info", session.info_text), ("caption", session.model_text)])
        )
        if len(sources) > 1:
            rows.append([InlineKeyboardButton("🎨 رنگ‌ها از چند پیام آمده (جدا کردن)", callback_data="product:colorsrc")])
    rows.append([InlineKeyboardButton("❌ لغو", callback_data="product:cancel")])
    return InlineKeyboardMarkup(rows)


def _short(text: str, limit: int = 32) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _fields_keyboard(session: ProductSession) -> InlineKeyboardMarkup:
    rows = []
    for index, key in enumerate(session.field_keys):
        label, _, current = next(
            (row for row in draft_edits.editable_fields(session.data) if row[0] == key), (key, key, "")
        )
        edit_button = InlineKeyboardButton(
            f"✏️ {label}: {_short(current, 24)}", callback_data=f"product:field:{index}"
        )
        if (key == "colors" and current != "—") or key.startswith("attr:"):
            rows.append([edit_button, InlineKeyboardButton("🗑 حذف", callback_data=f"product:field:delete:{index}")])
        else:
            rows.append([edit_button])
    rows.append([InlineKeyboardButton("↩️ بازگشت", callback_data="product:fields:back")])
    return InlineKeyboardMarkup(rows)


def _color_source_keyboard(session: ProductSession) -> InlineKeyboardMarkup:
    rows = []
    for index, label in enumerate(session.color_sources):
        state = "↩️ برگرداندن" if label in session.suppressed_colors else "➖ حذف رنگ‌های این پیام"
        rows.append([InlineKeyboardButton(f"{label} — {state}", callback_data=f"product:colorsrc:{index}")])
    rows.append([InlineKeyboardButton("↩️ بازگشت به پیش‌نمایش", callback_data="product:fields:back")])
    return InlineKeyboardMarkup(rows)


def _record_result(
    user_id: int, session: ProductSession, data: ProductData, *, status: str, error: str = "",
    product_id: object = None, edit_url: str = "", warnings: Sequence[str] = (),
    key: str | None = None, batch_id: str = "",
) -> dict[str, object]:
    """Store the outcome, and return the entry the card is built from.

    Failures are recorded too: a silent crash is what makes a shop owner ask
    «چرا سایت خالی است؟» with nothing to look at.

    With a ``key`` this *finishes* the pending intent written before the first
    request (plan 4.3) instead of appending a second card for the same attempt —
    one attempt, one card, whatever the outcome.
    """
    fields: dict[str, object] = {
        "status": status,
        "product_id": product_id,
        "edit_url": edit_url,
        "mode": session.mode,
        "title": data.title,
        "variations": data.variation_count,
        "price": data.price,
        "price_groups": data.prices,
        "model_prices": data.model_prices,
        "sale_price": data.sale_price,
        "stock": data.stock,
        "stock_status": data.stock_status,
        "stock_matrix": data.stock_matrix,
        "sku_prefix": data.sku_prefix,
        "images": len(session.files),
        "categories": data.categories,
        "warnings": list(warnings),
        "error": error,
        "report": _preview(session),
    }
    if key:
        finished = products_ledger.update(key, **fields)
        if finished is not None:
            return finished
    return products_ledger.record(user_id=user_id, batch_id=batch_id, key=key, **fields)  # type: ignore[arg-type]


async def show_preview(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    session = _session_of(query.from_user.id if query.from_user else 0, context)
    if session is None or session.data is None:
        return REVIEW
    await _render_card(
        context, query.from_user.id, session, _preview(session),
        parse_mode="HTML", reply_markup=_keyboard(session),
    )
    return REVIEW


def _zip_manifest(
    data: ProductData, *, usable_attributes: dict[str, list[str]], image_mode: str, batch: str,
    mode: str = "new",
) -> dict[str, object]:
    """``product.json`` inside the ZIP — the same facts the REST writer was given.

    Two shapes for the same product is how a seller ends up with two different shops, so the
    keys are written from here and nowhere else. ``model_colors`` travels with the ZIP so the
    WordPress importer builds the same restricted variation matrix instead of the full
    cartesian product; ``stock``/``stock_matrix``/``sale_price`` travel too even though today's importer
    does not apply them (docs/IMPORTER-CONTRACT.md says which keys are honoured and which are not —
    a key the plugin ignores is visible in the file, which beats a feature that exists only
    on one path).
    """
    return {
        "mode": mode,
        "title": data.title,
        "price": data.price,
        "prices": data.prices,
        "model_prices": data.model_prices,
        "wholesale_price": data.wholesale_price,
        "wholesale_model_prices": data.wholesale_model_prices,
        "sale_price": data.sale_price,
        "stock": data.stock,
        "stock_status": data.stock_status,
        "stock_matrix": data.stock_matrix,
        "sku_prefix": data.sku_prefix,
        "models": data.models,
        "attributes": usable_attributes,
        "model_colors": data.model_colors,
        "categories": data.categories,
        "description": product_description(data.to_dict()),
        "product_type": "variable" if usable_attributes else "simple",
        "image_mode": image_mode,
        "batch_id": batch,
    }


def _safe(name: str, fallback: str) -> str:
    """A file name we can actually write, still recognisable to the seller.

    Non-ASCII letters are *kept* (``\\w`` in this regex is Unicode-aware): the seller names a photo
    «01_مشکی.jpg», and that name is the only thing tying the picture to that colour — a
    per-variation image and a colour that silently loses its photo are the same bug.
    Slashes, control characters and the extension stay handled; the length is capped
    because a file system cares about bytes, not characters.
    """
    raw = Path(name).name
    stem, suffix = raw.rsplit(".", 1) if "." in raw[1:] else (raw, "")
    stem = re.sub(r"[^\w.-]+", "_", stem).strip("._")
    suffix = re.sub(r"[^A-Za-z0-9]+", "", suffix).lower()
    if not stem:
        return fallback
    return f"{stem[:80]}.{suffix}" if suffix else stem[:80]


def _media(message: Message) -> tuple[str, str] | None:
    if message.photo:
        return message.photo[-1].file_id, f"image_{message.message_id}.jpg"
    if message.document and (message.document.mime_type or "").startswith("image/"):
        return message.document.file_id, _safe(message.document.file_name or "image.jpg", f"image_{message.message_id}.jpg")
    return None


def _caption(messages: list[Message]) -> str:
    return "\n".join((m.caption or m.text or "") for m in sorted(messages, key=lambda x: x.message_id) if (m.caption or m.text))


def _append_model_caption(existing: str, incoming: str) -> str:
    """Keep model text from earlier photo batches when another batch arrives."""
    old = (existing or "").strip()
    new = (incoming or "").strip()
    if not new:
        return old
    if not old:
        return new
    if any(block.strip().casefold() == new.casefold() for block in old.split("\n\n")):
        return old
    return f"{old}\n\n{new}"


def _category_tree_lines(categories: Sequence[str]) -> list[str]:
    """Render category paths once as a compact parent/child tree."""
    tree: dict[str, dict] = {}
    visible = [str(category) for category in categories if str(category).strip() not in FORBIDDEN]
    for raw_path in _canonical_category_paths(visible):
        parts = [
            html.unescape(part).strip()
            for part in str(raw_path).replace("&gt;", ">").split(">")
            if html.unescape(part).strip()
        ]
        branch = tree
        for part in parts:
            branch = branch.setdefault(part, {})

    lines: list[str] = []

    def append_branch(branch: dict[str, dict], depth: int = 0) -> None:
        for name, children in branch.items():
            lines.append(f"{'  ' * depth}- {html.escape(name)}")
            append_branch(children, depth + 1)

    append_branch(tree)
    return lines


#: The order the live card fills in. Every stage keeps this order and only adds
#: rows, so a value that appeared once never jumps around while the AI works —
#: title first, then SKU, price, stock, models/attributes, categories.
CARD_STAGES: dict[str, tuple[str, ...]] = {
    "text": ("title", "sku", "price", "stock"),
    "models": ("title", "sku", "price", "stock", "features"),
    "full": ("title", "sku", "price", "stock", "features", "categories", "issues"),
}

#: What the card admits to while it is still filling (never a separate message).
CARD_FOOTERS: dict[str, str] = {
    "text": "⏳ در حال خواندن مدل و ویژگی‌ها…",
    "models": "⏳ در حال خواندن ویژگی‌ها و دسته‌بندی…",
}


def _title_rows(data: ProductData, *, placeholder: bool) -> list[str]:
    if not data.title and not placeholder:
        return []
    title = html.escape(data.title) if data.title else "—"
    return [f"<b>عنوان:</b> {title}"]


def _sku_rows(data: ProductData, *, placeholder: bool) -> list[str]:
    if not data.sku_prefix and not placeholder:
        return []
    sku = html.escape(data.sku_prefix) if data.sku_prefix else "—"
    return [f"<b>شناسه:</b> {sku}"]


def _price_rows(data: ProductData, *, placeholder: bool) -> list[str]:
    lines: list[str] = []
    if data.prices:
        price_parts = [f"{html.escape(str(group))}: {value:,} تومان" for group, value in data.prices.items()]
        if data.price:
            price_parts.append(f"پایه: {data.price:,} تومان")
        price_text = " | ".join(price_parts)
    else:
        price_text = f"{data.price:,} تومان" if data.price else "—"
    if data.price or data.prices or placeholder:
        lines.append(f"<b>قیمت:</b> {price_text}")
    if data.model_prices:
        lines.append(
            "<b>قیمت مدل‌های خاص:</b> " + " | ".join(
                f"{html.escape(str(model))}: {value:,} تومان"
                for model, value in data.model_prices.items()
            )
        )
    if data.sale_price:
        lines.append(f"<b>قیمت ویژه:</b> {data.sale_price:,} تومان")
    if data.wholesale_price or data.wholesale_model_prices:
        wholesale_parts = []
        if data.wholesale_price:
            wholesale_parts.append(f"پایه: {data.wholesale_price:,} تومان")
        wholesale_parts += [
            f"{html.escape(str(model))}: {value:,} تومان"
            for model, value in data.wholesale_model_prices.items()
        ]
        lines.append(
            "<b>قیمت همکاری (فقط ثبت، در سایت اعمال نمی‌شود):</b> " + " | ".join(wholesale_parts)
        )
    return lines


def _stock_rows(data: ProductData, plan: Any) -> list[str]:
    """Stock, availability and the explicit design × model table."""
    lines: list[str] = []
    if data.stock is not None:
        lines.append(f"<b>موجودی:</b> {data.stock:,} عدد")
    elif data.stock_status == "outofstock":
        lines.append("<b>موجودی:</b> ناموجود")
    elif data.stock_status == "onbackorder":
        lines.append("<b>موجودی:</b> پیش‌فروش")
    if data.stock_matrix or data.stock_matrix_errors:
        matrix_models = list(data.models) or list(dict.fromkeys(
            model for row in data.stock_matrix.values() for model in row
        ))
        matrix_total = sum(
            quantity for row in data.stock_matrix.values() for quantity in row.values()
            if quantity is not None
        )
        unavailable = sum(
            quantity is None for row in data.stock_matrix.values() for quantity in row.values()
        )
        lines.append(
            f"<b>موجودی طرح × دسته:</b> {len(data.stock_matrix)} طرح × "
            f"{len(matrix_models)} دسته؛ {matrix_total:,} عدد؛ {plan.count} واریژن"
        )
        if matrix_models and data.stock_matrix:
            lines.append("<b>ترتیب ستون‌ها:</b> " + " | ".join(
                f"{index + 1}={html.escape(model)}" for index, model in enumerate(matrix_models)
            ))
            rows = [
                f"{html.escape(design)}: " + " | ".join(
                    "?" if model not in row else "—" if row[model] is None else str(row[model])
                    for model in matrix_models
                )
                for design, row in data.stock_matrix.items()
            ]
            max_rows = 24
            if len(rows) > max_rows:
                rows = [*rows[:max_rows], f"… و {len(data.stock_matrix) - max_rows} طرح دیگر"]
            matrix_rows = "\n".join(rows)
            lines.append(f"<pre>{matrix_rows}</pre>")
        if unavailable:
            lines.append(f"ترکیب‌هایی که با «-» حذف می‌شوند: {unavailable}")
    return lines


def _feature_rows(data: ProductData, plan: Any) -> list[str]:
    lines = ["", "<b>ویژگی‌ها:</b>"]
    models = plan.models or list(data.models or [])
    model_text = " | ".join(html.escape(model) for model in models) if models else "—"
    lines.append(f"<b>مدل:</b> {model_text}")
    for name, values in plan.axes:
        if name == "مدل":
            continue
        lines.append(f"<b>{html.escape(name)}:</b> " + " | ".join(html.escape(value) for value in values))
    lines.append(f"<b>نوع محصول:</b> {'متغیر' if plan.is_variable else 'ساده'}")
    return lines


def _category_rows(data: ProductData) -> list[str]:
    lines = ["", "<b>دسته‌بندی:</b>"]
    category_lines = _category_tree_lines(data.categories)
    lines.extend(category_lines or ["—"])
    return lines


def _validation_issues(session: ProductSession) -> Any:
    data = session.data
    return validate_draft(
        data.to_dict() if data is not None else {},
        mode=session.mode,
        image_count=len(session.files),
        price_min=settings.price_min,
        price_max=settings.price_max,
        require_models=settings.require_models,
        unapplied_model_words=list(
            unmatched_model_words("\n".join((session.model_text, session.info_text)))
        ),
    )


def _issue_rows(issues: Any) -> list[str]:
    lines = [f"⛔ {html.escape(issue.message)}" for issue in issues.errors[:3]]
    lines.extend(f"⚠️ {html.escape(issue.message)}" for issue in issues.warnings[:3])
    return lines


def _card_text(
    session: ProductSession,
    *,
    stage: str = "full",
    data: ProductData | None = None,
    footer: str = "",
) -> str:
    """Render the live card at one stage of the fill.

    One renderer for both the still-filling card and the final preview, so the
    numbers cannot differ between what the owner watched appear and what they
    finally approve.
    """
    data = session.data if data is None else data
    if data is None:
        return "❌ هنوز اطلاعات محصولی ندارم."
    plan = plan_from_dict(data.to_dict())
    data.variation_count = plan.count
    placeholder = stage == "full"

    lines = ["📦 <b>پیش‌نمایش</b>"]
    if settings.woo_dry_run:
        lines.append("🧪 حالت آزمایشی")
    rows: dict[str, list[str]] = {
        "title": _title_rows(data, placeholder=placeholder),
        "sku": _sku_rows(data, placeholder=placeholder),
        "price": _price_rows(data, placeholder=placeholder),
        "stock": _stock_rows(data, plan),
        "features": _feature_rows(data, plan),
        "categories": _category_rows(data),
        "issues": _issue_rows(_validation_issues(session)) if stage == "full" else [],
    }
    for name in CARD_STAGES[stage]:
        if name == "features" and not (data.models or data.attributes) and not placeholder:
            continue
        lines.extend(rows[name])
    if footer:
        lines.extend(["", footer])
    return "\n".join(lines)


def _preview(session: ProductSession) -> str:
    """The finished card: every field, with «—» where the product has none."""
    return _card_text(session, stage="full")


def _quick_data(session: ProductSession) -> ProductData:
    """A cheap local read of the seller's own text — no network, no AI.

    This is what the live card is first painted from, so the seller sees their
    own numbers the moment their message arrives instead of a spinner. What it
    deliberately does **not** read: a title/price/SKU out of a media caption while
    PRODUCT INFO is still empty, because that is exactly the value the real
    pipeline refuses to trust — showing it would make the card flap.
    """
    models = list(session.models or [])
    if session.info_text.strip():
        return _fallback(session.info_text, models)
    return ProductData(models=models)


def _staged_preview(
    session: ProductSession, stage: str, *, data: ProductData | None = None
) -> str:
    """The card while it is still filling — only what is known at this stage."""
    return _card_text(
        session, stage=stage, data=data, footer=CARD_FOOTERS.get(stage, "")
    )


def _pending_hint(rules: list[learning.Rule]) -> str:
    """A readable name for the pending rules on the review button.

    A term rule is recognisable by the wrong word («سبز»); a price rule's key is
    only a digit count, so «4» alone would read like a bug report — «4 رقمی» says
    which numbers it is about.
    """
    parts = []
    for rule in rules[:3]:
        parts.append(rule.key[:14] if rule.kind == "term" else f"{rule.key} رقمی")
    if len(rules) > 3:
        parts.append(f"+{len(rules) - 3}")
    return "، ".join(parts)


def _open_questions(session: ProductSession) -> list[str]:
    """A short heads-up for inferred fields; their actual values are already above."""
    data = session.data
    if data is None:
        return []
    evidence = getattr(data, "evidence", None) or {}
    guessed = [name for name in ev.inferred_fields(evidence) if name not in set(session.verified_fields)]
    if not guessed:
        return []
    editable = {key: label for key, label, _value in draft_edits.editable_fields(data)}
    aliases = {"category": "categories", "colors": "colors", "model": "models"}
    labels = list(dict.fromkeys(
        editable.get(aliases.get(name, name), name) for name in guessed
    ))
    return ["⚠️ حدسی: " + "، ".join(labels)]


def _learn_from_diff(
    previous: ProductData | None,
    current: ProductData,
    incoming: str,
    source_text: str,
    context: str = "",
) -> list[str]:
    """Turn the owner's correction into a durable rule; return chat announcements.

    A correction is a field that already had a value and changed once the newest
    message was folded into PRODUCT INFO. Two guards keep this from learning
    nonsense:

    * the corrected value must actually be stated in the message that just
      arrived — that is what separates «you corrected me» from «the AI changed
      its mind between runs»;
    * only patterns that *generalize* become rules: a bare price that was off by
      a power of ten (so «1098» teaches 4-digit bare amounts = thousands), or a
      single word swapped for another. Anything else is logged, not memorized,
      because a whole rewritten title says nothing about the next product.

    ``previous`` is None before the first extraction — nothing to compare
    against yet, so the first message can never be a "correction".
    """
    if previous is None:
        return []
    notes: list[str] = []

    def _origin() -> str:
        """A keyword the owner really typed, to narrow this rule to later.

        Not the canonical model name: «iPhone 13» is what the parser decided, and a
        scope nobody can find in their own text is a rule that silently stops
        applying — worse than no scope at all. So each candidate is accepted only
        when it survives `learning.fold` inside the product's text, and if nothing
        qualifies the rule stays shop-wide and «🎯» refuses to narrow it.
        """
        where = learning.fold((context or source_text) + "\n" + (incoming or ""))
        candidates: list[str] = [str(x).strip() for x in (current.models or []) if x]
        candidates += [str(c).split(">")[-1].strip() for c in (current.categories or []) if c]
        tokens = [t for t in learning.fold(current.title).split() if len(t) >= 4]
        candidates += sorted(tokens, key=len, reverse=True)[:3]
        for candidate in candidates:
            folded = learning.fold(candidate)
            if len(folded) >= 2 and folded in where:
                return candidate
        return ""

    def _note(field: str, old: object, new: object, rule: learning.Rule | None) -> bool:
        learned = learning.remember(
            rule,
            learning.Correction(field=field, old=str(old), new=str(new)),
            origin=_origin(),
        )
        if learned and rule is not None:
            # Never "I applied it": a fresh rule is a proposal, and the replay is
            # the only way to say what it would do before it does it.
            notes.append(
                f"🧠 <b>یاد گرفتم (هنوز اعمالش نکرده‌ام):</b> {rule.describe()}\n"
                + learning_impact.preview(rule)
            )
        return learned

    def _stated_in_incoming(value: int) -> bool:
        return any(
            _number_from_line(line) == value for line in (incoming or "").splitlines()
        )

    # --- prices: the case that actually hurt (a million-scale amount) --------
    pairs: list[tuple[str, int, int]] = [("price", previous.price, current.price)]
    for group in sorted(set(previous.prices) | set(current.prices)):
        pairs.append(("prices", previous.prices.get(group, 0), current.prices.get(group, 0)))
    for field_name, old, new in pairs:
        if old and new and old != new and _stated_in_incoming(new):
            _note(
                field_name,
                f"{old:,}",
                f"{new:,}",
                learning.infer_price_scale(old, new, source_text),
            )

    # --- title: only a one-word swap generalizes -----------------------------
    # The corrected word must appear in the message that just arrived. The wrong
    # word may appear too — owners naturally write «مشکی نه سلفی» — so only the
    # presence of the correction is required.
    if previous.title and current.title and previous.title != current.title:
        rule = learning.infer_token_substitution(previous.title, current.title)
        if rule and rule.value in incoming:
            _note("title", previous.title, current.title, rule)

    # --- attribute values: one value swapped for another ---------------------
    for name in sorted(set(previous.attributes) & set(current.attributes)):
        old_values = previous.attributes.get(name) or []
        new_values = current.attributes.get(name) or []
        if not old_values or old_values == new_values:
            continue
        rule = learning.infer_value_substitution(old_values, new_values)
        if rule and rule.value in incoming:
            _note("attributes", f"{name}: {rule.key}", f"{name}: {rule.value}", rule)

    # --- SKU prefix: always the owner's deliberate choice, never a rule ------
    if previous.sku_prefix and current.sku_prefix and previous.sku_prefix != current.sku_prefix:
        _note("sku_prefix", previous.sku_prefix, current.sku_prefix, None)

    return notes


async def analyze(text: str, *, apply_rules: bool = True) -> ProductData:
    """Read one text through the flow's own pipeline; create nothing.

    Used by «🔍 تست پارسر» (:mod:`bot.modules.product_tools`). It builds a
    throwaway session and calls :func:`_extract` on purpose: a sandbox with
    its own simplified parser would answer a different question than «چرا
    ربات این متن را این‌طور خواند؟» — and a wrong answer there is worse
    than none.

    ``apply_rules=False`` runs the identical pipeline with the owner's learned
    rules switched off, which is how the test can show what a rule changed
    instead of leaving the owner to imagine it.
    """
    probe = ProductSession(mode="new")
    # A pasted sample is the *information* message, not a media caption: that
    # is where the flow reads title, price and SKU from. Feeding it as a
    # caption would test a different (and more forgiving) precedence rule.
    probe.info_text = (text or "").strip()
    probe.data = ProductData()
    if apply_rules:
        return await _extract(probe, learn=False)
    with learning.suspended():
        return await _extract(probe, learn=False)


def _canonical_category_paths(categories: Sequence[str]) -> list[str]:
    """Expand an unambiguous taxonomy leaf (e.g. «چاپی») to its full path."""
    paths = draft_edits.taxonomy_paths()
    by_leaf: dict[str, list[str]] = {}
    for path in paths:
        by_leaf.setdefault(path.split(" > ")[-1].casefold(), []).append(path)
    roots = {
        line.strip().casefold()
        for line in TAXONOMY.splitlines()
        if line.strip() and not line.startswith(" ")
    }
    result: list[str] = []
    seen: set[str] = set()
    for raw in categories:
        category = html.unescape(str(raw)).replace(" ← ", " > ").strip()
        category = re.sub(r"\s*>\s*", " > ", category)
        key = category.casefold()
        exact = next((path for path in paths if path.casefold() == key), None)
        if not exact and "/" in category:
            # A slash can be part of a category label («Airpods 1/2»); treat it
            # as a hierarchy separator only if that produces a real taxonomy path.
            candidate = re.sub(r"\s*/\s*", " > ", category).strip()
            exact = next((path for path in paths if path.casefold() == candidate.casefold()), None)
        if exact:
            category = exact
        elif key not in roots:
            matches = by_leaf.get(category.split(">")[-1].strip().casefold(), [])
            if len(matches) == 1:
                category = matches[0]
        folded = category.casefold()
        if category and folded not in seen:
            seen.add(folded)
            result.append(category)
    # Keep only the deepest selected paths; a child already carries its parent.
    return [
        category
        for category in result
        if not any(other.casefold().startswith(category.casefold() + " > ") for other in result)
    ]


def _extract_fingerprint(session: ProductSession) -> str:
    """Stable cache key for parser inputs and learned rules."""
    return hashlib.sha1(
        f"{session.model_text}|{session.info_text}|{learning.revision()}".encode()
    ).hexdigest()


def _model_identity_set(value: str) -> set[str]:
    """Compare model identities independent of order and harmless spelling normalization."""
    normalized = normalize_caption(value or "")
    source = normalized or (value or "")
    return {part.strip().casefold() for part in source.split(" | ") if part.strip()}


def _expected_ai_requests(session: ProductSession, *, defer_details: bool = False) -> int:
    """Network calls the configured extraction path will attempt for these inputs."""
    if not (settings.ai_base_url and settings.ai_token and settings.ai_model):
        return 0
    has_source = bool(session.model_text.strip() or session.info_text.strip())
    if not has_source:
        return 0
    return 1 + int(has_source and not defer_details)


class _AIFlowLogSink:
    """Keep only useful, secret-free AI outcomes for the eventual group card."""

    def __init__(self, destination: list[str]) -> None:
        self.destination = destination

    def add(self, level: int, message: str, *args: object) -> None:
        if "AI normalization failed" in message:
            detail = f"نرمال‌سازی مدل: خطای {args[0] if args else 'AI'}؛ پارسر قطعی حفظ شد"
        elif "AI omitted" in message:
            detail = f"نرمال‌سازی مدل: {args[0] if args else 0} نامزد قطعی حفظ شد"
        elif "AI returned" in message:
            detail = f"نرمال‌سازی مدل: پاسخ AI شامل {args[0] if args else '؟'} مدل بود"
        elif "AI is not configured" in message and level >= logging.WARNING:
            detail = "AI تنظیم نیست و نامزد قطعی برای بازبینی مشکوک تشخیص داده شد"
        else:
            return
        if detail not in self.destination:
            self.destination.append(detail)


async def _log_ai_diagnostics(
    context: ContextTypes.DEFAULT_TYPE, session: ProductSession
) -> None:
    for detail in session.ai_diagnostics:
        await _telegram_log(context, f"[ai:diagnostic] {detail}")
    session.ai_diagnostics.clear()


async def _stage(session: ProductSession, name: str) -> None:
    """Tell the live card that one more phase of the extraction is ready.

    The hook belongs to the session (and not to a parameter of :func:`_extract`)
    so that every caller — the media path, the text path, the parser test —
    gets the same behaviour without threading an argument through each one.
    A failing hook must never break an extraction: the card is decoration, the
    product is the job.
    """
    hook = getattr(session, "on_stage", None)
    if hook is None:
        return
    try:
        await hook(name)
    except Exception as exc:                                  # pragma: no cover - defensive
        logger.warning("paint of card stage %r failed: %s", name, exc)


@dataclass
class _CardPainter:
    """Repaints the live preview card as the extraction's phases complete.

    The first paint after a user message deletes the previous card and posts the
    new one at the bottom (see :func:`_render_card`); the later paints of the
    same extraction only edit it — the card is already where it belongs.
    """

    context: Any
    user_id: int
    session: ProductSession
    painted: bool = False

    async def paint(self, stage: str, *, data: ProductData | None = None) -> None:
        if stage == "full":
            text = _card_text(self.session, stage="full", data=data)
            keyboard = _keyboard(self.session)
        else:
            shown = data or _quick_data(self.session)
            text = _staged_preview(self.session, stage, data=shown)
            keyboard = (
                _keyboard(self.session) if self.session.data is not None else _collect_keyboard()
            )
        await _render_card(
            self.context,
            self.user_id,
            self.session,
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
            move_to_bottom=not self.painted,
        )
        self.painted = True


def _stage_painter(
    context: ContextTypes.DEFAULT_TYPE, user_id: int, session: ProductSession
) -> _CardPainter:
    """The painter a handler uses to fill the live card in, stage by stage."""
    return _CardPainter(context=context, user_id=user_id, session=session)


def _remember(session: ProductSession) -> None:
    """Keep the draft as it is *now* so the next action can be undone.

    Called right before a user action touches the draft — a new message, a photo
    batch, a manual field edit. The snapshot is a deep copy: the extraction that
    follows replaces lists inside the draft, and a shallow reference would then
    "restore" the very value the seller wants gone.
    """
    session.history.append(
        _DraftSnapshot(
            data=copy.deepcopy(session.data),
            info_text=session.info_text,
            model_text=session.model_text,
            files=list(session.files),
            suppressed_colors=list(session.suppressed_colors),
            verified_fields=list(session.verified_fields),
            dismissed=list(session.dismissed),
            last_extract_hash=session.last_extract_hash,
            color_summary=session.color_summary,
        )
    )
    del session.history[:-_UNDO_DEPTH]


def _restore(session: ProductSession) -> _DraftSnapshot | None:
    """Put the newest snapshot back on the session (and forget it)."""
    if not session.history:
        return None
    snapshot = session.history.pop()
    session.data = snapshot.data
    session.info_text = snapshot.info_text
    session.model_text = snapshot.model_text
    session.files = list(snapshot.files)
    session.suppressed_colors = list(snapshot.suppressed_colors)
    session.verified_fields = list(snapshot.verified_fields)
    session.dismissed = list(snapshot.dismissed)
    session.last_extract_hash = snapshot.last_extract_hash
    session.color_summary = snapshot.color_summary
    return snapshot


async def undo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """«↩️ برگرداندن به حالت قبل»: put the last draft back, exactly as it was.

    The snapshot holds the parsed draft, so this is a pure local restore: no
    re-reading the text, no AI, no site request — the previous preview is simply
    painted again on the same card (a tap edits in place, never a new message).
    """
    query = update.callback_query
    user_id = query.from_user.id if query.from_user else 0
    session = _session_of(user_id, context)
    if session is None:
        await query.answer("این جریان بسته شده است.", show_alert=True)
        return ConversationHandler.END
    snapshot = _restore(session)
    if snapshot is None:
        await query.answer("چیزی برای برگرداندن نیست.")
        return REVIEW if session.data is not None else COLLECT
    session.editing_field = ""
    await _telegram_log(
        context,
        f"[product:{user_id}] ↩️ برگشت به حالت قبل «{_short(session.info_text or session.model_text)}»",
    )
    if session.data is None:
        # Walking back past the very first message: the card goes back to being
        # a collect screen, not a broken preview of a product that never existed.
        await _render_card(
            context,
            user_id,
            session,
            "↩️ به حالت قبل برگشت. متن اطلاعات محصول را بفرست.",
            reply_markup=_collect_keyboard(),
        )
        await query.answer("↩️ به حالت قبل برگشت")
        return COLLECT
    await _render_card(
        context, user_id, session, _preview(session),
        parse_mode="HTML", reply_markup=_keyboard(session),
    )
    await query.answer("↩️ به حالت قبل برگشت")
    return REVIEW


async def _extract(session: ProductSession, *, learn: bool = True) -> ProductData:
    session.ai_diagnostics.clear()
    # ``learn=False`` is the parser-test sandbox: reading a sample must not add it
    # to the replay corpus of real products (see bot/services/learning_corpus.py).
    # The first message is the media caption and is used only for model
    # extraction. Product title/price/SKU/other attributes must come from the
    # later information messages, otherwise a descriptive caption such as
    # «قاب پلنگی لنز» can incorrectly win over the actual title.
    model_source = (session.model_text + "\n" + session.info_text).strip()
    # The shop's dictionary runs first, so the deterministic parser and the AI
    # read the same words (a supplier name or a preferred spelling should not
    # need an AI round trip to be understood).
    vocab_changes: list[str] = []
    model_source = vocabulary.apply(model_source, vocab_changes)
    caption_text = vocabulary.apply(session.model_text)
    info_text = vocabulary.apply(session.info_text)
    stock_matrix = parse_stock_matrix_sources([("info", info_text), ("caption", caption_text)])
    model_source = strip_stock_matrix_sections(model_source)
    deterministic = normalize_caption(model_source)
    if stock_matrix.models:
        deterministic = " | ".join(stock_matrix.models)
    # The old OPTION bot's AI normalizer is now the primary phone detector.
    # Accessory families such as AirPods are handled separately because the
    # phone normalizer deliberately rejects them. An empty caption/info block
    # has nothing for AI to normalize, so do not spend a network round trip.
    # Product details may be split between the media caption and later
    # Telegram messages. The extractor must receive both texts in one request
    # so title, SKU, price, colors and models can complement each other.
    combined_text = "\n".join(part for part in (caption_text, info_text) if part.strip())
    defer_details = session.defer_details and not info_text.strip()
    # Reuse one HTTPX pool for the model-normalization and details requests. They
    # are sequential and normally hit the same AI host, so separate clients paid
    # a second DNS/TCP/TLS setup for one product. Keep standalone service calls
    # self-contained; the shared client exists only for this extraction.
    share_client = bool(
        model_source
        and combined_text.strip()
        and not defer_details
        and settings.ai_base_url
        and settings.ai_token
        and settings.ai_model
    )
    async with AsyncExitStack() as stack:
        ai_client = None
        if share_client:
            ai_client = await stack.enter_async_context(ai_client_session())
        normalize_kwargs = {"client": ai_client} if ai_client is not None else {}
        if stock_matrix.found and stock_matrix.models:
            # Matrix headers are explicitly supplied model categories. They are
            # already canonical for this product, so don't spend an AI call
            # re-normalizing them or risk splitting a compatibility group.
            ai_models = deterministic
        elif model_source:
            ai_models = await ai_normalize(
                model_source,
                deterministic,
                job_log=_AIFlowLogSink(session.ai_diagnostics),
                **normalize_kwargs,
            )
        else:
            ai_models = deterministic
        normalized_model_list = ai_models or deterministic
        ai_model_guess = (
            not stock_matrix.found
            and _model_identity_set(normalized_model_list) != _model_identity_set(deterministic)
        )
        models = [x.strip() for x in normalized_model_list.split(" | ") if x.strip()]
        for accessory in extract_accessory_models(model_source):
            if accessory.casefold() not in {item.casefold() for item in models}:
                models.append(accessory)
        session.models = models
        # The models are known before the (slower) details request: let the live
        # card show them now instead of after everything.
        await _stage(session, "models")
        # Locks survive a re-extraction on purpose: the owner typed them by hand,
        # and the parser does not get to "re-decide" a deliberate edit.
        carried_edits = dict(session.data.user_edits) if session.data else {}
        if not combined_text.strip() or defer_details:
            # In the collection screen captions identify phone models; users are
            # explicitly asked to send price/title/features afterwards. Calling the
            # full extractor here used to make a second network AI round trip whose
            # result was never shown, then repeat it when PRODUCT INFO arrived.
            session.data = ProductData(models=models)
        else:
            extract_kwargs = {"client": ai_client} if ai_client is not None else {}
            session.data = await extract_product(
                combined_text,
                models,
                TAXONOMY,
                caption=caption_text,
                info_text=info_text,
                color_suppressed=set(session.suppressed_colors),
                diagnostic=session.ai_diagnostics.append,
                **extract_kwargs,
            )
    if stock_matrix.found and session.data is not None and session.data.models:
        session.models = list(session.data.models)
    if ai_model_guess and session.data.models:
        ev.merge(
            session.data.evidence,
            "models",
            ev.AI,
            quote="، ".join(session.data.models[:6]),
            overwrite=True,
        )
        session.data.notes.append("فهرست مدل‌ها با برداشت هوش مصنوعی تغییر کرده؛ لطفاً بررسی کن")
    session.data.user_edits = carried_edits
    draft_edits.apply_locks(session.data)
    # A value the owner already confirmed stays confirmed after a re-extraction:
    # the parser may read it from the AI again, but the shop has been told it is
    # right, and re-asking every time would train nobody but impatience.
    for field_name in session.verified_fields:
        ev.merge(session.data.evidence, field_name, ev.USER,
                 quote="تأییدشده توسط شما", overwrite=True)
    if session.suppressed_colors:
        session.data.notes.append(
            "🎨 رنگ این پیام‌ها حذف شد (درخواست خودت): " + "، ".join(session.suppressed_colors)
        )
    if vocab_changes:
        session.data.notes.append("واژه‌نامه اعمال شد: " + "، ".join(vocab_changes[:4]))
        ev.merge(session.data.evidence, "title", ev.VOCAB, quote="، ".join(vocab_changes[:2]))
    # AI is allowed to classify categories, but brand subcategories are
    # deterministic from the detected models. This prevents an AI response
    # containing only the parent category from losing «آیفون iphone».
    categories = [x.strip() for x in session.data.categories if x.strip() not in FORBIDDEN]
    parent = "قاب و کاور گوشی و تبلت"
    brand_paths = {
        "iphone": parent + " > آیفون iphone",
        "samsung": parent + " > سامسونگ samsung",
        "xiaomi": parent + " > شیائومی xiaomi",
    }
    model_text = " ".join(session.models).casefold()
    brand_detected = {
        "iphone": bool(re.search(r"\biphone\b", model_text)),
        "samsung": bool(re.search(r"\b(?:s\d{1,3}|a\d{1,3})(?:\s|$)|\b(?:samsung|galaxy|ultra|fe)\b", model_text)),
        "xiaomi": bool(re.search(r"\b(?:redmi|poco|xiaomi|mi)\b", model_text)),
    }
    for brand, path in brand_paths.items():
        if brand_detected[brand]:
            # Always use one canonical hierarchy. Remove AI's standalone
            # parent/leaf entries so the preview cannot show duplicates.
            leaf = path.split(" > ")[-1]
            categories = [
                x for x in categories
                if x not in {parent, leaf, path} and not x.startswith(path + " > ")
            ]
            categories.append(path)
    source = (session.model_text + "\n" + session.info_text).casefold()
    explicit_category_words = {
        "چاپی": r"چاپی|چاپ|پرینت",
        "سیلیکونی": r"سیلیکونی|سیلیکون",
        "عروسکی": r"عروسکی|عروسک",
        "ست دو نفره": r"ست\s*دو\s*نفره",
        "قاب تبلت": r"قاب\s*تبلت|تبلت",
        "گلس": r"گلس|محافظ\s*صفحه",
        "کیف سیلیکونی": r"کیف\s*سیلیکونی",
    }
    normalized_categories = []
    for category in categories:
        category = html.unescape(category).replace(" ← ", " > ").strip()
        leaf = category.split(">")[-1].strip()
        if leaf in explicit_category_words and not re.search(explicit_category_words[leaf], source):
            continue
        normalized_categories.append(category)
    # Keep one canonical path; a parent by itself is redundant when its child
    # path is already present.
    normalized_categories = list(dict.fromkeys(normalized_categories))
    for category in list(normalized_categories):
        if " > " in category:
            parent_name = category.split(" > ", 1)[0].strip()
            normalized_categories = [x for x in normalized_categories if x != parent_name]
    accessory_models = [x for x in session.models if x.casefold().startswith("airpods ")]
    if accessory_models:
        airpods_root = "لوازم جانبی ایرپاد > Apple AirPods"
        normalized_categories = [airpods_root]
        airpods_children = {
            "1/2": "Airpods 1/2", "3": "Airpods 3", "4": "Airpods 4",
            "pro": "Airpods Pro", "pro2": "Airpods Pro 2", "pro3": "Airpods Pro 3",
        }
        for model in accessory_models:
            compact = re.sub(r"\s+", "", model.casefold())
            for key, leaf in airpods_children.items():
                if key in compact:
                    path = airpods_root + " > " + leaf
                    if path not in normalized_categories:
                        normalized_categories.append(path)
    normalized_categories = apply_sku_category_policy(
        normalized_categories, session.data.sku_prefix
    )
    session.data.categories = _canonical_category_paths(normalized_categories)
    session.data.attributes = {k: v for k, v in session.data.attributes.items() if not is_model_attribute(k)}
    _apply_color_matrix(session, model_source)
    if learn:
        learning_corpus.record(model_source, session.data)
    return session.data


async def extract_product_metadata(
    model_text: str,
    info_text: str,
    *,
    diagnostics: list[str] | None = None,
) -> tuple[list[str], dict[str, list[str]]]:
    """Use the product parser without recording a product; optionally return its warnings."""
    session = ProductSession(model_text=model_text or "", info_text=info_text or "")
    data = await _extract(session, learn=False)
    if diagnostics is not None:
        diagnostics.extend(session.ai_diagnostics)
    return session.models, data.attributes if data is not None else {}


def _apply_color_matrix(session: ProductSession, source_text: str) -> None:
    """Split «which colors exist» from «which color goes with which phone».

    The post states the stock per model (``17promax: سفید/مشکی``), but
    WooCommerce attributes are flat. So the رنگ attribute keeps EVERY color
    mentioned in the post, while ``data.model_colors`` narrows the variations
    down to the pairs the seller actually listed. The deterministic parser is
    authoritative; the AI only fills models it could not resolve.
    """
    data = session.data
    if data is None:
        return
    if data.stock_matrix or data.stock_matrix_errors:
        # Design names may contain color words, but the explicit stock matrix
        # already defines every orderable pair. Treating «طرح آبی» as another
        # color axis would multiply rows and detach their quantities.
        data.model_colors = {}
        session.color_summary = "موجودی ماتریسی: طرح × دستهٔ گوشی"
        return
    matrix = parse_color_matrix(source_text)
    restrictions = matrix.restrictions_for(session.models)
    for label, colors in (data.model_colors or {}).items():
        if colors and label not in restrictions:
            restrictions[label] = list(colors)
    data.model_colors = restrictions
    session.color_summary = matrix.summary()

    if not matrix.colors:
        return

    color_name = next((name for name in data.attributes if is_color_attribute(name)), "رنگ")
    options = matrix.all_colors(extra=data.attributes.get(color_name, []))
    options = prune_unused_colors(options, session.models, restrictions)
    if len(options) < 2:
        return
    # Keep the original attribute position and merge duplicate color axes
    # («رنگ» + «رنگ‌بندی») into one, so WooCommerce never gets two color attributes.
    merged: dict[str, list[str]] = {}
    for name, values in data.attributes.items():
        if name == color_name:
            merged[name] = options
        elif is_color_attribute(name):
            continue
        else:
            merged[name] = values
    merged.setdefault(color_name, options)
    data.attributes = merged


async def _status(context: ContextTypes.DEFAULT_TYPE, chat_id: int, session: ProductSession, text: str) -> None:
    """Record progress internally; the caller owns the transient action heartbeat."""
    journal = product_journal.journal_for(context)
    if journal is not None:
        journal.stage(text)
    await _telegram_log(context, f"[product:{chat_id}] {text}")


async def _download_with_retry(
    context: ContextTypes.DEFAULT_TYPE, file_id: str, target: Path, attempts: int = 3
) -> int:
    """Telegram downloads can time out on shared hosting; retry only network timeouts."""
    last_error = None
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
        except (TimedOut, NetworkError, TimeoutError) as exc:
            last_error = exc
            target.unlink(missing_ok=True)
            if attempt == attempts:
                raise
            await _telegram_log(
                context,
                f"[telegram:retry] دانلود عکس تلاش {attempt}/{attempts} شکست خورد "
                f"({type(exc).__name__}); تلاش مجدد با تأخیر.",
            )
            await asyncio.sleep(attempt * 1.5)
    raise last_error if last_error else RuntimeError("download failed")


async def _prepare_files(user_id: int, messages: list[Message], context: ContextTypes.DEFAULT_TYPE) -> None:
    session = sessions.get(user_id)
    if session is None:
        # The flow was cancelled/ended while the album collector was still
        # waiting. Ignoring the batch is right; raising KeyError and showing the
        # user «خطا در پردازش عکس‌ها» after they pressed «❌ لغو» was not.
        logger.info("album flushed after the session ended (user %s) — ignored", user_id)
        return
    session.processing_media = True
    try:
        async with _typing_indicator(context, user_id, session):
            await _prepare_files_impl(user_id, messages, context)
    finally:
        session.processing_media = False


async def _prepare_files_impl(user_id: int, messages: list[Message], context: ContextTypes.DEFAULT_TYPE) -> None:
    session = sessions.get(user_id)
    if session is None:
        return
    # One workspace per session (not per batch): a second album appends to the
    # same product instead of orphaning the first download set on disk.
    root = session.workspace or workspace.new_dir(TEMP_DIR, user_id)
    session.workspace = root
    await _telegram_log(context, f"[product:{user_id}] شروع پردازش رسانه؛ تعداد پیام‌ها: {len(messages)}")
    await _telegram_log(context, f"[product:{user_id}] کپشن کامل رسانه:\n{_caption(messages) or '<بدون کپشن>'}")
    await _status(context, user_id, session, "📥 مرحله ۱ از ۴: دریافت عکس‌ها از تلگرام...")
    previous_files = list(session.files)
    media_items = [(m, _media(m)) for m in sorted(messages, key=lambda x: x.message_id)]
    media_items = [(m, item) for m, item in media_items if item]
    semaphore = asyncio.Semaphore(3)

    async def download_one(index, item):
        _message, media = item
        file_id, original = media
        target = root / f"{index:02d}_{_safe(original, 'image.jpg')}"
        async with semaphore:
            await _download_with_retry(context, file_id, target)
        size = target.stat().st_size if target.exists() else 0
        await _telegram_log(context, f"[product:{user_id}] عکس {index}/{len(media_items)} دریافت شد: {original} ({size} bytes)")
        return target, original, size

    await _status(context, user_id, session, f"📥 مرحله ۱ از ۴: دریافت هم‌زمان {len(media_items)} عکس...")
    downloaded = await asyncio.gather(*(download_one(i, item) for i, item in enumerate(media_items, 1)))
    await _status(context, user_id, session, f"🗜️ مرحله ۲ از ۴: فشرده‌سازی هم‌زمان {len(downloaded)} عکس...")

    async def compress_one(index, item):
        target, _original, original_size = item
        async with semaphore:
            compressed = await asyncio.to_thread(compress_image, target, root / "compressed")
        compressed_size = compressed.stat().st_size if compressed.exists() else 0
        saving = compressed_size_savings(original_size, compressed_size)
        await _telegram_log(
            context,
            f"[product:{user_id}] عکس {index} فشرده شد: {original_size} -> {compressed_size} bytes"
            + (f" ({saving}٪ کوچک‌تر)" if saving else ""),
        )
        return compressed

    new_files = list(await asyncio.gather(*(compress_one(i, item) for i, item in enumerate(downloaded, 1))))
    # From here the batch really joins the draft (images and caption), so this is
    # the point «↩️ برگرداندن» has to be able to put things back to.
    _remember(session)
    # A second batch (or a late album photo) must ADD images, never silently
    # replace the ones already collected for this product.
    session.files = previous_files + new_files
    session.model_text = _append_model_caption(session.model_text, _caption(messages))
    extraction_ms = 0.0
    expected_ai_requests = 0
    fingerprint = _extract_fingerprint(session)
    painter = _stage_painter(context, user_id, session)
    await painter.paint("text")     # the card appears now; the AI fills it in below
    if fingerprint == session.last_extract_hash and session.data is not None:
        await _telegram_log(context, f"[product:{user_id}] استخراج تکراری رد شد؛ متن کپشن/اطلاعات تغییری نکرده است.")
    else:
        await _status(context, user_id, session, "🤖 مرحله ۳ از ۴: تشخیص مدل‌ها و اطلاعات با AI...")
        # A text message can arrive while Telegram is still downloading or
        # parsing this album. Re-read if the inputs changed during an AI await,
        # so the final card never omits a message already stored in the session.
        session.on_stage = painter.paint
        try:
            while True:
                fingerprint = _extract_fingerprint(session)
                session.defer_details = not bool(session.info_text.strip())
                expected_ai_requests += _expected_ai_requests(
                    session, defer_details=session.defer_details
                )
                started = time.perf_counter()
                try:
                    await _extract(session)
                finally:
                    extraction_ms += (time.perf_counter() - started) * 1000
                    session.defer_details = False
                session.last_extract_hash = fingerprint
                if _extract_fingerprint(session) == fingerprint:
                    break
        finally:
            session.on_stage = None
        await _log_ai_diagnostics(context, session)
    await _telegram_log(
        context,
        f"[ai:summary] استخراج محصول: {expected_ai_requests} درخواست، {extraction_ms:.0f} ms",
    )
    await _telegram_log(context, f"[product:{user_id}] مدل‌های نهایی تشخیص‌داده‌شده:\n{chr(10).join(session.models) or '<هیچ مدلی تشخیص داده نشد>'}")
    if session.color_summary:
        await _telegram_log(context, f"[product:{user_id}] {session.color_summary}")
    combined_text = "\n".join(part for part in (session.model_text, session.info_text) if part.strip())
    await _telegram_log(context, f"[product:{user_id}] متن ترکیبی کپشن و اطلاعات:\n{combined_text or '<خالی>'}")
    await _telegram_log(context, f"[product:{user_id}] داده استخراج‌شده:\n{json.dumps(session.data.to_dict() if session.data else {}, ensure_ascii=False, indent=2)}")
    flow_state.record(user_id, chat_id=session.chat_id or user_id, mode=session.mode,
                      images=len(session.files), step="در انتظار تأیید")
    await _status(context, user_id, session, "✅ مرحله ۴ از ۴: اطلاعات آماده شد؛ در انتظار بررسی شما...")
    # A text may arrive during an AI request or while Telegram edits the card.
    # Reconcile and render until the input fingerprint stays stable through the
    # edit, so the last visible preview always includes every accepted message.
    while True:
        fingerprint = _extract_fingerprint(session)
        if fingerprint != session.last_extract_hash:
            session.defer_details = not bool(session.info_text.strip())
            requests = _expected_ai_requests(
                session, defer_details=session.defer_details
            )
            started = time.perf_counter()
            session.on_stage = painter.paint
            try:
                await _extract(session)
            finally:
                session.on_stage = None
                extraction_ms += (time.perf_counter() - started) * 1000
                session.defer_details = False
            session.last_extract_hash = fingerprint
            await _log_ai_diagnostics(context, session)
            await _telegram_log(
                context,
                f"[ai:summary] استخراج دوبارهٔ متن هم‌زمان: "
                f"{requests} درخواست، {extraction_ms:.0f} ms",
            )
        if session.info_text:
            await painter.paint("full")
        else:
            await _render_card(
                context,
                user_id,
                session,
                "✅ عکس‌ها دریافت و فشرده شدند. حالا متن اطلاعات محصول را بفرست.",
                reply_markup=_collect_keyboard(),
                move_to_bottom=not painter.painted,
            )
            painter.painted = True
        if _extract_fingerprint(session) == session.last_extract_hash:
            break


async def _flush_album(key: tuple[int, str], context: ContextTypes.DEFAULT_TYPE) -> None:
    await asyncio.sleep(settings.album_wait_seconds)
    messages = album_buffers.pop(key, [])
    album_tasks.pop(key, None)
    if messages:
        try:
            await _prepare_files(key[0], messages, context)
        except Exception as exc:
            details = traceback.format_exc()
            await _telegram_log(context, f"[product:{key[0]}] خطا در پردازش آلبوم: {type(exc).__name__}: {exc}\n{details}")
            session = sessions.get(key[0])
            if session is not None:
                # No new message and no card change: just a transient ⚡ on the last photo.
                await _report_failure(
                    context, key[0], session,
                    f"پردازش این عکس‌ها کامل نشد ({type(exc).__name__})؛ دوباره بفرست.",
                    message=messages[-1],
                )


async def entry(update: Update, context: ContextTypes.DEFAULT_TYPE, *,
                mode_override: str | None = None) -> int:
    """Menu/preview entry point.

    ``mode_override`` is how the restock flow hands a chat to the ZIP builder
    (:func:`begin_update`): the same permissions and cleanup, with the mode stated instead of
    read back out of the callback data.
    """
    user = update.effective_user
    query = update.callback_query
    # «📦 محصول بعدی» from the result card arrives here as product:next:<mode>:
    # the same settings, an empty draft. Registering it as an *entry point* (not
    # a plain handler) is what makes the flow's own states catch the messages
    # after the tap — an ordinary handler would leave the user talking to nobody.
    data = query.data or ""
    restock = data == CB.PHONE_RESTOCK and mode_override is None
    mode = mode_override or ("update" if data == CB.PHONE_RESTOCK or data.endswith(":update")
                             else "new")
    key = "product_restock" if mode == "update" else "product_new"
    if not user or not feature_allowed(user.id, key):
        await query.answer("⛔ دسترسی ندارید.", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    chat_id = query.message.chat_id if query.message else user.id
    flow_state.record(user.id, chat_id=chat_id, mode="restock" if restock else mode,
                      step="منتظر SKU/عنوان" if restock else "منتظر تصاویر")
    # Starting a new product must never inherit the previous product's images,
    # text or half-finished AI state — and its temp files must really go away.
    _cleanup(user.id)
    # …nor should another flow stay open behind it: one active flow per user.
    closed = flow_guard.close_others("product", user.id)
    if restock:
        # Lookup first, in the shop's own data: no ProductSession, no images, no ZIP.
        return await restock_flow.start(update, context, closed=closed)
    session = ProductSession(mode=mode)
    session.user_id = user.id
    session.chat_id = chat_id
    session.thread_id = getattr(query.message, "message_thread_id", None) if query.message else None
    sessions[user.id] = session
    await _telegram_log(context, f"[product:{user.id}] ورود به جریان محصول: {mode}")
    prompt = ("🔄 عکس‌ها و مدل‌های محصول موجود را بفرست. سپس قیمت و ویژگی‌های جدید را ارسال کن. "
              "عنوان و SKU محصول موجود تغییر نمی‌کند." if mode == "update" else
              "📦 عکس‌های محصول را بفرست. کپشن عکس‌ها باید مدل‌های گوشی باشد؛ بعد از آن متن قیمت، عنوان، پیشوند SKU و ویژگی‌های دیگر را ارسال کن.")
    if closed:
        prompt += "\n\n↩️ جریان «" + "»، «".join(closed) + "» قبلی‌ات بسته شد."
    # The guide is written ONCE and then never touched: no edit, no deletion, no
    # repaint. The live preview is a separate message that moves to the bottom of
    # the chat, so the instructions stay readable the whole way through.
    if data.startswith("product:next"):
        # Keep the completed result card intact; the guide follows it.
        prompt_message = await query.message.reply_text(
            prompt, reply_markup=_collect_keyboard()
        )
        session.guide_message_id = getattr(prompt_message, "message_id", None)
    elif query.message is not None:
        session.guide_message_id = query.message.message_id
        await _edit_message_if_changed(
            query, prompt, reply_markup=_collect_keyboard()
        )
    else:
        guide = await context.bot.send_message(
            text=prompt, reply_markup=_collect_keyboard(), **_target(session, user.id)
        )
        session.guide_message_id = getattr(guide, "message_id", None)
    return COLLECT


async def on_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return WAITING
    # Never ``setdefault`` here: after a timeout or a restart, an old photo used
    # to invent a session and start a half-state product.
    session = sessions.get(user.id)
    if session is None:
        await message.reply_text("این جریان بسته شده است. از منو دوباره «🆕 محصول جدید» را بزن.")
        return ConversationHandler.END
    if not session.chat_id:
        session.chat_id, session.thread_id = message.chat_id, message.message_thread_id
    if message.media_group_id:
        key = (user.id, message.media_group_id)
        album_buffers.setdefault(key, []).append(message)
        if key not in album_tasks:
            album_tasks[key] = asyncio.create_task(_flush_album(key, context))
    else:
        try:
            await _prepare_files(user.id, [message], context)
        except Exception as exc:
            await _telegram_log(
                context,
                f"[product:{user.id}] خطا در پردازش رسانه: {type(exc).__name__}: {exc}\n"
                + traceback.format_exc(),
            )
            await _report_failure(
                context, user.id, session,
                f"این عکس پردازش نشد ({type(exc).__name__})؛ دوباره بفرست.",
                message=message,
            )
    return REVIEW if session.data is not None else COLLECT


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Append every free-text message and refresh the single persistent card."""
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return WAITING
    session = sessions.get(user.id)
    if session is None:
        await message.reply_text("این جریان بسته شده است. از منو دوباره «🆕 محصول جدید» را بزن.")
        return ConversationHandler.END
    if not session.chat_id:
        session.chat_id, session.thread_id = message.chat_id, message.message_thread_id

    incoming = (message.text or "").strip()
    if not incoming:
        return REVIEW if session.data is not None else COLLECT
    # The misread this can undo: the seller corrects one line, the parser
    # re-reads everything, and the preview comes back worse. Snapshot first.
    _remember(session)
    session.info_text = (session.info_text + "\n" + incoming).strip()
    await _telegram_log(context, f"[product:{user.id}] متن جدید دریافت و به اطلاعات محصول اضافه شد:\n{incoming}")

    # Text may arrive while an album is still being downloaded/compressed. Keep
    # it immediately, update the existing card with that fact, and let the media
    # worker parse the final combined input before it renders the preview.
    if session.processing_media:
        if session.data is not None:
            card = (
                _preview(session)
                + "\n\n⏳ متن جدید ذخیره شد؛ با پایان پردازش عکس‌ها پیش‌نمایش کامل می‌شود."
            )
            await _render_card(
                context, user.id, session, card,
                parse_mode="HTML", reply_markup=_keyboard(session),
                move_to_bottom=True,
            )
            return REVIEW
        await _render_card(
            context,
            user.id,
            session,
            "✅ متن اطلاعات ذخیره شد؛ عکس‌ها در حال آماده‌سازی‌اند و پیش‌نمایش پس از پایان به‌روز می‌شود.",
            reply_markup=_collect_keyboard(),
            move_to_bottom=True,
        )
        return COLLECT

    previous = session.data
    painter = _stage_painter(context, user.id, session)
    # Paint before the AI: the seller's own numbers appear the moment their
    # message does, and the rest of the card fills in as the extraction finds it.
    await painter.paint("text")
    session.on_stage = painter.paint
    expected_ai_requests = _expected_ai_requests(
        session, defer_details=session.defer_details
    )
    extraction_started = time.perf_counter()
    try:
        async with _typing_indicator(context, user.id, session):
            data = await _extract(session)
    finally:
        session.defer_details = False
        session.on_stage = None
    extraction_ms = (time.perf_counter() - extraction_started) * 1000
    session.data = data
    session.last_extract_hash = _extract_fingerprint(session)
    await _log_ai_diagnostics(context, session)
    await _telegram_log(
        context,
        f"[ai:summary] استخراج محصول: {expected_ai_requests} درخواست، {extraction_ms:.0f} ms",
    )
    # Persistent self-learning is sudo-only; its trace belongs in the product
    # journal/log group, never as a second reply in the owner's chat.
    if rbac.is_sudo(user.id):
        product_text = (session.model_text + "\n" + session.info_text).strip()
        for note in _learn_from_diff(previous, data, incoming, session.info_text, product_text):
            await _telegram_log(context, f"[product:{user.id}] {note}")
    if session.color_summary:
        await _telegram_log(context, f"[product:{user.id}] {session.color_summary}")
    await _telegram_log(context, f"[product:{user.id}] پیش‌نمایش به‌روزرسانی شد:\n{json.dumps(data.to_dict(), ensure_ascii=False, indent=2)}")
    flow_state.record(
        user.id,
        chat_id=session.chat_id or user.id,
        mode=session.mode,
        images=len(session.files),
        step="پیش‌نمایش به‌روز شد",
    )
    # The final card of this message. It is an edit, not a new message: the card
    # that moved to the bottom a moment ago is already the newest thing in chat.
    await painter.paint("full")
    return REVIEW


def _target(session: ProductSession | None, fallback: int) -> dict[str, object]:
    """Proactive messages go to the chat (and thread) that started the flow."""
    chat = session.chat_id if session is not None and session.chat_id else fallback
    kwargs: dict[str, object] = {"chat_id": chat}
    if session is not None and session.thread_id:
        kwargs["message_thread_id"] = session.thread_id
    return kwargs


def _session_of(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> ProductSession | None:
    """The session for this user, created only if the flow is actually open.

    Callbacks can arrive after a restart or a timeout (Telegram keeps old
    buttons); replying to those with a fresh empty session was how a tap
    started a half-state product.
    """
    return sessions.get(user_id)


async def _refresh_preview(
    query: object, session: ProductSession, context: ContextTypes.DEFAULT_TYPE, extra: str = ""
) -> None:
    """Re-extract and update the existing card, keeping locks and suppressions."""
    prev_models = list(session.data.models) if session.data and session.data.models else []
    user_id = getattr(getattr(query, "from_user", None), "id", 0) or session.user_id
    async with _typing_indicator(context, user_id, session):
        data = await _extract(session)
    if prev_models and not data.models:
        logger.warning("استخراج مجدد مدل‌ها را از دست داد؛ مدل‌های تأییدشده قبلی حفظ می‌شوند.")
        data.models = prev_models
        if hasattr(data, "notes") and isinstance(data.notes, list):
            data.notes.append("⚠️ مدل‌های تأییدشدهٔ قبلی حفظ شدند.")
    session.data = data
    flow_state.record(
        user_id,
        chat_id=session.chat_id or user_id,
        mode=session.mode,
        images=len(session.files),
        step="ویرایش دستی",
    )
    card = _preview(session)
    if extra:
        card = f"✅ {html.escape(extra)}\n\n{card}"
    await _render_card(
        context, user_id, session, card,
        parse_mode="HTML", reply_markup=_keyboard(session),
    )


async def set_image_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user = update.effective_user
    session = sessions.get(user.id if user else 0)
    if not session or session.mode != "update":
        await query.answer("این گزینه فقط برای شارژ محصول موجود است.", show_alert=True)
        return REVIEW
    session.image_mode = "replace" if query.data == CB.PHONE_IMAGE_REPLACE else "keep"
    await query.answer("حالت تصاویر ذخیره شد.")
    if session.data:
        await _render_card(
            context, user.id, session, _preview(session),
            parse_mode="HTML", reply_markup=_keyboard(session),
        )
    return REVIEW


def _queue_for_retry(
    exc: BaseException, *, user_id: int, session: ProductSession, data: ProductData,
    batch: str, error: str, ledger_key: str | None,
) -> bool:
    """Keep a refused publish waiting for the shop, instead of telling the seller to retry.

    Three doors have to be open: the failure is one a later attempt can fix
    (:func:`bot.services.outbox.is_transient`), this is a real publish (dry-run queues nothing),
    and it is the REST mode (a ZIP is a file the seller uploads themselves). Nothing here may
    raise: a queue that cannot be written is a worse message, not a crashed flow.
    """
    if settings.woo_dry_run or session.mode != "new" or not outbox.is_transient(exc):
        return False
    try:
        return outbox_flow.enqueue_after_failure(
            user_id=user_id, chat_id=session.chat_id or user_id, thread_id=session.thread_id,
            mode=session.mode, data=data, files=session.files, batch_id=batch,
            ledger_key=ledger_key or "", error=error,
        )
    except Exception as inner:                                   # the publish already failed; be plain
        logger.warning("outbox: نتوانست صف را بنویسد: %s", inner)
        return False


def _queued_note(queued: bool) -> str:
    return "\n🐇 در صفِ تلاش مجدد است؛ نتیجه را همین‌جا می‌فرستم." if queued else ""


async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user = update.effective_user
    session = sessions.get(user.id if user else 0)
    if not session or not session.data:
        await query.answer("اول عکس و اطلاعات محصول را بفرست.", show_alert=True)
        return REVIEW
    data = session.data
    data.categories = _canonical_category_paths(
        apply_sku_category_policy(data.categories, data.sku_prefix)
    )
    # ONE shared gate for both output paths. Missing models are a warning by default:
    # a product with no model axis is still valid and WooCommerce can create it as simple.
    issues = validate_draft(
        data.to_dict(),
        mode=session.mode,
        image_count=len(session.files),
        price_min=settings.price_min,
        price_max=settings.price_max,
        require_models=settings.require_models,
        unapplied_model_words=list(
            unmatched_model_words("\n".join((session.model_text, session.info_text)))
        ),
    )
    if issues.blocking:
        await query.answer(f"⛔ {issues.errors[0].message}", show_alert=True)
        await _edit_message_if_changed(
            query, _preview(session), parse_mode="HTML", reply_markup=_keyboard(session)
        )
        return REVIEW
    # ♻️ idempotency (plan 4.3): the same content from the same chat is ONE product.
    # The key is derived from the payload (see bot/services/publish_batch.py), so a
    # retry after a crash finds its own earlier attempt instead of doubling it.
    batch = publish_batch.batch_id(data.to_dict(), session.files, chat_id=session.chat_id or user.id)
    batch_history = products_ledger.batch_history(batch)
    prior = batch_history[0] if batch_history else None
    has_live_attempt = any(
        str(entry.get("status") or "") in {"pending", "queued", "failed", "created"}
        for entry in batch_history
    )
    same_product = prior and str(prior.get("status")) == "created" and str(prior.get("mode") or "new") == session.mode
    if same_product and not session.force_publish:
        await query.answer("♻️ این بسته پیش‌تر ساخته شده است", show_alert=True)
        await context.bot.send_message(
            text=_already_published_note(prior),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                "🔁 با این حال دوباره بساز", callback_data="product:force")]]),
            **_target(session, user.id),  # type: ignore[arg-type]
        )
        return REVIEW
    session.force_publish = False
    if session.submitting:
        await query.answer("⏳ همین حالا یک ساخت در جریان است؛ لطفاً صبر کن.", show_alert=True)
        return REVIEW
    session.submitting = True
    # This is a callback, so Telegram can show a transient toast. Keep the
    # preview card intact while the publish runs; ``session.submitting`` blocks
    # a second tap without replacing the user's card with a status message.
    await query.answer(
        "در حال ساخت پیش‌نویس مستقیم…" if session.mode == "new" else "در حال ساخت فایل ZIP…"
    )
    await _telegram_log(context, f"[product:{user.id}] تأیید نهایی دریافت شد؛ داده نهایی:\n{json.dumps(data.to_dict(), ensure_ascii=False, indent=2)}")
    intent_key: str | None = None
    publish_returned = False
    product_id: int | str | None = None
    edit_url = ""

    async def keep_success_if_reply_failed(exc: BaseException) -> int:
        """Never turn a completed WooCommerce write into a failed/retryable product.

        The API call can succeed and the following Telegram card send can fail.
        Treating that as a publish failure made a repeated tap capable of creating
        a duplicate product, even though WooCommerce had already confirmed it.
        """
        status = "dry" if settings.woo_dry_run else "created"
        ledger_saved = False
        try:
            _record_result(
                user.id, session, data,
                status=status,
                product_id=None if settings.woo_dry_run else product_id,
                edit_url=edit_url,
                key=intent_key,
                batch_id=batch,
            )
            ledger_saved = True
        except Exception:
            logger.exception("Could not preserve the successful product ledger entry")
        reason = describe_exception(exc)
        ledger_note = "دفتر محصولات به‌روز شد" if ledger_saved else "ثبت در دفتر محصولات هم شکست خورد"
        if settings.woo_dry_run:
            log_line = f"🧪 اجرای آزمایشی کامل شد؛ {ledger_note}؛ ارسال کارت نتیجه ناموفق بود: {reason}"
            notice = "🧪 اجرای آزمایشی تمام شد؛ چیزی در سایت ساخته نشد. ارسال کارت نتیجه ناموفق بود."
        else:
            log_line = f"✅ پیش‌نویس محصول {product_id} ساخته شد؛ {ledger_note}؛ ارسال کارت نتیجه ناموفق بود: {reason}"
            history_note = (
                "از تاریخچهٔ محصولات می‌توانی جزئیات را ببینی."
                if ledger_saved else "شناسهٔ محصول را برای پیگیری نگه دار."
            )
            notice = (
                f"✅ پیش‌نویس ساخته شد (شناسهٔ محصول: {product_id}). "
                f"ارسال کارت نتیجه ناموفق بود؛ {history_note}"
            )
        try:
            await product_journal.send_log_message(context.bot, log_line)
        except Exception:
            logger.exception("Could not log post-publish notification failure")
        try:
            await _edit_message_if_changed(query, notice)
        except Exception as notify_exc:
            logger.warning(
                "Product %s was created, but its result could not be shown to user %s (%s): %s",
                product_id, user.id, type(notify_exc).__name__, str(notify_exc)[:240],
            )
            try:
                await context.bot.send_message(text=notice, **_target(session, user.id))
            except Exception:
                logger.exception("Could not send post-publish recovery notice")
        try:
            _cleanup(user.id)
        except Exception:
            logger.exception("Product %s was created, but flow cleanup failed", product_id)
        return ConversationHandler.END

    if session.mode == "new":
        try:
            await _status(context, user.id, session, "📤 در حال آپلود عکس‌ها و ساخت پیش‌نویس مستقیم در ووکامرس...")
            report: list[str] = []
            # The intent is written BEFORE the first request goes out. If the process
            # dies between the product POST and its response, the history keeps the
            # «⏳» card and the retry knows to look for the half-made product instead
            # of publishing a second one.
            intent_key = products_ledger.new_key(user.id)
            _record_result(user.id, session, data, status="pending", key=intent_key, batch_id=batch)
            async with _typing_indicator(context, user.id, session):
                product_id, edit_url = await create_draft(
                    data.to_dict(), session.files, dry_run=settings.woo_dry_run, report=report,
                    batch_id=batch,
                    # Any retained live attempt keeps recovery enabled. A dry-run card may
                    # follow an older real failure, so inspect this batch's full local history.
                    resume_existing=has_live_attempt,
                    meta=publish_batch.source_meta(
                        batch, chat_id=session.chat_id or user.id, thread_id=session.thread_id,
                        images=len(session.files), variations=data.variation_count,
                        bot_version=_BOT_VERSION,
                    ),
                )
            publish_returned = True
            resumed = any(line.startswith("[resume] جمع‌بندی") for line in report)
            await _telegram_log(
                context,
                f"[product:{user.id}] "
                + ("حالت آزمایشی (dry-run) اجرا شد؛ چیزی در سایت ساخته نشد. "
                   if settings.woo_dry_run else f"پیش‌نویس مستقیم ساخته شد: {product_id}")
                + ("\n" + product_journal.publish_trace_report(
                    report, dry_run=settings.woo_dry_run
                ) if report else ""),
            )
            outcome_warnings = [issue.message for issue in issues.warnings] + (
                ["🧪 حالت آزمایشی روشن است: هیچ چیزی در سایت ساخته نشد."] if settings.woo_dry_run else []
            ) + (
                ["♻️ این انتشار، تلاش نیمه‌کارهٔ قبلی را کامل کرد؛ محصول دومی ساخته نشد."] if resumed else []
            )
            entry = _record_result(
                user.id, session, data,
                status="dry" if settings.woo_dry_run else "created",
                product_id=None if settings.woo_dry_run else product_id,
                edit_url=edit_url,
                warnings=outcome_warnings,
                key=intent_key,
                batch_id=batch,
            )
            await _flush_journal(
                context,
                status="dry" if settings.woo_dry_run else "created",
                data=data,
                session=session,
                batch=batch,
                product_id=None if settings.woo_dry_run else product_id,
                edit_url=edit_url,
                warnings=outcome_warnings,
            )
            if resumed and prior and str(prior.get("status")) == "pending":
                # Close the old card with the same id: two entries, one story —
                # «این تلاش، آن تلاش نیمه‌کاره را تمام کرد».
                products_ledger.update(
                    str(prior.get("key")), status="created", product_id=product_id, edit_url=edit_url,
                    warnings=["♻️ همین محصول؛ تلاش بعدی آن را کامل کرد."],
                )
            await context.bot.send_message(
                text=result_card(entry), parse_mode="HTML", reply_markup=result_keyboard(entry),
                **_target(session, user.id),
            )
            if report and not settings.verbose_log:
                # Detailed HTTP traces belong in the configured log group, never in the
                # owner's private chat. VERBOSE_LOG already sends the full journal trace.
                await product_journal.send_publish_trace(
                    context.bot, report, dry_run=settings.woo_dry_run
                )
            _cleanup(user.id)
            return ConversationHandler.END
        except WooCommerceAPIError as exc:
            if publish_returned:
                return await keep_success_if_reply_failed(exc)
            audit_lines = exc.diagnostics or []
            await _telegram_log(
                context,
                f"[product:{user.id}] ساخت مستقیم ناموفق بود (HTTP {exc.status_code}): {exc}"
                + ("\n\n" + product_journal.publish_trace_report(
                    audit_lines, dry_run=settings.woo_dry_run
                ) if audit_lines else ""),
            )
            reason = f"HTTP {exc.status_code}: {exc}"
            queued = _queue_for_retry(exc, user_id=user.id, session=session, data=data,
                                      batch=batch, error=reason, ledger_key=intent_key)
            message = f"❌ ساخت مستقیم محصول ناموفق بود (HTTP {exc.status_code}):\n{exc}" + _queued_note(queued)
            _record_result(user.id, session, data, status="queued" if queued else "failed",
                           key=intent_key, batch_id=batch, error=reason)
            await _flush_journal(context, status="queued" if queued else "failed", data=data,
                                 session=session, batch=batch, errors=[reason])
            if audit_lines and not settings.verbose_log:
                await product_journal.send_publish_trace(
                    context.bot, audit_lines, dry_run=settings.woo_dry_run
                )
            await _edit_message_if_changed(query, message)
            session.submitting = False
            return REVIEW
        except Exception as exc:
            if publish_returned:
                return await keep_success_if_reply_failed(exc)
            details = traceback.format_exc()
            audit_lines = list(getattr(exc, "diagnostics", []) or [])
            reason = describe_exception(exc)
            await _telegram_log(
                context,
                f"[product:{user.id}] ساخت مستقیم ناموفق بود: {reason}"
                + ("\n\n" + product_journal.publish_trace_report(
                    audit_lines, dry_run=settings.woo_dry_run
                ) if audit_lines else "")
                + f"\n\n{details}",
            )
            queued = _queue_for_retry(exc, user_id=user.id, session=session, data=data,
                                      batch=batch, error=reason, ledger_key=intent_key)
            _record_result(user.id, session, data, status="queued" if queued else "failed",
                           key=intent_key, batch_id=batch, error=reason)
            await _flush_journal(context, status="queued" if queued else "failed", data=data,
                                 session=session, batch=batch, errors=[reason])
            if audit_lines and not settings.verbose_log:
                await product_journal.send_publish_trace(
                    context.bot, audit_lines, dry_run=settings.woo_dry_run
                )
            await _edit_message_if_changed(
                query, f"❌ ساخت مستقیم محصول ناموفق بود:\n{reason}" + _queued_note(queued)
            )
            session.submitting = False
            return REVIEW
    await _telegram_log(
        context,
        f"[product:{user.id}] حالت ZIP/شارژ انتخاب شد؛ هشدارها: "
        + ("؛ ".join(issue.message for issue in issues.issues) or "هیچ"),
    )
    zip_path = TEMP_DIR / f"product_{user.id}_{int(time.time())}.zip"
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    usable_attributes = {name: values for name, values in data.attributes.items() if len(values) >= 2}
    if len(data.models) >= 2:
        usable_attributes = {"مدل": data.models, **usable_attributes}
    manifest = _zip_manifest(data, usable_attributes=usable_attributes, image_mode=session.image_mode,
                             batch=batch, mode=session.mode)

    def _pack_zip(target_zip: Path, json_manifest: dict[str, object], files: list[Path]) -> None:
        with zipfile.ZipFile(target_zip, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("product.json", json.dumps(json_manifest, ensure_ascii=False, indent=2))
            for index, path in enumerate(files, 1):
                archive.write(path, f"images/{index:02d}_{path.name}")

    await _status(context, user.id, session, "📦 در حال ساخت و ارسال فایل ZIP...")
    async with _typing_indicator(context, user.id, session):
        await asyncio.to_thread(_pack_zip, zip_path, manifest, list(session.files))
        await _telegram_log(context, f"[product:{user.id}] ZIP ساخته شد: {zip_path.name}؛ تعداد تصاویر: {len(session.files)}")
        with zip_path.open("rb") as handle:
            await context.bot.send_document(
                filename="product.zip",
                document=handle,
                caption="✅ فایل محصول آماده شد. این فایل را در افزونه وردپرس آپلود کن.",
                **_target(session, user.id),  # type: ignore[arg-type]
            )
    await _telegram_log(context, f"[product:{user.id}] ZIP برای کاربر ارسال شد.")
    await _edit_message_if_changed(query, "✅ ZIP ساخته و ارسال شد.")
    entry = _record_result(user.id, session, data, status="zip", batch_id=batch,
                           warnings=[issue.message for issue in issues.warnings])
    await _flush_journal(context, status="zip", data=data, session=session, batch=batch,
                         warnings=[issue.message for issue in issues.warnings])
    await context.bot.send_message(text=result_card(entry), parse_mode="HTML",
                                   reply_markup=result_keyboard(entry), **_target(session, user.id))
    _cleanup(user.id)
    return ConversationHandler.END


async def force_publish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """«🔁 با این حال دوباره بساز» — the way out of the idempotency gate.

    Deliberately not a setting: the owner has to see the warning first, and this
    only lifts the gate for the next tap. Two identical products is a real thing a
    shop may want; what must not happen is reaching it by accident after a crash.
    """
    query = update.callback_query
    user = update.effective_user
    session = sessions.get(user.id if user else 0)
    if not session or not session.data:
        await query.answer("این جریان بسته شده است. از منو دوباره «🆕 محصول جدید» را بزن.", show_alert=True)
        return ConversationHandler.END
    session.force_publish = True
    return await confirm(update, context)


def _cleanup(user_id: int) -> None:
    """Forget the session and remove everything it put on disk.

    Deleting ``path.parent`` used to remove only ``<root>/compressed`` and leave
    every original download behind, so a handful of abandoned flows were enough
    to fill /tmp on the shared host. Pending album tasks are cancelled here too:
    a task that wakes up after the session is gone used to answer the user with
    a KeyError traceback.
    """
    restock_flow.cleanup(user_id)
    session = sessions.pop(user_id, None)
    for key in [key for key in album_buffers if key[0] == user_id]:
        album_buffers.pop(key, None)
        task = album_tasks.pop(key, None)
        if task is not None and not task.done():
            task.cancel()
    if session is None:
        return
    flow_state.clear(user_id)
    if session.workspace is not None:
        shutil.rmtree(session.workspace, ignore_errors=True)
    else:
        for path in session.files:
            shutil.rmtree(path.parent, ignore_errors=True)


def sweep_temp_dir(max_age_hours: float | None = None) -> int:
    """Delete stale workspaces (crashes, abandoned flows, killed restarts)."""
    hours = settings.temp_ttl_hours if max_age_hours is None else max_age_hours
    return workspace.sweep(TEMP_DIR, hours, label="product workspace")


async def cb_back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """«⬅️ بازگشت به منو» inside the flow — end it, do not just render the menu.

    The shared menu handler in bot/modules/start.py leaves this conversation
    ACTIVE, so every later text message was appended to the abandoned product's
    PRODUCT INFO and every later photo re-ran the whole pipeline (and the
    compression flow never got them, because this module registers first).
    """
    query = update.callback_query
    user = update.effective_user
    await query.answer()
    if user:
        _cleanup(user.id)
    await _edit_message_if_changed(
        query,
        main_menu_text(user.id if user else None, user),
        reply_markup=main_menu_keyboard(user.id if user else None),
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def on_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """An idle flow is over: drop the state and its files, and say so."""
    user = update.effective_user
    restock_open = bool(user and restock_flow.sessions.get(user.id))
    product_open = bool(user and user.id in sessions)
    # Which flow gave up is both a number and a sentence — the metric wants a key, the
    # user has to be told «شارژ محصول» — so one branch decides both and they cannot drift.
    if product_open:
        kind, label = "product", "ساخت محصول"
    elif restock_open:
        kind, label = "restock", "شارژ محصول"
    else:
        kind, label = "flow", "جریان"
    metrics.note_abandoned(kind)
    await product_journal.flush(context, status="abandoned")
    if user:
        _cleanup(user.id)
    message = update.effective_message
    if message:
        # The sentence has to name the flow that was actually open: telling someone who was
        # charging stock that a «product build» timed out sends them looking for one.
        await message.reply_text(
            f"⌛ جریان {label} به‌خاطر بی‌فعالیت بسته شد و فایل‌های موقت پاک شدند. "
            "برای شروع دوباره از منوی اصلی وارد شو."
        )


async def sweep(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Hourly /tmp janitor, so a crash can never leak workspaces forever."""
    await asyncio.to_thread(sweep_temp_dir)


async def edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Open the field picker by editing the persistent card in place."""
    query = update.callback_query
    user_id = query.from_user.id if query.from_user else 0
    session = _session_of(user_id, context)
    if session is None or session.data is None:
        await query.answer("اول اطلاعات محصول را بفرست تا چیزی برای اصلاح باشد.", show_alert=True)
        return COLLECT
    session.field_keys = [key for key, _label, _current in draft_edits.editable_fields(session.data)]
    await query.answer()
    await _render_card(
        context, user_id, session, "✏️ فیلد را انتخاب کن:",
        reply_markup=_fields_keyboard(session),
    )
    return REVIEW


async def edit_free(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """The free-text edit path is now the ordinary automatic append behavior."""
    await update.callback_query.answer("متن تازه را بفرست؛ خودکار به اطلاعات اضافه می‌شود.")
    return REVIEW


async def open_color_sources(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Which message stated which colors, with a one-tap way to unmix them."""
    query = update.callback_query
    await query.answer()
    session = _session_of(query.from_user.id if query.from_user else 0, context)
    if session is None:
        return REVIEW
    blocks = ev.parse_sources([("info", session.info_text), ("caption", session.model_text)])
    sources = draft_edits.colors_by_message(blocks)
    session.color_sources = list(sources)
    lines = ["🎨 رنگ‌ها از این پیام‌ها آمده (اگر پیام دوم محصول دیگری است، رنگش را حذف کن):", ""]
    for label, colors in sources.items():
        mark = " — حذف‌شده" if label in session.suppressed_colors else ""
        lines.append(f"• {label}{mark}: {'، '.join(colors[:8])}")
    await _render_card(
        context, query.from_user.id, session, "\n".join(lines),
        reply_markup=_color_source_keyboard(session),
    )
    return REVIEW


async def toggle_color_source(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    session = _session_of(query.from_user.id if query.from_user else 0, context)
    if session is None or not session.color_sources:
        await query.answer("چیزی برای حذف نیست.", show_alert=True)
        return REVIEW
    await query.answer()
    _remember(session)                 # the split is one tap away from being wrong
    index = int(query.data.rsplit(":", 1)[1])
    label = session.color_sources[index]
    if label in session.suppressed_colors:
        session.suppressed_colors.remove(label)
    else:
        session.suppressed_colors.append(label)
    await _refresh_preview(query, session, context)
    return REVIEW


async def accept_suggestion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """One tap: fix this product AND teach the shop dictionary the typo."""
    query = update.callback_query
    await query.answer()
    session = _session_of(query.from_user.id if query.from_user else 0, context)
    if session is None or session.data is None:
        return REVIEW
    index = int(query.data.rsplit(":", 1)[1])
    items = getattr(session.data, "suggestions", None) or []
    if index >= len(items):
        return REVIEW
    _remember(session)
    message = draft_edits.accept_suggestion(session.data, items[index])
    session.dismissed.append(f"{items[index].get('kind')}:{items[index].get('word')}")
    session.data.suggestions = []
    await _refresh_preview(query, session, context, extra=message)
    return REVIEW


async def confirm_guessed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """«بله، درست است»: the inferred values become the owner's own statement.

    Only provenance changes — no field is rewritten, no request is sent, and the
    extraction is not repeated, so a tap that means "I checked it" cannot also
    mean "the AI gets another chance at my product".
    """
    query = update.callback_query
    session = _session_of(query.from_user.id if query.from_user else 0, context)
    if session is None or session.data is None:
        await query.answer("جریان محصول باز نیست.", show_alert=True)
        return ConversationHandler.END
    guessed = ev.inferred_fields(getattr(session.data, "evidence", None) or {})
    confirmed = [name for name in guessed if name not in set(session.verified_fields)]
    if confirmed:
        _remember(session)             # provenance is undoable too
    for field_name in confirmed:
        session.verified_fields.append(field_name)
        ev.merge(session.data.evidence, field_name, ev.USER,
                 quote="تأییدشده توسط شما", overwrite=True)
    if not confirmed:
        await query.answer("چیزی برای تأیید نمانده بود.")
        return REVIEW
    await query.answer()
    await _telegram_log(
        context, f"[product:{session.user_id}] تأیید مقادیر حدسی: {'، '.join(confirmed)}"
    )
    await _render_card(
        context, query.from_user.id, session, _preview(session),
        parse_mode="HTML", reply_markup=_keyboard(session),
    )
    return REVIEW


async def dismiss_suggestion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    session = _session_of(query.from_user.id if query.from_user else 0, context)
    if session is None or session.data is None:
        return REVIEW
    index = int(query.data.rsplit(":", 1)[-1])
    items = getattr(session.data, "suggestions", None) or []
    _remember(session)
    if index < len(items):
        session.dismissed.append(f"{items[index].get('kind')}:{items[index].get('word')}")
    await _refresh_preview(query, session, context)
    return REVIEW


async def back_from_picker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    session = _session_of(query.from_user.id if query.from_user else 0, context)
    if session is not None and session.data is not None:
        await _render_card(
            context, query.from_user.id, session, _preview(session),
            parse_mode="HTML", reply_markup=_keyboard(session),
        )
    return REVIEW


async def pick_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    session = _session_of(query.from_user.id if query.from_user else 0, context)
    if session is None or session.data is None:
        return WAITING
    index = int(query.data.rsplit(":", 1)[1])
    if index >= len(session.field_keys):
        # An old keyboard: stay where the user is, do not drop them to collecting.
        return REVIEW
    key = session.field_keys[index]
    session.editing_field = key
    # An edit step must be escapable with a button, not only by remembering the
    # word «انصراف» — that is how a person ends up stuck typing into a field.
    await _render_card(
        context, query.from_user.id, session, draft_edits.prompt_for(key, session.data),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("↩️ انصراف", callback_data="product:field:cancel")
        ]]),
    )
    return EDITING_FIELD


async def delete_attribute_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Remove a variation attribute directly from its picker row."""
    query = update.callback_query
    session = _session_of(query.from_user.id if query.from_user else 0, context)
    if session is None or session.data is None:
        await query.answer("پیش‌نمایش منقضی شده است.", show_alert=True)
        return REVIEW
    try:
        index = int(query.data.rsplit(":", 1)[1])
        key = session.field_keys[index]
    except (ValueError, IndexError):
        await query.answer("این گزینه منقضی شده است.", show_alert=True)
        return REVIEW
    if key != "colors" and not key.startswith("attr:"):
        await query.answer("این فیلد قابل حذف نیست.", show_alert=True)
        return REVIEW

    error = draft_edits.apply_edit(session.data, key, "حذف")
    if error:
        await query.answer(error, show_alert=True)
        return REVIEW
    session.editing_field = ""
    session.data.variation_count = plan_from_dict(session.data.to_dict()).count
    await query.answer("ویژگی حذف شد")
    await _render_card(
        context, query.from_user.id, session, _preview(session),
        parse_mode="HTML", reply_markup=_keyboard(session),
    )
    return REVIEW


async def cancel_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    session = _session_of(query.from_user.id if query.from_user else 0, context)
    if session is not None:
        session.editing_field = ""
    if session is not None and session.data is not None:
        await _render_card(
            context, query.from_user.id, session, _preview(session),
            parse_mode="HTML", reply_markup=_keyboard(session),
        )
    return REVIEW


async def field_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Apply one explicit field edit and keep the response on the same card."""
    user = update.effective_user
    message = update.effective_message
    session = sessions.get(user.id if user else 0)
    if session is None or session.data is None or not session.editing_field:
        return REVIEW if session is not None and session.data is not None else WAITING
    text = (message.text or "") if message else ""
    if text.strip() in {"انصراف", "بی‌خیال", "بازگشت"}:
        session.editing_field = ""
        await _render_card(
            context, user.id, session, _preview(session),
            parse_mode="HTML", reply_markup=_keyboard(session),
            move_to_bottom=True,
        )
        return REVIEW
    key = session.editing_field
    _remember(session)
    error = draft_edits.apply_edit(session.data, key, text)
    if error:
        session.history.pop()          # rejected input changed nothing to undo
        prompt = draft_edits.prompt_for(key, session.data)
        await _render_card(
            context,
            user.id,
            session,
            f"⚠️ {html.escape(error)}\n\n{prompt}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("↩️ انصراف", callback_data="product:field:cancel")
            ]]),
            move_to_bottom=True,
        )
        return EDITING_FIELD
    session.editing_field = ""
    session.data.variation_count = plan_from_dict(session.data.to_dict()).count
    await _render_card(
        context, user.id, session, _preview(session),
        parse_mode="HTML", reply_markup=_keyboard(session),
        move_to_bottom=True,
    )
    return REVIEW
def _collect_keyboard() -> InlineKeyboardMarkup:
    """The way out of a flow that has nothing to confirm yet: only «لغو».

    The first instruction message carries this keyboard and is never edited, so
    the seller always knows how to stop — and the card that fills in below it
    brings the real actions once there is a product to act on. The old
    «عکس‌ها تمام شد» button is gone: the album timer already flushes a batch, and
    any text or a second photo triggers the same processing.
    """
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="product:cancel")]])


async def finish_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Compatibility response for old cards; new ones no longer carry this button.

    The collect keyboard shrank to «❌ لغو» and the album timer advances the flow
    by itself, but a card sent before this change still carries «✅ عکس‌ها تمام
    شد؛ ادامه». Tapping it must keep working, so the handler flushes the pending
    albums onto the same card — a tap edits in place and never posts again.
    """
    query = update.callback_query
    user_id = query.from_user.id if query.from_user else 0
    session = _session_of(user_id, context)
    if session is None:
        await query.answer("این جریان بسته شده است.", show_alert=True)
        return ConversationHandler.END
    if session.processing_media:
        await query.answer("در حال آماده‌سازی عکس‌هاست؛ کمی صبر کن.")
        return COLLECT

    pending_keys = [item for item in list(album_buffers) if item[0] == user_id]
    if not pending_keys and not session.files:
        await query.answer("اول دست‌کم یک عکس بفرست.", show_alert=True)
        return COLLECT
    await query.answer(
        "در حال آماده‌سازی عکس‌ها…" if pending_keys else "عکس‌ها آماده‌اند؛ پیش‌نمایش بررسی می‌شود."
    )

    for key in pending_keys:
        task = album_tasks.pop(key, None)
        if task is not None and not task.done():
            task.cancel()
        buffered = album_buffers.pop(key, [])
        if buffered:
            await _prepare_files(user_id, buffered, context)
    if session.processing_media:
        # Another album worker won the race; its final step edits this same card.
        return COLLECT
    if not session.files:
        return COLLECT
    if not session.info_text:
        await _render_card(
            context,
            user_id,
            session,
            "✅ عکس‌ها آماده‌اند. حالا متن اطلاعات محصول را بفرست (قیمت، عنوان، پیشوند SKU…).",
            reply_markup=_collect_keyboard(),
        )
        return COLLECT
    await _render_card(
        context, user_id, session, _preview(session),
        parse_mode="HTML", reply_markup=_keyboard(session),
    )
    return REVIEW


async def add_more(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """No mode switch is needed: review accepts more photos and text directly."""
    query = update.callback_query
    session = _session_of(query.from_user.id if query.from_user else 0, context)
    if session is None:
        await query.answer("این جریان بسته شده است.", show_alert=True)
        return ConversationHandler.END
    await query.answer("عکس یا متن تازه را همین‌جا بفرست؛ پیش‌نمایش خودکار به‌روز می‌شود.")
    return REVIEW


async def on_review_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Review-screen text follows the same automatic append-and-refresh path."""
    return await on_text(update, context)


async def accept_proposal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Compatibility response for proposal buttons left on old Telegram cards."""
    await update.callback_query.answer("متن‌های تازه اکنون خودکار به اطلاعات محصول اضافه می‌شوند.")
    return REVIEW


async def reject_proposal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Compatibility response for proposal buttons left on old Telegram cards."""
    await update.callback_query.answer("برای افزودن متن تازه دیگر تأیید جداگانه لازم نیست.")
    return REVIEW


async def exit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if user:
        _cleanup(user.id)
    await update.effective_message.reply_text("❌ ساخت محصول لغو شد. برای شروع دوباره /start را بزن.")
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    await update.callback_query.answer()
    if user:
        _cleanup(user.id)
    await _edit_message_if_changed(
        update.callback_query, "❌ ساخت محصول لغو شد. برای شروع دوباره /start را بزن."
    )
    return ConversationHandler.END


async def notify_interrupted_flows(app: Application) -> None:
    """Tell each owner-side chat that a product flow was cut short by a restart.

    Without this the bot simply forgets the half-built product and the user
    assumes their photos vanished on Telegram's side.
    """
    names = {"new": "ساخت محصول", "update": "ساخت فایل برای محصول موجود",
             "restock": "شارژ محصول موجود"}
    for user_id, info in flow_state.take_pending().items():
        chat_id = info.get("chat_id") or user_id
        mode = str(info.get("mode") or "new")
        # The sentence has to describe the flow that was really open: telling someone whose
        # stock line was cut short that «photos were deleted» sends them looking for images.
        detail = (f"مرحله: {info.get('step')}" if mode == "restock"
                  else f"عکس‌های دریافتی: {info.get('images', 0)}")
        files = ("فایل‌های موقت پاک شدند؛ " if mode != "restock" else "")
        try:
            await app.bot.send_message(
                chat_id,
                f"♻️ ربات ری‌استارت شد و جریان نیمه‌کارهٔ {names.get(mode, 'محصول')} بسته شد\n"
                f"{detail}\n{files}برای شروع دوباره از منوی اصلی وارد شو.",
            )
        except Exception as exc:
            logger.warning("could not announce the interrupted flow to %s: %s", chat_id, exc)


async def begin_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Enter the builder in update mode from another flow (the restock «📦 فایل/ZIP» button).

    Deliberately a call to :func:`entry` and not a copy of it: the permission check, the
    cleanup and the flow guard must not exist twice, or one of them will start lying.
    """
    return await entry(update, context, mode_override="update")


def close_for(user_id: int) -> bool:
    """End this user's product flow (session, album buffers, temp files).

    Returns whether anything was actually open, so :mod:`bot.services.flow_guard`
    can tell the user what it closed instead of silently eating their work.
    """
    was_open = user_id in sessions
    _cleanup(user_id)
    return was_open


# Registered at import time (not only in register(app)): the guard must know
# how to close this flow even if a test or tool imports the module directly.
flow_guard.register("product", "ساخت محصول", close_for)
# Starting the builder closes an open restock diff, and vice versa (both entry paths call
# close_others), so a stale «✅ اعمال» can never write over a product being built.
flow_guard.register("restock", "شارژ محصول موجود", restock_flow.cleanup)


def register(app: Application) -> None:
    # The same buttons work on both screens: the album collector renders the
    # preview from a background task and cannot move the state itself, so a
    # review keyboard may appear while the flow is still COLLECT.
    review_callbacks = [
        CallbackQueryHandler(confirm, pattern=r"^product:confirm$"),
        CallbackQueryHandler(force_publish, pattern=r"^product:force$"),
        CallbackQueryHandler(set_image_mode, pattern=f"^({CB.PHONE_IMAGE_KEEP}|{CB.PHONE_IMAGE_REPLACE})$"),
        CallbackQueryHandler(edit, pattern=r"^product:edit$"),
        CallbackQueryHandler(edit_free, pattern=r"^product:edit:free$"),
        CallbackQueryHandler(open_color_sources, pattern=r"^product:colorsrc$"),
        CallbackQueryHandler(toggle_color_source, pattern=r"^product:colorsrc:\d+$"),
        CallbackQueryHandler(accept_suggestion, pattern=r"^product:sug:\d+$"),
        CallbackQueryHandler(dismiss_suggestion, pattern=r"^product:sug:no:\d+$"),
        CallbackQueryHandler(confirm_guessed, pattern=f"^{CB.PRODUCT_CONFIRM_GUESSED}$"),
        CallbackQueryHandler(delete_attribute_field, pattern=r"^product:field:delete:\d+$"),
        CallbackQueryHandler(pick_field, pattern=r"^product:field:\d+$"),
        CallbackQueryHandler(back_from_picker, pattern=r"^product:fields:back$"),
        CallbackQueryHandler(show_preview, pattern=r"^product:preview$"),
        CallbackQueryHandler(cb_back_to_menu, pattern=f"^{CB.MAIN_MENU}$"),
        CallbackQueryHandler(cancel, pattern=r"^product:cancel$"),
        CallbackQueryHandler(undo, pattern=r"^product:undo$"),
    ]
    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(entry, pattern=f"^({CB.PHONE_POST}|{CB.PHONE_NEW}|{CB.PHONE_RESTOCK})$"),
            CallbackQueryHandler(entry, pattern=r"^product:next:(new|update)$"),
        ],
        states={COLLECT: [
            MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_media),
            MessageHandler(filters.TEXT & ~filters.COMMAND, on_text),
            CallbackQueryHandler(finish_media, pattern=r"^product:mediaend$"),
            *review_callbacks,
        ],
        REVIEW: [
            MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_media),
            MessageHandler(filters.TEXT & ~filters.COMMAND, on_review_text),
            CallbackQueryHandler(add_more, pattern=r"^product:addmore$"),
            CallbackQueryHandler(accept_proposal, pattern=r"^product:prop:yes$"),
            CallbackQueryHandler(reject_proposal, pattern=r"^product:prop:no$"),
            *review_callbacks,
        ],
        EDITING_FIELD: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, field_value),
            CallbackQueryHandler(cancel_field, pattern=r"^product:field:cancel$"),
            CallbackQueryHandler(cancel_field, pattern=r"^product:fields:back$"),
        ],
        # «شارژ محصول موجود» is a second path through this one conversation (see
        # bot/modules/restock_flow.py). Sharing the ConversationHandler is what makes the
        # handover to the ZIP builder honest: a second conversation would leave the framework's
        # state pointing at the flow the user just left, and their next message would talk to
        # nobody.
        **restock_flow.states(),
        # TIMEOUT-state handlers receive the conversation's last update, so both
        # the message and the callback form are covered.
        ConversationHandler.TIMEOUT: [
            MessageHandler(filters.ALL, on_timeout),
            CallbackQueryHandler(on_timeout),
        ],
        },
        fallbacks=[
            CallbackQueryHandler(cancel, pattern=r"^product:cancel$"),
            CommandHandler(["cancel", "start", "menu"], exit_command),
        ],
        name="product_builder",
        # Without this the flow never ends: the user leaves through the menu and
        # their next unrelated message is silently taken as product info.
        conversation_timeout=settings.flow_timeout_seconds,
    )
    app.add_handler(conv)

    # Clean up leftovers from crashes/restarts on boot, then hourly. The JobQueue
    # only exists when APScheduler is installed (PTB's [job-queue] extra), so the
    # bot still runs on a bare install — the sweep is simply not scheduled there.
    if importlib.util.find_spec("apscheduler") and app.job_queue is not None:
        app.job_queue.run_once(sweep, when=5, name="product_temp_sweep_boot")
        app.job_queue.run_repeating(
            sweep, interval=3600, first=600, name="product_temp_sweep_hourly"
        )
