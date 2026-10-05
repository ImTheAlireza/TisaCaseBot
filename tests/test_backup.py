"""تست‌های سرویسِ پشتیبانِ روزانه و ماژولِ نگهداری.

پوشش:
* یک زیپ در روز (و نه بیشتر، مگر به force).
* چرخش بر اساس `backup_keep`.
* نوشتنِ اتمی با `os.replace` (فایلِ `.zip.tmp` نمی‌مانَد) و دسترسی‌های 0700/0600.
* کپیِ سالم از SQLite (می‌توان دوباره باز کرد) و صفِ outbox.
* `due()` برای جبرانِ استارت.
* گزارشِ روزانه بدونِ لاگ‌چت هم بالا نمی‌آید.
"""
from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
import time
import zipfile
from pathlib import Path
from unittest import mock

os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("SUDO_IDS", "1234567")

from bot.modules import maintenance
from bot.services import backup, metrics, outbox
from tests._flow_harness import patched_settings, settings_with


class _IsolatedBackup:
    """یک data_dir و backup_dir و DBهای موقت برای هر تست؛ هیچ‌چیز روی رپو نمی‌نویسد."""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="tisa-test-backup-"))
        self.data = self.root / "data"
        self.bk = self.root / "backups"
        self.data.mkdir()
        self.outbox_files = self.data / "outbox_files"
        self.outbox_files.mkdir()

    def seed_state(self, name: str = "roles.json", body: str = '{"admins":{}}') -> Path:
        p = self.data / name
        p.write_text(body, encoding="utf-8")
        return p

    def seed_sqlite(self, path: Path, table: str = "metrics") -> None:
        with sqlite3.connect(path) as db:
            db.execute(f"CREATE TABLE IF NOT EXISTS {table} (k TEXT PRIMARY KEY, v INTEGER)")
            db.execute(f"INSERT INTO {table} VALUES ('hit', 1)")
            db.commit()

    def attach(self) -> None:
        """نشاندن مسیرهای ماژول‌ها روی این فضای ایزوله."""
        backup.DATA_DIR = self.data
        backup.BACKUP_DIR = self.bk
        metrics.DB_PATH = self.data / "metrics.sqlite3"
        outbox.DB_PATH = self.data / "outbox.sqlite3"
        outbox.FILES_DIR = self.outbox_files

    def cleanup(self) -> None:
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# ---- سرویسِ بک‌آپ ---------------------------------------------------------


def test_creates_one_zip_per_day_and_skips_when_present() -> None:
    iso = _IsolatedBackup()
    iso.seed_state()
    iso.attach()
    iso.seed_sqlite(metrics.DB_PATH, "metrics")
    iso.seed_sqlite(outbox.DB_PATH, "outbox_jobs")
    (iso.outbox_files / "queued.jpg").write_bytes(b"binary")
    try:
        s = settings_with(backup_dir=iso.bk, backup_keep=7, daily_report_hour=0, log_chat_id=0)
        with patched_settings(s):
            now = time.mktime(time.strptime("2026-10-05 04:00:00", "%Y-%m-%d %H:%M:%S"))
            first = backup.create(now=now)
            assert first is not None and first.exists()
            assert first.name == "backup-20261005.zip"
            # بارِ دوم force=False → پس داده می‌شود و چیزی تازه نساخته
            assert backup.create(now=now) is None
            # force=True → زیپ دوباره ساخته می‌شود (همان نامِ امروز)
            forced = backup.create(now=now, force=True)
            assert forced == first and forced.exists()
    finally:
        iso.cleanup()


def test_zip_contents_and_permissions() -> None:
    iso = _IsolatedBackup()
    iso.seed_state("roles.json")
    iso.seed_state("publish_ledger.json", "[]")
    iso.seed_state("roles.json.bak", "{}")
    iso.attach()
    iso.seed_sqlite(metrics.DB_PATH, "metrics")
    iso.seed_sqlite(outbox.DB_PATH, "outbox_jobs")
    (iso.outbox_files / "a.jpg").write_bytes(b"AAA")
    (iso.outbox_files / "b.png").write_bytes(b"BBB")
    try:
        s = settings_with(backup_dir=iso.bk, backup_keep=7, daily_report_hour=0, log_chat_id=0)
        with patched_settings(s):
            zip_path = backup.create(now=time.time())
            assert zip_path is not None
            # دسترسی‌ها
            assert _mode(zip_path) == 0o600
            assert _mode(zip_path.parent) == 0o700
            # فایلِ موقت نمانده
            assert not list(iso.bk.glob("*.tmp"))
            # محتویات زیپ
            with zipfile.ZipFile(zip_path) as zf:
                names = set(zf.namelist())
                assert "roles.json" in names
                assert "publish_ledger.json" in names
                assert "roles.json.bak" in names
                assert "metrics.sqlite3" in names
                assert "outbox.sqlite3" in names
                assert "outbox_files/a.jpg" in names
                assert "outbox_files/b.png" in names
                # SQLite داخل زیپ سالم است
                tmp = iso.root / "_check.db"
                tmp.write_bytes(zf.read("metrics.sqlite3"))
                with sqlite3.connect(tmp) as db:
                    row = db.execute("SELECT v FROM metrics WHERE k='hit'").fetchone()
                    assert row == (1,)
                tmp.unlink()
    finally:
        iso.cleanup()


def test_rotate_keeps_newest_only() -> None:
    iso = _IsolatedBackup()
    iso.attach()
    iso.bk.mkdir(parents=True, exist_ok=True)
    # چهار فایل با زمان‌های متفاوت (خودمان می‌سازیم، نه create)
    names = ["backup-20261001.zip", "backup-20261002.zip",
             "backup-20261003.zip", "backup-20261004.zip"]
    for n in names:
        (iso.bk / n).write_bytes(b"x")
    try:
        removed = backup.rotate(keep=2)
        assert removed == 2
        remaining = sorted(p.name for p in iso.bk.glob("backup-*.zip"))
        assert remaining == ["backup-20261003.zip", "backup-20261004.zip"]
    finally:
        iso.cleanup()


def test_due_logic_and_latest() -> None:
    iso = _IsolatedBackup()
    iso.attach()
    iso.bk.mkdir(parents=True, exist_ok=True)
    try:
        assert backup.latest() is None
        assert backup.due() is True
        # فایلی با زمان قدیم (۲۵ ساعت پیش)
        old = iso.bk / "backup-20261001.zip"
        old.write_bytes(b"x")
        old_ts = time.time() - 25 * 3600
        os.utime(old, (old_ts, old_ts))
        assert backup.latest() == old
        # target امروز وجود ندارد؛ max_age=20 → باید due باشد
        assert backup.due(max_age_hours=20) is True
        # تازه‌ساز
        today = backup.target_for()
        today.write_bytes(b"x")
        assert backup.due() is False
    finally:
        iso.cleanup()


def test_missing_sqlite_is_skipped_not_fatal() -> None:
    """اگر metrics یا outbox هنوز DB نداشته باشند، create باید فقط JSONها را زیپ کند."""
    iso = _IsolatedBackup()
    iso.seed_state("roles.json")
    # هیچ sqlite ای نمی‌سازیم
    iso.attach()
    try:
        s = settings_with(backup_dir=iso.bk, backup_keep=7, daily_report_hour=0, log_chat_id=0)
        with patched_settings(s):
            z = backup.create(now=time.time())
            assert z is not None
            with zipfile.ZipFile(z) as zf:
                names = set(zf.namelist())
            assert "roles.json" in names
            assert "metrics.sqlite3" not in names
            assert "outbox.sqlite3" not in names
    finally:
        iso.cleanup()


# ---- گزارشِ روزانه --------------------------------------------------------


def test_report_text_builds_without_log_chat() -> None:
    iso = _IsolatedBackup()
    iso.seed_state()
    iso.attach()
    try:
        s = settings_with(backup_dir=iso.bk, backup_keep=7, daily_report_hour=9, log_chat_id=0)
        with patched_settings(s):
            txt = maintenance.report_text()
            assert "گزارش روزانه" in txt
            assert "آخرین پشتیبان" in txt
            # بدون بک‌آپ نباید ترکید
            assert "—" in txt
            # حالا یک بک‌آپ می‌سازیم؛ نامش باید در گزارش بیاید
            backup.create(now=time.time())
            txt2 = maintenance.report_text()
            assert "backup-" in txt2
    finally:
        iso.cleanup()


# ---- استارت (زمان‌بندی + جبران) -------------------------------------------


def test_start_runs_catchup_when_due_and_schedules_jobs() -> None:
    iso = _IsolatedBackup()
    iso.seed_state()
    iso.attach()
    try:
        s = settings_with(backup_dir=iso.bk, backup_keep=7, daily_report_hour=9, log_chat_id=0)

        class FakeJobQueue:
            def __init__(self) -> None:
                self.calls: list[tuple[str, int]] = []
            def run_daily(self, callback, time, name=None):
                self.calls.append((name, time.hour))

        class FakeApp:
            def __init__(self) -> None:
                self.job_queue = FakeJobQueue()
                self.bot = mock.Mock()

        with patched_settings(s):
            # قبل از start بک‌آپ نیست → due=True
            assert backup.due() is True
            app = FakeApp()
            # asyncio.runِ maintenance.start
            import asyncio
            asyncio.run(maintenance.start(app))
            # بک‌آپِ catch-up ساخته شده
            assert backup.latest() is not None
            # فقط بک‌آپ زمان‌بندی شده (log_chat_id=0 پس گزارش زمان‌بندی نشده)
            names = [c[0] for c in app.job_queue.calls]
            assert names == ["tisa_backup"]
            assert app.job_queue.calls[0][1] == maintenance.BACKUP_HOUR  # 4
    finally:
        iso.cleanup()


def test_start_schedules_report_when_log_chat_set() -> None:
    iso = _IsolatedBackup()
    iso.seed_state()
    iso.attach()
    # بک‌آپِ امروز را از قبل می‌سازیم که catch-up اجرا نشود
    try:
        s = settings_with(backup_dir=iso.bk, backup_keep=7, daily_report_hour=17, log_chat_id=-100123)

        class FakeJobQueue:
            def __init__(self) -> None:
                self.calls: list[tuple[str, int]] = []
            def run_daily(self, callback, time, name=None):
                self.calls.append((name, time.hour))

        class FakeApp:
            def __init__(self) -> None:
                self.job_queue = FakeJobQueue()
                self.bot = mock.Mock()

        with patched_settings(s):
            backup.create(now=time.time())  # امروز هست
            app = FakeApp()
            import asyncio
            asyncio.run(maintenance.start(app))
            assert {c[0] for c in app.job_queue.calls} == {"tisa_backup", "tisa_daily_report"}
            by_name = dict(app.job_queue.calls)
            assert by_name["tisa_daily_report"] == 17
    finally:
        iso.cleanup()


def test_daily_report_hour_zero_disables_report_but_keeps_backup() -> None:
    iso = _IsolatedBackup()
    iso.seed_state()
    iso.attach()
    try:
        s = settings_with(backup_dir=iso.bk, backup_keep=7, daily_report_hour=0, log_chat_id=-100123)

        class FakeJobQueue:
            def __init__(self) -> None:
                self.calls: list[str] = []
            def run_daily(self, callback, time, name=None):
                self.calls.append(name)

        class FakeApp:
            def __init__(self) -> None:
                self.job_queue = FakeJobQueue()
                self.bot = mock.Mock()

        with patched_settings(s):
            backup.create(now=time.time())
            app = FakeApp()
            import asyncio
            asyncio.run(maintenance.start(app))
            assert app.job_queue.calls == ["tisa_backup"]
    finally:
        iso.cleanup()
