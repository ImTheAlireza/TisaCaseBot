"""شمارنده‌های عملیاتی: «دیشب چیزی خراب شد؟» با یک نگاه جواب داده شود.

چرا SQLite و نه فایل JSON (مثل بقیهٔ state): این عدد‌ها از چند رشتهٔ مختلف
هندلرِ جریان، زهکشی صفِ خروجی و job زمان‌بندی‌شده نوشته می‌شوند و فقط برای
خوانده‌شدن هستند؛ JSON روی همین حجم، یعنی یک رقابتِ بازنویسی روی چند عدد.
 SQLite با `INSERT … ON CONFLICT` همان چند عدد را اتمیک اضافه می‌کند.

**هیچ‌چیز اینجا نباید منتشرکردن را بشکند.**نوشتنِ یک متریک اگر شکست، فقط یک لاگ
می‌شود (یک‌بار در هر پروسه، نه هر فراخوانی) — نبودِ عدد بهتر از جریانِ نیمه‌کاره است.

هر کلید در :data:`COUNTERS` تعریف می‌شود؛ صفحهٔ «📊 وضعیت» و `/export_metrics` هر دو
همان یک لیست را می‌خوانند، پس عددی که هیچ‌جا نوشته نمی‌شود هم در صفحه نمی‌آید.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
import contextlib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from bot.config import data_dir

logger = logging.getLogger(__name__)

DB_PATH = data_dir() / "metrics.sqlite3"

#: فقط یک‌بار دربارهٔ خرابی دیتابیس هشدار بده: اگر دیسک پر باشد، هر انتشار یک
#: هشدارِ مشابه می‌افزاید و لاگِ مفید زیر سر‌وصدا گم می‌شود.
_warned = False
_lock = threading.Lock()
_schema_lock = threading.Lock()
_initialized: dict[Path, tuple[int, int]] = {}

#: One connection per thread, kept open. Opening + configuring + closing a connection
#: for every single counter was 1.5–1.7 ms of *event-loop* time per number — cheap on a
#: fast disk, a real stall on a shared host, and even the status screen paid it. The key
#: is (path, file identity), so a replaced or swapped database (tests, a fresh install)
#: reconnects instead of writing into a deleted inode.
_local = threading.local()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS counters (
    key     TEXT PRIMARY KEY,
    n       INTEGER NOT NULL DEFAULT 0,
    total   REAL    NOT NULL DEFAULT 0,
    peak    REAL    NOT NULL DEFAULT 0,
    updated REAL    NOT NULL DEFAULT 0
);
"""


@dataclass(frozen=True)
class Counter:
    """یک شمارندهٔ ثبت‌شده، با همان چیزی که کاربر باید درباره‌اش بداند."""

    key: str
    label: str
    #: «count» یک تعداد است؛ «ms» زمان است (میانگین و بیشینه هم معنی دارد)
    kind: str = "count"
    #: چه‌کار باید کرد وقتی این عدد غیرعادی است — همان راهنمای صفحهٔ وضعیت
    hint: str = ""


# هر کلید اینجا یک نویسنده و یک خواننده دارد؛ بدونِ یکی، ردیفش حذف می‌شود.
COUNTERS: dict[str, Counter] = {
    c.key: c
    for c in (
        Counter("products_created", "محصول ساخته‌شده", hint="شمارش فقط وقتی است که فروشگاه product id داده."),
        Counter("variations_created", "واریژن ساخته‌شده"),
        Counter("images_uploaded", "تصویر آپلودشده"),
        Counter("restocks_applied", "اپدیت اعمال‌شده"),
        Counter("publish_queued", "به صفِ تلاشِ دوباره رفته", hint="یعنی یک خطای موقتِ شبکه/سایت؛ در 📤 صف ببین."),
        Counter(
            "publish_failed",
            "پس از همهٔ تلاش‌ها ناموفق",
            hint="این‌ها منتشر نشده‌اند؛ فایل product.zip را دستی وارد کن یا علت را در لاگ ببین.",
        ),
        Counter("sku_collisions", "تعارض SKU", hint="SKU تکراری باعث شماره‌گذاری دوباره می‌شود؛ اگر زیاد است، پیشوند را عوض کن."),
        Counter("ai_calls", "فراخوانی هوش مصنوعی"),
        Counter("ai_failures", "شکست هوش مصنوعی", hint="با شکست، پارسر داخلی جایش را می‌گیرد؛ متن‌های پیچیده را دستی چک کن."),
        Counter("ai_latency_ms", "زمان پاسخ هوش مصنوعی", kind="ms", hint="اگر بیشینه نزدیک AI_TIMEOUT_SECONDS است، همان‌جا تایم‌اوت می‌خوری."),
        Counter("extract_fallback_used", "بازگشت به پارسر داخلی", hint="هر بار یعنی هوش مصنوعی جواب نداد."),
        Counter("flow_abandoned", "جریان رهاشده", hint="بسته‌شدن با تایم‌اوت/لغو؛ اگر زیاد است جریان بیش از حد طول می‌کشد یا سؤال‌ها گیج‌کننده‌اند."),
        Counter("buttons_denied", "دکمه بدون دسترسی", hint="کسی دکمه‌ای را زده که حقش نبوده (یا منوی کهنه داشته)."),
        Counter("tracking_converted", "فایل ردیابی تبدیل‌شده"),
        Counter("tracking_review_rows", "ردیفِ نیازمندِ بازبینی (فایل‌های ردیابی)"),
        Counter("compress_batches", "دستهٔ عکس‌های فشرده‌شده"),
        Counter("compress_images", "عکس فشرده و ارسال‌شده"),
        Counter("compress_download_ms", "دریافت عکس", kind="ms"),
        Counter("compress_cpu_ms", "فشرده‌سازی عکس", kind="ms"),
        Counter("compress_upload_ms", "ارسال عکس", kind="ms"),
    )
}

#: نسبت‌ها ذخیره نمی‌شوند؛ از شمارنده‌های خودشان حساب می‌شوند (یک کسرِ انبارشده،
#: بعد از چند روز معنی‌اش را از دست می‌دهد).
RATIOS: dict[str, tuple[str, str, str]] = {
    "ai_failure_rate": ("ai_failures", "ai_calls", "نرخ شکست هوش مصنوعی"),
}


def _initialize(connection: sqlite3.Connection) -> None:
    """Configure a newly created/replaced database file once."""
    connection.execute("PRAGMA journal_mode=WAL")
    # Numbers, not money: a power cut losing the last few counters is acceptable, and
    # FULL would fsync on every increment — the very cost this module used to pay.
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.executescript(_SCHEMA)


def _connect() -> sqlite3.Connection:
    path = Path(DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5.0)
    try:
        stat = path.stat()
        identity = (stat.st_dev, stat.st_ino)
        # WAL mode and the schema are properties of this database file, not each
        # individual counter write. Running both on every metric introduced disk
        # work and lock contention into otherwise tiny event-loop callbacks.
        with _schema_lock:
            if _initialized.get(path) != identity or stat.st_size == 0:
                _initialize(connection)
                stat = path.stat()
                _initialized[path] = (stat.st_dev, stat.st_ino)
    except Exception:
        connection.close()
        raise
    return connection


def _key() -> tuple[str, tuple[int, int] | None]:
    """(path, identity) of the database file — ``None`` while it does not exist yet."""
    path = Path(DB_PATH)
    try:
        stat = path.stat()
        return str(path), (stat.st_dev, stat.st_ino)
    except OSError:
        return str(path), None


def _drop() -> None:
    """Forget this thread's connection (broken handle, or replaced database file)."""
    connection = getattr(_local, "connection", None)
    if connection is not None:
        with contextlib.suppress(sqlite3.Error):
            connection.close()
    _local.connection = None
    _local.key = None


def _connection() -> sqlite3.Connection:
    """This thread's long-lived connection, opened or replaced as needed."""
    key = _key()
    connection = getattr(_local, "connection", None)
    if connection is not None and getattr(_local, "key", None) != key:
        _drop()
        connection = None
    if connection is None:
        connection = _connect()
        _local.connection = connection
        # Read the key *after* connecting: the first call creates the file, and a key
        # captured before that could never match the next call's.
        _local.key = _key()
    return connection


def close() -> None:
    """Close this thread's connection (shutdown, or a test swapping ``DB_PATH``)."""
    _drop()


@contextmanager
def _db():
    """The kept connection: commit on success, forget it on failure."""
    connection = _connection()
    try:
        yield connection
        connection.commit()
    except Exception:
        _drop()
        raise


def _fail_once(exc: Exception) -> None:
    global _warned
    if not _warned:
        _warned = True
        logger.warning("metrics disabled for this process: %s (%s)", type(exc).__name__, exc)


def _write(key: str, *, by: int, value: float) -> None:
    """Add to a counter; never raises (see the module docstring for why)."""
    if key not in COUNTERS:  # pragma: no cover - a typo must not reach the disk
        logger.debug("metrics: unknown counter %r ignored", key)
        return
    try:
        with _lock, _db() as db:
            db.execute(
                """
                INSERT INTO counters(key, n, total, peak, updated) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    n = n + excluded.n,
                    total = total + excluded.total,
                    peak = MAX(peak, excluded.peak),
                    updated = excluded.updated
                """,
                (key, by, value, value, time.time()),
            )
    except (sqlite3.Error, OSError) as exc:
        _drop()
        _fail_once(exc)


def incr(key: str, by: int = 1) -> None:
    """Count `by` occurrences of something that happened."""
    _write(key, by=by, value=float(by))


def note_abandoned(where: str) -> None:
    """A flow closed on its own after idling.

    Only timeouts count: «/cancel» is a decision, not a sign that the screen confused
    someone, and mixing the two makes the number useless.
    """
    incr("flow_abandoned")
    logger.info("flow abandoned while idle: %s", where)


def note_denial(where: str) -> None:
    """A button was pressed by someone the role check turned away.

    The number alone is not actionable — «denied: 40» says nothing until you know which
    screen keeps offering a button too far, so the name of the place is logged with it.
    """
    incr("buttons_denied")
    logger.info("button denied at %s", where)


def incr_shop(key: str, by: int = 1) -> None:
    """Count something the *shop* did, and count nothing while ``TISA_DRY_RUN`` is on.

    A rehearsal goes through the same code with a fake transport, so without this the
    night's numbers would quietly include every dry run — and the point of the counters
    is to be able to say «دیشب ۳ محصول واقعاً ساخته شد».
    """
    from bot.config import settings

    if settings.woo_dry_run:
        return
    incr(key, by=by)


def observe_shop(key: str, value: float) -> None:
    """`observe` for a shop-side measurement (see :func:`incr_shop` for why)."""
    from bot.config import settings

    if settings.woo_dry_run:
        return
    observe(key, value)


def observe(key: str, value: float) -> None:
    """Record one measurement (latency): count, sum and peak are kept."""
    _write(key, by=1, value=max(0.0, float(value)))


def elapsed(started: float) -> float:
    """Milliseconds since `time.perf_counter()` — so callers do not repeat the maths."""
    return (time.perf_counter() - started) * 1000.0


def snapshot() -> dict[str, tuple[int, float, float, float]]:
    """``key → (count, total, peak, updated)``; empty when there is nothing to show."""
    if not DB_PATH.exists():
        return {}
    try:
        with _db() as db:
            rows = db.execute("SELECT key, n, total, peak, updated FROM counters").fetchall()
    except (sqlite3.Error, OSError) as exc:
        _fail_once(exc)
        return {}
    return {str(row[0]): (int(row[1]), float(row[2]), float(row[3]), float(row[4])) for row in rows}


def rate(numerator: str, denominator: str) -> float | None:
    """`numerator/denominator` from the stored counts, or ``None`` with no data.

    ``None`` is not ``0.0``: «هیچ فراخوانی‌ای نشده» with «همه سالم بوده» را نباید
    یکی دانست — صفحهٔ وضعیت همین تفاوت را نشان می‌دهد.
    """
    seen = snapshot()
    den = seen.get(denominator, (0, 0.0, 0.0, 0.0))[0]
    if not den:
        return None
    return seen.get(numerator, (0, 0.0, 0.0, 0.0))[0] / den


def _line(counter: Counter, row: tuple[int, float, float, float]) -> str:
    n, total, peak, _updated = row
    if counter.kind == "ms":
        return f"⏱ {counter.label}: {total / n:.0f} ms میانگین · {peak:.0f} ms بیشینه ({n:.0f} بار)"
    return f"• {counter.label}: {int(n):,}"


def status_lines() -> list[str]:
    """What «📊 وضعیت» shows: only counters that have actually fired."""
    seen = snapshot()
    lines = [_line(counter, seen[key]) for key, counter in COUNTERS.items() if key in seen]
    for _key, (numerator, denominator, label) in RATIOS.items():
        value = rate(numerator, denominator)
        if value is not None:
            lines.append(f"📉 {label}: {value * 100:.1f}٪")
    return lines


def export_csv() -> str:
    """/export_metrics: the raw numbers, header first — Excel-safe, no Persian prose."""
    seen = snapshot()
    lines = ["key,counter,n,total,peak,updated_iso"]
    for key, counter in COUNTERS.items():
        n, total, peak, updated = seen.get(key, (0, 0.0, 0.0, 0.0))
        when = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(updated)) if updated else ""
        lines.append(f'{key},"{counter.label}",{n},{total:g},{peak:g},{when}')
    for name, (numerator, denominator, label) in RATIOS.items():
        value = rate(numerator, denominator)
        lines.append(f'{name},"{label}",,,"{"" if value is None else f"{value:.4f}"}",')
    return "\n".join(lines) + "\n"


def probe() -> tuple[int, str]:
    """(rows on disk, problem) for ``--check-config``.

    A metrics database that cannot be written costs only the numbers — no flow stops —
    but the operator should learn that at boot, not when a screen stays empty.
    """
    if not DB_PATH.exists():
        return 0, ""
    try:
        with _db() as db:
            rows = db.execute("SELECT COUNT(*) FROM counters").fetchone()[0]
    except (sqlite3.Error, OSError) as exc:
        return 0, f"دیتابیس متریک‌ها باز نشد: {type(exc).__name__}: {exc}"
    return int(rows), ""


def path() -> Path:
    return DB_PATH


__all__ = [
    "COUNTERS",
    "DB_PATH",
    "RATIOS",
    "close",
    "elapsed",
    "export_csv",
    "incr",
    "incr_shop",
    "note_abandoned",
    "note_denial",
    "observe",
    "observe_shop",
    "path",
    "probe",
    "rate",
    "snapshot",
    "status_lines",
]
