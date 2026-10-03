"""Small, shared primitives for private and durable runtime files."""
from __future__ import annotations

import os
from pathlib import Path


def private_dir(path: Path) -> Path:
    """Create an owner-only state/work directory, including an existing one."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
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
