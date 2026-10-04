"""Atomic JSON state with durable replacement, backup recovery and isolated reads.

The primary is never moved away before its replacement is ready. A failed save
leaves the last readable primary (and backup) in place, and cannot mutate the
cached state through a caller's dict. Critical callers use ``checked_write``:
permission changes and publish intents must not report success without storage.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import tempfile
import threading
import weakref
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from bot.services.fsutils import fsync_dir, private_dir

logger = logging.getLogger(__name__)

_locks: weakref.WeakValueDictionary[str, Any] = weakref.WeakValueDictionary()
_locks_guard = threading.RLock()
_cache: OrderedDict[str, tuple[tuple[int, int, int, int], Any]] = OrderedDict()
_CACHE_LIMIT = 128


class StateWriteError(OSError):
    """The requested state change was not durably acknowledged."""


def lock_for(path: Path | str) -> Any:
    # Reentrant: stores may hold their read-modify-write lock while writing.
    key = str(Path(path).absolute())
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _locks[key] = lock
        return lock


def _signature(file: Path) -> tuple[int, int, int, int]:
    stat = file.stat()
    return stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino


def _cache_put(key: str, signature: tuple[int, int, int, int], data: Any) -> None:
    with _locks_guard:
        _cache[key] = (signature, copy.deepcopy(data))
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_LIMIT:
            _cache.popitem(last=False)


def read_json(path: Path | str, default: Any = None, *, recover: bool = True) -> Any:
    """Return an independent value; also try the backup when the primary is missing."""
    file = Path(path)
    key = str(file)
    with lock_for(file):
        try:
            signature = _signature(file)
            with _locks_guard:
                cached = _cache.get(key)
                if cached and cached[0] == signature:
                    _cache.move_to_end(key)
                    return copy.deepcopy(cached[1])
            data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            invalidate(file)
            if not recover:
                logger.error("could not read security state %s; failing closed", file)
                return copy.deepcopy(default)
            backup = file.with_suffix(file.suffix + ".bak")
            try:
                data = json.loads(backup.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                if not isinstance(exc, FileNotFoundError):
                    logger.error("could not read %s or its backup: %s", file, exc)
                return copy.deepcopy(default)
            logger.warning("recovered %s from its backup copy", file)
            # Do not cache a broken primary's signature against backup contents.
            return data
        _cache_put(key, signature, data)
        return copy.deepcopy(data)


def _stage(file: Path, payload: bytes) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=f".{file.name}.", suffix=".tmp", dir=file.parent)
    temp = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        return temp
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def write_json(path: Path | str, data: Any) -> bool:
    """Save via fsync + replace; return False without hiding the previous state."""
    file = Path(path)
    staged: list[Path] = []
    with lock_for(file):
        try:
            private_dir(file.parent)
            # Reject NaN/infinity: they are not JSON and break PHP/API consumers.
            payload = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
            temp = _stage(file, payload)
            staged.append(temp)
            if file.exists():
                try:
                    previous = file.read_bytes()
                    json.loads(previous)  # never replace a good backup with corrupt data
                    backup = file.with_suffix(file.suffix + ".bak")
                    backup_temp = _stage(backup, previous)
                    staged.append(backup_temp)
                    os.replace(backup_temp, backup)
                except (OSError, ValueError):
                    logger.warning("could not refresh the backup of %s; preserving the primary", file)
            os.replace(temp, file)
            fsync_dir(file.parent)
            _cache_put(str(file), _signature(file), data)
            return True
        except (OSError, TypeError, ValueError) as exc:
            invalidate(file)
            logger.error("could not save %s: %s", file, exc, exc_info=True)
            return False
        finally:
            for temp in staged:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    logger.warning("could not remove state staging file %s", temp)


def checked_write(
    path: Path | str, data: Any, writer: Callable[[Path | str, Any], bool] | None = None,
) -> None:
    """Make a critical state change fail visibly, before an external side effect."""
    if not (writer or write_json)(path, data):
        invalidate(path)
        raise StateWriteError(f"State could not be saved: {path}")


def invalidate(path: Path | str | None = None) -> None:
    with _locks_guard:
        if path is None:
            _cache.clear()
        else:
            _cache.pop(str(path), None)


async def write_json_async(path: Path | str, data: Any) -> bool:
    """``write_json`` off the event loop.

    The synchronous API stays synchronous (CLI, tests, service internals), but a
    *tap* that saves state must not sit on the loop while the disk does its three
    fsyncs: on a slow host that stall is paid by every other user (measured with a
    30 ms fsync: one save made the next user's update wait 95 ms; through a thread,
    6 ms). Durability is unchanged — the same ``write_json`` runs, it just does not
    block the loop while running.
    """
    return await asyncio.to_thread(write_json, path, data)


async def checked_write_async(
    path: Path | str, data: Any, writer: Callable[[Path | str, Any], bool] | None = None,
) -> None:
    """``checked_write`` off the event loop; still raises :class:`StateWriteError`."""
    await asyncio.to_thread(checked_write, path, data, writer)


__all__ = [
    "StateWriteError",
    "checked_write",
    "checked_write_async",
    "invalidate",
    "lock_for",
    "read_json",
    "write_json",
    "write_json_async",
]
