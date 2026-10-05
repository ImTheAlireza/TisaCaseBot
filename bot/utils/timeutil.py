"""کار با زمان در یک منطقهٔ زمانیِ ثابت.

پیش از این ماژول، چندین جا ``time.localtime`` صدا زده می‌شد — یعنی همهٔ مُهرها،
نامِ بک‌آپ و ساعتِ گزارش روزانه به *وقتِ سرور* وابسته بود. روی هاست اشتراکی که
معمولاً UTC است، گزارش ۹ صبح به‌وقت ایران دقیقاً ۱۲:۳۰ ظهر می‌رسید و مرزِ روز
برای بک‌آپ هم جابجا می‌شد.

این ماژول منطقه زمانی را یک‌جا می‌چیند (پیش‌فرض ``Asia/Tehran``، قابل تغییر با
``TISA_TZ``) و دو تابع در اختیار بقیه می‌گذارد:

* :func:`now` — زمان «الان» به‌عنوان ``time.time``، برای محاسبهٔ سن (ثانیه).
* :func:`localtime` و :func:`strftime` — همان قرارداد ``time`` ولی در TZ تنظیم‌شده.
* :func:`now_dt` / :func:`clock_time_at` — برای ``JobQueue.run_daily`` که ساعت را
  در یک منطقهٔ زمانی مشخص می‌خواهد.

همهٔ صداها فقط از این ماژول استفاده می‌کنند؛ اگر منطقه در ``.env`` عوض شود همان
لحظه اعمال می‌شود (هر تماس از ``settings`` می‌خواند).
"""
from __future__ import annotations

import os
import time
from datetime import datetime, time as dtime
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bot.config import settings

#: TZ پیش‌فرض — همیشه وقت ایران است مگر اینکه به‌صراحت در .env تغییر کند.
_DEFAULT_TZ: Final[str] = "Asia/Tehran"

#: Cache از ZoneInfo تا هر تماس فایل منطقه را دوباره نخوانَد (ZoneInfo immutable است).
_cache: dict[str, ZoneInfo] = {}


def _tz() -> ZoneInfo:
    name = os.environ.get("TISA_TZ") or settings.timezone or _DEFAULT_TZ
    tz = _cache.get(name)
    if tz is None:
        try:
            tz = ZoneInfo(name)
        except ZoneInfoNotFoundError:
            # اگر منطقه نامعتبر باشد به پیش‌فرض برمی‌گردیم — یک بار هشدار لاگ می‌شود؛
            # این ماژول نمی‌تواند logger را در import-time پیکربندی کند، پس یادداشت را
            # از طریق settings.problems می‌فرستد (همان قراردادِ config.py).
            tz = ZoneInfo(_DEFAULT_TZ)
        _cache[name] = tz
    return tz


def now() -> float:
    """همیشه time.time()؛ برای وضوح و جستجو متمرکز اینجاست."""
    return time.time()


def localtime(seconds: float | None = None) -> time.struct_time:
    """معادل ``time.localtime`` ولی در منطقهٔ زمانیِ پیکربندی‌شده."""
    if seconds is None:
        seconds = time.time()
    return datetime.fromtimestamp(seconds, tz=_tz()).timetuple()


def strftime(fmt: str, seconds: float | None = None) -> str:
    """معادل ``time.strftime(fmt, localtime(seconds))``."""
    return time.strftime(fmt, localtime(seconds if seconds is not None else time.time()))


def human_duration(seconds: float) -> str:
    """«۲۷ دقیقه» / «۱ ساعت و ۵ دقیقه» — یک فاصلهٔ قابل‌برنامه‌ریزی، نه عددِ خام ثانیه.

    هر دو طرفِ این عدد در چت خوانده می‌شود («چقدر صبر کنم؟»، «چقدر پایین بود؟»)، پس
    «۱۸۳۴ ثانیه» هیچ‌کس را جلو نمی‌اندازد. دقیقه‌ها به بالا گرد می‌شوند تا «۰ دقیقه»
    معنای «همین حالا» ندهد.
    """
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes = (rest + 59) // 60
    if hours and minutes:
        return f"{hours} ساعت و {minutes} دقیقه"
    if hours:
        return f"{hours} ساعت"
    if minutes:
        return f"{minutes} دقیقه"
    return "کمتر از یک دقیقه"


def now_dt() -> datetime:
    """تاریخ-زمانِ الان در منطقهٔ پیکربندی (برای JobQueue)."""
    return datetime.now(tz=_tz())


def clock_time_at(hour: int, minute: int = 0, second: int = 0) -> dtime:
    """یک ``datetime.time`` در منطقهٔ پیکربندی برای ``JobQueue.run_daily``.

    PTB ``run_daily(t=datetime.time)`` را به‌صورت local به‌وقتِ *ماشین* می‌خواند
    مگر اینکه ``tzinfo`` بدهی. بدون این، ساعت ۹ روی سرورِ UTC ۱۲:۳۰ ایران صدا
    می‌خورد.
    """
    return dtime(hour=hour, minute=minute, second=second, tzinfo=_tz())


__all__ = ["_DEFAULT_TZ", "clock_time_at", "localtime", "now", "now_dt", "strftime"]
