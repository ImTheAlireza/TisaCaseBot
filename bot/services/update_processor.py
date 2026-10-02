"""Concurrent between users, strictly ordered within a user's conversations.

Never use bare ``concurrent_updates=True`` with ConversationHandler. Every
update and conversation timeout acquires the same per-user reentrant lock.
Different users can perform I/O concurrently; a user's FSM is still sequential.
"""
from __future__ import annotations

import asyncio
import inspect
import weakref
from collections.abc import Awaitable
from typing import Any

from telegram.ext import BaseUpdateProcessor


class UserLock:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.owner: asyncio.Task[Any] | None = None
        self.depth = 0

    async def __aenter__(self) -> UserLock:
        task = asyncio.current_task()
        if task is not self.owner:
            await self.lock.acquire()
            self.owner = task
        self.depth += 1
        return self

    async def __aexit__(self, *args: Any) -> None:
        self.depth -= 1
        if not self.depth:
            self.owner = None
            self.lock.release()


_locks: weakref.WeakValueDictionary[tuple[asyncio.AbstractEventLoop, int], UserLock] = weakref.WeakValueDictionary()


def lock_for_user(user_id: int) -> UserLock:
    key = asyncio.get_running_loop(), user_id
    lock = _locks.get(key)
    if lock is None:
        lock = UserLock()
        _locks[key] = lock
    return lock


class PerUserUpdateProcessor(BaseUpdateProcessor):
    def __init__(self, max_concurrent_updates: int = 8) -> None:
        super().__init__(max_concurrent_updates)

    async def initialize(self) -> None:
        return

    async def shutdown(self) -> None:
        return

    async def do_process_update(self, update: object, coroutine: Awaitable[Any]) -> None:
        user = getattr(update, "effective_user", None)
        started = False
        try:
            async with lock_for_user(getattr(user, "id", 0)):
                started = True
                await coroutine
        finally:
            if not started and inspect.iscoroutine(coroutine):
                coroutine.close()
