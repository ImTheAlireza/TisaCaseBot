"""نسخهٔ پشتیبانِ روزانه از وضعیتِ ربات — بدون cron و بدون وابستگی تازه.

**چه چیزی:** فایل‌های JSONِ وضعیت (`roles.json`، دفترِ انتشار، حافظهٔ یادگیری…)،
پایگاهِ صف و پایگاهِ متریک‌ها (با API رسمیِ `sqlite3.backup`، پس کپیِ نصفه ممکن نیست)
و فایل‌های درانتظارِ آپلود. همه در یک zipِ تاریخ‌دار زیر `<TISA_DATA_DIR>/backups`.

**قراردادها (چرا این‌طور):**

* **اتمی**: zip در فایلِ موقت ساخته می‌شود و بعد `os.replace` می‌شود؛ پشتیبانِ نصفه
  هرگز «پشتیبان» شمرده نمی‌شود.
* **روزی یکی**: اگر zipِ امروز هست، دوباره ساخته نمی‌شود؛ ری‌استارت‌های پیاپی یک
  کپیِ تازه نمی‌سازند.
* **چرخشِ محدود**: فقط `TISA_BACKUP_KEEP` تای تازه می‌ماند و فقط فایل‌هایی پاک می‌شوند
  که خودِ این ماژول ساخته است (`backup-YYYYMMDD.zip`).
* **خصوصی**: پوشهٔ پشتیبان `0700` و خودِ zip `0600` — این‌ها دادهٔ فروشگاه‌اند.

هیچ‌کدام از این‌ها خطای مرگبار نمی‌دهد: پشتیبان‌گیری که خودش ربات را بخواباند،
از نداشتنِ پشتیبان بدتر است. هر خرابی لاگ می‌شود و `None` برمی‌گردد.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
import zipfile
from pathlib import Path

from bot.config import data_dir, settings
from bot.services import metrics, outbox
from bot.services.fsutils import fsync_dir, private_dir, private_file

logger = logging.getLogger(__name__)

DATA_DIR = data_dir()
BACKUP_DIR = settings.backup_dir
#: نامِ فایل‌هایی که خودمان می‌سازیم؛ چرخش فقط به همین‌ها دست می‌زند.
PREFIX = "backup-"


def _stamp(now: float | None = None) -> str:
    return time.strftime("%Y%m%d", time.localtime(time.time() if now is None else now))


def directory() -> Path:
    """The backup directory (resolved at call time, so tests can repoint it)."""
    return Path(BACKUP_DIR)


def target_for(now: float | None = None) -> Path:
    return directory() / f"{PREFIX}{_stamp(now)}.zip"


def latest() -> Path | None:
    found = sorted(directory().glob(f"{PREFIX}*.zip")) if directory().is_dir() else []
    return found[-1] if found else None


def due(now: float | None = None, *, max_age_hours: float = 20.0) -> bool:
    """Is today's backup missing, or is the newest one already old?

    The age check matters on hosts that restart often: ``run_daily`` alone would
    never fire if the process never lives past 03:00, so startup does a catch-up.
    """
    if target_for(now).exists():
        return False
    newest = latest()
    if newest is None:
        return True
    age_hours = (time.time() - newest.stat().st_mtime) / 3600
    return age_hours >= max_age_hours


def _state_files() -> list[Path]:
    """JSON state plus backups-of-JSON — everything small and irreplaceable."""
    files: list[Path] = []
    for pattern in ("*.json", "*.json.bak"):
        files.extend(sorted(DATA_DIR.glob(pattern)))
    return [path for path in files if path.is_file()]


def _sqlite_copy(source: Path, target: Path) -> bool:
    """A consistent copy of a live SQLite database (never a torn file copy)."""
    try:
        with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src, sqlite3.connect(target) as dst:
            src.backup(dst)
        return True
    except sqlite3.Error:
        logger.warning("backup: could not copy %s; skipping it", source, exc_info=True)
        return False


def _queued_files() -> list[Path]:
    root = Path(outbox.FILES_DIR)
    if not root.is_dir():
        return []
    return [path for path in sorted(root.rglob("*")) if path.is_file()]


def create(*, now: float | None = None, force: bool = False) -> Path | None:
    """Write today's zip (unless it exists) and rotate. Returns the path or ``None``."""
    directory_ = directory()
    target = target_for(now)
    if target.exists() and not force:
        return None
    temporary = target.with_suffix(".zip.tmp")
    databases: list[tuple[Path, str]] = []
    try:
        private_dir(directory_)
        for source, name in ((Path(metrics.path()), "metrics.sqlite3"),
                             (Path(outbox.DB_PATH), "outbox.sqlite3")):
            if not source.is_file():
                continue
            copy = directory_ / f".tmp-{name}"
            if _sqlite_copy(source, copy):
                databases.append((copy, name))
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in _state_files():
                archive.write(path, arcname=path.name)
            for path in _queued_files():
                archive.write(path, arcname=f"outbox_files/{path.name}")
            for copy, name in databases:
                archive.write(copy, arcname=name)
        os.replace(temporary, target)
        private_file(target)
        fsync_dir(directory_)
    except (OSError, ValueError, zipfile.BadZipFile):
        logger.exception("backup: could not create %s", target)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - cleanup only
            pass
        return None
    finally:
        for copy, _name in databases:
            try:
                copy.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - cleanup only
                pass
    removed = rotate(settings.backup_keep)
    logger.info("backup: %s ساخته شد (%.1f KB)%s", target.name, target.stat().st_size / 1024,
                f"؛ {removed} نسخهٔ قدیمی پاک شد" if removed else "")
    return target


def rotate(keep: int) -> int:
    """Keep the newest ``keep`` zips; delete older ones we made. Returns how many went."""
    directory_ = directory()
    if not directory_.is_dir():
        return 0
    keep = max(1, int(keep))
    found = sorted(directory_.glob(f"{PREFIX}*.zip"))
    removed = 0
    for path in found[:-keep]:
        try:
            path.unlink()
            removed += 1
        except OSError:
            logger.warning("backup: could not remove the old %s", path, exc_info=True)
    return removed


__all__ = ["BACKUP_DIR", "DATA_DIR", "PREFIX", "create", "directory", "due", "latest", "rotate", "target_for"]
