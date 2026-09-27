"""Conversation-driven product package builder.

The bot never calls the shared-host WordPress installation. It prepares a ZIP
which the future WordPress importer can turn into a draft variable product.
"""
from __future__ import annotations

import asyncio
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
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.error import TimedOut, NetworkError
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
from bot.services.ai_normalizer import ai_normalize
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
from bot.services.validation import validate_draft
from bot.services.image_compressor import compress_image, compressed_size_savings
from bot.services.product_extractor import (
    ProductData,
    _number_from_line,
    extract_accessory_models,
    extract_product,
)
from bot.services.woo_client import describe_exception
from bot.services.woocommerce_direct import WooCommerceAPIError, create_draft, product_description

# The flow is a small state machine (§4.2 of docs/CODE-REVIEW-AND-UPGRADE-PLAN.md):
#
#   entry ─▶ COLLECT ──(پیش‌نمایش)──▶ REVIEW ──(تأیید)──▶ publish (the blocking handler)
#              ▲                        │
#              └───── «➕ افزودن» ───────┘      EDITING_FIELD hangs off REVIEW
#
# The states are not decoration. In COLLECT, free text *is* the product info; in
# REVIEW the same text is only a proposal until a button says yes. That one
# difference is what keeps «نه صبر کن، قیمت را عوض نکن» from becoming part of a
# product — the class of bug the review called P1-4/P1-5.
COLLECT = 0
EDITING_FIELD = 1
REVIEW = 2
#: the collecting state, named the way the older handlers speak it
WAITING = COLLECT

TEMP_DIR = Path("/tmp/tisaposttowp-products")

@dataclass
class ProductSession:
    files: list[Path] = field(default_factory=list)
    model_text: str = ""
    info_text: str = ""
    models: list[str] = field(default_factory=list)
    data: ProductData | None = None
    status_message_id: int | None = None
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
    # Where the flow was started (chat + forum thread). Every proactive message
    # has to go back there; ``user.id`` was a private-chat assumption that breaks
    # in a topic chat.
    chat_id: int = 0
    thread_id: int | None = None
    # Free text typed while reviewing: held until the owner says yes (§4.2).
    pending_text: str = ""
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


sessions: dict[int, ProductSession] = {}
album_buffers: dict[tuple[int, str], list[Message]] = {}
album_tasks: dict[tuple[int, str], asyncio.Task] = {}

logger = logging.getLogger(__name__)


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
    await product_journal.flush(
        context,
        status=status,
        title=(getattr(data, "title", "") or "").strip()[:80],
        product_id=product_id,
        edit_url=edit_url,
        batch_id=batch,
        variations=getattr(data, "variation_count", 0),
        images=len(session.files),
        price=f"{price:,} تومان" if price else "",
        mode=session.mode,
    )


def _audit_for_chat(lines: list[str]) -> str:
    """Condense the WooCommerce audit trail into the most diagnostic lines.

    The single most valuable line is the FIRST POST attempt: it carries the
    actual WooCommerce error message and body. The per-attempt SKU probes are
    collapsed into one-line counts so that spam never crowds the real error out
    of the chat message (the full trace still goes to the log group).
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
    """The rehearsal trace, for the owner's chat.

    Deliberately NOT :func:`_audit_for_chat`: that one is built for a *failure*
    (it keeps the first POST attempt and collapses the SKU probes so a real error
    is not drowned out). A dry run has no error to surface — what matters is which
    requests would have gone out, in which order, so every step is kept, minus the
    payload dump that the ``[payload]`` line already carries.
    """
    if not lines:
        return ""
    steps = [line for line in lines if line.startswith("[dry-run]")]
    notes = [line for line in lines if not line.startswith("[dry-run]") and not line.startswith("[payload]")]
    body = "\n".join([*steps, *notes])
    if len(body) > budget:
        kept = body[:budget].rsplit("\n", 1)[0]
        dropped = body.count("\n") - kept.count("\n")
        body = kept + "\n" + f"… ({dropped} خط دیگر — کاملش در لاگ)"
    header = "🧪 درخواست‌هایی که ساخته شدند و ارسال نشدند (هیچ‌کدام به سایت نرفتند):"
    return header + "\n" + body


def _attach_audit(message: str, audit_lines: list[str], budget: int = 4000) -> str:
    """Append a condensed audit to a chat message, staying under Telegram's limit."""
    view = _audit_for_chat(audit_lines)
    if not view:
        return message
    header = "\n\n📋 جزئیات تلاش‌ها:\n"
    available = budget - len(message) - len(header)
    if available <= 0:
        return message
    if len(view) > available:
        view = view[: max(0, available - 1)] + "…"
    return message + header + view


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
        "sale_price": data.sale_price,
        "stock": data.stock,
        "stock_status": data.stock_status,
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


def _change_card(line: str, session: ProductSession) -> str:
    """Say what changed after a manual edit, instead of re-rendering everything.

    A one-field edit used to print the whole preview again, so the owner had
    to re-read five screens to find their own change. The diff states it; the
    full preview is one button away.
    """
    data = session.data
    variations = data.variation_count if data is not None else 0
    body = line or "مقداری تغییر نکرد؛ همان مقدار قبلی بود."
    return "\n".join(["✅ اعمال شد", body, "", f"🎨 {variations} واریژن ساخته می‌شود"])


def _after_edit_keyboard(session: ProductSession) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("👁 پیش‌نمایش کامل", callback_data="product:preview")],
        [InlineKeyboardButton("✏️ فیلد دیگر", callback_data="product:edit")],
        [InlineKeyboardButton("✅ تأیید و ساخت", callback_data="product:confirm"),
         InlineKeyboardButton("❌ لغو", callback_data="product:cancel")],
    ]
    return InlineKeyboardMarkup(rows)


async def show_preview(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    session = _session_of(query.from_user.id if query.from_user else 0, context)
    if session is None or session.data is None:
        return REVIEW
    await query.message.edit_text(_preview(session), parse_mode="HTML", reply_markup=_keyboard(session))
    return REVIEW


def _zip_manifest(
    data: ProductData, *, usable_attributes: dict[str, list[str]], image_mode: str, batch: str,
    mode: str = "new",
) -> dict[str, object]:
    """``product.json`` inside the ZIP — the same facts the REST writer was given.

    Two shapes for the same product is how a seller ends up with two different shops, so the
    keys are written from here and nowhere else. ``model_colors`` travels with the ZIP so the
    WordPress importer builds the same restricted variation matrix instead of the full
    cartesian product; ``stock``/``sale_price`` travel too even though today's importer
    ignores them (docs/IMPORTER-CONTRACT.md says which keys are honoured and which are not —
    a key the plugin ignores is visible in the file, which beats a feature that exists only
    on one path).
    """
    return {
        "mode": mode,
        "title": data.title,
        "price": data.price,
        "prices": data.prices,
        "sale_price": data.sale_price,
        "stock": data.stock,
        "stock_status": data.stock_status,
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


def _preview(session: ProductSession) -> str:
    """Show only the product's essential publish fields in a clean hierarchy."""
    data = session.data
    if data is None:
        return "❌ هنوز اطلاعات محصولی ندارم."
    plan = plan_from_dict(data.to_dict())
    data.variation_count = plan.count

    lines = ["📦 <b>پیش‌نمایش</b>"]
    if settings.woo_dry_run:
        lines.append("🧪 حالت آزمایشی")
    title = html.escape(data.title) if data.title else "—"
    lines.append(f"<b>عنوان:</b> {title}")

    if data.prices:
        price_parts = [f"{html.escape(str(group))}: {value:,} تومان" for group, value in data.prices.items()]
        if data.price:
            price_parts.append(f"پایه: {data.price:,} تومان")
        price_text = " | ".join(price_parts)
    else:
        price_text = f"{data.price:,} تومان" if data.price else "—"
    lines.append(f"<b>قیمت:</b> {price_text}")
    if data.sale_price:
        lines.append(f"<b>قیمت ویژه:</b> {data.sale_price:,} تومان")
    if data.stock is not None:
        lines.append(f"<b>موجودی:</b> {data.stock:,} عدد")
    elif data.stock_status == "outofstock":
        lines.append("<b>موجودی:</b> ناموجود")
    elif data.stock_status == "onbackorder":
        lines.append("<b>موجودی:</b> پیش‌فروش")
    sku = html.escape(data.sku_prefix) if data.sku_prefix else "—"
    lines.append(f"<b>شناسه:</b> {sku}")

    lines.append("")
    lines.append("<b>ویژگی‌ها:</b>")
    models = plan.models or list(data.models or [])
    model_text = " | ".join(html.escape(model) for model in models) if models else "—"
    lines.append(f"<b>مدل:</b> {model_text}")
    for name, values in plan.axes:
        if name == "مدل":
            continue
        lines.append(f"<b>{html.escape(name)}:</b> " + " | ".join(html.escape(value) for value in values))
    lines.append(f"<b>نوع محصول:</b> {'متغیر' if plan.is_variable else 'ساده'}")

    lines.append("")
    lines.append("<b>دسته‌بندی:</b>")
    category_lines = _category_tree_lines(data.categories)
    lines.extend(category_lines or ["—"])

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
        lines.extend(f"⛔ {html.escape(issue.message)}" for issue in issues.errors[:3])
    return "\n".join(lines)


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


async def _extract(session: ProductSession, *, learn: bool = True) -> ProductData:
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
    deterministic = normalize_caption(model_source)
    # The old OPTION bot's AI normalizer is now the primary phone detector.
    # Accessory families such as AirPods are handled separately because the
    # phone normalizer deliberately rejects them.
    ai_models = await ai_normalize(model_source, deterministic)
    models = [x.strip() for x in (ai_models or deterministic).split(" | ") if x.strip()]
    for accessory in extract_accessory_models(model_source):
        if accessory.casefold() not in {item.casefold() for item in models}:
            models.append(accessory)
    session.models = models
    # Do not make an unnecessary second AI request while only the photos are
    # being processed. It runs as soon as the information message arrives.
    # Product details may be split between the media caption and later
    # Telegram messages. The extractor must receive both texts in one request
    # so title, SKU, price, colors and models can complement each other.
    combined_text = "\n".join(part for part in (caption_text, info_text) if part.strip())
    # Locks survive a re-extraction on purpose: the owner typed them by hand,
    # and the parser does not get to "re-decide" a deliberate edit.
    carried_edits = dict(session.data.user_edits) if session.data else {}
    session.data = (await extract_product(
        combined_text,
        models,
        TAXONOMY,
        caption=caption_text,
        info_text=info_text,
        color_suppressed=set(session.suppressed_colors),
    ) if combined_text.strip() else ProductData(models=models))
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


async def extract_product_metadata(model_text: str, info_text: str) -> tuple[list[str], dict[str, list[str]]]:
    """Use the actual product-flow parser/AI pipeline without recording a product."""
    session = ProductSession(model_text=model_text or "", info_text=info_text or "")
    data = await _extract(session, learn=False)
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
    """Keep one live progress message and mirror every stage to the log group."""
    journal = product_journal.journal_for(context)
    if journal is not None:
        journal.stage(text)
    await _telegram_log(context, f"[product:{chat_id}] {text}")
    target = _target(session, chat_id)
    thread = {"message_thread_id": target["message_thread_id"]} if "message_thread_id" in target else {}
    try:
        if session.status_message_id:
            await context.bot.edit_message_text(
                chat_id=int(target["chat_id"]), message_id=session.status_message_id, text=text, **thread
            )
        else:
            msg = await context.bot.send_message(text=text, **target)  # type: ignore[arg-type]
            session.status_message_id = msg.message_id
    except Exception:
        pass


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
            return tg_file.file_size or 0
        except (TimedOut, NetworkError, TimeoutError) as exc:
            last_error = exc
            if attempt == attempts:
                raise
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
    # A second batch (or a late album photo) must ADD images, never silently
    # replace the ones already collected for this product.
    session.files = previous_files + new_files
    await _status(context, user_id, session, "🤖 مرحله ۳ از ۴: تشخیص مدل‌ها و اطلاعات با AI...")
    session.model_text = _caption(messages)
    await _extract(session)
    session.processing_media = False
    await _telegram_log(context, f"[product:{user_id}] مدل‌های نهایی تشخیص‌داده‌شده:\n{chr(10).join(session.models) or '<هیچ مدلی تشخیص داده نشد>'}")
    if session.color_summary:
        await _telegram_log(context, f"[product:{user_id}] {session.color_summary}")
    combined_text = "\n".join(part for part in (session.model_text, session.info_text) if part.strip())
    await _telegram_log(context, f"[product:{user_id}] متن ترکیبی کپشن و اطلاعات:\n{combined_text or '<خالی>'}")
    await _telegram_log(context, f"[product:{user_id}] داده استخراج‌شده:\n{json.dumps(session.data.to_dict() if session.data else {}, ensure_ascii=False, indent=2)}")
    flow_state.record(user_id, chat_id=user_id, mode=session.mode,
                      images=len(session.files), step="در انتظار تأیید")
    await _status(context, user_id, session, "✅ مرحله ۴ از ۴: اطلاعات آماده شد؛ در انتظار بررسی شما...")
    if session.info_text:
        await context.bot.send_message(text=_preview(session), parse_mode="HTML",
                                       reply_markup=_keyboard(session), **_target(session, user_id))
    else:
        await context.bot.send_message(text="✅ عکس‌ها دریافت و فشرده شدند. حالا متن اطلاعات محصول را بفرست.",
                                       reply_markup=_collect_keyboard(), **_target(session, user_id))


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
            await context.bot.send_message(
                text=f"❌ خطا در پردازش عکس‌ها: {type(exc).__name__}: {exc}",
                **_target(sessions.get(key[0]), key[0]))


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
    if data.startswith("product:next"):
        # «محصول بعدی» is tapped on the result card; editing that message would
        # delete the id and link the owner may still be reading.
        await query.message.reply_text(prompt)
    else:
        await query.edit_message_text(prompt)
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
        await _prepare_files(user.id, [message], context)
    return WAITING


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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
    incoming = message.text or ""
    session.info_text = (session.info_text + "\n" + incoming).strip()
    await _telegram_log(context, f"[product:{user.id}] متن جدید دریافت شد:\n{incoming}")
    # The text may arrive immediately after the album, before the delayed
    # album collector has finished downloading/compressing it. Keep the text
    # in the session and let _prepare_files render the final preview later.
    if not session.files or session.processing_media:
        await message.reply_text(
            "✅ متن دریافت شد؛ عکس‌ها هنوز در حال پردازش‌اند."
            "\n\nاگر عکس دیگری نمی‌فرستی، «✅ عکس‌ها تمام شد؛ ادامه» را بزن تا عکس‌های دریافت‌شده پردازش شوند و به مرحلهٔ بعد بروی."
            " اگر هنوز عکس می‌فرستی، دکمه را نزن و عکس را بفرست.",
            reply_markup=_collect_keyboard(),
        )
        return COLLECT
    # Extraction costs up to two AI requests. If nothing changed since the last
    # extraction there is nothing to redo — and re-asking the model was also how
    # it could quietly "change its mind" about a value the owner had accepted.
    fingerprint = hashlib.sha1(
        f"{session.model_text}|{session.info_text}|{learning.revision()}".encode()
    ).hexdigest()
    if fingerprint == session.last_extract_hash and session.data is not None:
        await message.reply_text(
            "ℹ️ چیز تازه‌ای نسبت به آخرین استخراج ندیدم؛ همان مقادیر معتبرند. "
            "اگر می‌خواهی چیزی را عوض کنی، دقیقاً همان فیلد را بنویس (مثلاً «قیمت 698000»)."
        )
        return WAITING
    previous = session.data
    data = await _extract(session)
    session.last_extract_hash = fingerprint
    # Persistent self-learning is sudo-only: a rule rewrites how EVERY later
    # product is parsed, so it should not be creatable by a shared admin account.
    # The correction still applies to this session for everyone — that part is
    # just the parser honoring the newest line (see product_extractor._scan_prices).
    if rbac.is_sudo(user.id):
        product_text = (session.model_text + "\n" + session.info_text).strip()
        for note in _learn_from_diff(previous, data, incoming, session.info_text, product_text):
            await message.reply_html(note)
    if session.color_summary:
        await _telegram_log(context, f"[product:{user.id}] {session.color_summary}")
    await _telegram_log(context, f"[product:{user.id}] پیش‌نمایش به‌روزرسانی شد:\n{json.dumps(data.to_dict(), ensure_ascii=False, indent=2)}")
    await message.reply_html(_preview(session), reply_markup=_keyboard(session))
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
    """Re-extract and re-render, keeping the owner's locks and suppressions."""
    prev_models = list(session.data.models) if session.data and session.data.models else []
    data = await _extract(session)
    if prev_models and not data.models:
        logger.warning("استخراج مجدد مدل‌ها را از دست داد؛ مدل‌های تأییدشده قبلی حفظ می‌شوند.")
        data.models = prev_models
        if hasattr(data, "notes") and isinstance(data.notes, list):
            data.notes.append("⚠️ مدل‌های تأییدشدهٔ قبلی حفظ شدند.")
    session.data = data
    flow_state.record(
        getattr(getattr(query, "from_user", None), "id", 0) or 0,
        mode=session.mode, images=len(session.files), step="ویرایش دستی",
    )
    if extra:
        await query.message.reply_text(extra)          # type: ignore[union-attr]
    await query.message.reply_html(_preview(session), reply_markup=_keyboard(session))  # type: ignore[union-attr]


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
        await query.edit_message_text(_preview(session), parse_mode="HTML", reply_markup=_keyboard(session))
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
    # ONE shared gate for both output paths. It used to be two different checks,
    # so the REST path happily published a product with no models while the ZIP
    # importer rejected exactly that.
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
        await query.edit_message_text(
            _preview(session), parse_mode="HTML", reply_markup=_keyboard(session)
        )
        return REVIEW
    # ♻️ idempotency (plan 4.3): the same content from the same chat is ONE product.
    # The key is derived from the payload (see bot/services/publish_batch.py), so a
    # retry after a crash finds its own earlier attempt instead of doubling it.
    batch = publish_batch.batch_id(data.to_dict(), session.files, chat_id=session.chat_id or user.id)
    prior = products_ledger.find_batch(batch)
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
    try:
        # Replacing the keyboard with a plain «working» message is what makes a
        # double tap impossible instead of merely unlikely.
        await query.edit_message_text("⏳ در حال ساخت… لطفاً چند لحظه صبر کن.")
    except Exception:
        pass
    await query.answer("در حال ساخت پیش‌نویس مستقیم..." if session.mode == "new" else "در حال ساخت فایل ZIP...")
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
            await query.edit_message_text(notice)
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
            product_id, edit_url = await create_draft(
                data.to_dict(), session.files, dry_run=settings.woo_dry_run, report=report,
                batch_id=batch,
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
                + (("\n--- گزارش dry-run ---\n" + "\n".join(report)) if report else ""),
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
            if report and rbac.is_sudo(user.id):
                # The whole point of a dry run is the trace, so it goes to the
                # sudo user (owner) and not only to the log group (LOG_CHAT_ID is optional).
                await context.bot.send_message(text=_dry_run_report(report), **_target(session, user.id))
            _cleanup(user.id)
            return ConversationHandler.END
        except WooCommerceAPIError as exc:
            if publish_returned:
                return await keep_success_if_reply_failed(exc)
            audit_lines = exc.diagnostics or []
            await _telegram_log(
                context,
                f"[product:{user.id}] ساخت مستقیم ناموفق بود (HTTP {exc.status_code}): {exc}\n\n"
                f"--- لاگ گام‌به‌گام ---\n" + "\n".join(audit_lines),
            )
            reason = f"HTTP {exc.status_code}: {exc}"
            queued = _queue_for_retry(exc, user_id=user.id, session=session, data=data,
                                      batch=batch, error=reason, ledger_key=intent_key)
            message = f"❌ ساخت مستقیم محصول ناموفق بود (HTTP {exc.status_code}):\n{exc}" + _queued_note(queued)
            _record_result(user.id, session, data, status="queued" if queued else "failed",
                           key=intent_key, batch_id=batch, error=reason)
            await _flush_journal(context, status="queued" if queued else "failed", data=data,
                                 session=session, batch=batch, errors=[reason])
            await query.edit_message_text(_attach_audit(message, audit_lines))
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
                + ("\n\n--- لاگ گام‌به‌گام ---\n" + "\n".join(audit_lines) if audit_lines else "")
                + f"\n\n{details}",
            )
            queued = _queue_for_retry(exc, user_id=user.id, session=session, data=data,
                                      batch=batch, error=reason, ledger_key=intent_key)
            _record_result(user.id, session, data, status="queued" if queued else "failed",
                           key=intent_key, batch_id=batch, error=reason)
            await _flush_journal(context, status="queued" if queued else "failed", data=data,
                                 session=session, batch=batch, errors=[reason])
            await query.edit_message_text(
                _attach_audit(
                    f"❌ ساخت مستقیم محصول ناموفق بود:\n{reason}" + _queued_note(queued), audit_lines
                )
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

    await asyncio.to_thread(_pack_zip, zip_path, manifest, list(session.files))
    await _telegram_log(context, f"[product:{user.id}] ZIP ساخته شد: {zip_path.name}؛ تعداد تصاویر: {len(session.files)}")
    with zip_path.open("rb") as handle:
        await context.bot.send_document(filename="product.zip", document=handle,
                                        caption="✅ فایل محصول آماده شد. این فایل را در افزونه وردپرس آپلود کن.",
                                        **_target(session, user.id))  # type: ignore[arg-type]
    await _telegram_log(context, f"[product:{user.id}] ZIP برای کاربر ارسال شد.")
    await query.edit_message_text("✅ ZIP ساخته و ارسال شد.")
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
    await query.answer("🔁 باشه؛ این بار تکراری ساخته می‌شود.")
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
    await query.edit_message_text(
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
    """Open the field picker: edit ONE thing, see it applied, nothing else moves."""
    query = update.callback_query
    await query.answer()
    session = _session_of(query.from_user.id if query.from_user else 0, context)
    if session is None or session.data is None:
        await query.message.reply_text("اول عکس‌ها و متن اطلاعات محصول را بفرست تا چیزی برای اصلاح باشد.")
        # Nothing to review yet: stay in the collecting state, where free text is
        # the information we are waiting for instead of a "proposal".
        return COLLECT
    session.field_keys = [key for key, _label, _current in draft_edits.editable_fields(session.data)]
    await query.message.reply_text("✏️ فیلد را انتخاب کن:", reply_markup=_fields_keyboard(session))
    return REVIEW


async def edit_free(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    await update.callback_query.message.reply_text(
        "✏️ اصلاحاتت را به‌صورت متن بفرست؛ اطلاعات جدید روی اطلاعات قبلی اعمال می‌شود."
    )
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
    await query.message.reply_text("\n".join(lines), reply_markup=_color_source_keyboard(session))
    return REVIEW


async def toggle_color_source(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    session = _session_of(query.from_user.id if query.from_user else 0, context)
    if session is None or not session.color_sources:
        await query.answer("چیزی برای حذف نیست.", show_alert=True)
        return REVIEW
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
    await query.answer()
    session = _session_of(query.from_user.id if query.from_user else 0, context)
    if session is None or session.data is None:
        await query.answer("جریان محصول باز نیست.", show_alert=True)
        return ConversationHandler.END
    guessed = ev.inferred_fields(getattr(session.data, "evidence", None) or {})
    confirmed = [name for name in guessed if name not in set(session.verified_fields)]
    for field_name in confirmed:
        session.verified_fields.append(field_name)
        ev.merge(session.data.evidence, field_name, ev.USER,
                 quote="تأییدشده توسط شما", overwrite=True)
    if not confirmed:
        await query.message.reply_text("چیزی برای تأیید نمانده بود.")
        return REVIEW
    await _telegram_log(
        context, f"[product:{session.user_id}] تأیید مقادیر حدسی: {'، '.join(confirmed)}"
    )
    await query.message.edit_text(_preview(session), parse_mode="HTML", reply_markup=_keyboard(session))
    return REVIEW


async def dismiss_suggestion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    session = _session_of(query.from_user.id if query.from_user else 0, context)
    if session is None or session.data is None:
        return REVIEW
    index = int(query.data.rsplit(":", 1)[-1])
    items = getattr(session.data, "suggestions", None) or []
    if index < len(items):
        session.dismissed.append(f"{items[index].get('kind')}:{items[index].get('word')}")
    await _refresh_preview(query, session, context)
    return REVIEW


async def back_from_picker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    session = _session_of(query.from_user.id if query.from_user else 0, context)
    if session is not None and session.data is not None:
        await query.message.edit_text(_preview(session), parse_mode="HTML", reply_markup=_keyboard(session))
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
    await query.message.edit_text(
        draft_edits.prompt_for(key, session.data),
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
    await query.message.edit_text(_preview(session), parse_mode="HTML", reply_markup=_keyboard(session))
    return REVIEW


async def cancel_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    session = _session_of(query.from_user.id if query.from_user else 0, context)
    if session is not None:
        session.editing_field = ""
    if session is not None and session.data is not None:
        await query.message.edit_text(_preview(session), parse_mode="HTML", reply_markup=_keyboard(session))
    return REVIEW


async def field_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Parse one hand-typed value for one field, then show the new preview."""
    user = update.effective_user
    message = update.effective_message
    session = sessions.get(user.id if user else 0)
    if session is None or session.data is None or not session.editing_field:
        return REVIEW if session is not None and session.data is not None else WAITING
    text = (message.text or "") if message else ""
    if text.strip() in {"انصراف", "بی‌خیال", "بازگشت"}:
        session.editing_field = ""
        if message:
            await message.reply_html(_preview(session), reply_markup=_keyboard(session))
        return REVIEW
    key = session.editing_field
    before = draft_edits.snapshot(session.data)
    count_before = plan_from_dict(session.data.to_dict()).count
    error = draft_edits.apply_edit(session.data, key, text)
    if error:
        if message:
            await message.reply_text(
                f"⚠️ {error}" + "\n\n" + "دوباره بنویس یا «انصراف» را بفرست."
            )
        return EDITING_FIELD
    session.editing_field = ""
    after = draft_edits.snapshot(session.data)
    session.data.variation_count = plan_from_dict(session.data.to_dict()).count
    line = draft_edits.diff(before, after, variations=(count_before, session.data.variation_count))
    if message:
        await message.reply_text(
            _change_card(line, session), reply_markup=_after_edit_keyboard(session)
        )
    return REVIEW
def _collect_keyboard() -> InlineKeyboardMarkup:
    """The collecting screen: «I am done with the photos», plus the way out.

    The album collector waits ``ALBUM_WAIT_SECONDS`` before it dares to process
    anything. A person who knows they sent the last photo should not have to
    hope that timer was long enough — one tap flushes it.
    """
    rows = [[InlineKeyboardButton("✅ عکس‌ها تمام شد؛ ادامه", callback_data="product:mediaend")],
            [InlineKeyboardButton("❌ لغو", callback_data="product:cancel")]]
    return InlineKeyboardMarkup(rows)


async def finish_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Flush any pending album now and move to the review screen."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id if query.from_user else 0
    session = _session_of(user_id, context)
    if session is None:
        await query.message.reply_text("این جریان بسته شده است. از منو دوباره «🆕 محصول جدید» را بزن.")
        return ConversationHandler.END
    for key in [item for item in list(album_buffers) if item[0] == user_id]:
        task = album_tasks.pop(key, None)
        if task is not None and not task.done():
            task.cancel()
        buffered = album_buffers.pop(key, [])
        if buffered:
            await _prepare_files(user_id, buffered, context)
    if session.processing_media:
        await query.answer("پردازش عکس‌ها هنوز تمام نشده؛ چند لحظه دیگر…", show_alert=True)
        return COLLECT
    if not session.files:
        await query.answer("اول دست‌کم یک عکس بفرست.", show_alert=True)
        return COLLECT
    if not session.info_text:
        await query.message.reply_text("✅ عکس‌ها آماده‌اند. حالا متن اطلاعات محصول را بفرست (قیمت، عنوان، پیشوند SKU…).")
        return COLLECT
    await query.message.reply_html(_preview(session), reply_markup=_keyboard(session))
    return REVIEW


async def add_more(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Back to collecting, on purpose — instead of guessing from free text."""
    query = update.callback_query
    await query.answer()
    session = _session_of(query.from_user.id if query.from_user else 0, context)
    if session is None:
        return ConversationHandler.END
    session.pending_text = ""
    await query.message.reply_text(
        "📦 عکس یا متن جدید را بفرست؛ بعد از هر پیام پیش‌نمایش تازه می‌شود. وقتی عکس دیگری نداری، «✅ عکس‌ها تمام شد؛ ادامه» را بزن.",
        reply_markup=_collect_keyboard(),
    )
    return COLLECT


async def on_review_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """On the review screen, typed text is a *proposal*, not a silent rewrite."""
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return REVIEW
    session = sessions.get(user.id)
    if session is None:
        await message.reply_text("این جریان بسته شده است. از منو دوباره «🆕 محصول جدید» را بزن.")
        return ConversationHandler.END
    if session.data is None:
        # Nothing reviewed yet (the preview has not been rendered): there is no
        # draft to protect, so the text is simply the information we asked for.
        return await on_text(update, context)
    incoming = (message.text or "").strip()
    if not incoming:
        return REVIEW
    if incoming in {"انصراف", "بی‌خیال"}:
        session.pending_text = ""
        await message.reply_html(_preview(session), reply_markup=_keyboard(session))
        return REVIEW
    session.pending_text = incoming
    quoted = incoming if len(incoming) <= 400 else incoming[:400] + "…"
    await message.reply_text(
        "این را به اطلاعات همین محصول اضافه کنم؟\n\n" + quoted,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ بله، اضافه کن", callback_data="product:prop:yes"),
            InlineKeyboardButton("⏭️ نه، ولش کن", callback_data="product:prop:no"),
        ]]),
    )
    return REVIEW


async def accept_proposal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id if query.from_user else 0
    session = _session_of(user_id, context)
    if session is None or not session.pending_text:
        await query.answer("چیزی برای اضافه کردن نمانده است.", show_alert=True)
        return REVIEW
    incoming, session.pending_text = session.pending_text, ""
    session.info_text = (session.info_text + "\n" + incoming).strip()
    await _telegram_log(context, f"[product:{user_id}] متن پیشنهادی تأیید و اعمال شد:\n{incoming}")
    data = await _extract(session)
    session.data = data
    flow_state.record(user_id, chat_id=session.chat_id or user_id, mode=session.mode,
                      images=len(session.files), step="متن تأییدشده اعمال شد")
    await query.message.reply_html(_preview(session), reply_markup=_keyboard(session))
    return REVIEW


async def reject_proposal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    session = _session_of(query.from_user.id if query.from_user else 0, context)
    if session is not None:
        session.pending_text = ""
    await query.answer("↩️ اضافه نشد؛ هیچ فیلدی عوض نشد.")
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
    await update.callback_query.edit_message_text("❌ ساخت محصول لغو شد. برای شروع دوباره /start را بزن.")
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
