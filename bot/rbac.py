"""Role-Based Access Control (RBAC) for the bot.

Two roles:

* **sudo**  — the bot owner, set once in ``.env`` via ``SUDO_IDS``. Full access.
* **admin** — people added by sudo from the «👥 مدیریت ادمین‌ها» screen. What they
  may use is decided per button in «⚙️ تنظیمات» (see :mod:`bot.buttons`).

Everyone else is an ordinary ``user`` with no access at all. Sudo and admins are
the only roles allowed to use the bot; everything lives in private chats (see
bot/app.py), so a non-sudo/non-admin who reaches the bot is blocked at every
entry point.

Admins are persisted to ``data/roles.json`` (git-ignored) so they survive
restarts. Sudo is NOT stored there — it always comes from the environment, so
sudo can never be removed by a misclicked button. Writes go through
:mod:`bot.services.jsonstore` (atomic + ``.bak``): the restart button can fire at
any moment, and a half-written roles.json used to mean «no admins left».

Pending invites
---------------
An admin can also be created from a *typed numeric id*, which is a privilege
grant based on nothing but digits. Such records are stored as
``pending`` with a short code and only become real admins when that person
themselves sends ``/start <code>`` in a private chat. A guessed or mistyped id
therefore cannot open anything, and the owner sees exactly what is waiting.
"""

from __future__ import annotations

import logging
import math
import secrets
import time

from bot.config import settings
from bot.services.jsonstore import checked_write, lock_for, read_json, write_json
from bot.config import data_dir

logger = logging.getLogger(__name__)

# data/ is git-ignored on purpose — runtime-managed state lives here.
DATA_DIR = data_dir()
ROLES_FILE = DATA_DIR / "roles.json"

_lock = lock_for(ROLES_FILE)

SUDO = "sudo"
ADMIN = "admin"
USER = "user"

INVITE_TTL_SECONDS = 24 * 3600


# --- Roles -------------------------------------------------------------------

def is_sudo(user_id: int | None) -> bool:
    """Sudo list is authoritative from .env — it cannot be edited at runtime."""
    return bool(user_id) and user_id in settings.sudo_ids


def sudo_ids() -> frozenset[int]:
    """The sudo (owner) Telegram IDs from .env — never stored in roles.json."""
    return settings.sudo_ids


# --- Storage -------------------------------------------------------------------

def _load() -> dict:
    """Return the stored ``{"admins": {id: {...}}}`` dict (empty if missing)."""
    data = read_json(ROLES_FILE, None, recover=False)
    if not isinstance(data, dict):
        return {"admins": {}}
    admins = data.get("admins")
    if not isinstance(admins, dict):
        data["admins"] = {}
    return data


def _save(data: dict) -> None:
    with _lock:
        checked_write(ROLES_FILE, data, write_json)


# --- Admins -------------------------------------------------------------------

def admins() -> dict:
    """Confirmed admins as ``{str(id): record}`` (pending invites excluded)."""
    return {
        str(uid): dict(record or {})
        for uid, record in _load()["admins"].items()
        if isinstance(record, dict) and not record.get("pending")
    }


def pending_invites() -> dict:
    """Invites that were created but not yet accepted by the person itself."""
    now = time.time()
    out: dict[str, dict] = {}
    for uid, record in _load()["admins"].items():
        if isinstance(record, dict) and record.get("pending"):
            if not _invite_current(record, now):
                continue
            out[str(uid)] = dict(record)
    return out


def is_admin(user_id: int | None) -> bool:
    if not user_id:
        return False
    record = _load()["admins"].get(str(user_id))
    return isinstance(record, dict) and not record.get("pending")


def role(user_id: int | None) -> str:
    if is_sudo(user_id):
        return SUDO
    if is_admin(user_id):
        return ADMIN
    return USER


def is_allowed(user_id: int | None) -> bool:
    """Only sudo and (confirmed) admins may use the bot at all."""
    return role(user_id) in (SUDO, ADMIN)


def add_admin(user_id: int, *, name: str = "", added_by: int | None = None) -> None:
    """Persist a new, already-confirmed admin. Safe to call repeatedly."""
    with _lock:
        data = _load()
        data["admins"][str(user_id)] = {
            "name": name,
            "added_by": added_by,
            "ts": time.time(),
        }
        checked_write(ROLES_FILE, data, write_json)
    logger.info("Admin added: user %s (%s) by %s", user_id, name, added_by)


def issue_invite(user_id: int, *, name: str = "", added_by: int | None = None) -> str:
    """Create a pending admin + invite code; returns the code to hand over."""
    code = secrets.token_hex(4).upper()
    with _lock:
        data = _load()
        record = data["admins"].get(str(user_id)) or {}
        record.update({
            "name": name or record.get("name", ""),
            "added_by": added_by,
            "ts": time.time(),
            "pending": True,
            "code": code,
        })
        data["admins"][str(user_id)] = record
        checked_write(ROLES_FILE, data, write_json)
    logger.info("Admin invite issued for user %s by %s", user_id, added_by)
    return code


def _invite_current(record: dict, now: float) -> bool:
    try:
        issued = float(record.get("ts", 0))
    except (TypeError, ValueError):
        return False
    return math.isfinite(issued) and 0 <= now - issued < INVITE_TTL_SECONDS


def confirm_invite(user_id: int, code: str) -> bool:
    """Activate a pending admin when that user presents the right code."""
    code = (code or "").strip().upper()
    if not code:
        return False
    with _lock:
        data = _load()
        record = data["admins"].get(str(user_id))
        if not isinstance(record, dict) or not record.get("pending"):
            return False
        if not _invite_current(record, time.time()):
            return False
        if not secrets.compare_digest(str(record.get("code", "")).upper(), code):
            return False
        record.pop("pending", None)
        record.pop("code", None)
        record["accepted_at"] = time.time()
        data["admins"][str(user_id)] = record
        checked_write(ROLES_FILE, data, write_json)
    logger.info("Admin invite accepted by user %s", user_id)
    return True


def cancel_invite(user_id: int) -> bool:
    """Drop a pending invite (the admin record itself is kept if it exists)."""
    with _lock:
        data = _load()
        record = data["admins"].get(str(user_id))
        if not isinstance(record, dict) or not record.get("pending"):
            return False
        data["admins"].pop(str(user_id), None)
        checked_write(ROLES_FILE, data, write_json)
    logger.info("Admin invite cancelled for user %s", user_id)
    return True


def remove_admin(user_id: int) -> bool:
    """Remove an admin (or its pending invite). True if something was removed."""
    with _lock:
        data = _load()
        removed = data["admins"].pop(str(user_id), None) is not None
        if removed:
            checked_write(ROLES_FILE, data, write_json)
    if removed:
        logger.info("Admin removed: user %s", user_id)
    return removed


__all__ = [
    "ADMIN",
    "SUDO",
    "USER",
    "add_admin",
    "admins",
    "cancel_invite",
    "confirm_invite",
    "is_admin",
    "is_allowed",
    "is_sudo",
    "pending_invites",
    "remove_admin",
    "role",
    "sudo_ids",
]
