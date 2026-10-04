"""Bounded, cancellable processes for untrusted file/image parsing.

Cancelling ``to_thread`` only cancels the waiter; CPU/memory use and disk writes
continue afterwards. A short-lived spawn process can be terminated and reaped
on timeout or flow cancellation. Workers are pure leaf jobs: no REST, Telegram,
shared state writes, or inherited event-loop/network clients.

Why a child is kept warm
------------------------
A fresh ``spawn`` child re-imports its libraries for every job. Measured: 231 ms of
a 481 ms image compression and 442 ms of a 566 ms 200-row xlsx read were process
start-up, ``import pandas`` alone costing 251 ms. So a child that just finished a
job is *parked* — as long as more work is already queued behind it (an album or a
product batch compresses several images at once, and ``MAX_WORKERS`` makes the rest
wait) — and the next job is handed to that same process instead of a new one. When
it is the last job, the child is terminated as before: no extra process sits around
holding RAM in the steady state.

Isolation is unchanged where it matters: each job still runs in its own process,
not in the bot's; a crash, a timeout, a cancelled flow or a dead pipe kills that
process and the next job spawns a fresh one; the memory cap still applies to every
child; and a parked child exits by itself after :data:`IDLE_TTL_SECONDS` without a
job, is retired after :data:`MAX_JOBS_PER_WORKER` jobs, and is retired early if its
RSS passes :data:`RECYCLE_RSS_MB` — so a slow leak cannot accumulate unnoticed.
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

#: How long a parked child waits for the next job before exiting on its own.
IDLE_TTL_SECONDS = 8.0
#: A child is retired after this many jobs…
MAX_JOBS_PER_WORKER = 25
#: …or when its resident memory passes this, whichever comes first.
RECYCLE_RSS_MB = 500
#: CPU backstop for the whole child (seconds); each job additionally gets its own budget.
_CPU_CEILING_SECONDS = 3600


class WorkerCrash(RuntimeError):
    """The child process died before answering (memory cap, CPU cap, or a crash).

    A type of its own so a flow can answer with something to *do* — «فایل را تقسیم کن» —
    instead of forwarding «File worker exited before returning a result».
    """


class _Warm:
    """A child process that can take another job, plus its pipe to us."""

    __slots__ = ("connection", "jobs", "process")

    def __init__(self, process: Any, connection: Any) -> None:
        self.process = process
        self.connection = connection
        self.jobs = 0

    def alive(self) -> bool:
        return bool(self.process.is_alive())

    def rss_mb(self) -> float:
        return _rss_mb(getattr(self.process, "pid", None))


# --- the child ------------------------------------------------------------------

def _limits() -> int:
    """Cap memory for the whole child; return the CPU ceiling (seconds) armed per job."""
    # BLAS must not start dozens of threads inside each bounded worker.
    os.environ["OPENBLAS_NUM_THREADS"] = "1"
    os.environ["OMP_NUM_THREADS"] = "1"
    try:
        import resource
    except ImportError:                       # pragma: no cover - POSIX production
        return 0
    try:
        resource.setrlimit(resource.RLIMIT_AS, (MEMORY_MB * 1024 * 1024,) * 2)
    except (OSError, ValueError):              # pragma: no cover - no fake guarantee on Windows
        pass
    try:
        _soft, hard = resource.getrlimit(resource.RLIMIT_CPU)
        ceiling = hard if 0 < hard < _CPU_CEILING_SECONDS else _CPU_CEILING_SECONDS
        resource.setrlimit(resource.RLIMIT_CPU, (ceiling, ceiling))
        return ceiling
    except (OSError, ValueError):              # pragma: no cover
        return 0


def _arm_cpu(hard: int, timeout: float) -> None:
    """Give *this* job its own CPU budget on top of what the child already spent.

    A long-lived child would otherwise burn one shared budget and die mid-job; the
    soft limit can be raised again up to the hard ceiling set at start-up.
    """
    if not hard:
        return
    try:
        import resource

        spent = resource.getrusage(resource.RUSAGE_SELF)
        budget = math.ceil(timeout) + 1
        soft = math.ceil(spent.ru_utime + spent.ru_stime) + budget
        resource.setrlimit(resource.RLIMIT_CPU, (min(soft, hard), hard))
    except (ImportError, OSError, ValueError):  # pragma: no cover
        pass


def _serve(connection: Any, config: Any) -> None:
    """Long-lived worker: import once, then answer one job at a time."""
    hard = _limits()
    # Preserve the caller's validated runtime limits, including per-test overrides.
    from bot import config as configuration

    configuration.settings = config
    while True:
        try:
            # Exit on our own when nobody needs us: a warm process is paid for in RAM.
            if not connection.poll(IDLE_TTL_SECONDS):
                return
            job = connection.recv()
        except (EOFError, OSError):
            return
        if job is None:
            return
        function, args, kwargs, timeout = job
        _arm_cpu(hard, timeout)
        try:
            module = __import__(function.__module__, fromlist=["settings"])
            if hasattr(module, "settings"):
                module.__dict__["settings"] = config
            outcome: tuple[bool, Any] = (True, function(*args, **kwargs))
        except BaseException as exc:
            outcome = (False, exc)
        try:
            connection.send(outcome)
        except Exception:
            return
        finally:
            # Drop the job's references (a 4000×3000 image stays alive otherwise) and
            # hand the free pages back where the allocator allows it.
            del function, args, kwargs, outcome
            with contextlib.suppress(Exception):
                import gc

                gc.collect()


def _rss_mb(pid: int | None) -> float:
    """Resident memory of the child in MiB (0 when it cannot be read)."""
    if not pid:
        return 0.0
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return float(line.split()[1]) / 1024.0
    except (OSError, ValueError, IndexError):  # pragma: no cover - non-Linux/raced exit
        return 0.0
    return 0.0


# --- the pool -------------------------------------------------------------------

_slots: asyncio.Semaphore | None = None
_loop: asyncio.AbstractEventLoop | None = None
#: Children waiting for a job. Only ever filled while another job of the same burst is
#: still running or queued — see :func:`run`; an idle bot keeps no worker process.
_idle: list[_Warm] = []
#: Jobs inside :func:`run` right now, queued or running (used for that decision).
_load = 0


def _semaphore() -> asyncio.Semaphore:
    global _slots, _loop
    loop = asyncio.get_running_loop()
    if _slots is None or _loop is not loop:
        _loop = loop
        _slots = asyncio.Semaphore(MAX_WORKERS)
    return _slots


def _spawn(config: Any) -> _Warm:
    context = multiprocessing.get_context("spawn")
    parent_end, child_end = context.Pipe(duplex=True)
    process = context.Process(
        target=_serve, args=(child_end, config), name="TisaFileWorker", daemon=True,
    )
    try:
        process.start()
    finally:
        child_end.close()
    return _Warm(process, parent_end)


def _forget(worker: _Warm) -> None:
    """Drop a dead child's bookkeeping (its pipe, its process handle)."""
    with contextlib.suppress(ValueError):
        _idle.remove(worker)
    with contextlib.suppress(OSError, ValueError):
        worker.connection.close()
    if not worker.alive():
        with contextlib.suppress(OSError, ValueError):
            worker.process.close()


def _usable(worker: _Warm) -> bool:
    """A parked child we can still hand a job to.

    An idle child never writes on the pipe, so any readable data means its end is
    closed (it hit its idle TTL and exited) — that check is what keeps a job from
    being handed to a process that is already gone.
    """
    if not worker.alive():
        return False
    try:
        return not worker.connection.poll(0)
    except (OSError, ValueError):              # pragma: no cover - closed handle
        return False


def _take(config: Any) -> _Warm:
    """A warm child if one survived, otherwise a fresh process."""
    for worker in list(_idle):
        if _usable(worker):
            _idle.remove(worker)
            return worker
        _forget(worker)
    return _spawn(config)


async def _retire(worker: _Warm) -> None:
    """Terminate a child and reap it — after a crash, a timeout, a cancellation, or
    when it is simply the last job of the burst."""
    with contextlib.suppress(ValueError):
        _idle.remove(worker)
    with contextlib.suppress(OSError, ValueError):
        worker.connection.close()
    process = worker.process
    if process.pid is not None:
        if process.is_alive():
            process.terminate()
        await asyncio.to_thread(process.join, 0.5)
        if process.is_alive():
            process.kill()
            await asyncio.to_thread(process.join)
        process.close()


async def _exchange(worker: _Warm, function: Callable[..., Any], args: tuple[Any, ...],
                    kwargs: dict[str, Any], timeout: float) -> Any:
    """Hand one job to the child and wait for its single answer."""
    try:
        await asyncio.to_thread(worker.connection.send, (function, args, kwargs, timeout))
        success, result = await asyncio.to_thread(worker.connection.recv)
    except (EOFError, BrokenPipeError, ConnectionResetError, OSError) as exc:
        raise WorkerCrash("File worker exited before returning a result (resource limit or crash)") from exc
    worker.jobs += 1
    if not success:
        raise result
    return result


async def run(function: Callable[..., Any], *args: Any, timeout: float = 120.0, **kwargs: Any) -> Any:
    """Run one leaf job; never return from cancellation with a live worker."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Worker timeout must be positive and finite")
    global _load
    from bot.config import settings

    _load += 1
    try:
        async with asyncio.timeout(timeout), _semaphore():
            worker = _take(settings)
            try:
                result = await _exchange(worker, function, args, kwargs, timeout)
            except BaseException:
                await _retire(worker)
                raise
            # More work is queued (this burst is not finished) → keep the child warm
            # for it. The last job of a burst retires every parked worker, so nothing
            # stays resident once the album/batch is done.
            if _load > 1 and worker.jobs < MAX_JOBS_PER_WORKER and worker.rss_mb() < RECYCLE_RSS_MB:
                _idle.append(worker)
            else:
                await _retire(worker)
                for parked in list(_idle):
                    await _retire(parked)
            return result
    finally:
        _load -= 1


async def shutdown() -> None:
    """Stop parked workers (the application's ``post_stop``); safe to call twice."""
    for worker in list(_idle):
        await _retire(worker)
