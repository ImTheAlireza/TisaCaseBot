"""یک کارت ساختاریافته به‌ازای هر محصول، برای چت لاگ (`LOG_CHAT_ID`).

مسیر انتشار تا «✅ ساخته شد» چهارده پیام جدا می‌فرستاد: دریافت هر عکس، فشرده‌سازی،
کپشن، مدل‌ها، JSON استخراج‌شده، دادهٔ نهایی… و نتیجه‌اش یک چتِ خوانده‌نشدنی بود.
لاگی که خوانده نشود ممیزی نیست؛ پس اینجا همه‌چیز در **یک کارت** جمع می‌شود و
خط‌به‌خطِ همان اطلاعات فقط با ``VERBOSE_LOG=1`` فرستاده می‌شود (برای همان محصول،
همان لحظه، در پیامی جدا).

ژورنال در ``chat_data`` همان چت زندگی می‌کند، نه در یک دیکشنری جهانی: دو ادمین هم‌زمان
یعنی دو ژورنال، و هیچ‌کدام روی دیگری نمی‌نویسد.

اینجا هیچ عددی «حداقل» یا «تقریبی» نیست: کارت فقط چیزهایی را می‌گوید که جریان واقعاً
به او گفته (فکت‌ها را همان لحظه‌ای که اتفاق می‌افتند ثبت می‌کند)، و اگر چیزی ثبت نشده
باشد همان را می‌نویسد — نه صفر.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from bot.config import settings
from bot.utils.text import MESSAGE_LIMIT, clip

logger = logging.getLogger(__name__)

#: کجا در ``chat_data`` نگه داشته می‌شود (هم‌سبکِ ``tisa_tracking_pending``).
KEY = "tisa_product_journal"

#: ردیف‌های trace که در یک پیام جا می‌شوند؛ بیشتر از این یعنی لاگِ فایل، نه چت.
_TRACE_CHARS = 3 * MESSAGE_LIMIT
_MAX_LIST_ITEMS = 6

@dataclass
class Journal:
    """What one product left behind, while it is still on screen."""

    started: float = field(default_factory=time.perf_counter)
    stages: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    facts: dict[str, str] = field(default_factory=dict)
    trace: list[str] = field(default_factory=list)

    # --- جمع‌کردن -------------------------------------------------------------

    def stage(self, text: str) -> None:
        """One visible step of the flow (repeats are collapsed: «⏳…» may be edited)."""
        clean = (text or "").strip()
        if clean and (not self.stages or self.stages[-1] != clean):
            self.stages.append(clean)

    def warn(self, text: str) -> None:
        clean = (text or "").strip()
        if clean and clean not in self.warnings:
            self.warnings.append(clean)

    def fail(self, text: str) -> None:
        clean = (text or "").strip()
        if clean and clean not in self.errors:
            self.errors.append(clean)

    def line(self, text: str) -> None:
        """A trace line — kept whether or not it will be sent."""
        clean = (text or "").strip()
        if clean:
            self.trace.append(clean)

    def fact(self, **values: Any) -> None:
        """Set a field of the card. ``None``/empty never overwrites what is known."""
        for name, value in values.items():
            if value is None or str(value).strip() == "":
                continue
            self.facts[name] = str(value)

    @property
    def seconds(self) -> float:
        return time.perf_counter() - self.started

    # --- خروجی ----------------------------------------------------------------

    def card(self, status: str = "") -> str:
        """The one message: what was built, what it cost, what to look at."""
        facts = self.facts
        head = {
            "created": "✅ محصول ساخته شد",
            "dry": "🧪 تمرین (بدون نوشتن روی سایت)",
            "zip": "📦 ZIP ساخته شد",
            "queued": "🕐 به صفِ تلاشِ دوباره رفت",
            "failed": "❌ منتشر نشد",
            "restocked": "🔄 شارژ اعمال شد",
            "abandoned": "⌛ جریان رها شد (تایم‌اوت)",
            "error": "❌ خطا در جریان",
        }.get(status, "🧾 کارت محصول")
        title = facts.get("title") or "— بی‌عنوان"
        lines = [f"{head} · {title}"]
        if facts.get("product_id"):
            lines.append(f"🆔 محصول {facts['product_id']}")
        if facts.get("edit_url"):
            lines.append(f"🔗 {facts['edit_url']}")
        if facts.get("batch_id"):
            lines.append(f"📦 شناسهٔ این محتوا: {facts['batch_id']}")

        details = [
            f"⏱ {self.seconds:.0f} ثانیه",
            f"🎨 {facts.get('variations', '—')} واریژن",
            f"🖼 {facts.get('images', '—')} تصویر",
        ]
        if facts.get("price"):
            details.append(f"💰 {facts['price']}")
        lines.append(" · ".join(details))

        if self.stages:
            lines += ["", *self._stage_lines()]
        diagnostics = self._diagnostic_lines()
        if diagnostics:
            lines += ["", "🔎 پایش عملیات:", *[f"   · {line}" for line in diagnostics]]
        if self.warnings:
            shown = self.warnings[:_MAX_LIST_ITEMS]
            lines += ["", f"⚠️ {len(self.warnings)} هشدار:"] + [f"   · {w}" for w in shown]
            if len(self.warnings) > _MAX_LIST_ITEMS:
                lines.append(f"   · … و {len(self.warnings) - _MAX_LIST_ITEMS} مورد دیگر")
        if self.errors:
            lines += ["", "❌ خطاها:"] + [f"   · {e}" for e in self.errors[:_MAX_LIST_ITEMS]]
        return clip("\n".join(lines), note="\n… (کارت بلند بود؛ ادامه در logs/bot.log)")

    def _diagnostic_lines(self) -> list[str]:
        """Include one compact network/AI digest on every group card.

        Full traces stay opt-in, but the outcome, request count/latency and every
        transport failure are always visible in LOG_CHAT_ID without a message flood.
        """
        raw_lines = "\n".join(self.trace).splitlines()
        http_done = [line for line in raw_lines if line.startswith("[http:done]")]
        elapsed = [
            int(match.group(1))
            for line in http_done
            if (match := re.search(r"در (\d+) ms", line))
        ]
        http_retries = [line for line in raw_lines if line.startswith("[retry]")]
        ai_lines = [line for line in raw_lines if line.startswith("[ai:summary]")]
        ai_diagnostics = [
            line for line in raw_lines if line.startswith("[ai:diagnostic]")
        ]
        failures = [
            line for line in raw_lines
            if line.startswith(("[http:error]", "[media:error]", "[variation:error]", "[sku:error]"))
        ]
        result: list[str] = []
        if http_done:
            timing = f"؛ جمع پاسخ‌ها {sum(elapsed)} ms" if elapsed else ""
            result.append(f"HTTP: {len(http_done)} درخواست{timing}")
        if http_retries:
            result.append(f"تلاش مجدد شبکه: {len(http_retries)}")
        result.extend(ai_lines[-2:])
        result.extend(ai_diagnostics[-4:])
        result.extend(failures[-5:])
        return result

    def _stage_lines(self) -> list[str]:
        """The road this product took. Long runs are folded — a card is not a scroll."""
        if len(self.stages) <= _MAX_LIST_ITEMS:
            return ["🧭 " + " ← ".join(self.stages)]
        shown = self.stages[: _MAX_LIST_ITEMS - 1]
        return ["🧭 " + " ← ".join(shown), f"🧭 … و {len(self.stages) - len(shown)} مرحلهٔ دیگر"]

    def trace_text(self) -> str:
        """The verbose body: the same lines the flow logged, in order."""
        if not self.trace:
            return ""
        body = "\n".join(self.trace)
        if len(body) > _TRACE_CHARS:
            body = body[:_TRACE_CHARS] + "\n… ادامه در `logs/bot.log`"
        return body


async def send_log_message(bot: Any, text: str, *, parse_mode: str | None = None) -> bool:
    """Send one diagnostic to ``LOG_CHAT_ID`` without breaking the user flow.

    Returns whether Telegram accepted the message; configuration/permission failures
    are reported to the local log so a missing group message is diagnosable.
    """
    target = settings.log_chat_id
    if not target:
        logger.warning("log chat message skipped: LOG_CHAT_ID is empty")
        return False
    kwargs: dict[str, Any] = {"chat_id": target, "text": text}
    if parse_mode:
        kwargs["parse_mode"] = parse_mode
    try:
        await bot.send_message(**kwargs)
        return True
    except Exception as exc:
        logger.warning(
            "log chat send failed (LOG_CHAT_ID=%s, %s): %s",
            target,
            type(exc).__name__,
            str(exc)[:240],
        )
        return False


def publish_trace_report(
    lines: Sequence[str], *, dry_run: bool = False, budget: int = 3600
) -> str:
    """Format a truthful, compact shop-request trace for the configured log chat."""
    if not lines:
        return ""
    steps = [str(line) for line in lines if str(line).startswith("[dry-run]")]
    notes = [
        str(line) for line in lines
        if not str(line).startswith(("[dry-run]", "[payload]"))
    ]
    body = "\n".join([*steps, *notes])
    if len(body) > budget:
        kept = body[:budget].rsplit("\n", 1)[0]
        dropped = body.count("\n") - kept.count("\n")
        body = kept + "\n" + f"… ({dropped} خط دیگر — کاملش در لاگ فایل است)"
    header = (
        "🧪 درخواست‌هایی که ساخته شدند و ارسال نشدند (هیچ‌کدام به سایت نرفتند):"
        if dry_run
        else "📋 ردپای انتشار واقعی (درخواست‌ها به سایت ارسال شدند):"
    )
    return header + ("\n" + body if body else "")


async def send_publish_trace(
    bot: Any, lines: Sequence[str], *, dry_run: bool = False
) -> bool:
    """Send the detailed publish audit only to ``LOG_CHAT_ID`` (never to the owner DM)."""
    text = publish_trace_report(lines, dry_run=dry_run)
    return await send_log_message(bot, text) if text else False


def journal_for(context: Any) -> Journal | None:
    """This chat's journal (created on first use); ``None`` without a ``chat_data``."""
    data = getattr(context, "chat_data", None)
    if data is None:
        return None
    found = data.get(KEY)
    if isinstance(found, Journal):
        return found
    found = Journal()
    data[KEY] = found
    return found


def reset(context: Any) -> None:
    """Forget the journal — a new product in the same chat starts a new card."""
    data = getattr(context, "chat_data", None)
    if data is not None:
        data.pop(KEY, None)


async def flush(
    context: Any, *, status: str = "", chat_id: object = None, **facts: Any
) -> Journal | None:
    """Send the card (and the trace, with ``VERBOSE_LOG=1``) — once, then it is gone.

    Returns the journal it sent, so a caller can still log it, and ``None`` when there
    is nothing to say: a flow that produced no line at all must not send an empty card.
    """
    journal = journal_for(context)
    if journal is None:
        return None
    for name, value in facts.items():
        journal.fact(**{name: value})
    if not (journal.stages or journal.trace or journal.facts or journal.errors or journal.warnings):
        return None
    card = journal.card(status)
    # The journal is closed *before* sending: a send that throws must not leave a
    # half-flushed card to be re-sent on the next step of the same flow.
    reset(context)
    await _send(context, card, journal, chat_id=chat_id)
    return journal


async def _send(context: Any, card: str, journal: Journal, *, chat_id: object) -> None:
    """Write the card to the log file and, if configured, to the log chat."""
    logger.info("product card:\n%s", card)
    if journal.trace and settings.verbose_log:
        logger.info("product trace:\n%s", journal.trace_text())
    target = settings.log_chat_id
    if not target:
        logger.warning("product log card skipped: LOG_CHAT_ID is empty")
        return
    try:
        await context.bot.send_message(chat_id=target, text=card)
        if settings.verbose_log and journal.trace:
            trace = journal.trace_text()
            for start in range(0, len(trace), MESSAGE_LIMIT):
                await context.bot.send_message(
                    chat_id=target, text=trace[start : start + MESSAGE_LIMIT]
                )
    except Exception as exc:
        # Never break publishing, but do not hide an invalid chat id, missing
        # membership, or missing send permission at INFO log level.
        logger.warning(
            "product log delivery failed (LOG_CHAT_ID=%s, %s): %s",
            target,
            type(exc).__name__,
            str(exc)[:240],
        )


__all__ = [
    "Journal", "flush", "journal_for", "publish_trace_report", "reset",
    "send_log_message", "send_publish_trace",
]
