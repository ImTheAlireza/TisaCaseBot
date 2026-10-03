"""Cross-task/process publish leases on the shared private state volume.

The bundled WordPress plugin also fences native WooCommerce product creation by
batch ID at the database level. This local lease avoids duplicate uploads and
serializes retries on one host/volume. Do not treat a local filesystem lock as a
cross-host guarantee if the importer/fencing plugin is not installed.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from bot.config import data_dir
from bot.services.fsutils import private_dir


@asynccontextmanager
async def hold(namespace: str, batch: str, *, timeout: float = 120.0) -> AsyncIterator[None]:
    if not batch:
        yield
        return
    import fcntl  # Linux/POSIX is the supported deployment platform

    root = private_dir(data_dir() / "publish-locks")
    key = hashlib.sha256(f"{namespace}|{batch}".encode()).hexdigest()
    descriptor = os.open(root / f"{int(key[:8], 16) % 128:03d}.lock", os.O_CREAT | os.O_RDWR, 0o600)
    started = time.monotonic()
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() - started >= timeout:
                    raise TimeoutError("این بسته در پردازش دیگری در حال انتشار است؛ بعداً دوباره امتحان کن.") from None
                await asyncio.sleep(0.05)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
        # Never unlink the lock: waiters may already hold the old inode.
