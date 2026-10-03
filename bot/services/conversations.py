"""PTB 21 adapter for reliably ending a different flow's conversation.

PTB 21 has no public end-for-user API. Keep its small, version-specific state
access here (not in feature modules), and exercise it through real dispatch
regression tests. All bot conversations use per_user=True/per_message=False.
"""
from __future__ import annotations

import weakref
from typing import Any

from telegram.ext import ConversationHandler

from bot.services.update_processor import lock_for_user

_registry: list[weakref.ReferenceType[FlowConversationHandler]] = []


class FlowConversationHandler(ConversationHandler):
    __slots__ = ("__weakref__", "flow_name")

    def __init__(self, *args: Any, flow: str, **kwargs: Any) -> None:
        self.flow_name = flow
        kwargs.setdefault("allow_reentry", True)
        super().__init__(*args, **kwargs)
        if not self.per_user or self.per_message:
            raise ValueError("Managed flows require per_user=True and per_message=False")
        _registry.append(weakref.ref(self))

    def end_for_user(self, user_id: int) -> bool:
        closed = False
        user_index = 1 if self.per_chat else 0
        for key in list(self._conversations):
            if key[user_index] != user_id:
                continue
            # Remove the timeout too: it must not clean up a newly opened flow.
            job = self.timeout_jobs.pop(key, None)
            if job is not None:
                job.schedule_removal()
            self._update_state(self.END, key)
            closed = True
        return closed


    async def _trigger_timeout(self, context: Any) -> None:
        # Jobs are not processed by Application's update processor. Serialize
        # them too, so an old timeout cannot erase a newly opened conversation.
        data = context.job.data if context.job is not None else None
        key = getattr(data, "conversation_key", ())
        index = 1 if self.per_chat else 0
        if len(key) <= index:
            return
        async with lock_for_user(int(key[index])):
            await super()._trigger_timeout(context)


def close_for_user(user_id: int, *, except_flow: str = "") -> set[str]:
    closed: set[str] = set()
    live: list[weakref.ReferenceType[FlowConversationHandler]] = []
    for reference in _registry:
        handler = reference()
        if handler is None:
            continue
        live.append(reference)
        if handler.flow_name != except_flow and handler.end_for_user(user_id):
            closed.add(handler.flow_name)
    _registry[:] = live
    return closed
