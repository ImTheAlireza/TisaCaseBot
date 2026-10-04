"""نگهداریِ دوره‌ای: پشتیبانِ روزانه و «📊 گزارش روزانه» در چتِ لاگ.

هر دو کار با همان `JobQueue` خودِ PTB زمان‌بندی می‌شوند (نه cron): روی هاستِ اشتراکی
نصبِ cron کارِ اضافه است و ری‌استارتِ ربات هم نباید پشتیبان را عقب بیندازد — برای
همین در استارتِ هر پروسه یک «جبران» هم اجرا می‌شود اگر پشتیبانِ امروز نباشد یا
آخری بیش از ۲۰ ساعت عمر داشته باشد.

گزارش فقط وقتی فرستاده می‌شود که `LOG_CHAT_ID` تنظیم باشد؛ ساعتِ آن با
`TISA_DAILY_REPORT_HOUR` (پیش‌فرض ۹، صفر = خاموش) و ساعتِ پشتیبان ۰۴:۰۰ به وقتِ
سرور است.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import time as clock_time

from telegram.ext import Application, ContextTypes

from bot import __version__
from bot.config import settings
from bot.services import backup, metrics, outbox

logger = logging.getLogger(__name__)

#: Server-local hour of the daily backup. The catch-up pass covers hosts that
#: restart more often than that hour arrives.
BACKUP_HOUR = 4


async def backup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue callback: one zip per day, built off the event loop."""
    try:
        await asyncio.to_thread(backup.create)
    except Exception:  # pragma: no cover - create() already reports its own failures
        logger.exception("maintenance: backup job failed")


def report_text() -> str:
    """The daily summary: version, service counters, queue depth, last backup."""
    lines = [f"📊 <b>گزارش روزانه</b> — نسخهٔ {__version__}", ""]
    counters = metrics.status_lines()
    lines.extend(counters or ["امروز شمارنده‌ای ثبت نشده است."])
    try:
        queue = outbox.stats()
    except Exception:  # pragma: no cover - a broken queue must not lose the report
        logger.exception("maintenance: could not read queue stats")
        queue = {}
    if queue:
        lines.append(f"📤 صفِ ارسال: {int(queue.get('pending', 0))} مورد در انتظار")
    newest = backup.latest()
    lines.append("🗄 آخرین پشتیبان: " + (newest.name if newest else "—"))
    return "\n".join(lines)


async def report_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = settings.log_chat_id
    if not chat_id:
        return
    try:
        await context.bot.send_message(chat_id=chat_id, text=report_text(), parse_mode="HTML")
    except Exception:
        logger.exception("maintenance: daily report could not be sent")


async def start(app: Application) -> int:
    """Schedule both jobs; run the missed backup now."""
    if backup.due():
        try:
            await asyncio.to_thread(backup.create)
        except Exception:  # pragma: no cover - create() already reports its own failures
            logger.exception("maintenance: catch-up backup failed")
    if app.job_queue is not None:
        app.job_queue.run_daily(backup_job, time=clock_time(hour=BACKUP_HOUR), name="tisa_backup")
        if settings.log_chat_id and settings.daily_report_hour:
            app.job_queue.run_daily(
                report_job,
                time=clock_time(hour=settings.daily_report_hour),
                name="tisa_daily_report",
            )
    else:  # pragma: no cover - JobQueue is installed in every supported setup
        logger.warning("maintenance: JobQueue نصب نیست؛ پشتیبان/گزارش زمان‌بندی نشد")
    return 0


__all__ = ["BACKUP_HOUR", "backup_job", "report_job", "report_text", "start"]
