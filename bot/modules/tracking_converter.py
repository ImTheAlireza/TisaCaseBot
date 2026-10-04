"""تبدیل فایل کد رهگیری — the warehouse's daily tool, as a conversation.

Button «📦 تبدیل فایل کد رهگیری» → bot asks for an order file (.xlsx / .csv / .pdf) →
the file is read and answered with `tracking.csv` plus, when something was wrong,
an editable `needs-review.xlsx`, `needs-review.csv` and `problems.csv` whose rows point at
the cell in the source sheet.

Three rules shape this module:

* **Never guess a column.** When a file has two plausible headers (both «بارکد» and
  «کد رهگیری»), taking the first one is how a wrong code reaches the tracking system.
  The bot asks instead, inside the same conversation (``MAP_COLUMNS``), and the answer
  applies to that file only — remembering it forever would turn one odd export into a
  permanent misreading of every later one.
* **A file is processed once unless someone says otherwise.** The same export arriving
  twice is normal (someone else already ran it), and silently producing a second import
  file is how two different rows end up in the system. A repeat is answered with the
  earlier card and a «🔁 دوباره پردازشش کن» button, so re-running stays a decision
  (``DUPLICATE_FILE``).
* **Nothing waits in /tmp forever.** A question can go unanswered and the bot can be
  restarted mid-file, so a download lives in a per-session workspace that an hourly
  sweep collects — the same pattern the product flow uses, from
  :mod:`bot.services.workspace`.

Processing logic lives in :mod:`bot.services.processor` (no Telegram code there).
"""

from __future__ import annotations

import asyncio
import html
import io
import logging
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
from bot.services.conversations import FlowConversationHandler
from bot.keyboards import main_menu_keyboard, main_menu_text
from bot.services import flow_guard, metrics, processor, tracking_ledger, worker, workspace
from bot.services.access import guard_feature
from bot.utils.ui import answer_and, answer_and_edit

logger = logging.getLogger(__name__)

# Conversation states
ASK_FILE = 0
MAP_COLUMNS = 1
DUPLICATE_FILE = 2

ALLOWED_EXTS = {".xlsx", ".xlsm", ".csv", ".pdf"}

#: پسوندهایی که نمی‌پذیریم — با راهِ حلِ خودشان. «دوباره بفرست» برای فایل xls یعنی
#: فرستادنِ همان فایلِ xls؛ جمله باید بگوید چطور xlsx شود.
_UNSUPPORTED_HINTS = {
    ".xls": "این فایل اکسل قدیمی (xls) است؛ در اکسل بازش کن و «Save As → Excel Workbook (*.xlsx)» بزن.",
    ".ods": "این فایل LibreOffice است؛ در همان برنامه «Save As → xlsx» بزن.",
    ".txt": "این فایل متنی است؛ پسوندش را به csv تغییر بده (اگر جدولِ جداکننده‌دار است).",
    ".numbers": "این فایل Numbers است؛ از آن «Export → Excel» بگیر.",
}
#: Where a download waits between messages (swept, like the product flow's workspace).
TEMP_DIR = Path("/tmp/tisaposttowp-tracking")
PENDING_KEY = "tisa_tracking_pending"
_active_contexts: dict[int, ContextTypes.DEFAULT_TYPE] = {}


def close_for(user_id: int) -> bool:
    context = _active_contexts.pop(user_id, None)
    if context is None:
        return False
    _release(context)
    return True


flow_guard.register("tracking", "تبدیل کد رهگیری", close_for)

#: «trk:pick:<field>:<column index>» — the answer to a column question.
PICK_PREFIX = "trk:pick:"


#: «سقف‌ها: …» در یک خط؛ خودِ رشته از `Settings.limits_line` می‌آید تا این صفحه و
#: «📊 وضعیت» دو نسخهٔ متفاوت از یک قول نداشته باشند.
def _limits_line() -> str:
    return f"سقف‌ها: {settings.limits_line}"


#: پیامِ ورودِ جریان: فقط «چه بفرست». توضیحِ گام‌ها، هشدارها و خطِ سقف‌ها حذف شد
#: (خواستهٔ صاحب ربات: «خیلی توضیح اضافه دارد»). همین سقف‌ها همان‌جایی گفته می‌شوند که
#: به کار می‌آیند — «🚧 …» کنارِ خودِ خطا و «📊 وضعیت» — تا دو نسخهٔ مختلف از یک قول نداشته باشیم.
INSTRUCTIONS = (
    "📦 <b>تبدیل فایل کد رهگیری</b>\n\n"
    "یک فایل با یکی از این فرمت‌ها بفرست (به‌صورت Document، نه عکس):\n"
    "📊 اکسل (<code>.xlsx</code> / <code>.xlsm</code>) — خروجی جدول سفارش‌ها\n"
    "📄 CSV (<code>.csv</code>)\n"
    "📑 PDF (<code>.pdf</code>) — خروجی مستقیم سامانه تیساکیس / تیسا چاپ"
)

NEXT_FILE_TEXT = "📤 فایل بعدی را بفرست، یا برگرد به منو."

#: راهِ حلِ هر پسوند — «چه بفرستم» به‌جای «دوباره امتحان کن». Every functional error in
#: this flow ends with one of these, because «خطا در پردازش» alone sends the warehouse
#: back to the same file.
_EXCEL_HINT = (
    "راه‌حل: فایل را در اکسل باز کن و «Save As → Excel Workbook (*.xlsx)» بزن، "
    "یا همان گزارش را از سامانه به‌صورت CSV/PDF بگیر."
)
_RECOVERY_HINTS = {
    ".xlsx": _EXCEL_HINT,
    ".xlsm": _EXCEL_HINT,
    ".csv": (
        "راه‌حل: خروجی CSV را دوباره از سامانه بگیر (جداکننده یا کدگذاری‌اش ممکن است خراب شده باشد)، "
        "یا همان گزارش را به‌صورت اکسل/PDF بفرست."
    ),
    ".pdf": "راه‌حل: اگر PDF اسکن‌شده یا عکس است، خروجیِ متنی (اکسل/CSV) همان گزارش را از سامانه بگیر.",
}


def _recovery_hint(path) -> str:
    return _RECOVERY_HINTS.get(Path(path).suffix.lower() or "", _RECOVERY_HINTS[".xlsx"])


def _cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ بازگشت به منو", callback_data=CB.TRACKING_CANCEL)]])


# --- The pending file (what a question is about) -------------------------------


def pending_of(context: ContextTypes.DEFAULT_TYPE) -> dict:
    """The download this conversation is waiting on, if any."""
    data = context.chat_data.get(PENDING_KEY) if context.chat_data is not None else None
    return data if isinstance(data, dict) else {}


def _hold(context: ContextTypes.DEFAULT_TYPE, **fields: object) -> None:
    """Remember the file across messages (a question may be answered minutes later)."""
    if context.chat_data is None:
        return
    state = pending_of(context)
    state.update(fields)
    context.chat_data[PENDING_KEY] = state


def _release(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Forget the file and delete its workspace — nothing outlives the question."""
    state = pending_of(context)
    if context.chat_data is not None:
        context.chat_data.pop(PENDING_KEY, None)
    directory = state.get("dir")
    if directory:
        workspace.remove(Path(str(directory)))


# --- Flow steps ---------------------------------------------------------------


async def entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Button pressed → show instructions, wait for a file."""
    query = update.callback_query
    user = update.effective_user
    # Available to sudo always, and to admins while the button is visible to them.
    if not user or not feature_allowed(user.id, "tracking"):
        await query.answer("⛔ دسترسی ندارید.", show_alert=True)
        return ConversationHandler.END

    flow_guard.close_others("tracking", user.id)
    close_for(user.id)
    _active_contexts[user.id] = context
    logger.info("User %s entered tracking-converter flow", user.id)
    await answer_and_edit(query, INSTRUCTIONS, reply_markup=_cancel_keyboard(), parse_mode="HTML")
    return ASK_FILE


@guard_feature("tracking", on_denial=close_for, checker=lambda uid, key: feature_allowed(uid, key))
async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """A document arrived while we're waiting — process it, or ask what it means."""
    msg = update.effective_message
    user = update.effective_user
    doc = msg.document
    fname = doc.file_name or "file"
    ext = Path(fname).suffix.lower()
    user_id = user.id if user else 0

    _release(context)  # an abandoned question from the previous file is gone now
    if ext not in ALLOWED_EXTS:
        hint = _UNSUPPORTED_HINTS.get(ext)
        await msg.reply_text(
            "❌ فقط فایل‌های xlsx / xlsm / csv / pdf پشتیبانی می‌شوند."
            + (f"\n{hint}" if hint else " دوباره بفرست."),
            reply_markup=_cancel_keyboard(),
        )
        return ASK_FILE

    status = await msg.reply_text("⏳ در حال پردازش…")
    directory = workspace.new_dir(TEMP_DIR, user_id)
    workspace.protect(directory)
    tmp = directory / f"input{ext}"
    _hold(context, dir=str(directory), path=str(tmp), fname=fname, user_id=user_id)
    try:
        tg_file = await doc.get_file()
        # سقف حجم این‌جا چک می‌شود، نه بعد از دانلود: یک فایل چند صد مگابایتی هم
        # دیسک را پر می‌کند هم ربات را برای همه می‌خواباند.
        if tg_file.file_size and tg_file.file_size > settings.max_file_mb * 1024 * 1024:
            await _drop_status(status)
            await msg.reply_text(
                f"🚧 حجم فایل ({tg_file.file_size / 1024 / 1024:.1f} MB) از سقف "
                f"{settings.max_file_mb:g} MB بیشتر است — فایل را تقسیم کن یا "
                "MAX_FILE_MB را در .env بالا ببر.",
                reply_markup=_cancel_keyboard(),
            )
            _release(context)
            return ASK_FILE
        await tg_file.download_to_drive(str(tmp))
        if tmp.stat().st_size > settings.max_file_mb * 1024 * 1024:
            raise processor.RowLimitError("حجم واقعی فایل دانلودشده از MAX_FILE_MB بیشتر است")
    except Exception as exc:
        # Only the download is wrapped: `_run` answers a bad file with a message of its
        # own, and a failed *send* must not be reported as «خطا در پردازش».
        logger.exception("tracking download failed: %s", fname)
        await _drop_status(status)
        await msg.reply_text(
            f"❌ فایل دانلود نشد:\n{exc}\n\nدوباره بفرست یا برگرد به منو.",
            reply_markup=_cancel_keyboard(),
        )
        _release(context)
        return ASK_FILE

    fp = tracking_ledger.fingerprint(tmp)
    _hold(context, fp=fp)
    earlier = tracking_ledger.find(fp)
    if earlier and not pending_of(context).get("force"):
        return await _ask_about_repeat(msg, earlier)
    return await _finish(msg, status, context, await _run(tmp, fname))


async def _run(path: Path, fname: str, layout: processor.Layout | None = None):
    """Process in a bounded, killable worker; the time ceiling stops actual work.

    Returns the :class:`~bot.services.processor.Report`, or a ready-to-send message
    when the file itself is unusable (too many rows / unreadable) — the caller only
    has to choose where to send it.
    """
    try:
        return await worker.run(processor.process_file, str(path), fname, layout=layout,
                                timeout=settings.process_timeout_seconds)
    except TimeoutError:
        return (
            f"⏱️ پردازش فایل بیشتر از {settings.process_timeout_seconds:g} ثانیه طول کشید. "
            "فایل را به بخش‌های کوچک‌تر تقسیم کن یا مستقیم از خروجی متنی/PDF سامانه استفاده کن."
            f"\n\n🚧 {_limits_line()}"
        )
    except processor.RowLimitError as exc:
        return f"🚧 {exc}\n\nسقف‌ها در .env قابل تغییرند (MAX_ROWS / MAX_FILE_MB)."
    except (worker.WorkerNoMemory, MemoryError) as exc:
        # خودِ ربات جا ندارد، نه فایل: تا امروز این حالت یک «❌ خطا در پردازش: MemoryError»
        # بی‌توضیح بود (فایل ۲۲KB هم همین را می‌گرفت). حالا جمله می‌گوید چه چیزی را بالا ببرد.
        logger.error("tracking worker is out of memory: %s", exc)
        return (
            "🚧 حافظهٔ پردازشِ ربات پر است و این فایل — هرچقدر هم کوچک — جا نشد.\n"
            "دو کار: ۱) WORKER_MEMORY_MB را در .env بالا ببر (و ربات را ری‌استارت کن)؛ "
            "۲) اگر سرور/سوپروایزر خودش سقف دارد (`ulimit -v`، systemd `LimitAS`)، همان را "
            "بالا ببر.\n"
            "اعدادِ دقیق — پایه، سقف و فضای آزادِ کارگر — در logs/bot.log نوشته شده‌اند."
        )
    except worker.WorkerCrash:
        # The child died (memory/CPU cap) or the bot was restarted mid-file. There is no
        # sentence to translate, only something to do: split the file.
        logger.exception("tracking worker stopped mid-file: %s", fname)
        return (
            "🚧 پردازش این فایل وسطِ کار متوقف شد — فایل برای حافظهٔ ربات سنگین بود یا ربات "
            "ری‌استارت شد.\n"
            "فایل را به دو یا چند بخش کوچک‌تر تقسیم کن و هر بخش را جدا بفرست؛ "
            "یا از سامانه خروجی CSV بگیر (سبک‌تر از اکسل است).\n\n"
            f"🚧 {_limits_line()}"
        )
    except Exception as exc:  # pragma: no cover — parser-level
        logger.exception("tracking file failed: %s", fname)
        reason = str(exc).strip() or type(exc).__name__
        return (
            f"❌ خطا در پردازش:\n{reason}\n\n{_recovery_hint(path)}\n"
            f"(جزئیاتِ فنی در logs/bot.log)\n{NEXT_FILE_TEXT}"
        )


def _record(context: ContextTypes.DEFAULT_TYPE, report: processor.Report) -> None:
    """Say what was done, so the next copy of this file is recognised as a repeat."""
    state = pending_of(context)
    fp = str(state.get("fp") or "")
    if not fp or report.needs_answer:
        return
    metrics.incr("tracking_converted")
    if report.needs_review:
        metrics.incr("tracking_review_rows", report.needs_review)
    tracking_ledger.remember(
        fp,
        user_id=state.get("user_id"),
        fname=str(state.get("fname") or "file"),
        report=report.as_dict(),
    )


async def _drop_status(status) -> None:
    try:
        await status.delete()
    except Exception:  # pragma: no cover — best effort
        pass


async def _send_report(msg, status, report: processor.Report) -> None:
    """The files, in the order the warehouse works with them."""
    await msg.reply_document(
        # BOM → اکسل فارسی را درست نشان می‌دهد
        document=io.BytesIO(report.csv_text.encode("utf-8-sig")),
        filename="tracking.csv",
        caption=report.summary[:950],  # محدودیت کپشن تلگرام ~1024
    )
    # The review list goes out as XLSX when it can (it is the only form that can be
    # edited and sent back without Excel eating the digits); the CSV is the fallback
    # for a workbook that could not be built, not a duplicate of it in the chat.
    if report.review_xlsx:
        await msg.reply_document(
            document=io.BytesIO(report.review_xlsx),
            filename="needs-review.xlsx",
            caption=(
                f"🔧 {report.needs_review} ردیف باید دیده شود — {report.dropped} تای آن‌ها "
                "اصلاً در tracking.csv ننوشته شدند.\n"
                "ستون بارکد این فایل «متن» است، پس اکسل رقم‌هایش را خراب نمی‌کند: اصلاحش کن و "
                "همین فایل را دوباره بفرست تا همان را بخوانم."
            ),
        )
    elif report.review_csv:
        await msg.reply_document(
            document=io.BytesIO(report.review_csv.encode("utf-8-sig")),
            filename="needs-review.csv",
            caption=(
                f"🔧 {report.needs_review} ردیف با دلیل (بدون اکسل، چون فایل xlsx ساخته نشد). "
                "ستون بارکد را در اکسل باز نکن — رقم‌هایش می‌رود."
            ),
        )
    if report.problems_csv:
        await msg.reply_document(
            document=io.BytesIO(report.problems_csv.encode("utf-8-sig")),
            filename="problems.csv",
            caption=("📋 همهٔ مشکلات، با ستون «محل سطر» (مثلاً Sheet1!B12) تا در فایل اصلی پیدایش کنی."),
        )
    await _drop_status(status)
    await msg.reply_text(NEXT_FILE_TEXT, reply_markup=_cancel_keyboard())


# --- The two questions --------------------------------------------------------


def _question_keyboard(questions: tuple[processor.Question, ...]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for question in questions:
        for column in question.options:
            rows.append(
                [
                    InlineKeyboardButton(
                        column.label(), callback_data=f"{PICK_PREFIX}{question.field}:{column.index}"
                    )
                ]
            )
        if question.allow_skip:
            rows.append(
                [
                    InlineKeyboardButton(
                        "بدون این ستون ادامه بده", callback_data=f"{PICK_PREFIX}{question.field}:-1"
                    )
                ]
            )
    rows.append([InlineKeyboardButton("❌ بی‌خیال، این فایل نه", callback_data=CB.TRACKING_CANCEL)])
    return InlineKeyboardMarkup(rows)


async def _ask_about_columns(
    msg, status, context: ContextTypes.DEFAULT_TYPE, report: processor.Report
) -> int:
    """The file is ambiguous — keep it on disk and ask which column is meant."""
    await _drop_status(status)
    lines = [
        "🗺️ <b>کدام ستون؟</b>",
        "",
        "دو ستون محتمل دیدم. حدس زدن یعنی رفتنِ کدِ اشتباه به سامانهٔ رهگیری، "
        "پس می‌پرسم. یکی را انتخاب کن — همین یک فایل، برای فایل‌های بعدی چیزی به خاطرم "
        "نمی‌ماند.",
    ]
    for question in report.questions:
        lines += ["", html.escape(question.prompt)]
    if len(report.questions) > 1:
        lines += ["", f"({len(report.questions)} سؤال دارم؛ بعد از هر جواب، بعدی را می‌پرسم.)"]
    _hold(context, layout=report.layout or processor.Layout())
    await msg.reply_html("\n".join(lines), reply_markup=_question_keyboard(report.questions))
    return MAP_COLUMNS


async def _finish(msg, status, context: ContextTypes.DEFAULT_TYPE, report) -> int:
    """The one way a run ends: apologise, ask what is missing, or send the files."""
    if isinstance(report, str):
        await _drop_status(status)
        await msg.reply_text(report, reply_markup=_cancel_keyboard())
        _release(context)
        return ASK_FILE
    if report.needs_answer:
        return await _ask_about_columns(msg, status, context, report)
    await _send_report(msg, status, report)
    _record(context, report)
    _release(context)
    return ASK_FILE


@guard_feature("tracking", on_denial=close_for, checker=lambda uid, key: feature_allowed(uid, key))
async def on_pick_column(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """An answer to «کدام ستون؟» → re-read the same file with that column."""
    query = update.callback_query
    state = pending_of(context)
    path = state.get("path")
    if not path or not Path(str(path)).exists():
        await query.answer("⚠️ آن فایل دیگر این‌جا نیست؛ دوباره بفرستش.", show_alert=True)
        return ASK_FILE

    data = str(query.data or "")
    try:
        field, index = data[len(PICK_PREFIX) :].rsplit(":", 1)
        chosen = int(index)
        if field not in (processor.FIELD_BARCODE, processor.FIELD_CODE):
            raise ValueError(field)
    except ValueError:
        await query.answer("❌ این دکمه به همین صفحه نیست.", show_alert=True)
        return MAP_COLUMNS

    await query.answer()
    stored = state.get("layout")
    base = stored if isinstance(stored, processor.Layout) else processor.Layout.from_dict(stored or {})
    # The answer replaces only the field it answers; the rest of the layout stands.
    layout = base.with_choice(field, chosen)
    state["layout"] = layout
    status = await query.message.reply_text("⏳ دارم با همین ستون دوباره می‌خوانم…")
    report = await _run(Path(str(path)), str(state.get("fname") or "file"), layout)
    return await _finish(query.message, status, context, report)


# --- Duplicate file -----------------------------------------------------------


async def _ask_about_repeat(msg, earlier: dict) -> int:
    """The same file was converted before — say so instead of quietly doing it twice."""
    await msg.reply_text(
        "♻️ این فایل قبلاً پردازش شده:\n\n"
        f"{tracking_ledger.describe(earlier)}\n\n"
        "اگر واقعاً باید دوباره ساخته شود (مثلاً فایلِ قبلی اشتباه وارد سامانه شده)، «🔁» را بزن.",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🔁 دوباره پردازشش کن", callback_data=CB.TRACKING_RETRY),
                    InlineKeyboardButton("📤 فایل دیگری بفرست", callback_data=CB.TRACKING_MORE),
                ],
                [InlineKeyboardButton("⬅️ بازگشت به منو", callback_data=CB.TRACKING_CANCEL)],
            ]
        ),
    )
    return DUPLICATE_FILE


@guard_feature("tracking", on_denial=close_for, checker=lambda uid, key: feature_allowed(uid, key))
async def on_retry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """«🔁 دوباره پردازشش کن» → the file on disk is processed again, on purpose."""
    query = update.callback_query
    await query.answer()
    state = pending_of(context)
    path = state.get("path")
    if not path or not Path(str(path)).exists():
        await query.message.reply_text(
            "⚠️ آن فایل دیگر این‌جا نیست (ربات ری‌استارت شده یا وقت گذشته). دوباره بفرستش."
        )
        return ASK_FILE
    _hold(context, force=True)
    status = await query.message.reply_text("⏳ در حال پردازش…")
    report = await _run(Path(str(path)), str(state.get("fname") or "file"))
    return await _finish(query.message, status, context, report)


@guard_feature("tracking", on_denial=close_for, checker=lambda uid, key: feature_allowed(uid, key))
async def on_send_another(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """«📤 فایل دیگری بفرست» → drop this one, stay in the flow."""
    query = update.callback_query
    _release(context)
    await answer_and(query, query.message.reply_text(NEXT_FILE_TEXT, reply_markup=_cancel_keyboard()))
    return ASK_FILE


@guard_feature("tracking", on_denial=close_for, checker=lambda uid, key: feature_allowed(uid, key))
async def on_wrong_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Text/photo/etc. while waiting for a file — nudge."""
    await update.effective_message.reply_text(
        "لطفاً فایل را به‌صورت Document (گیره 📎 → File) بفرست — xlsx / csv / pdf.",
        reply_markup=_cancel_keyboard(),
    )
    return ASK_FILE


# --- Exits ---------------------------------------------------------------------


async def cb_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """«بازگشت به منو» button → end flow, show main menu."""
    query = update.callback_query
    user = update.effective_user
    if user:
        close_for(user.id)
    _release(context)
    await answer_and_edit(
        query,
        main_menu_text(user.id if user else None, user),
        reply_markup=main_menu_keyboard(user.id if user else None),
        parse_mode="HTML",
        quiet=True,
    )
    return ConversationHandler.END


async def cmd_exit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/cancel, /start or /menu during the flow → end it, show main menu."""
    user = update.effective_user
    if user:
        close_for(user.id)
    _release(context)
    await update.effective_message.reply_html(
        main_menu_text(user.id if user else None, user),
        reply_markup=main_menu_keyboard(user.id if user else None),
    )
    return ConversationHandler.END


async def on_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Idle flow: the download is dropped, because nothing may wait forever."""
    metrics.note_abandoned("tracking")
    _release(context)
    msg = update.effective_message if update is not None else None
    if msg is not None:
        await msg.reply_text(
            "⌛ چون مدتی خبری نشد، این جریان بسته شد و فایلِ پردازش‌نشده پاک شد. "
            "هر وقت خواستی «📦 تبدیل فایل کد رهگیری» را دوباره بزن."
        )


def _sweep_now() -> int:
    return workspace.sweep(TEMP_DIR, settings.temp_ttl_hours, label="tracking workspace")


async def sweep(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Hourly janitor for abandoned files (the product flow's sweep, for our root)."""
    await asyncio.to_thread(_sweep_now)


# --- Registration --------------------------------------------------------------


def register(app: Application) -> None:
    conv = FlowConversationHandler(
        flow="tracking",
        entry_points=[CallbackQueryHandler(entry, pattern=f"^{CB.TRACKING_CONVERT}$")],
        states={
            ASK_FILE: [
                MessageHandler(filters.Document.ALL, on_document),
                CallbackQueryHandler(cb_cancel, pattern=f"^{CB.TRACKING_CANCEL}$"),
                MessageHandler(~filters.COMMAND, on_wrong_input),
            ],
            MAP_COLUMNS: [
                CallbackQueryHandler(
                    on_pick_column,
                    pattern=rf"^{PICK_PREFIX}(?:{processor.FIELD_BARCODE}|{processor.FIELD_CODE}):-?\d+$",
                ),
                CallbackQueryHandler(cb_cancel, pattern=f"^{CB.TRACKING_CANCEL}$"),
            ],
            DUPLICATE_FILE: [
                CallbackQueryHandler(on_retry, pattern=f"^{CB.TRACKING_RETRY}$"),
                CallbackQueryHandler(on_send_another, pattern=f"^{CB.TRACKING_MORE}$"),
                CallbackQueryHandler(cb_cancel, pattern=f"^{CB.TRACKING_CANCEL}$"),
            ],
            # An idle flow ends through here, and PTB hands it the conversation's last
            # update — which may be the message (a document) or the callback (a column
            # question), so both forms are registered.
            ConversationHandler.TIMEOUT: [
                MessageHandler(filters.ALL, on_timeout),
                CallbackQueryHandler(on_timeout),
            ],
        },
        fallbacks=[
            CommandHandler(["cancel", "start", "menu"], cmd_exit),
        ],
        name="tracking_converter",
        # Without this the flow never ends: a user who leaves through the menu leaves
        # every later document in the chat being read as an order file.
        conversation_timeout=settings.flow_timeout_seconds,
    )
    app.add_handler(conv)
    if app.job_queue is not None:
        # An abandoned question or a killed process leaks nothing past the TTL.
        app.job_queue.run_once(sweep, when=7, name="tracking_temp_sweep_boot")
        app.job_queue.run_repeating(sweep, interval=3600, first=610, name="tracking_temp_sweep_hourly")


__all__ = [
    "ALLOWED_EXTS",
    "ASK_FILE",
    "DUPLICATE_FILE",
    "MAP_COLUMNS",
    "TEMP_DIR",
    "cmd_exit",
    "entry",
    "on_document",
    "on_pick_column",
    "on_retry",
    "on_send_another",
    "on_timeout",
    "on_wrong_input",
    "pending_of",
    "register",
    "sweep",
]
