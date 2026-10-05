"""تست‌های ماژول زمان (timeutil) — منطقه زمانیِ ثابت، parse امن، و JobQueue ساعتِ تزی.

تنها راهِ مطمئن برای اینکه گزارش ساعت ۹ به‌وقت ایران واقعاً ۹ صبح ایران می‌رسد
(و نه ۱۲:۳۰ ظهر روی هاست UTC) این است که ``datetime.time``ای که به JobQueue داده
می‌شود tzinfo داشته باشد.
"""
from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo

os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("SUDO_IDS", "1234567")

from bot.modules import maintenance
from bot.utils import timeutil
from tests._flow_harness import patched_settings, settings_with


def test_default_tz_is_asia_tehran() -> None:
    tz = timeutil._tz()
    assert tz.key == "Asia/Tehran"


def test_strftime_uses_configured_tz_not_server_localtime() -> None:
    """مُهر ساعت ۶ صبح UTC = ۹:۳۰ یا ۱۰:۳۰ ایران بسته به daylight (ولی قطعاً UTC نیست)."""
    # 2026-10-05 06:00 UTC یعنی ۰۹:۰۰ یا ۱۰:۰۰ ایران. تست می‌کند که برچسب
    # ساعتی که strftime می‌دهد در بازهٔ ایران است (نه ۰۶:۰۰ UTC).
    utc_6am = datetime(2026, 10, 5, 6, 0, tzinfo=ZoneInfo("UTC")).timestamp()
    stamp = timeutil.strftime("%H:%M", utc_6am)
    assert stamp != "06:00", "strftime باید از TISA_TZ بدهد نه UTC سرور"
    # Iran standard/daylight yields 09:30 or 10:30 — either is fine; the hour must be 9 or 10.
    hour = int(stamp.split(":")[0])
    assert hour in (9, 10), f"ساعت انتظار ۹ یا ۱۰، {stamp} داده شد"


def test_invalid_tz_falls_back_to_default() -> None:
    s = settings_with(timezone="Not/A_Real_Zone")
    with patched_settings(s):
        # cache را برای این تست پاک می‌کنیم
        timeutil._cache.clear()
        # با env غلط باید به پیش‌فرض برگردد
        os.environ["TISA_TZ"] = "Not/A_Real_Zone"
        try:
            tz = timeutil._tz()
            assert tz.key == "Asia/Tehran"
        finally:
            os.environ.pop("TISA_TZ", None)
            timeutil._cache.clear()


def test_clock_time_at_has_tzinfo() -> None:
    t = timeutil.clock_time_at(hour=9)
    assert t.hour == 9
    assert t.tzinfo is not None
    assert t.tzinfo.key == "Asia/Tehran"


def test_maintenance_run_daily_uses_tz_aware_time() -> None:
    """هر دو job که run_daily می‌گیرند باید tzinfo داشته باشند تا PTB اشتباه زمان‌بندی نکند."""
    s = settings_with(
        timezone="Asia/Tehran",
        backup_dir="/tmp/tisa-bk-test",
        backup_keep=7,
        daily_report_hour=9,
        log_chat_id=-100123,
    )

    class FakeJobQueue:
        def __init__(self) -> None:
            self.calls = []
        def run_daily(self, callback, *, time, name=None):
            self.calls.append((name, time))

    class FakeApp:
        def __init__(self) -> None:
            self.job_queue = FakeJobQueue()
            self.bot = None

    import asyncio
    # باحال‌سازی این است که due() چک نکند (پوشه موجود نیست، latest None است
    # و due=True می‌شود → catch-up اجرا می‌شود اما backup.create خودش خطا را
    # می‌بلعد چون private_dir ممکن است بسازد). ما فقط چک می‌کنیم run_daily‌ها
    # tzinfo دارند.
    with patched_settings(s):
        timeutil._cache.clear()
        app = FakeApp()
        # جلوی create واقعی را می‌گیریم (پوشهٔ /tmp/tisa-bk-test ممکن است موجود نباشد
        # یا با دست دیگر ساخته شده باشد و ما به نتیجه‌اش نیازی نداریم).
        with __import__("unittest").mock.patch("bot.services.backup.create", return_value=None):
            asyncio.run(maintenance.start(app))
    assert len(app.job_queue.calls) == 2
    for name, t in app.job_queue.calls:
        assert t.tzinfo is not None, f"job {name} بدون tzinfo زمان‌بندی شده"
        assert t.tzinfo.key == "Asia/Tehran"
