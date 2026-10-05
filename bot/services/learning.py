"""Self-learning memory for the owner's corrections.

When the bot misreads something and the owner fixes it, the fix should not be
lost with the session — and it should not be memorized as a one-off either.
This module turns a correction into the *narrowest rule that still generalizes*:

``price_scale``
    A bare price was off by a power of ten: ``1098`` was read as 1,098 and
    corrected to 1,098,000, so a bare 4-digit amount means "thousands".
    Next time ``1298`` is read as 1,298,000 with no help.

``term``
    One word was replaced by another (a color, a brand spelling, a title word):
    «سلفی» -> «مشکی». Applied to attribute values and title tokens later.

Everything else is still recorded in ``corrections`` (a bounded log) so the
owner can see what the bot was told, even when no safe rule could be inferred.

**A learned rule is a proposal, not a law.** One generalization rewrites every
later product, and the mistake that hurts is the one that is silent: a term rule
that merges two colors quietly deletes a variation from the catalog and nothing
in the chat says so. So a fresh rule is saved as ``pending`` and changes nothing
until the owner confirms it, and the confirmation message states what the rule
*would have done* to the recent products — see :mod:`bot.services.learning_impact`,
which replays the rule over the corpus in ``data/learning_corpus.json``.

A rule also carries where it should apply (``scope``): shop-wide, or only when a
word (the category it was learned in) appears in the text being parsed.

Rules live in ``data/learned.json`` (git-ignored, like ``roles.json``) and are
managed from the «🧠 یادگیری» screen, because a memory that cannot be edited is
a liability.

Storage format ``version: 2``. A ``version: 1`` file is read as before with two
decisions: its rules are already trusted (``active`` — the owner has been relying
on them) and their scope is the whole shop.
"""
from __future__ import annotations

import hashlib
import logging
import copy
import re
import threading
import time
from collections import deque as _deque
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from dataclasses import field as _dc_field
from typing import Any
from bot.config import data_dir

logger = logging.getLogger(__name__)

# Runtime state lives in the repo-root data/ directory, next to rbac's
# roles.json — both are sudo-managed and git-ignored. (parents[2] because this
# module sits two levels below the root, in bot/services/.)
DATA_DIR = data_dir()
LEARNED_FILE = DATA_DIR / "learned.json"

_lock = threading.Lock()

# Guards: a learned rule must never be able to produce an absurd price.
_MAX_SCALED_PRICE = 10 ** 12
_MAX_RULES = 200
_MAX_CORRECTIONS = 300
# How many "this rule actually fired here" examples one rule remembers. They are
# a courtesy for the owner reading the screen, not a ledger, so the list stays
# small and bounded (the file must not grow with the shop).
_MAX_APPLIED = 5

MEMORY_VERSION = 2

# The rule lifecycle. ``pending`` is the default for anything newly inferred:
# a rule the owner has not confirmed must not be able to change a product.
STATUS_PENDING = "pending"
STATUS_ACTIVE = "active"
STATUS_DISABLED = "disabled"
STATUSES = (STATUS_PENDING, STATUS_ACTIVE, STATUS_DISABLED)
STATUS_LABELS = {
    STATUS_PENDING: "⏳ در انتظار تأیید تو",
    STATUS_ACTIVE: "✅ فعال",
    STATUS_DISABLED: "⏸ غیرفعال",
}

# A rule applies shop-wide, or only in the context it was learned from.
SCOPE_SHOP = "shop"
SCOPE_WORD = "word:"          # scope == SCOPE_WORD + the keyword

_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_ZWNJ = "\u200c"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

def fold(text: str) -> str:
    """Digits, case and ZWNJ normalized — enough to compare a keyword to a text.

    Deliberately not ``color_matrix.normalize_text``: this module is imported by
    the money parser, which the matrix itself uses, so the fold must stay local.
    """
    out = (text or "").translate(_DIGITS).replace(_ZWNJ, "").casefold()
    return re.sub(r"\s+", " ", out).strip()


@dataclass
class Rule:
    """One learned generalization, plus whether it may be trusted yet."""

    kind: str                      # "price_scale" | "term"
    key: str                       # digits (price_scale) or the wrong term
    value: str                     # multiplier (price_scale) or the correct term
    example: str = ""              # the correction it was learned from
    hits: int = 0                  # how often it has been applied since
    created: float = _dc_field(default_factory=time.time)
    status: str = STATUS_PENDING   # pending | active | disabled
    scope: str = SCOPE_SHOP        # shop | word:<keyword>
    origin: str = ""               # the product/category it was learned from
    applied_to: list[dict[str, str]] = _dc_field(default_factory=list)

    @property
    def rule_id(self) -> str:
        return f"{self.kind}:{self.key}"

    @property
    def is_active(self) -> bool:
        return self.status == STATUS_ACTIVE

    @property
    def keyword(self) -> str:
        """The word this rule is limited to, or "" when it is shop-wide."""
        if self.scope.startswith(SCOPE_WORD):
            return self.scope[len(SCOPE_WORD):]
        return ""

    def matches(self, where: str = "") -> bool:
        """Does this rule apply to the text being parsed?

        A shop-wide rule always applies. A scoped rule fires only when its
        keyword is present in ``where`` — and when the caller has no context at
        all (empty ``where``) the text the rule would edit is used, so a term
        rule never misses because the caller forgot to pass a product text.
        """
        keyword = self.keyword
        if not keyword:
            return True
        return fold(keyword) in fold(where or "")

    def status_line(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    def scope_line(self) -> str:
        keyword = self.keyword
        return f"فقط وقتی «{keyword}» در متن باشد" if keyword else "همهٔ فروشگاه"

    def describe(self) -> str:
        if self.kind == "price_scale":
            body = (
                f"💰 عدد {self.key} رقمیِ بدون پسوند = ×{int(self.value):,} "
                f"(نمونه: {self.example})"
            )
        else:
            body = f"🔤 «{self.key}» ← «{self.value}» (نمونه: {self.example})"
        if self.keyword:
            body += f" — فقط برای «{self.keyword}»"
        return body


def _examples_of(rule: Rule) -> list[dict[str, str]]:
    """Disk examples plus the ones only queued in memory (flushed on next write)."""
    queued = [item for key, item in _APPLIED_QUEUE if key == rule.rule_id]
    return (list(rule.applied_to) + queued)[-_MAX_APPLIED:]


@dataclass
class Correction:
    """A raw record of one correction, kept even when no rule was inferred."""

    field: str
    old: str
    new: str
    rule: str = ""                 # rule_id when a rule was learned from it
    created: float = _dc_field(default_factory=time.time)

    def describe(self) -> str:
        label = {
            "price": "قیمت", "prices": "قیمت گروهی", "title": "عنوان",
            "sku_prefix": "پیشوند SKU", "attributes": "ویژگی",
            "learning": "تأیید/غیرفعال‌سازی قاعده",
        }.get(self.field, self.field)
        arrow = f" ← قاعده: {self.rule}" if self.rule else " (بدون قاعدهٔ قابل تعمیم)"
        return f"{label}: «{self.old}» ← «{self.new}»{arrow}"


@dataclass
class Memory:
    rules: dict[str, Rule] = _dc_field(default_factory=dict)
    corrections: list[Correction] = _dc_field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "version": MEMORY_VERSION,
            "rules": [asdict(rule) for rule in self.rules.values()],
            "corrections": [asdict(item) for item in self.corrections],
        }

    @classmethod
    def from_json(cls, data: Any) -> Memory:
        memory = cls()
        if not isinstance(data, dict):
            return memory
        for item in data.get("rules") or []:
            if not isinstance(item, dict):
                continue
            try:
                applied = [
                    {str(k): str(v) for k, v in entry.items()}
                    for entry in item.get("applied_to") or []
                    if isinstance(entry, dict)
                ][-_MAX_APPLIED:]
                rule = Rule(
                    kind=str(item.get("kind", "")),
                    key=str(item.get("key", "")),
                    value=str(item.get("value", "")),
                    example=str(item.get("example", "")),
                    hits=int(item.get("hits", 0) or 0),
                    created=float(item.get("created", 0) or time.time()),
                    # A v1 file has no status: those rules were live and the
                    # owner has been relying on them, so the migration keeps
                    # them live instead of silently switching them off.
                    status=str(item.get("status") or STATUS_ACTIVE),
                    scope=str(item.get("scope") or SCOPE_SHOP),
                    origin=str(item.get("origin") or ""),
                    applied_to=applied,
                )
            except (TypeError, ValueError):
                continue
            if rule.status not in STATUSES:
                rule.status = STATUS_ACTIVE
            if rule.kind and rule.key and rule.value:
                memory.rules[rule.rule_id] = rule
        for item in data.get("corrections") or []:
            if not isinstance(item, dict):
                continue
            memory.corrections.append(
                Correction(
                    field=str(item.get("field", "")),
                    old=str(item.get("old", "")),
                    new=str(item.get("new", "")),
                    rule=str(item.get("rule", "")),
                    created=float(item.get("created", 0) or time.time()),
                )
            )
        return memory


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _read_disk() -> Memory:
    from bot.services.jsonstore import read_json

    data = read_json(LEARNED_FILE, None)
    if data is None:
        return Memory()
    return Memory.from_json(data)


# ``price_multiplier`` and ``apply_terms`` run inside the price parser, i.e. once
# per line of every product — they must not touch the disk each time. The cache
# is keyed on (path, mtime) so an external edit, or a test monkeypatching
# LEARNED_FILE, invalidates it automatically.
_CACHE: Memory | None = None
_CACHE_KEY: tuple[str, float] | None = None


def load() -> Memory:
    """An isolated snapshot; only a durable ``_write`` changes the shared cache."""
    global _CACHE, _CACHE_KEY
    try:
        mtime = LEARNED_FILE.stat().st_mtime if LEARNED_FILE.exists() else -1.0
    except OSError:
        mtime = -1.0
    key = (str(LEARNED_FILE), mtime)
    if _CACHE is None or key != _CACHE_KEY:
        _CACHE = _read_disk()
        _CACHE_KEY = key
    return copy.deepcopy(_CACHE)


def _write(memory: Memory) -> None:
    """Persist atomically and refresh the cache so the next ``load`` is current.

    The queued application examples are written here on purpose: recording that
    «this rule fired on that product» happens on the parsing path, where a disk
    write per product would be absurd. At most the examples learned since the
    last write are lost by a crash — nothing that changes behaviour.
    """
    global _CACHE, _CACHE_KEY
    from bot.services.jsonstore import checked_write, write_json

    _flush_examples(memory)
    try:
        checked_write(LEARNED_FILE, memory.to_json(), write_json)
    except OSError:
        _CACHE = None
        _CACHE_KEY = None
        raise
    try:
        mtime = LEARNED_FILE.stat().st_mtime
    except OSError:
        mtime = -1.0
    _CACHE = copy.deepcopy(memory)
    _CACHE_KEY = (str(LEARNED_FILE), mtime)


def _put_rule(memory: Memory, rule: Rule) -> bool:
    """Insert/refresh a rule. Returns True when something changed.

    Re-learning the same mapping keeps the trust the owner already gave it, but a
    rule whose *meaning* changed (a different replacement, a different
    multiplier) is a different promise and has to be confirmed again.
    """
    existing = memory.rules.get(rule.rule_id)
    if existing and existing.value == rule.value and existing.example == rule.example:
        return False
    if existing:
        rule.hits = existing.hits          # keep the usage counter
        rule.created = existing.created
        rule.applied_to = existing.applied_to
        rule.scope = existing.scope
        rule.origin = existing.origin or rule.origin
        if existing.value == rule.value:
            rule.status = existing.status
    memory.rules[rule.rule_id] = rule
    # Bound the store: drop the oldest low-usage rules first.
    if len(memory.rules) > _MAX_RULES:
        oldest = sorted(memory.rules.values(), key=lambda r: (r.hits, r.created))
        for stale in oldest[: len(memory.rules) - _MAX_RULES]:
            memory.rules.pop(stale.rule_id, None)
    return True


def _log_correction(memory: Memory, correction: Correction) -> None:
    memory.corrections.append(correction)
    del memory.corrections[:-_MAX_CORRECTIONS]


# ---------------------------------------------------------------------------
# Rule application
# ---------------------------------------------------------------------------

def price_multiplier(raw_digits: int, has_suffix: bool, *, where: str = "") -> int:
    """The learned thousands-multiplier for a bare amount, else 1.

    Only bare numbers are scaled: an explicit suffix («۱۰۹۸ هزار», «1098t») is
    already unambiguous and must not be scaled twice.

    ``where`` is the line (or product text) the amount was read from; a rule the
    owner limited to one category is only honoured inside it.
    """
    if has_suffix or raw_digits <= 0 or _suspended():
        return 1
    with _lock:
        rule = load().rules.get(f"price_scale:{raw_digits}")
        if not rule or not rule.is_active or not rule.matches(where):
            return 1
        try:
            multiplier = int(rule.value)
        except ValueError:
            return 1
        if multiplier <= 1:
            return 1
        # Hits are a usage stat, not state anyone branches on: they stay in the
        # in-memory cache and reach disk with the next rule change. Writing here
        # would mean one file write per parsed price line.
        rule.hits += 1
    return multiplier


def scaled_price_is_sane(value: int) -> bool:
    return 0 < value <= _MAX_SCALED_PRICE


# A learned term must never edit the inside of a longer word. Persian has no
# word characters for ZWNJ-joined forms, so whitespace/punctuation edges are
# matched explicitly.
_TERM_EDGE = r"[\w\u0600-\u06FF]"


def replace_term(text: str, wrong: str, right: str) -> tuple[str, int]:
    """Replace one term in ``text``; returns the new text and how often it fired.

    Shared by :func:`apply_terms` and by the impact replay: a preview that used
    its own matching would be predicting a different rule than the one being
    activated.
    """
    if not text or not wrong:
        return text, 0
    pattern = re.compile(
        r"(?<!" + _TERM_EDGE + r")" + re.escape(wrong) + r"(?!" + _TERM_EDGE + r")"
    )
    return pattern.subn(right, text)


def apply_terms(
    text: str, *, where: str = "", fired: list[str] | None = None
) -> str:
    """Replace learned wrong terms with their corrections inside ``text``.

    Disabled and unconfirmed rules never rewrite anything, and a rule the owner
    limited to one category is skipped when ``where`` (the product text) is about
    something else.

    ``fired``, when a caller passes a list, receives the ids of the rules that
    actually changed the text — the caller is the only place that knows which
    product it was, which is what the memory screen lists as examples.
    """
    if not text or _suspended():
        return text
    with _lock:
        terms = [
            (rule.key, rule.value)
            for rule in load().rules.values()
            if rule.kind == "term" and rule.is_active and rule.matches(where or text)
        ]
    if not terms:
        return text
    out = text
    matched: list[str] = []
    for wrong, right in terms:
        out, count = replace_term(out, wrong, right)
        if count:
            matched.append(wrong)
            if fired is not None:
                fired.append(f"term:{wrong}")
    if matched:
        # Same reasoning as price_multiplier: a usage counter is not worth a
        # disk write on the hot path.
        with _lock:
            memory = load()
            for wrong in matched:
                rule = memory.rules.get(f"term:{wrong}")
                if rule:
                    rule.hits += 1
    return out


def apply_terms_to_values(
    values: list[str], *, where: str = "", fired: list[str] | None = None
) -> list[str]:
    """Apply learned term rules to a list of attribute values, de-duplicated.

    De-duplication after substitution is what makes a *collapsing* rule
    dangerous — «سبز» → «سفید» would merge two color axes into one and silently
    remove a variation — which is exactly why a term rule is only ever activated
    after its impact on the recent products has been shown to the owner.
    """
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        fixed = apply_terms(str(value), where=where, fired=fired)
        if fixed and fixed not in seen:
            seen.add(fixed)
            out.append(fixed)
    return out


def short_id(rule_id: str) -> str:
    """A short, stable, callback-safe handle for a rule.

    Telegram caps ``callback_data`` at 64 bytes, and a term rule's id embeds an
    arbitrary Persian word (2 bytes per character in UTF-8), so the id itself
    cannot go on the button. A 4-byte digest is 8 ASCII characters and, with at
    most ``_MAX_RULES`` rules in play, a collision is not a practical concern —
    and a miss simply means the button reports "not found" instead of deleting
    the wrong rule.
    """
    return hashlib.blake2b(rule_id.encode("utf-8"), digest_size=4).hexdigest()


def rule_by_short_id(sid: str) -> Rule | None:
    with _lock:
        for rule in load().rules.values():
            if short_id(rule.rule_id) == sid:
                return rule
    return None


def revision() -> int:
    """Cheap token that changes whenever the rule set changes.

    The product flow caches an extraction by text; without folding the rules into
    that key, learning a rule would not affect an already-extracted product until
    its text changed by accident. Confirming, disabling or re-scoping a rule
    changes what parsing does too, so the *state* is part of the token as well.
    """
    with _lock:
        memory = load()
        return (
            len(memory.rules)
            + sum(rule.hits for rule in memory.rules.values())
            + sum(len(rule.status) + len(rule.scope) for rule in memory.rules.values())
        )


def rules_sorted() -> list[Rule]:
    with _lock:
        return sorted(load().rules.values(), key=lambda item: (item.kind, item.created))


# ---------------------------------------------------------------------------
# Lifecycle: propose -> preview -> confirm
# ---------------------------------------------------------------------------

# Application examples are collected while parsing (no disk write on that path)
# and reach the file with the next rule change. A bounded deque keeps memory sane
# even if the owner never opens the learning screen again; at most the recent
# examples are lost by a crash, which changes no behaviour.
_APPLIED_QUEUE: _deque[tuple[str, dict[str, str]]] = _deque(maxlen=64)


def note_application(rule_id: str, *, title: str = "", effect: str = "") -> None:
    """Remember where a rule actually fired, for the owner to inspect later.

    Only term rules are recorded this way: a rewrite of a word is visible in the
    draft («عنوان: «سلفی» ← «مشکی»»), while a scaled number has already become the
    price and is not distinguishable from one the seller wrote — inventing an
    example there would be a guess.
    """
    if not rule_id:
        return
    _APPLIED_QUEUE.append((rule_id, {"title": (title or "")[:120], "effect": effect[:160]}))


def _flush_examples(memory: Memory) -> None:
    """Move queued examples into their rules, just before the file is written."""
    if not _APPLIED_QUEUE:
        return
    for rule_id, item in list(_APPLIED_QUEUE):
        rule = memory.rules.get(rule_id)
        if rule is None:
            continue
        if rule.applied_to and rule.applied_to[-1] == item:
            continue                      # the same product, extracted again
        rule.applied_to.append(item)
        del rule.applied_to[:-_MAX_APPLIED]
    _APPLIED_QUEUE.clear()


def applied_examples(rule: Rule) -> list[dict[str, str]]:
    """The products this rule was actually used on (newest last)."""
    return _examples_of(rule)


def get_rule(rule_id: str) -> Rule | None:
    with _lock:
        return load().rules.get(rule_id)


def rules_with_status(status: str) -> list[Rule]:
    with _lock:
        return sorted(
            (rule for rule in load().rules.values() if rule.status == status),
            key=lambda item: item.created,
            reverse=True,
        )


def pending_rules() -> list[Rule]:
    """What the bot learned but has not been allowed to use yet."""
    return rules_with_status(STATUS_PENDING)


def count_by_status() -> dict[str, int]:
    with _lock:
        counts = dict.fromkeys(STATUSES, 0)
        for rule in load().rules.values():
            counts[rule.status] = counts.get(rule.status, 0) + 1
    return counts


def _set_status(rule_id: str, status: str) -> Rule | None:
    """Move one rule to ``status``. Returns None when there is nothing to change."""
    if status not in STATUSES:
        raise ValueError(f"unknown rule status: {status}")
    with _lock:
        memory = load()
        rule = memory.rules.get(rule_id)
        if rule is None or rule.status == status:
            return None
        before = rule.status
        rule.status = status
        _log_correction(memory, Correction(field="learning", old=before, new=status, rule=rule_id))
        _write(memory)
    logger.info("Rule %s is now %s", rule_id, status)
    return rule


def confirm_rule(rule_id: str) -> Rule | None:
    """Activate a proposed rule: the owner looked at its impact and agreed."""
    return _set_status(rule_id, STATUS_ACTIVE)


def reject_rule(rule_id: str) -> bool:
    """Forget a proposal that should never have been made."""
    return delete_rule(rule_id)


def disable_rule(rule_id: str) -> Rule | None:
    """Stop using a rule but keep it (reversible, unlike deleting)."""
    return _set_status(rule_id, STATUS_DISABLED)


def enable_rule(rule_id: str) -> Rule | None:
    """Use a rule again — after disabling it, or instead of the preview."""
    return _set_status(rule_id, STATUS_ACTIVE)


def toggle_scope(rule_id: str) -> Rule | None:
    """Narrow a rule to the product context it was learned in, or widen it back.

    Only a rule learned from a real product can be narrowed, and the keyword
    comes from that product — never typed — so the screen cannot invent a scope
    the parser would be unable to match.
    """
    with _lock:
        memory = load()
        rule = memory.rules.get(rule_id)
        if rule is None:
            return None
        if rule.keyword:
            rule.scope = SCOPE_SHOP
        elif rule.origin:
            rule.scope = f"{SCOPE_WORD}{rule.origin}"
        else:
            return None
        _write(memory)
    logger.info("Rule %s scope is now %s", rule_id, rule.scope)
    return rule


# The parser-test sandbox has to answer "what would this text read as *without*
# my learned rules?" through the real pipeline. A per-task flag keeps that
# pipeline identical and lets one caller show both results.
_SUSPENSION: ContextVar[bool] = ContextVar("learning_suspended", default=False)


def _suspended() -> bool:
    return _SUSPENSION.get()


class _Suspended:
    """Context manager used as ``with learning.suspended():``."""

    def __enter__(self) -> _Suspended:
        self._token = _SUSPENSION.set(True)
        return self

    def __exit__(self, *exc: object) -> None:
        _SUSPENSION.reset(self._token)


def suspended() -> _Suspended:
    """Temporarily ignore every learned rule (the parser-test diff uses this)."""
    return _Suspended()


# ---------------------------------------------------------------------------
# Rule inference
# ---------------------------------------------------------------------------

def bare_numbers(text: str) -> list[tuple[int, str]]:
    """Every ``(value, digits)`` bare number in ``text``, in order of appearance.

    "Bare" means no unit suffix right after it: «۱۰۹۸ هزار» or «768t» already say
    what they mean. Both the inference guard below and the impact replay read
    numbers through here, so a preview can never disagree with the rule it is
    previewing.
    """
    out: list[tuple[int, str]] = []
    for match in re.finditer(r"(?<![\d.])([۰-۹٠-٩\d][۰-۹٠-٩\d,،.]*)(?![\d.])", text or ""):
        token = match.group(1)
        raw = token.translate(_DIGITS).replace(",", "").replace("،", "").replace(".", "")
        if not raw.isdigit():
            continue
        tail = (text or "")[match.end():match.end() + 8]
        if re.match(r"\s*(?:تومان|تومن|هزار|میلیون|ت|t|k)\b", tail, re.I):
            continue
        try:
            out.append((int(raw), raw))
        except ValueError:
            continue
    return out


def _bare_number_equal_to(text: str, target: int) -> str | None:
    """Find the bare numeric token in ``text`` whose literal value is ``target``.

    Returns the token as written, so the rule can be keyed on its digit count
    («1098» -> 4 digits) rather than on the parsed value.
    """
    if target <= 0:
        return None
    for value, raw in bare_numbers(text):
        if value == target:
            return raw
    return None


def _stated_amounts(text: str) -> set[int]:
    """Every amount ``text`` plainly states, in tomans.

    Deliberately small and dependency-free (this module must not import the
    parser, which imports it): it only has to answer "did the owner actually
    write the corrected number?", not reproduce every parsing rule.
    """
    source = text or ""
    out: set[int] = set()
    for match in re.finditer(
        r"(?<![\d.])([۰-۹٠-٩\d][۰-۹٠-٩\d,،.]*)\s*(میلیارد|میلیون|هزار|تومان|تومن|ت|k|t)?",
        source,
        re.I,
    ):
        raw = match.group(1).translate(_DIGITS).replace(",", "").replace("،", "").replace(".", "")
        if not raw.isdigit():
            continue
        value = int(raw)
        suffix = (match.group(2) or "").casefold()
        out.add(value)
        if suffix in {"هزار", "ت", "k", "t"}:
            out.add(value * 1000)
        elif suffix == "میلیون":
            out.add(value * 10 ** 6)
        elif suffix == "میلیارد":
            out.add(value * 10 ** 9)
    # Compound unit expressions: «۱ میلیون و ۹۸ هزار تومان» = 1,098,000.
    total = 0
    for word, factor in (("میلیارد", 10 ** 9), ("میلیون", 10 ** 6), ("هزار", 10 ** 3)):
        for match in re.finditer(r"(\d[\d.]*)\s*" + word, source):
            try:
                total += int(float(match.group(1).rstrip(".")) * factor)
            except ValueError:
                continue
    if total:
        out.add(total)
    return out


def infer_price_scale(old: int, new: int, source_text: str) -> Rule | None:
    """Learn a digit-length -> multiplier rule from a price correction.

    Three guards, because a wrong scale rule silently corrupts every later
    product:

    * the corrected value must be exactly the misread one times a power of ten
      (so «698000» -> «750000», an ordinary price change, never becomes a rule);
    * the misread value must appear in the source as a *bare* number, which is
      what makes it ambiguous and therefore worth a rule keyed on its length;
    * the corrected value must actually be stated in the source, so a parser or
      model that merely changed its mind cannot teach the bot anything.
    """
    if old <= 0 or new <= 0 or new == old or new % old != 0:
        return None
    ratio = new // old
    if ratio <= 1 or re.fullmatch(r"10+", str(ratio)) is None:
        return None
    if new not in _stated_amounts(source_text):
        return None
    raw = _bare_number_equal_to(source_text, old)
    if not raw:
        return None
    digits = len(raw)
    if not 1 <= digits <= 15:
        return None
    if not scaled_price_is_sane(old * ratio):
        return None
    return Rule(
        kind="price_scale",
        key=str(digits),
        value=str(ratio),
        example=f"{raw} ← {new:,}",
    )


def infer_token_substitution(old: str, new: str) -> Rule | None:
    """Learn a word-level replacement when two strings differ in ONE local spot.

    Used for titles and free text: «Air skin» -> «Airskin», «قاب سیلیکن» ->
    «قاب سیلیکونی». Anchoring on a shared prefix/suffix is what keeps this safe:

    * a wholesale rewrite shares no anchor, so it is not a generalizable term;
    * an insertion («قاب آیفون» -> «قاب محافظ آیفون ۱۷») has one side contained
      in the other, i.e. the owner added information rather than renaming a word;
    * a pure number swap is a value change, not a spelling rule.
    """
    old_tokens = (old or "").split()
    new_tokens = (new or "").split()
    if not old_tokens or not new_tokens or old_tokens == new_tokens:
        return None

    limit = min(len(old_tokens), len(new_tokens))
    prefix = 0
    while prefix < limit and old_tokens[prefix] == new_tokens[prefix]:
        prefix += 1
    suffix = 0
    while (
        suffix < limit - prefix
        and old_tokens[len(old_tokens) - 1 - suffix] == new_tokens[len(new_tokens) - 1 - suffix]
    ):
        suffix += 1
    # At least one token of shared context, otherwise it is a different string
    # entirely and no term can be inferred from it.
    if prefix + suffix == 0:
        return None

    wrong = " ".join(old_tokens[prefix: len(old_tokens) - suffix]).strip(".,:;!?،؛")
    right = " ".join(new_tokens[prefix: len(new_tokens) - suffix]).strip(".,:;!?،؛")
    if not wrong or not right or wrong == right:
        return None
    # An expansion/contraction of the same words is added detail, not a rename.
    if wrong in right or right in wrong:
        return None
    if wrong.translate(_DIGITS).isdigit() and right.translate(_DIGITS).isdigit():
        return None
    return Rule(kind="term", key=wrong, value=right, example=f"{old} ← {new}")


def infer_value_substitution(old_values: list[str], new_values: list[str]) -> Rule | None:
    """Learn a term rule when one attribute value was swapped for another.

    Requires the rest of the list to be unchanged, so a genuinely different
    product (a new color list) is logged but never turned into a rule.
    """
    old_list = [str(v) for v in old_values or []]
    new_list = [str(v) for v in new_values or []]
    if not old_list or old_list == new_list or len(old_list) != len(new_list):
        return None
    removed = [v for v in old_list if v not in new_list]
    added = [v for v in new_list if v not in old_list]
    if len(removed) != 1 or len(added) != 1:
        return None
    wrong, right = removed[0].strip(), added[0].strip()
    if not wrong or not right or wrong == right:
        return None
    return Rule(kind="term", key=wrong, value=right, example=f"{wrong} ← {right}")


# ---------------------------------------------------------------------------
# Recording (called by the product flow)
# ---------------------------------------------------------------------------

def remember(rule: Rule | None, correction: Correction, *, origin: str = "") -> bool:
    """Persist a correction (and its rule when one could be inferred).

    Returns True when a new/changed rule was stored, so the caller only
    announces «یاد گرفتم» when something was actually learned.

    ``origin`` is the product context the rule came from (a model name or a
    category leaf). It is what makes the owner able to narrow the rule to that
    context later, so it has to be recorded while the product is still known.
    """
    with _lock:
        memory = load()
        changed = False
        if rule is not None:
            correction.rule = rule.rule_id
            if origin and not rule.origin:
                rule.origin = origin[:60]
            changed = _put_rule(memory, rule)
        _log_correction(memory, correction)
        _write(memory)
    if rule is not None:
        logger.info("Learned rule %s from %r -> %r", rule.rule_id, correction.old, correction.new)
    return changed


def delete_rule(rule_id: str) -> bool:
    with _lock:
        memory = load()
        if rule_id not in memory.rules:
            return False
        memory.rules.pop(rule_id, None)
        _write(memory)
    logger.info("Deleted learned rule %s", rule_id)
    return True


def clear_rules() -> int:
    with _lock:
        memory = load()
        count = len(memory.rules)
        memory.rules.clear()
        _write(memory)
    logger.info("Cleared %d learned rules", count)
    return count


def recent_corrections(limit: int = 8) -> list[Correction]:
    with _lock:
        memory = load()
        return list(reversed(memory.corrections[-limit:]))


def rules_for_prompt(where: str = "") -> str:
    """A compact prompt fragment so the AI honors the same learned rules.

    Only confirmed, in-scope rules are sent: the deterministic parser and the AI
    have to agree on one rule set, so a proposal nobody approved is absent from
    both, and a rule the owner limited to one category is not forced on another
    one (the AI would "fix" words nobody asked it about).
    """
    with _lock:
        memory = load()
        rules = [
            rule for rule in memory.rules.values()
            if rule.is_active and rule.matches(where) and not _suspended()
        ]
    if not rules:
        return ""
    parts = []
    for rule in sorted(rules, key=lambda item: (item.kind, item.key)):
        if rule.kind == "price_scale":
            parts.append(
                f"A bare {rule.key}-digit price number with no suffix means "
                f"x{rule.value} (e.g. {rule.example})."
            )
        else:
            parts.append(f"Always write «{rule.key}» as «{rule.value}».")
    return "LEARNED OWNER RULES (apply them exactly):\n" + "\n".join(parts)


__all__ = [
    "MEMORY_VERSION",
    "SCOPE_SHOP",
    "SCOPE_WORD",
    "STATUSES",
    "STATUS_ACTIVE",
    "STATUS_DISABLED",
    "STATUS_LABELS",
    "STATUS_PENDING",
    "Correction",
    "Memory",
    "Rule",
    "applied_examples",
    "apply_terms",
    "apply_terms_to_values",
    "bare_numbers",
    "clear_rules",
    "confirm_rule",
    "count_by_status",
    "delete_rule",
    "disable_rule",
    "enable_rule",
    "fold",
    "get_rule",
    "infer_price_scale",
    "infer_token_substitution",
    "infer_value_substitution",
    "load",
    "note_application",
    "pending_rules",
    "price_multiplier",
    "recent_corrections",
    "reject_rule",
    "remember",
    "replace_term",
    "revision",
    "rule_by_short_id",
    "rules_for_prompt",
    "rules_sorted",
    "rules_with_status",
    "scaled_price_is_sane",
    "short_id",
    "suspended",
    "toggle_scope",
]
