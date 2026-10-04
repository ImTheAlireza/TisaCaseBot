"""Tests for the latency work: fairness, overlapped sends, warm workers, kept connections.

Each test guards a *measured* improvement, not an implementation detail:

* a user's queued taps no longer park the global slots (601 ms → 11 ms measured);
* a tap is answered while its screen update travels (one round-trip less);
* a burst of file jobs reuses its child process (≈0.2–0.4 s per extra item);
* metric writes keep one connection per thread (1.6 ms → 0.03 ms per number).
"""
from __future__ import annotations

import asyncio
import multiprocessing
import os
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from telegram.error import BadRequest

from bot.services import jsonstore, metrics, worker
from bot.services.update_processor import DISPATCH_SLOTS, PerUserUpdateProcessor
from bot.utils.ui import answer_and, answer_and_edit

# --- shared, spawn-safe helpers (a worker child must be able to import these) ----

def _who_am_i(_label: str) -> int:
    """Return the worker child's pid — the test's way of seeing reuse."""
    return os.getpid()


def _hold(seconds: float) -> str:
    time.sleep(seconds)
    return "done"


def _update(user_id: int) -> SimpleNamespace:
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id))


class TestFairUpdateProcessor(unittest.TestCase):
    """A queued tap waits for its own user, not for one of the eight work slots."""

    def test_another_users_update_is_not_starved_by_a_queued_burst(self):
        async def scenario() -> tuple[float, list[int]]:
            processor = PerUserUpdateProcessor(8)
            order: list[int] = []
            started = time.perf_counter()

            async def tap(user_id: int, index: int) -> None:
                order.append(user_id)
                await asyncio.sleep(0.06)

            tasks = [
                asyncio.create_task(processor.process_update(_update(1), tap(1, index)))
                for index in range(8)
            ]
            await asyncio.sleep(0.01)          # user 1's taps are now inside the processor
            waited = time.perf_counter()
            tasks.append(asyncio.create_task(processor.process_update(_update(2), tap(2, 0))))
            await asyncio.gather(*tasks)
            return (waited - started) * 1000, order

        elapsed, order = asyncio.run(scenario())
        # The second user starts long before user 1's eight taps have finished (which
        # takes ~480 ms). Head-of-line blocking used to hold this at ~600 ms.
        self.assertLess(elapsed, 200, f"other user waited {elapsed:.0f} ms")
        self.assertEqual(order.count(1), 8, "every queued tap still runs")
        self.assertEqual(order[0], 1, "the first user's taps run first, in order")

    def test_a_user_stays_sequential_and_the_global_budget_is_kept(self):
        async def scenario() -> tuple[int, list[str]]:
            processor = PerUserUpdateProcessor(2)
            running = 0
            peak = 0
            trace: list[str] = []

            async def job(user_id: int, index: int) -> None:
                nonlocal running, peak
                running += 1
                peak = max(peak, running)
                trace.append(f"{user_id}:{index}")
                await asyncio.sleep(0.02)
                running -= 1

            await asyncio.gather(*(
                processor.process_update(_update(user), job(user, index))
                for user, index in ((1, 0), (1, 1), (2, 0), (3, 0), (4, 0))
            ))
            return peak, trace

        peak, trace = asyncio.run(scenario())
        self.assertEqual(peak, 2, "the run budget, not the dispatch slots, caps the work")
        self.assertEqual([item for item in trace if item.startswith("1:")], ["1:0", "1:1"])

    def test_the_dispatch_slots_are_deliberately_generous(self):
        processor = PerUserUpdateProcessor(8)
        self.assertEqual(processor.max_concurrent_updates, DISPATCH_SLOTS)


class TestOverlappedTap(unittest.TestCase):
    """answerCallbackQuery and the screen update travel at the same time."""

    def _query(self, seen: list[tuple[str, float]], *, edit_error: Exception | None = None,
               answer_error: Exception | None = None):
        async def answer(text=None, **kwargs):
            seen.append(("answer", time.perf_counter()))
            await asyncio.sleep(0.03)
            if answer_error:
                raise answer_error

        async def edit_message_text(text, **kwargs):
            seen.append(("edit", time.perf_counter()))
            await asyncio.sleep(0.03)
            if edit_error:
                raise edit_error
            return SimpleNamespace(message_id=1)

        return SimpleNamespace(answer=answer, edit_message_text=edit_message_text)

    def test_answer_goes_out_first_and_both_travel_together(self):
        async def scenario():
            seen: list[tuple[str, float]] = []
            query = self._query(seen)
            started = time.perf_counter()
            await answer_and_edit(query, "متن")
            return (time.perf_counter() - started) * 1000, seen

        elapsed, seen = asyncio.run(scenario())
        self.assertEqual([kind for kind, _ in seen], ["answer", "edit"], "answer, then edit")
        # Sequential would be ≈60 ms (two 30 ms round-trips); overlapped is ≈30 ms.
        self.assertLess(elapsed, 50, f"the two calls were not overlapped ({elapsed:.0f} ms)")

    def test_a_failed_edit_still_leaves_the_tap_answered(self):
        async def scenario():
            seen: list[tuple[str, float]] = []
            query = self._query(seen, edit_error=BadRequest("message is not modified"))
            with self.assertRaises(BadRequest):
                await answer_and_edit(query, "متن")
            return seen

        seen = asyncio.run(scenario())
        self.assertIn("answer", [kind for kind, _ in seen])

    def test_quiet_swallows_only_the_unchanged_message_error(self):
        async def scenario():
            return await answer_and_edit(self._query([]), "متن", quiet=True)

        async def unchanged():
            query = self._query([], edit_error=BadRequest("Message is not modified"))
            return await answer_and_edit(query, "متن", quiet=True)

        result = asyncio.run(scenario())
        self.assertTrue(result, "a replaced screen reports True")
        self.assertFalse(asyncio.run(unchanged()), "an unchanged screen reports False")

        async def raising():
            query = self._query([], edit_error=BadRequest("chat not found"))
            return await answer_and_edit(query, "متن", quiet=True)

        with self.assertRaises(BadRequest):
            asyncio.run(raising())

    def test_plain_work_can_be_overlapped_too(self):
        async def scenario():
            seen: list[str] = []

            async def work():
                seen.append("work")
                await asyncio.sleep(0.01)
                return 7

            query = SimpleNamespace(answer=lambda **kwargs: asyncio.sleep(0))
            value = await answer_and(query, work())
            return value, seen

        value, seen = asyncio.run(scenario())
        self.assertEqual((value, seen), (7, ["work"]))


class TestWarmWorkers(unittest.TestCase):
    """A burst of file jobs reuses its child; a lone job leaves nothing behind."""

    def _children(self) -> list[str]:
        return [process.name for process in multiprocessing.active_children()]

    def test_a_burst_of_jobs_reuses_one_worker_process(self):
        original = worker.MAX_WORKERS
        worker.MAX_WORKERS = 1
        try:
            async def scenario():
                async def one():
                    return await worker.run(_who_am_i, "x", timeout=60)

                return await asyncio.gather(one(), one())

            first, second = asyncio.run(scenario())
        finally:
            worker.MAX_WORKERS = original
        self.assertEqual(first, second, "the second job should reuse the warm child")
        self.assertEqual(self._children(), [], "no worker process may stay resident")

    def test_a_worker_is_recycled_after_its_job_budget(self):
        original, jobs = worker.MAX_JOBS_PER_WORKER, worker.MAX_WORKERS
        worker.MAX_WORKERS, worker.MAX_JOBS_PER_WORKER = 1, 1
        try:
            async def scenario():
                async def one():
                    return await worker.run(_who_am_i, "x", timeout=60)

                return await asyncio.gather(one(), one())

            first, second = asyncio.run(scenario())
        finally:
            worker.MAX_WORKERS, worker.MAX_JOBS_PER_WORKER = original, jobs
        self.assertNotEqual(first, second, "a spent child must be replaced, not reused")

    def test_a_lone_job_leaves_no_idle_process(self):
        result = asyncio.run(worker.run(_who_am_i, "x", timeout=60))
        self.assertIsInstance(result, int)
        self.assertEqual(self._children(), [])

    def test_a_timeout_kills_the_child_and_the_next_job_still_works(self):
        async def scenario():
            with self.assertRaises(TimeoutError):
                await worker.run(_hold, 2.0, timeout=0.15)
            return await worker.run(_who_am_i, "x", timeout=60)

        pid = asyncio.run(scenario())
        self.assertIsInstance(pid, int)
        self.assertEqual(self._children(), [])


class TestKeptMetricsConnection(unittest.TestCase):
    """One connection per thread: the write path no longer reopens SQLite per number."""

    def setUp(self) -> None:
        metrics.close()
        self.addCleanup(metrics.close)

    def test_the_same_connection_serves_many_counters(self):
        for _ in range(5):
            metrics.incr("ai_calls")
        first = metrics._local.connection
        metrics.incr("ai_calls")
        self.assertIs(metrics._local.connection, first, "the connection must be reused")
        self.assertGreaterEqual(metrics.snapshot().get("ai_calls", (0,))[0], 6)

    def test_a_swapped_database_is_not_written_into(self):
        import tempfile

        original = metrics.DB_PATH
        first = Path(tempfile.mkdtemp(prefix="metrics-a-")) / "metrics.sqlite3"
        second = Path(tempfile.mkdtemp(prefix="metrics-b-")) / "metrics.sqlite3"
        try:
            metrics.DB_PATH = first
            metrics.incr("ai_calls")
            metrics.DB_PATH = second
            metrics.incr("ai_calls")
            rows = metrics.snapshot()
            self.assertEqual(rows.get("ai_calls", (0,))[0], 1, "the new file starts at one")
            metrics.close()
            metrics.DB_PATH = first
            self.assertEqual(metrics.snapshot().get("ai_calls", (0,))[0], 1)
        finally:
            metrics.DB_PATH = original


class TestAsyncStateWrites(unittest.TestCase):
    """``*_async`` wrappers keep the durability contract and run off the loop."""

    def test_a_write_off_the_loop_lands_on_disk(self):
        import tempfile

        path = Path(tempfile.mkdtemp(prefix="state-async-")) / "state.json"
        self.assertTrue(asyncio.run(jsonstore.write_json_async(path, {"a": 1})))
        self.assertEqual(jsonstore.read_json(path), {"a": 1})

    def test_a_failed_critical_write_still_raises(self):
        import tempfile

        path = Path(tempfile.mkdtemp(prefix="state-async-")) / "state.json"
        with self.assertRaises(jsonstore.StateWriteError):
            asyncio.run(jsonstore.checked_write_async(path, {"a": 1}, lambda *_args: False))


if __name__ == "__main__":
    unittest.main()
