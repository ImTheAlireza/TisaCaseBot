"""Bounded, cancellable processes for untrusted file/image parsing.

Cancelling ``to_thread`` only cancels the waiter; CPU/memory use and disk writes
continue afterwards. A short-lived spawn process can be terminated and reaped
on timeout or flow cancellation. Workers are pure leaf jobs: no REST, Telegram,
shared state writes, or inherited event-loop/network clients.
"""
from __future__ import annotations

import asyncio
import contextlib
import math
import multiprocessing
import os
from collections.abc import Callable
from typing import Any

MAX_WORKERS = 2
MEMORY_MB = 768
_slots: asyncio.Semaphore | None = None
_loop: asyncio.AbstractEventLoop | None = None


def _entry(connection: Any, function: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any], config: Any, timeout: float) -> None:
    try:
        # BLAS must not start dozens of threads inside each bounded worker.
        os.environ["OPENBLAS_NUM_THREADS"] = "1"
        os.environ["OMP_NUM_THREADS"] = "1"
        try:
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (MEMORY_MB * 1024 * 1024,) * 2)
            cpu = max(1, math.ceil(timeout) + 1)
            resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))
        except (ImportError, OSError, ValueError):  # POSIX production; no fake guarantee on Windows
            pass
        # Preserve the caller's validated runtime limits, including per-test overrides.
        from bot import config as configuration
        configuration.settings = config
        module = __import__(function.__module__, fromlist=["settings"])
        if hasattr(module, "settings"):
            module.__dict__["settings"] = config
        connection.send((True, function(*args, **kwargs)))
    except BaseException as exc:
        try:
            connection.send((False, exc))
        except Exception:
            connection.send((False, RuntimeError(f"File worker failed: {type(exc).__name__}")))
    finally:
        connection.close()


def _semaphore() -> asyncio.Semaphore:
    global _slots, _loop
    loop = asyncio.get_running_loop()
    if _slots is None or _loop is not loop:
        _loop = loop
        _slots = asyncio.Semaphore(MAX_WORKERS)
    return _slots


async def run(function: Callable[..., Any], *args: Any, timeout: float = 120.0, **kwargs: Any) -> Any:
    """Run one leaf job; never return from cancellation with a live worker."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Worker timeout must be positive and finite")
    from bot.config import settings

    async with asyncio.timeout(timeout), _semaphore():
        context = multiprocessing.get_context("spawn")
        receiving, sending = context.Pipe(duplex=False)
        process = context.Process(
            target=_entry, args=(sending, function, args, kwargs, settings, timeout),
            name="TisaFileWorker", daemon=True,
        )
        result_task: asyncio.Task[Any] | None = None
        try:
            process.start()
            sending.close()
            result_task = asyncio.create_task(asyncio.to_thread(receiving.recv))
            try:
                success, result = await asyncio.shield(result_task)
            except EOFError as exc:
                raise RuntimeError("File worker exited before returning a result (resource limit or crash)") from exc
            if not success:
                raise result
            return result
        finally:
            sending.close()
            if process.pid is not None:
                if process.is_alive():
                    process.terminate()
                await asyncio.to_thread(process.join, 0.5)
                if process.is_alive():
                    process.kill()
                    await asyncio.to_thread(process.join)
                process.close()
            if result_task is not None:
                with contextlib.suppress(EOFError, OSError, asyncio.CancelledError):
                    await result_task
            receiving.close()
