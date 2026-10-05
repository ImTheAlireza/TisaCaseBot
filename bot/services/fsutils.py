"""Small, shared primitives for private and durable runtime files."""
from __future__ import annotations

import os
import stat
from pathlib import Path


def private_dir(path: Path) -> Path:
    """Create (or adopt) an owner-only work/state directory — safely.

    ``mkdir(exist_ok=True)`` + ``chmod`` is not enough on a shared host: a predictable
    path (the old ``/tmp/tisaposttowp-*`` roots) can be created *first* by another
    account, or replaced by a symlink, and then our files are readable, deletable, or
    redirected elsewhere. So the directory is now lstat-ed: it must not be a symlink,
    it must belong to us, and it must end up ``0700``.
    """
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        path.mkdir(mode=0o700, parents=True)
        info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise PermissionError(f"مسیر موقت یک symlink است و پذیرفته نمی‌شود: {path}")
    if not stat.S_ISDIR(info.st_mode):
        raise NotADirectoryError(f"مسیر موقت یک پوشه نیست: {path}")
    if info.st_uid != os.getuid():
        raise PermissionError(f"پوشهٔ موقت به کاربر دیگری تعلق دارد: {path}")
    path.chmod(0o700)
    return path


def private_file(path: Path) -> None:
    path.chmod(0o600)


def fsync_dir(path: Path) -> None:
    """Persist a rename, not just the file's contents (POSIX/Linux deployment)."""
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
