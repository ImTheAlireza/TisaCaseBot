"""One active flow per user: starting something new closes what was open.

Two half-finished flows in one chat were a real failure mode — a product with
three images from a *different* product, or an abandoned compression workspace
eating /tmp. The user's mental model is obviously «one thing at a time»; the
code just never enforced it.

The registry closes both each flow's work and PTB's conversation tracker through
our tested PTB 21 adapter. Otherwise an abandoned flow still consumes the next
message even after its session dictionary was cleared.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)

#: flow name -> (label the user reads, closer returning True when it closed something)
_closers: dict[str, tuple[str, Callable[[int], bool]]] = {}


def register(name: str, label: str, closer: Callable[[int], bool]) -> None:
    """Declare that ``name`` can be closed for a user. Idempotent per module."""
    _closers[name] = (label, closer)


def close_others(name: str, user_id: int) -> list[str]:
    """Close every *other* flow of ``user_id``; return the labels that were open.

    A closer must be cheap, idempotent and safe to call when nothing is open —
    it runs on the entry path of a flow, where raising is not an option.
    """
    from bot.services.conversations import close_for_user

    framework_closed = close_for_user(user_id, except_flow=name)
    closed: list[str] = []
    for other, (label, closer) in list(_closers.items()):
        if other == name:
            continue
        try:
            if closer(user_id) or other in framework_closed:
                closed.append(label)
        except Exception:                          # pragma: no cover - defensive
            logger.exception("flow_guard: closing %s for user %s failed", other, user_id)
    return closed


def registered() -> list[str]:
    return sorted(_closers)


__all__ = ["close_others", "register", "registered"]
