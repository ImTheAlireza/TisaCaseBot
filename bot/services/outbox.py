"""A durable queue for publishes the shop refused *because it was busy* (plan 4.8).

Why a queue exists at all: one publish is several requests (media → product → variations).
When the store answers 429 or 5xx, the retry policy inside :mod:`bot.services.woo_client`
has already spent its three tries in about fifteen seconds, and the seller's only remaining
option was «دوباره بزن» — by hand, minutes later, for every product of a bulk day. The
outbox turns that into something the bot keeps on its own: the attempt is stored with its
images and its batch id, and retried on a schedule until it succeeds or gives up.

Two boundaries keep it honest:

* **not a second publish path.** Enqueue happens only on a *transient* failure, and the drain
  calls the same :func:`bot.services.woocommerce_direct.create_draft` with the same batch id —
  so a queued retry that lands twice still cannot create two products (see
  :mod:`bot.services.publish_batch`);
* **not a state store for the conversation.** Images are *copied* into ``data/outbox_files/``
  when the item is queued, because the session workspace is deleted the moment the flow ends;
  a queue that points at deleted files is a queue that fails at 3 a.m.

Giving up is a recorded outcome, not a deleted row: the item stays with ``status='dropped'``
and the last error, and the publish card in the history is finished as a failure.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import threading
import time

import httpx
from contextlib import contextmanager
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bot.config import data_dir

logger = logging.getLogger(__name__)

STATUS_PENDING = "pending"
STATUS_DROPPED = "dropped"

#: Statuses that mean «the shop could not answer right now» — the only ones worth a queue.
#: 500 is in here even though :mod:`bot.services.woo_client` refuses to retry it in the same
#: request (a PHP fatal usually repeats immediately): minutes later, after the object cache has
#: cooled, it often just works. A 400 is a wrong value and never becomes right by waiting.
TRANSIENT_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})


def is_transient(exc: BaseException) -> bool:
    """Could a later attempt plausibly succeed? This is the gate in front of the queue."""
    from bot.services.woo_client import WooCommerceAPIError      # no cycle: it imports nothing here

    if isinstance(exc, WooCommerceAPIError):
        return int(getattr(exc, "status_code", 0) or 0) in TRANSIENT_STATUS_CODES
    # httpx's transport failures (ConnectError, ReadTimeout, RemoteProtocolError) are the usual
    # shape of «the shop is unreachable», and they do *not* subclass ConnectionError — naming
    # them is not decoration. The request either never landed or its answer was lost, and the
    # content-addressed batch id is what makes trying again safe.
    return isinstance(exc, (TimeoutError, ConnectionError, OSError, httpx.TransportError))


#: How many times the queue knocks before it stops. Eight, because the waits grow:
#: 1m, 2m, 4m, 8m, 16m, 32m, 64m ≈ two hours of trying, which is the window in which a
#: WooCommerce maintenance break is still "coming back".
MAX_ATTEMPTS = 8
#: Retries the queue still owes after the flow's own failed attempt. ``MAX_ATTEMPTS`` counts
#: the whole story — the attempt that just failed is number one — and the UI says «X بار دیگر»
#: from this constant, so a promise on screen and the counter in the database cannot drift.
REMAINING_TRIES_AFTER_FIRST = MAX_ATTEMPTS - 1
#: …and an absolute cut-off, so an item enqueued right after a reboot is not retried for a week.
MAX_AGE_SECONDS = 24 * 3600
BACKOFF_BASE_SECONDS = 60
MAX_DELAY_SECONDS = 3 * 60 * 60
#: One drain must not hog the event loop (or the host's rate limit) after a long outage.
DRAIN_LIMIT = 5

DATA_DIR = data_dir()
DB_PATH = DATA_DIR / "outbox.sqlite3"
FILES_DIR = DATA_DIR / "outbox_files"
_schema_lock = threading.Lock()
_initialized: dict[Path, tuple[int, int]] = {}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    batch_id    TEXT PRIMARY KEY,
    chat_id     INTEGER NOT NULL,
    thread_id   INTEGER,
    user_id     TEXT NOT NULL,
    mode        TEXT NOT NULL DEFAULT 'new',
    payload     TEXT NOT NULL,
    images      TEXT NOT NULL DEFAULT '[]',
    attempts    INTEGER NOT NULL DEFAULT 0,
    next_at     REAL NOT NULL,
    last_error  TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS outbox_due ON outbox (status, next_at);
"""


@dataclass(frozen=True)
class QueuedPublish:
    """One publish waiting for the shop to be well again."""

    batch_id: str
    chat_id: int
    user_id: str
    payload: dict[str, Any]
    images: list[Path]
    attempts: int
    next_at: float
    last_error: str = ""
    status: str = STATUS_PENDING
    created_at: float = 0.0
    thread_id: int | None = None
    mode: str = "new"
    #: Images the spool lost (someone cleaned ``data/``, a moved host). The drain must say so
    #: instead of publishing a product whose pictures quietly vanished.
    missing_images: list[Path] = field(default_factory=list)

    @property
    def ledger_key(self) -> str:
        return str(self.payload.get("ledger_key") or "")


def backoff_seconds(attempts: int) -> int:
    """1m, 2m, 4m … capped — the queue must not become a denial of service on the shop."""
    return min(BACKOFF_BASE_SECONDS * (2 ** max(0, attempts - 1)), MAX_DELAY_SECONDS)


def _initialize(conn: sqlite3.Connection) -> None:
    """Set file-level journal mode and create the queue schema."""
    try:                                    # WAL needs a real fs; a plain journal is fine too
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:                   # pragma: no cover - host dependent
        logger.warning("outbox: WAL is not available here; using the default journal")
    conn.executescript(_SCHEMA)


def _connect() -> sqlite3.Connection:
    path = Path(DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        stat = path.stat()
        identity = (stat.st_dev, stat.st_ino)
        # WAL mode and DDL belong to the database file, not each enqueue/due call.
        # Re-running them on the hot path needlessly acquires SQLite's schema locks.
        with _schema_lock:
            if _initialized.get(path) != identity or stat.st_size == 0:
                _initialize(conn)
                stat = path.stat()
                _initialized[path] = (stat.st_dev, stat.st_ino)
    except Exception:
        conn.close()
        raise
    return conn


@contextmanager
def _db():
    """A connection that is *closed*: sqlite3's ``with`` only commits, and the queue is
    opened once per drain attempt — a leak per retry would outlive the retry."""
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _row_to_entry(row: sqlite3.Row) -> QueuedPublish:
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        stored = json.loads(row["images"] or "[]")
    except (TypeError, ValueError):
        stored = []
    wanted = [Path(str(item)) for item in stored if str(item).strip()]
    present = [path for path in wanted if path.is_file()]
    return QueuedPublish(
        batch_id=str(row["batch_id"]),
        chat_id=int(row["chat_id"] or 0),
        thread_id=(int(row["thread_id"]) if row["thread_id"] not in (None, "") else None),
        user_id=str(row["user_id"]),
        mode=str(row["mode"] or "new"),
        payload=payload,
        images=present,
        missing_images=[path for path in wanted if path not in present],
        attempts=int(row["attempts"] or 0),
        next_at=float(row["next_at"] or 0.0),
        last_error=str(row["last_error"] or ""),
        status=str(row["status"] or STATUS_PENDING),
        created_at=float(row["created_at"] or 0.0),
    )


def spool_images(batch_id: str, paths: Sequence[Path]) -> list[Path]:
    """Copy the images into the queue directory and return *those* paths.

    The session workspace is deleted when the flow ends, so the queue owns its own bytes —
    and a retry that publishes half a product because a temp file vanished is worse than one
    that never started.
    """
    if not paths:
        return []
    target = FILES_DIR / batch_id
    target.mkdir(parents=True, exist_ok=True)
    kept: list[Path] = []
    for index, path in enumerate(paths, 1):
        try:
            if not path.is_file():
                continue
            # Re-numbered on purpose: the workspace order is the gallery order, and the
            # per-colour images keep their name inside the number («01_مشکی.jpg»).
            shutil.copy2(path, target / f"{index:02d}_{path.name}")
            kept.append(target / f"{index:02d}_{path.name}")
        except OSError as exc:
            logger.warning("outbox: تصویر %s در صف کپی نشد: %s", path, exc)
    return kept


def enqueue(
    *,
    batch_id: str,
    chat_id: int,
    user_id: int | str,
    payload: dict[str, Any],
    images: Sequence[Path] = (),
    thread_id: int | None = None,
    mode: str = "new",
    error: str = "",
    delay: float = BACKOFF_BASE_SECONDS,
    now: float | None = None,
) -> bool:
    """Store (or refresh) the queued attempt for this content. False = nothing was stored.

    ``batch_id`` is unique: queueing the same product twice *updates* the one row instead of
    adding a second one, which is the whole point of a content-addressed id.
    """
    moment = time.time() if now is None else now
    try:
        spooled = spool_images(batch_id, images)
        with _db() as conn:
            conn.execute(
                """
                INSERT INTO outbox (batch_id, chat_id, thread_id, user_id, mode, payload, images,
                                    attempts, next_at, last_error, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                ON CONFLICT(batch_id) DO UPDATE SET
                    payload    = excluded.payload,
                    images     = excluded.images,
                    next_at    = MAX(outbox.next_at, excluded.next_at),
                    last_error = excluded.last_error,
                    status     = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (
                    batch_id, int(chat_id or 0), thread_id, str(user_id), mode,
                    json.dumps(payload, ensure_ascii=False),
                    json.dumps([str(path) for path in spooled], ensure_ascii=False),
                    moment + max(0.0, delay), (error or "")[:400], STATUS_PENDING, moment, moment,
                ),
            )
        return True
    except (sqlite3.Error, OSError) as exc:
        logger.error("outbox: نتوانست در صف بنویسد (%s): %s", type(exc).__name__, exc)
        return False


def due(now: float | None = None, *, limit: int = DRAIN_LIMIT) -> list[QueuedPublish]:
    """Items whose backoff has elapsed, oldest first."""
    moment = time.time() if now is None else now
    try:
        with _db() as conn:
            rows = conn.execute(
                "SELECT * FROM outbox WHERE status = ? AND next_at <= ? "
                "ORDER BY created_at LIMIT ?",
                (STATUS_PENDING, moment, max(1, int(limit))),
            ).fetchall()
    except (sqlite3.Error, OSError) as exc:
        logger.error("outbox: خواندن صف ناموفق بود: %s", exc)
        return []
    return [_row_to_entry(row) for row in rows]


def note_failure(entry: QueuedPublish, error: str, *, now: float | None = None) -> QueuedPublish:
    """Count the attempt, schedule the next one, or give up (and say so in the row)."""
    moment = time.time() if now is None else now
    attempts = entry.attempts + 1
    too_old = bool(entry.created_at) and (moment - entry.created_at) > MAX_AGE_SECONDS
    give_up = attempts >= MAX_ATTEMPTS or too_old
    status = STATUS_DROPPED if give_up else STATUS_PENDING
    next_at = moment + backoff_seconds(attempts)
    try:
        with _db() as conn:
            conn.execute(
                "UPDATE outbox SET attempts = ?, next_at = ?, last_error = ?, status = ?, updated_at = ? "
                "WHERE batch_id = ?",
                (attempts, next_at, (error or "")[:400], status, moment, entry.batch_id),
            )
    except (sqlite3.Error, OSError) as exc:                       # the row is what it is
        logger.error("outbox: ثبت خطا ناموفق بود: %s", exc)
    if give_up:
        forget_files(entry.batch_id)
    return QueuedPublish(
        batch_id=entry.batch_id, chat_id=entry.chat_id, user_id=entry.user_id,
        payload=entry.payload, images=entry.images, attempts=attempts, next_at=next_at,
        last_error=(error or "")[:400], status=status, created_at=entry.created_at,
        thread_id=entry.thread_id, mode=entry.mode, missing_images=entry.missing_images,
    )


def abandon(batch_id: str, error: str, *, now: float | None = None) -> None:
    """Take the item out of the queue *without* deleting it: a 400 is an outcome too.

    The row keeps ``status='dropped'`` and the reason, so «چرا این محصول ساخته نشد؟» still has
    an answer weeks later; deleting it would turn a decision into a missing record.
    """
    moment = time.time() if now is None else now
    try:
        with _db() as conn:
            conn.execute(
                "UPDATE outbox SET status = ?, last_error = ?, updated_at = ? WHERE batch_id = ?",
                (STATUS_DROPPED, (error or "")[:400], moment, batch_id),
            )
    except (sqlite3.Error, OSError) as exc:
        logger.error("outbox: ثبت خطای تکراری ناموفق بود: %s", exc)
    forget_files(batch_id)


def succeed(batch_id: str, *, now: float | None = None) -> None:
    """The product is on the shop: drop the row and the images it was holding."""
    try:
        with _db() as conn:
            conn.execute("DELETE FROM outbox WHERE batch_id = ?", (batch_id,))
    except (sqlite3.Error, OSError) as exc:
        logger.error("outbox: پاک‌کردن ردیف ناموفق بود: %s", exc)
    forget_files(batch_id)


def forget_files(batch_id: str) -> None:
    shutil.rmtree(FILES_DIR / batch_id, ignore_errors=True)


def pending(*, now: float | None = None) -> int:
    try:
        with _db() as conn:
            return int(conn.execute(
                "SELECT COUNT(*) FROM outbox WHERE status = ?", (STATUS_PENDING,)
            ).fetchone()[0])
    except (sqlite3.Error, OSError):
        return 0


def stats(*, now: float | None = None) -> dict[str, Any]:
    """What «چند توی صف مونده؟» should answer, and what ``--check-config`` prints."""
    moment = time.time() if now is None else now
    try:
        with _db() as conn:
            rows = conn.execute("SELECT status, attempts, next_at, last_error FROM outbox").fetchall()
    except (sqlite3.Error, OSError) as exc:
        return {"error": str(exc), "pending": 0, "dropped": 0, "waiting_seconds": 0}
    waiting = [row for row in rows if row["status"] == STATUS_PENDING]
    return {
        "pending": len(waiting),
        "dropped": sum(1 for row in rows if row["status"] == STATUS_DROPPED),
        "ready_now": sum(1 for row in waiting if float(row["next_at"] or 0) <= moment),
        "waiting_seconds": round(
            max([float(row["next_at"] or 0) - moment for row in waiting] or [0.0])
        ),
        "last_error": str(waiting[-1]["last_error"] or "") if waiting else "",
    }


def probe() -> str:
    """``""`` when the queue can actually be used, else a Persian problem for ``--check-config``."""
    try:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = _connect()
        try:
            conn.execute("INSERT INTO outbox (batch_id, chat_id, user_id, payload, next_at, created_at, updated_at)"
                         " VALUES ('__probe__', 0, 'probe', '{}', 0, 0, 0)"
                         " ON CONFLICT(batch_id) DO UPDATE SET updated_at = 0")
            conn.execute("DELETE FROM outbox WHERE batch_id = '__probe__'")
        finally:
            conn.close()
        FILES_DIR.mkdir(parents=True, exist_ok=True)
    except (sqlite3.Error, OSError) as exc:
        return f"صفِ ارسال قابل‌استفاده نیست ({type(exc).__name__}): {exc}"
    return ""


def clear_for_tests() -> None:
    """Empty the queue and its files. Only for the suite."""
    try:
        with _db() as conn:
            conn.execute("DELETE FROM outbox")
    except (sqlite3.Error, OSError):
        pass
    shutil.rmtree(FILES_DIR, ignore_errors=True)
