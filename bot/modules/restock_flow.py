"""«🔄 اپدیت محصول موجود» — the first step: find the product in the shop.

An update is a new product's intake **compared with a product the shop already has**, so the
only thing this module does is *pick that product*:

* :func:`bot.services.product_match.find` looks it up **in the shop** (exact SKU, then title), so
  nobody has to remember a product id; a seller's own recent cards are one tap away;
* the tapped candidate is read with its variations, and the chat is handed to the builder
  (:func:`bot.modules.product_flow.begin_update`): photos, caption and lines are sent the way a
  new product's are, the preview card shows only what differs from this product, and
  «✅ اعمال تغییرات» writes it through REST.

The search state lives in :mod:`bot.modules.product_flow`'s conversation — the handlers
registered in that one :class:`ConversationHandler` return it — because handing a chat from one
conversation to another would leave the framework's state pointing at the flow the user just
left, and their next message would talk to nobody. What is shared is the *conversation*, not
the logic: nothing in here builds a product and nothing in here writes to the shop.

The rules of the new-product flow hold here too: a status is a typing indicator, not a message;
a failure is a ⚡ on the message that caused it (or a toast on a button), never a new message;
and the log group — not the seller's chat — gets the technical trace.
"""
from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot.config import settings
from bot.constants import CB
from bot.services import flow_state, product_match, products_ledger
from bot.services.woo_client import Audit, describe_exception

logger = logging.getLogger(__name__)

#: The only state of this flow. It is a state of product_flow's conversation (see above).
RESTOCK_MATCH = 5

#: How many candidates to offer as buttons — more is a list nobody reads.
MAX_CANDIDATES = 8
#: The button-text budget; the full line is always in the message above the buttons.
BUTTON_LABEL = 52
#: Ledger statuses whose product still exists in the shop and is worth offering again.
_OFFERED = ("created", "restocked", "updated", "dry")


@dataclass
class RestockSession:
    """What this chat is in the middle of doing. It does not survive a restart, on purpose:
    a candidate list read minutes ago is worth less than searching again."""

    chat_id: int | None = None
    thread_id: int | None = None
    candidates: list[product_match.Candidate] = field(default_factory=list)
    dry_run: bool = False


sessions: dict[int, RestockSession] = {}


def cleanup(user_id: int) -> bool:
    """Drop this user's search session; True when something was open.

    Called by the builder's ``_cleanup`` and registered with :mod:`bot.services.flow_guard`, so
    starting any other flow cannot leave a half-finished search behind.
    """
    flow_state.clear(user_id)
    return sessions.pop(user_id, None) is not None


# — entry —

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE, *,
                closed: list[str] | None = None, keep_card: bool = False) -> int:
    """The «🔄 اپدیت محصول» tap; product_flow's entry calls this with the query in hand.

    ``closed`` is what the flow guard dropped to make room here — said out loud, because «my
    other screen vanished» must never be a mystery. ``keep_card`` is for the button on a result
    card: the finished result stays readable and the search is a reply under it.
    """
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return ConversationHandler.END
    message = query.message
    session = RestockSession(dry_run=bool(settings.woo_dry_run),
                             chat_id=message.chat_id if message else user.id,
                             thread_id=getattr(message, "message_thread_id", None) if message else None)
    sessions[user.id] = session
    await _log(context, f"[restock:{user.id}] ورود به جست‌وجوی محصول برای اپدیت")
    prompt = _search_prompt(session.dry_run)
    if closed:
        prompt += "\n\n↩️ جریان «" + "»، «".join(closed) + "» قبلی‌ات بسته شد."
    keyboard = await _search_keyboard(user.id)
    if keep_card and message is not None:
        await message.reply_text(prompt, reply_markup=keyboard)
    else:
        await query.edit_message_text(prompt, reply_markup=keyboard)
    return RESTOCK_MATCH


def _search_prompt(dry_run: bool = False) -> str:
    text = (
        "🔄 اپدیت محصول — اول محصول را از خودِ فروشگاه پیدا می‌کنم.\n\n"
        "یا SKU را بنویس (دقیق)، یا بخشی از عنوانش را — مثلاً «IP13 پرو مکس».\n"
        "بعد عکس‌ها و اطلاعات تازه را می‌فرستی (مثل محصول جدید) و فقط چیزی که فرق دارد عوض می‌شود."
    )
    if dry_run:
        # In a rehearsal there is no catalogue to search; saying «دمو» is the only way the
        # screen can be reached at all, and hiding that would make the dry run untestable.
        text += "\n\n🧪 حالت آزمایشی: فروشگاه واقعی صدا زده نمی‌شود. برای دیدن مسیر، «دمو» را بنویس."
    return text


async def _search_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """This seller's recent cards, so a product the bot itself made is one tap away."""
    rows: list[list[InlineKeyboardButton]] = []
    for entry in products_ledger.recent(8):
        product_id = entry.get("product_id")
        if not product_id or str(entry.get("status")) not in _OFFERED:
            continue
        if str(entry.get("user_id")) != str(user_id):
            continue
        title = html.unescape(str(entry.get("title") or ""))[:30] or "محصول"
        rows.append([InlineKeyboardButton(
            f"🧾 {title} · #{product_id}", callback_data=f"{CB.RESTOCK_PICK}:{product_id}")])
    rows.append([InlineKeyboardButton("⏹ انصراف", callback_data=CB.RESTOCK_CANCEL)])
    return InlineKeyboardMarkup(rows)


# — step 1: find it —

async def handle_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """A SKU or a title in the chat: answer with the products it could be."""
    message = update.effective_message
    session = _session(update)
    if session is None or message is None:
        return ConversationHandler.END
    wanted = " ".join((message.text or "").split())
    if not wanted:
        await _flag(context, message)
        return RESTOCK_MATCH
    await _typing(context, message)
    audit = Audit()
    try:
        found = await product_match.find(wanted, limit=MAX_CANDIDATES, audit=audit,
                                         dry_run=session.dry_run)
    except Exception as exc:                       # the shop is down, or answered nonsense
        await _fail(context, message, f"جست‌وجو در فروشگاه ناموفق بود: {describe_exception(exc)}", audit)
        return RESTOCK_MATCH
    if not found:
        await message.reply_text(
            f"چیزی با «{wanted}» در فروشگاه پیدا نکردم.\n\n"
            "اگر SKU را می‌دانی همان را دقیق بنویس؛ اگر عنوان عوض شده، با یک کلمهٔ دیگر جست‌وجو کن.",
            reply_markup=_kb([[InlineKeyboardButton("⏹ انصراف", callback_data=CB.RESTOCK_CANCEL)]]),
        )
        return RESTOCK_MATCH
    session.candidates = found
    head = ("📊 این محصول را پیدا کردم:" if len(found) == 1
            else f"📊 {len(found)} محصول پیدا شد؛ کدام است؟")
    # Plain text (no parse mode): the shop's entities («&amp;») are unescaped, never escaped twice.
    body = "\n".join(f"{index + 1}. {html.unescape(candidate.label())}"
                     for index, candidate in enumerate(found))
    rows = [[InlineKeyboardButton(
        _short(candidate.label()), callback_data=f"{CB.RESTOCK_PICK}:{candidate.product_id}")]
        for candidate in found]
    rows.append([InlineKeyboardButton("🔎 جستجوی دوباره", callback_data=CB.RESTOCK_RETRY_SEARCH),
                 InlineKeyboardButton("⏹ انصراف", callback_data=CB.RESTOCK_CANCEL)])
    await message.reply_text(f"{head}\n\n{body}", reply_markup=_kb(rows))
    return RESTOCK_MATCH


async def pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """A candidate was tapped: read it with its variations, then hand the chat to the builder."""
    query = update.callback_query
    user = update.effective_user
    session = _session(update)
    if query is None or session is None or user is None:
        return ConversationHandler.END
    try:
        product_id = int(str(query.data or "").rsplit(":", 1)[-1])
    except ValueError:
        await query.answer("دکمهٔ قدیمی است؛ دوباره جستجو کن.", show_alert=True)
        return RESTOCK_MATCH
    await _typing(context, query.message)
    audit = Audit()
    try:
        product = await product_match.read(product_id, audit=audit, dry_run=session.dry_run)
    except Exception as exc:
        reason = describe_exception(exc)
        await _log(context, f"[restock:{user.id}] محصول {product_id} خوانده نشد: {reason}\n"
                   + "\n".join(audit.lines[-6:]))
        # A toast: the candidate list stays where it is, so another one can be tapped at once.
        await query.answer(f"❌ محصول {product_id} خوانده نشد: {reason}"[:190], show_alert=True)
        return RESTOCK_MATCH
    await query.answer()
    from bot.modules import product_flow            # lazy: product_flow owns the builder
    return await product_flow.begin_update(update, context, product)


async def retry_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user = update.effective_user
    session = _session(update)
    if query is None or session is None or user is None:
        return ConversationHandler.END
    await query.answer()
    session.candidates = []
    if query.message:
        await query.message.edit_text(_search_prompt(session.dry_run),
                                      reply_markup=await _search_keyboard(user.id))
    return RESTOCK_MATCH


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user = update.effective_user
    if user is not None:
        cleanup(user.id)
    if query is not None:
        await query.answer()
        if query.message:
            # A tap edits the message under the finger; it never adds one.
            await query.message.edit_text("⏹ اپدیت لغو شد؛ چیزی در فروشگاه عوض نشد.")
    return ConversationHandler.END


async def ignore_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """A photo or file before a product was picked: a ⚡ on it, not a lecture.

    There is nothing to attach it to yet, and the message above already says what to type. PTB
    has no «keep my state» sentinel for a message handler (returning ``None`` ends the
    conversation), so the state is returned explicitly.
    """
    await _flag(context, update.effective_message)
    return RESTOCK_MATCH


# — plumbing —

def _session(update: Update) -> RestockSession | None:
    user = update.effective_user
    return sessions.get(user.id) if user else None


def _short(label: str) -> str:
    text = html.unescape(label)
    return text if len(text) <= BUTTON_LABEL else text[:BUTTON_LABEL - 1] + "…"


def _kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(rows)


async def _typing(context: ContextTypes.DEFAULT_TYPE, message: Any) -> None:
    """Telegram's transient «typing…» — the only progress signal this flow gives."""
    send = getattr(getattr(context, "bot", None), "send_chat_action", None)
    chat_id = getattr(message, "chat_id", None)
    if send is None or chat_id is None:
        return
    try:
        await send(chat_id=chat_id, action=ChatAction.TYPING)
    except Exception as exc:                       # best-effort: never break the flow for a spinner
        logger.debug("chat action unavailable (%s): %s", type(exc).__name__, exc)


async def _flag(context: ContextTypes.DEFAULT_TYPE, message: Any) -> None:
    """The ⚡ reaction on a message that could not be handled (best-effort, like the builder's)."""
    from bot.modules import product_flow
    user = getattr(message, "from_user", None)
    await product_flow._react_failure(context, getattr(user, "id", 0), None, message)


async def _fail(context: ContextTypes.DEFAULT_TYPE, message: Any, text: str, audit: Audit) -> None:
    """A failure: ⚡ on the seller's message, the reason and the HTTP tail in the log group."""
    logger.warning("[restock] %s", text)
    await _flag(context, message)
    tail = "\n".join(audit.lines[-6:])
    await _log(context, f"[restock] {text}" + (f"\n{tail}" if tail else ""))


async def _log(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    from bot.modules.product_flow import _telegram_log
    await _telegram_log(context, text)


def callbacks() -> list[Any]:
    """Buttons of the search screens, so «انصراف» works wherever it is pressed."""
    return [
        CallbackQueryHandler(retry_search, pattern=rf"^{CB.RESTOCK_RETRY_SEARCH}$"),
        CallbackQueryHandler(cancel, pattern=rf"^{CB.RESTOCK_CANCEL}$"),
    ]


def states() -> dict[int, list[Any]]:
    """The state product_flow's conversation adds for this flow."""
    return {
        RESTOCK_MATCH: [
            CallbackQueryHandler(pick, pattern=rf"^{CB.RESTOCK_PICK}:[0-9]+$"),
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_search),
            MessageHandler(filters.PHOTO | filters.Document.ALL, ignore_media),
            *callbacks(),
        ],
    }


__all__ = ["RESTOCK_MATCH", "RestockSession", "callbacks", "cancel", "cleanup", "pick", "sessions",
           "states"]
