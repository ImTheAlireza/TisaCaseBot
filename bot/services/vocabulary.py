"""The shop's own dictionary — word substitutions applied before anything parses.

Every shop has words the model never learns: a supplier name, a transliteration
the owner prefers, a nickname for a warehouse. Previously each one had to be
encoded either as a regex in ``phone_parser`` or as a learned term correction
that only fires after the owner has corrected the bot once. This is the
human-facing version of the same idea: one JSON file, applied to the caption and
the product info *before* parsing, so the deterministic path and the AI see the
same words.

The file lives next to the other persisted state and is written atomically::

    {"version": 1, "rules": {"پلومریا": "Plumeria"}}
"""
from __future__ import annotations

import logging
import re

from bot.services.jsonstore import checked_write, lock_for, read_json, write_json
from bot.config import data_dir

logger = logging.getLogger(__name__)

DATA_DIR = data_dir()
VOCAB_FILE = DATA_DIR / "vocabulary.json"

_lock = lock_for(VOCAB_FILE)
_CACHE: dict[str, object] = {"rules": None}


def _rules_raw() -> dict[str, str]:
    cached = _CACHE.get("rules")
    if isinstance(cached, dict):
        return cached
    data = read_json(VOCAB_FILE, None)
    raw = data.get("rules") if isinstance(data, dict) else None
    rules: dict[str, str] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            from_word, to_word = str(key).strip(), str(value).strip()
            # A rule that maps a word to itself (or to empty) would loop or
            # delete content; both are worse than ignoring the rule.
            if from_word and to_word and from_word != to_word:
                rules[from_word] = to_word
    _CACHE["rules"] = rules
    return rules


def invalidate() -> None:
    """Drop the cache (tests, and after an external edit of the file)."""
    _CACHE["rules"] = None


def rules() -> dict[str, str]:
    return dict(_rules_raw())


def apply(text: str, changes: list[str] | None = None) -> str:
    """Replace every known word; ``changes`` collects what was rewritten.

    Longest keys first so «قاب پلومریا» wins over «پلومریا». Matching is case
    insensitive for Latin and word-bounded for Latin words, because
    «Iphone15» must not become «Iphone۱۵phone».
    """
    result = text or ""
    for source, target in sorted(_rules_raw().items(), key=lambda kv: -len(kv[0])):
        if source not in result and source.lower() not in result.lower():
            continue
        if re.fullmatch(r"[A-Za-z0-9 +\-]+", source):
            pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(source)}(?![A-Za-z0-9])", re.I)
        else:
            pattern = re.compile(re.escape(source), re.I)
        replaced, count = pattern.subn(target, result)
        if count and replaced != result:
            result = replaced
            if changes is not None:
                changes.append(f"«{source}» → «{target}» ({count} بار)")
    return result


def summary(text: str) -> str:
    """One-line description of the rules that fire on ``text`` (for the preview)."""
    changes: list[str] = []
    apply(text, changes)
    return "، ".join(changes)


def set_rule(source: str, target: str) -> None:
    source, target = str(source).strip(), str(target).strip()
    if not source or not target:
        raise ValueError("هر دو طرف قاعده لازم است")
    data = read_json(VOCAB_FILE, None)
    existing = data.get("rules") if isinstance(data, dict) else None
    rules_map = {str(k): str(v) for k, v in (existing or {}).items()}
    rules_map[source] = target
    with _lock:
        checked_write(VOCAB_FILE, {"version": 1, "rules": rules_map}, write_json)
    invalidate()


def remove_rule(source: str) -> bool:
    data = read_json(VOCAB_FILE, None)
    existing = data.get("rules") if isinstance(data, dict) else None
    rules_map = {str(k): str(v) for k, v in (existing or {}).items()}
    if source not in rules_map:
        return False
    rules_map.pop(source)
    with _lock:
        checked_write(VOCAB_FILE, {"version": 1, "rules": rules_map}, write_json)
    invalidate()
    return True


__all__ = [
    "VOCAB_FILE", "apply", "invalidate", "remove_rule", "rules", "set_rule",
    "summary",
]
