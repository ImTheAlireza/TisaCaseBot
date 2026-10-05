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
process and the next job spawns a fresh one; every child is memory-capped; and a
parked child exits by itself after :data:`IDLE_TTL_SECONDS` without a job, is retired
after :data:`MAX_JOBS_PER_WORKER` jobs, and is retired early if its RSS passes
:data:`RECYCLE_RSS_MB` or its free address space drops under the next file's needs —
so a slow leak cannot accumulate unnoticed.

The cap is a *file* budget, not an absolute ceiling
---------------------------------------------------
``RLIMIT_AS`` is armed as «whatever this child already holds + :data:`MEMORY_MB`», after
the job's libraries are imported. An absolute cap (the previous behaviour) can be filled
by the interpreter's own imports alone: on such a host a 22 KB file fails with
``MemoryError`` in every format, because no allocation for it ever succeeds. The budget
comes from ``WORKER_MEMORY_MB``, and ``logs/bot.log`` prints baseline, budget, cap and
headroom for every child — so «چرا حافظه پر شد؟» has a number for an answer. A child
with less than :data:`MIN_HEADROOM_MB` free refuses the job with :class:`WorkerNoMemory`
instead of letting a random allocation die deep inside pandas.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import multiprocessing
import os
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

MAX_WORKERS = 2
#: چه‌قدر *فایل* می‌تواند حافظه خرج کند (MB) — نه سقفِ کلِ پردازه.
#:
#: نسخهٔ قبل این عدد را از ابتدا روی کلِ آدرس‌فضای فرزند می‌گذاشت، بدون توجه به
#: این‌که خودِ فرزند چقدر را همین حالا اشغال کرده. روی هاستی که پایهٔ ایمپورت‌ها
#: (python + pandas + کتابخانه‌ها) به همان سقف نزدیک بود، نتیجه «MemoryError» برای
#: یک فایل ۲۲KB بود — در هر سه فرمت، چون هیچ‌کدام حتی شروع نشده بودند. حالا
#: سقف = «آن‌چه فرزند همین حالا دارد + بودجهٔ زیر»، اندازه‌گیری‌شده در
#: آدرس‌فضای همین پردازه (``/proc/self/status``): سقفِ فایل، نه سقفِ مفسر.
MEMORY_MB = 768
#: کمترین فضای آزادِ آدرس که هنوز می‌ارزد کار را شروع کرد (MB). کمتر از این،
#: جوابِ صریح می‌دهیم («حافظهٔ کارگر پر است») نه MemoryErrorِ وسطِ pandas.
MIN_HEADROOM_MB = 96

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


class WorkerNoMemory(WorkerCrash):
    """The child had no free address space left to start the job.

    Deliberately *not* reported as «فایل سنگین بود»: the file may be 22 KB. The child's
    own imports (plus a cap inherited from the host) can fill the whole address space, and
    then every format fails the same way — the bug the shop reported. The flow answers this
    one with the memory knob to raise, and :func:`_serve` refuses the job *before* letting a
    random allocation die as ``MemoryError``.
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

    def room_mb(self) -> float:
        """Free address space under this child's own cap (∞ when it has none).

        The cap is on *address space*, the recycle gate is on *RSS*: a child that parsed
        one big file can look slim in RSS and still have no room left for a second job.
        Parking it would hand the next file — even a 22 KB one — a process that can no
        longer allocate.
        """
        pid = getattr(self.process, "pid", None)
        limit = _limit_mb(pid)
        if limit <= 0:
            return math.inf
        return limit - _vsz_mb(pid)


# --- the child ------------------------------------------------------------------

def _prepare_child() -> int:
    """BLAS threads to one and the CPU ceiling armed; returns the CPU ceiling (seconds)."""
    # BLAS must not start dozens of threads inside each bounded worker.
    os.environ["OPENBLAS_NUM_THREADS"] = "1"
    os.environ["OMP_NUM_THREADS"] = "1"
    try:
        import resource
    except ImportError:                       # pragma: no cover - POSIX production
        return 0
    try:
        _soft, hard = resource.getrlimit(resource.RLIMIT_CPU)
        ceiling = hard if 0 < hard < _CPU_CEILING_SECONDS else _CPU_CEILING_SECONDS
        resource.setrlimit(resource.RLIMIT_CPU, (ceiling, ceiling))
        return ceiling
    except (OSError, ValueError):              # pragma: no cover
        return 0


def _memory_target(baseline: int, budget: int, hard: int) -> tuple[int, int]:
    """``(cap, headroom)`` for a child that already occupies ``baseline`` bytes.

    The cap is the interpreter's own footprint **plus** what one file may spend — not an
    absolute number that a heavy import set can eat on its own. ``hard`` is a ceiling the
    host itself set (systemd ``LimitAS``, a supervisor ``ulimit -v``, a parent process);
    it always wins, and then the headroom is whatever is left, honestly.
    """
    cap = baseline + max(0, budget)
    if 0 < hard < cap:
        cap = hard
    return cap, cap - baseline


def _budget_bytes(config: Any) -> int:
    """The per-file memory budget (``WORKER_MEMORY_MB``), in bytes."""
    value = getattr(config, "worker_memory_mb", None)
    try:
        megabytes = float(MEMORY_MB) if value is None else float(value)
    except (TypeError, ValueError):
        megabytes = float(MEMORY_MB)
    if not math.isfinite(megabytes) or megabytes <= 0:
        megabytes = float(MEMORY_MB)
    return int(megabytes * 1024 * 1024)


def _arm_memory(config: Any) -> float:
    """Cap what the *file* may spend, on top of what this child already holds.

    Called once per child, **after** the job's module (pandas, …) is imported, because that
    import is exactly the footprint the old absolute cap used to collide with. Returns the
    headroom left (MB) — ``-1`` when nothing could be measured, ``0`` when the host left
    none. The numbers are logged either way: when a shop sends a 22 KB file and gets a
    memory error, ``logs/bot.log`` must say *which* number ran out.
    """
    try:
        import resource
    except ImportError:                       # pragma: no cover - POSIX production
        return -1.0
    baseline = _self_vsz_bytes()
    budget = _budget_bytes(config)
    try:
        _soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    except (OSError, ValueError):              # pragma: no cover
        return -1.0
    cap, headroom = _memory_target(baseline, budget, hard)
    try:
        resource.setrlimit(resource.RLIMIT_AS, (cap, hard if hard > 0 else cap))
    except (OSError, ValueError):              # pragma: no cover - no fake guarantee on Windows
        logger.warning("worker: could not set RLIMIT_AS to %.0f MB", cap / 1048576)
        return -1.0
    logger.info(
        "worker memory: baseline %.0f MB + budget %.0f MB → cap %.0f MB (host hard %s), "
        "headroom %.0f MB",
        baseline / 1048576, budget / 1048576, cap / 1048576,
        f"{hard / 1048576:.0f} MB" if hard > 0 else "none", headroom / 1048576,
    )
    if headroom < MIN_HEADROOM_MB * 1024 * 1024:
        logger.warning(
            "worker memory: only %.0f MB left after imports — every file will be refused "
            "until WORKER_MEMORY_MB (or the host's own limit) is raised",
            headroom / 1048576,
        )
    return headroom / 1048576


def _headroom_mb() -> float:
    """Free address space under *our own* cap, in MB (``-1`` when nothing bounds us)."""
    try:
        import resource

        soft, _hard = resource.getrlimit(resource.RLIMIT_AS)
    except (ImportError, OSError, ValueError):  # pragma: no cover
        return -1.0
    if soft <= 0:                               # RLIM_INFINITY: nothing to run into
        return -1.0
    return (soft - _self_vsz_bytes()) / 1048576


def _self_vsz_bytes() -> int:
    """This process's address space, in bytes (0 when unreadable)."""
    return int(_status_mb("/proc/self/status", "VmSize") * 1048576)


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
    hard = _prepare_child()
    # Preserve the caller's validated runtime limits, including per-test overrides.
    from bot import config as configuration

    configuration.settings = config
    armed = False
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
            if not armed:
                # بعد از ایمپورتِ سنگینِ ماژول: سقف روی «پایه + بودجهٔ فایل» بسته می‌شود،
                # وگرنه همان ایمپورت‌ها بودجهٔ فایل را می‌خورند.
                _arm_memory(config)
                armed = True
            headroom = _headroom_mb()
            if 0 <= headroom < MIN_HEADROOM_MB:
                # جملهٔ صریح، به‌جای این‌که یک تخصیصِ تصادفیِ pandas به MemoryError بخورد.
                raise WorkerNoMemory(
                    f"worker has {headroom:.0f} MB of address space free — not enough to "
                    "start a job (raise WORKER_MEMORY_MB or the host's own limit)"
                )
            outcome: tuple[bool, Any] = (True, function(*args, **kwargs))
        except BaseException as exc:
            outcome = (False, exc)
        try:
            connection.send(outcome)
        except Exception:
            # Parent is already gone (timeout/kill); nothing we can do but exit.
            logger.debug("worker: parent closed the pipe before we could send; exiting.")
            return
        finally:
            # Drop the job's references (a 4000×3000 image stays alive otherwise) and
            # hand the free pages back where the allocator allows it.
            del function, args, kwargs, outcome
            with contextlib.suppress(Exception):
                import gc

                gc.collect()


def _status_mb(path: str, key: str) -> float:
    """One ``Vm*`` field of a ``/proc`` status file, in MiB (0.0 when unreadable)."""
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if line.startswith(key + ":"):
                    return float(line.split()[1]) / 1024.0
    except (OSError, ValueError, IndexError):  # pragma: no cover - non-Linux/raced exit
        return 0.0
    return 0.0


def _rss_mb(pid: int | None) -> float:
    """Resident memory of the child in MiB (0 when it cannot be read)."""
    return _status_mb(f"/proc/{pid}/status", "VmRSS") if pid else 0.0


def _vsz_mb(pid: int | None) -> float:
    """Address space of the child in MiB (0 when it cannot be read)."""
    return _status_mb(f"/proc/{pid}/status", "VmSize") if pid else 0.0


def _limit_mb(pid: int | None) -> float:
    """The child's own soft address-space ceiling in MiB (0 = unlimited or unreadable)."""
    if not pid:
        return 0.0
    try:
        with open(f"/proc/{pid}/limits", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("Max address space"):
                    fields = line.split()
                    if len(fields) > 3 and fields[3] != "unlimited":
                        return int(fields[3]) / 1048576
    except (OSError, ValueError, IndexError):  # pragma: no cover - non-Linux
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
            if (
                _load > 1
                and worker.jobs < MAX_JOBS_PER_WORKER
                and worker.rss_mb() < RECYCLE_RSS_MB
                and worker.room_mb() >= MIN_HEADROOM_MB
            ):
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
