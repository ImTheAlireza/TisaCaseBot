"""Runtime-persisted sudo preferences (stored in data/preferences.json).

Holds the per-button visibility flags the sudo owner flips from inside the bot
(«⚙️ تنظیمات» screen), so they survive restarts without touching .env. A missing
key means the button is visible (default on). Reads are cached and writes are
atomic — see :mod:`bot.services.jsonstore`.
"""

from __future__ import annotations

import logging

from bot.services.jsonstore import checked_write, lock_for, read_json, write_json
from bot.config import data_dir

logger = logging.getLogger(__name__)

# data/ is git-ignored on purpose — runtime-managed state lives here.
DATA_DIR = data_dir()
FILE = DATA_DIR / "preferences.json"

_lock = lock_for(FILE)


def _load() -> dict:
    """Return stored preferences as ``{"button_visibility": {key: bool}}``."""
    data = read_json(FILE, None)
    visibility: dict[str, bool] = {}
    if isinstance(data, dict):
        # Migrate the older single compress flag (pre-registry layout).
        legacy = data.get("show_compress_to_admins")
        if isinstance(legacy, bool):
            visibility.setdefault("compress", legacy)
        stored = data.get("button_visibility")
        if isinstance(stored, dict):
            for key, value in stored.items():
                if isinstance(key, str) and isinstance(value, bool):
                    visibility[key] = value
    return {"button_visibility": visibility}


def button_visible(key: str) -> bool:
    """Whether the button ``key`` is currently visible (default True)."""
    return _load()["button_visibility"].get(key, True)


def set_button_visible(key: str, value: bool) -> None:
    """Persist the visibility flag for button ``key``."""
    with _lock:
        data = _load()
        data["button_visibility"][key] = bool(value)
        checked_write(FILE, data, write_json)


__all__ = ["button_visible", "set_button_visible"]
