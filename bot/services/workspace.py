"""Private session directories with active-work protection and race-safe cleanup."""
from __future__ import annotations

import logging
import shutil
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

from bot.services.fsutils import private_dir

logger = logging.getLogger(__name__)
_active: set[Path] = set()
_guard = threading.RLock()


def new_dir(root: Path, owner: object, prefix: str = "") -> Path:
    private_dir(root)
    path = Path(tempfile.mkdtemp(prefix=f"{prefix}{owner}_", dir=root))
    path.chmod(0o700)
    return path


def protect(path: Path) -> None:
    with _guard:
        _active.add(path.resolve())


def remove(path: Path) -> None:
    with _guard:
        try:
            shutil.rmtree(path, ignore_errors=True)
        finally:
            _active.discard(path.resolve())


def iter_workspaces(root: Path) -> Iterator[Path]:
    try:
        entries = list(root.iterdir())
    except OSError:
        return iter(())
    found: list[tuple[float, Path]] = []
    for path in entries:
        try:
            if path.is_dir() and not path.is_symlink():
                found.append((path.stat().st_mtime, path))
        except OSError:
            continue
    return iter(path for _modified, path in sorted(found))


def sweep(root: Path, max_age_hours: float, *, label: str = "workspace") -> int:
    removed = 0
    cutoff = time.time() - max(1.0, max_age_hours) * 3600
    for path in iter_workspaces(root):
        with _guard:
            if path.resolve() in _active:
                continue
            try:
                if path.stat().st_mtime >= cutoff:
                    continue
                shutil.rmtree(path)
                removed += 1
            except OSError:
                continue
    if removed:
        logger.info("swept %d stale %s(s) older than %g h", removed, label, max_age_hours)
    return removed


def close_owner(root: Path, owner: object, *, prefix: str = "") -> bool:
    removed = False
    needle = f"{prefix}{owner}_"
    for path in iter_workspaces(root):
        if not path.name.startswith(needle):
            continue
        with _guard:
            if path.resolve() in _active:
                continue
            try:
                shutil.rmtree(path)
                removed = True
            except OSError:
                continue
    return removed


__all__ = ["close_owner", "iter_workspaces", "new_dir", "protect", "remove", "sweep"]
