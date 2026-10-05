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
import os
import re
import tempfile
import shutil
import sqlite3
import threading
import time
import uuid

import httpx
from contextlib import contextmanager
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from bot.config import data_dir
from bot.services.fsutils import fsync_dir, private_dir, private_file

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

    from bot.services.jsonstore import StateWriteError

    if isinstance(exc, StateWriteError):
        return False
    if isinstance(exc, WooCommerceAPIError):
        return (int(getattr(exc, "status_code", 0) or 0) in TRANSIENT_STATUS_CODES
                and getattr(exc, "retry_after", 0.0) < MAX_AGE_SECONDS)
    # httpx's transport failures (ConnectError, ReadTimeout, RemoteProtocolError) are the usual
    # shape of «the shop is unreachable», and they do *not* subclass ConnectionError — naming
    # them is not decoration. The request either never landed or its answer was lost, and the
    # content-addressed batch id is what makes trying again safe.
    return isinstance(exc, (TimeoutError, ConnectionError, OSError, httpx.TransportError))


def is_silent(exc: BaseException) -> bool:
    """Did the shop fail to *answer*, rather than answer «busy»?

    This is the difference between «the payload may be fine, wait» and «the host is down, come
    back later». The status code cannot tell them apart on its own: a ``503`` written by
    :func:`bot.services.woo_fencing.require` means the probe got nothing at all, while a ``503``
    that came back over the wire means LiteSpeed answered and is busy — so the fence tags its own
    error (``host_silent``) and everything else is recognised by its transport class.
    """
    if getattr(exc, "host_silent", False):
        return True
    return isinstance(
        exc,
        (TimeoutError, httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError),
    )


#: How many times the queue knocks before it stops. Eight, because the waits grow:
#: 1m, 2m, 4m, 8m, 16m, 32m, 64m ≈ two hours of trying, which is the window in which a
#: WooCommerce maintenance break is still "coming back".
MAX_ATTEMPTS = 8
#: The wait when the shop did not answer *at all* (:func:`is_silent`). Eight ordinary backoffs
#: are spent inside the first half-hour of a real outage, after which the product is dropped
#: while the host is still down — so silence is waited out in half-hour probes that do **not**
#: consume an attempt. The 24-hour ceiling still decides when the queue stops caring.
SILENT_RETRY_SECONDS = 30 * 60
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
    updated_at: float = 0.0
    generation: str = ""
    claim_token: str = ""
    #: Images the spool lost (someone cleaned ``data/``, a moved host). The drain must say so
    #: instead of publishing a product whose pictures quietly vanished.
    missing_images: list[Path] = field(default_factory=list)

    @property
    def ledger_key(self) -> str:
        return str(self.payload.get("ledger_key") or "")


def backoff_seconds(attempts: int) -> int:
    """1m, 2m, 4m … capped — the queue must not become a denial of service on the shop."""
    return min(BACKOFF_BASE_SECONDS * (2 ** min(8, max(0, attempts - 1))), MAX_DELAY_SECONDS)


def _initialize(conn: sqlite3.Connection) -> None:
    """Set file-level journal mode and create the queue schema."""
    try:                                    # WAL needs a real fs; a plain journal is fine too
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:                   # pragma: no cover - host dependent
        logger.warning("outbox: WAL is not available here; using the default journal")
    conn.executescript(_SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(outbox)")}
    # هر ستونِ migration اینجا تعریف می‌شود؛ identifierها فقط از الف/عدد/آندرلاین
    # تشکیل شده‌اند تا حتی اگر کسی migration ای با ورودیِ بیرونی به این حلقه اضافه
    # کرد، تزریق SQL ممکن نباشد (sqlite برای identifier پارامتر ندارد).
    _migrations: dict[str, str] = {
        "generation": "TEXT NOT NULL DEFAULT ''",
        "claim_token": "TEXT NOT NULL DEFAULT ''",
        "lease_until": "REAL NOT NULL DEFAULT 0",
    }
    _ident = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
    for name, declaration in _migrations.items():
        if name in columns:
            continue
        if not _ident.match(name) or not _ident.match(declaration.split()[0]):
            logger.error("outbox: refusing to apply migration for %r", name)
            continue
        conn.execute(f'ALTER TABLE outbox ADD COLUMN "{name}" {declaration}')


def _connect() -> sqlite3.Connection:
    path = Path(DB_PATH)
    private_dir(path.parent)
    conn = sqlite3.connect(path, timeout=0.25)
    private_file(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=250")
        conn.execute("PRAGMA synchronous=FULL")
        stat = path.stat()
        identity = (stat.st_dev, stat.st_ino)
        # WAL mode and DDL belong to the database file, not each enqueue/due call.
        # Re-running them on the hot path needlessly acquires SQLite's schema locks.
        with _schema_lock:
            if _initialized.get(path) != identity or stat.st_size == 0:
                _initialize(conn)
                stat = path.stat()
                _initialized[path] = (stat.st_dev, stat.st_ino)
                while len(_initialized) > 128:
                    _initialized.pop(next(iter(_initialized)))
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
    wanted = [Path(str(item)) for item in stored if str(item).strip()] if isinstance(stored, list) else []
    present = [path for path in wanted if path.resolve().is_relative_to(FILES_DIR.resolve()) and path.is_file()]
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
        updated_at=float(row["updated_at"] or 0.0),
        generation=str(row["generation"] or ""), claim_token=str(row["claim_token"] or ""),
    )


def _batch_dir(batch_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", batch_id):
        raise ValueError("Invalid outbox batch id")
    return FILES_DIR / batch_id


def spool_images(batch_id: str, paths: Sequence[Path]) -> list[Path]:
    """All images or nothing; each refresh gets an immutable private generation."""
    if not paths:
        return []
    private_dir(FILES_DIR)
    root = private_dir(_batch_dir(batch_id))
    target = Path(tempfile.mkdtemp(prefix="spool-", dir=root))
    target.chmod(0o700)
    kept: list[Path] = []
    try:
        for index, raw in enumerate(paths, 1):
            path = Path(raw)
            if not path.is_file() or path.stat().st_size == 0:
                raise OSError(f"Required image missing/empty: {path}")
            destination = target / f"{index:02d}_{path.name}"
            shutil.copy2(path, destination)
            private_file(destination)
            if destination.stat().st_size != path.stat().st_size:
                raise OSError(f"Incomplete image copy: {path}")
            with destination.open("rb") as handle:
                os.fsync(handle.fileno())
            kept.append(destination)
        fsync_dir(target)
        fsync_dir(root)
        return kept
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        try:
            root.rmdir()
        except OSError:
            pass
        raise


def _forget_paths(paths: Sequence[Path]) -> None:
    """Clean only the committed row's files, never a concurrently refreshed spool."""
    root = FILES_DIR.resolve()
    parents: set[Path] = set()
    for raw in paths:
        path = Path(raw)
        if not path.resolve().is_relative_to(root):
            logger.error("outbox: refusing cleanup outside the spool: %s", path)
            continue
        try:
            path.unlink(missing_ok=True)
            parents.add(path.parent)
        except OSError:
            logger.warning("outbox: could not clean %s", path)
    for parent in parents:
        while parent != root and parent.is_relative_to(root):
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent


def _stored_paths(conn: sqlite3.Connection, batch_id: str, expected_updated_at: float | None = None, expected_generation: str | None = None, claim_token: str | None = None) -> list[Path]:
    row = conn.execute("SELECT images, updated_at, generation, claim_token FROM outbox WHERE batch_id = ?", (batch_id,)).fetchone()
    if row is None:
        if expected_generation is not None or claim_token:
            raise RuntimeError("Queue item no longer exists; acknowledgement deferred")
        return []
    if expected_generation is not None and str(row["generation"]) != expected_generation:
        raise RuntimeError("Queue generation changed; acknowledgement deferred")
    if claim_token and str(row["claim_token"]) != claim_token:
        raise RuntimeError("Queue claim lost; acknowledgement deferred")
    if expected_updated_at is not None and float(row["updated_at"]) != expected_updated_at:
        raise RuntimeError("Queue item was refreshed during the attempt; acknowledgement deferred")
    try:
        paths = json.loads(row["images"] or "[]")
    except (TypeError, ValueError):
        paths = []
    return [Path(str(path)) for path in paths] if isinstance(paths, list) else []


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
    spooled: list[Path] = []
    try:
        _batch_dir(batch_id)
        spooled = spool_images(batch_id, images)
        with _db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            active = conn.execute("SELECT lease_until FROM outbox WHERE batch_id = ?", (batch_id,)).fetchone()
            if active is not None and float(active[0]) > moment:
                raise ValueError("An active queue generation cannot be refreshed")
            conn.execute(
                """
                INSERT INTO outbox (batch_id, chat_id, thread_id, user_id, mode, payload, images,
                                    attempts, next_at, last_error, status, created_at, updated_at, generation)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(batch_id) DO UPDATE SET
                    payload    = excluded.payload,
                    images     = excluded.images,
                    next_at    = MAX(outbox.next_at, excluded.next_at),
                    last_error = excluded.last_error,
                    status     = excluded.status,
                    updated_at = excluded.updated_at,
                    generation = excluded.generation, claim_token = '', lease_until = 0
                """,
                (
                    batch_id, int(chat_id or 0), thread_id, str(user_id), mode,
                    json.dumps(dict(payload) | {"_queued_image_count": len(images)}, ensure_ascii=False, allow_nan=False),
                    json.dumps([str(path) for path in spooled], ensure_ascii=False),
                    moment + max(0.0, delay), (error or "")[:400], STATUS_PENDING, moment, moment, uuid.uuid4().hex,
                ),
            )
        return True
    except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
        _forget_paths(spooled)
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


LEASE_SECONDS = 300


def claim_due(now: float | None = None) -> QueuedPublish | None:
    """Atomically claim one row; no network inside the SQL transaction."""
    moment = time.time() if now is None else now
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM outbox WHERE status = ? AND next_at <= ? AND lease_until <= ? "
            "ORDER BY created_at LIMIT 1", (STATUS_PENDING, moment, moment),
        ).fetchone()
        if row is None:
            return None
        token = uuid.uuid4().hex
        conn.execute("UPDATE outbox SET claim_token = ?, lease_until = ? WHERE batch_id = ?",
                     (token, moment + LEASE_SECONDS, row["batch_id"]))
        return replace(_row_to_entry(row), claim_token=token)


def renew_claim(entry: QueuedPublish, *, now: float | None = None) -> bool:
    moment = time.time() if now is None else now
    with _db() as conn:
        result = conn.execute("UPDATE outbox SET lease_until = ? WHERE batch_id = ? "
                              "AND generation = ? AND claim_token = ? AND status = ?",
                              (moment + LEASE_SECONDS, entry.batch_id, entry.generation, entry.claim_token, STATUS_PENDING))
        return result.rowcount == 1


def release_claim(entry: QueuedPublish) -> None:
    with _db() as conn:
        conn.execute("UPDATE outbox SET claim_token = '', lease_until = 0 WHERE batch_id = ? "
                     "AND generation = ? AND claim_token = ?", (entry.batch_id, entry.generation, entry.claim_token))


def note_failure(entry: QueuedPublish, error: str, *, now: float | None = None) -> QueuedPublish:
    """Acknowledge the retry state durably before deleting any dropped files."""
    moment = time.time() if now is None else now
    attempts = entry.attempts + 1
    give_up = attempts >= MAX_ATTEMPTS or expired(entry, now=moment)
    status = STATUS_DROPPED if give_up else STATUS_PENDING
    next_at = moment + backoff_seconds(attempts)
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        paths = _stored_paths(conn, entry.batch_id, entry.updated_at or None, entry.generation or None, entry.claim_token or None)
        conn.execute(
            "UPDATE outbox SET attempts = ?, next_at = ?, last_error = ?, status = ?, updated_at = ?, claim_token = '', lease_until = 0 WHERE batch_id = ?",
            (attempts, next_at, (error or "")[:400], status, moment, entry.batch_id),
        )
    if give_up:
        _forget_paths(paths)
    return replace(entry, attempts=attempts, next_at=next_at, last_error=(error or "")[:400], status=status, updated_at=moment)


def expired(entry: QueuedPublish, *, now: float | None = None) -> bool:
    moment = time.time() if now is None else now
    return moment - entry.created_at >= MAX_AGE_SECONDS or entry.attempts >= MAX_ATTEMPTS


def abandon(batch_id: str, error: str, *, now: float | None = None, expected_updated_at: float | None = None, expected_generation: str | None = None, claim_token: str | None = None) -> None:
    """Persist the terminal result first; propagate failed SQL acknowledgement."""
    moment = time.time() if now is None else now
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        paths = _stored_paths(conn, batch_id, expected_updated_at, expected_generation, claim_token)
        conn.execute(
            "UPDATE outbox SET status = ?, last_error = ?, updated_at = ? WHERE batch_id = ?",
            (STATUS_DROPPED, (error or "")[:400], moment, batch_id),
        )
    _forget_paths(paths)


def succeed(batch_id: str, *, now: float | None = None, expected_updated_at: float | None = None, expected_generation: str | None = None, claim_token: str | None = None) -> None:
    """Only a committed DELETE acknowledges success and releases that row's files."""
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        paths = _stored_paths(conn, batch_id, expected_updated_at, expected_generation, claim_token)
        conn.execute("DELETE FROM outbox WHERE batch_id = ?", (batch_id,))
    _forget_paths(paths)


def prune(*, now: float | None = None, retention_days: int = 7) -> int:
    """Bound terminal history and orphan generations without touching pending bytes."""
    moment = time.time() if now is None else now
    with _db() as conn:
        rows = conn.execute("SELECT images FROM outbox WHERE status = ?", (STATUS_PENDING,)).fetchall()
        live: set[Path] = set()
        for row in rows:
            try:
                live.update(Path(str(path)).parent.resolve() for path in json.loads(row["images"] or "[]"))
            except (TypeError, ValueError):
                pass
        count = conn.execute("DELETE FROM outbox WHERE status = ? AND updated_at < ?",
                             (STATUS_DROPPED, moment - max(1, retention_days) * 86400)).rowcount
    if FILES_DIR.exists():
        for batch_dir in list(FILES_DIR.iterdir()):
            if not batch_dir.is_dir() or batch_dir.is_symlink():
                continue
            for generation in list(batch_dir.iterdir()):
                try:
                    if generation.is_dir() and generation.resolve() not in live and generation.stat().st_mtime < moment - MAX_AGE_SECONDS:
                        shutil.rmtree(generation)
                except OSError:
                    continue
            try:
                batch_dir.rmdir()
            except OSError:
                pass
    return count


def pending(*, now: float | None = None) -> int:
    try:
        with _db() as conn:
            return int(conn.execute(
                "SELECT COUNT(*) FROM outbox WHERE status = ?", (STATUS_PENDING,)
            ).fetchone()[0])
    except (sqlite3.Error, OSError):
        return 0


def is_queued(batch_id: str) -> bool:
    """Is this exact batch still waiting for the shop?

    The product flow's idle timer asks it before it calls a quiet seller an «abandoned» build:
    a queued publish retries for up to two hours on its own, and saying «جریان بسته شد» over it
    both spikes a metric that is not true and frightens someone whose product is about to appear.
    An unreadable database answers ``False`` — the conservative direction, since the row is then
    reported as finished rather than promised forever.
    """
    if not batch_id:
        return False
    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT 1 FROM outbox WHERE batch_id = ? AND status = ? LIMIT 1",
                (batch_id, STATUS_PENDING),
            ).fetchone()
    except (sqlite3.Error, OSError):
        return False
    return row is not None


def get(batch_id: str, *, user_id: int | str | None = None) -> QueuedPublish | None:
    """One queued item, or ``None``. Asked with a ``user_id``, somebody else's row is absent.

    The seller's «📤 صف من» button needs exactly this: the row *and* the proof that the batch id
    in the callback data is theirs. A foreign or unknown id is answered as «چیزی در صف نیست»,
    which is the honest reply and the only safe one to build a button out of.
    """
    if not batch_id:
        return None
    try:
        with _db() as conn:
            row = conn.execute("SELECT * FROM outbox WHERE batch_id = ?", (batch_id,)).fetchone()
    except (sqlite3.Error, OSError):
        return None
    if row is None or (user_id is not None and str(row["user_id"]) != str(user_id)):
        return None
    return _row_to_entry(row)


def rows_for(user_id: int | str, *, limit: int = 10) -> list[QueuedPublish]:
    """What this one seller has waiting, newest first — the queue seen from their side."""
    try:
        with _db() as conn:
            rows = conn.execute(
                "SELECT * FROM outbox WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                (str(user_id), max(1, int(limit))),
            ).fetchall()
    except (sqlite3.Error, OSError):
        return []
    return [_row_to_entry(row) for row in rows]


def defer(entry: QueuedPublish, error: str, *, now: float | None = None) -> QueuedPublish:
    """Wait out a silent host without spending one of the promised attempts.

    ``attempts`` stays where it was on purpose: the eight tries the seller was told about are
    then still eight *real* tries, instead of eight knocks inside the first half-hour of an
    outage after which the product is dropped while the shop is still down.
    """
    moment = time.time() if now is None else now
    next_at = moment + SILENT_RETRY_SECONDS
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE outbox SET next_at = ?, last_error = ?, updated_at = ?, claim_token = '',"
            " lease_until = 0 WHERE batch_id = ? AND status = ?",
            (next_at, (error or "")[:400], moment, entry.batch_id, STATUS_PENDING),
        )
    return replace(entry, next_at=next_at, last_error=(error or "")[:400], updated_at=moment)


def push_now(batch_id: str, *, user_id: int | str | None = None) -> bool:
    """Make a waiting item due immediately — «همین حالا» — and report whether it was there.

    Only an unclaimed row is touched: a drain that is mid-flight has a lease, and cutting that
    lease would let a second attempt publish the same batch while the first is still waiting for
    the shop. ``False`` therefore also means «الان دارد تلاش می‌شود», and the caller says so.
    """
    if not batch_id:
        return False
    moment = time.time()
    sql = ("UPDATE outbox SET next_at = ?, updated_at = ?"
           " WHERE batch_id = ? AND status = ? AND lease_until <= ?")
    params: list[Any] = [moment - 1, moment, batch_id, STATUS_PENDING, moment]
    if user_id is not None:
        sql += " AND user_id = ?"
        params.append(str(user_id))
    try:
        with _db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(sql, params).rowcount
    except (sqlite3.Error, OSError):
        return False
    return bool(changed)


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


# Test-only: lets a test point the queue at a throwaway database without leftovers.
def clear_for_tests() -> None:
    """Empty the queue and its files. Only for the suite."""
    try:
        with _db() as conn:
            conn.execute("DELETE FROM outbox")
    except (sqlite3.Error, OSError):
        pass
    shutil.rmtree(FILES_DIR, ignore_errors=True)
