"""Rehearsal for a learned rule: what it *would have done* to recent products.

Activating a rule is the moment where a small misunderstanding becomes a shop-wide
one. ««سبز» یعنی «سفید»» looks harmless and, applied to a catalog, merges two
color axes and silently deletes a variation from every product that had both. A
number the owner never sees is a number nobody can object to, so a rule is only
confirmed after this module has replayed it on the products in
:mod:`bot.services.learning_corpus` and said, in the chat, what the replay found.

The replay is not an approximation of the parser: it calls the same
``learning.replace_term`` and the same ``plan.plan_from_dict`` that fills the
variation count on the preview card. That is the whole point — a preview computed
by different code would be a second opinion, not a rehearsal.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from bot.services import learning, learning_corpus, plan
from bot.services.learning import Rule

logger = logging.getLogger(__name__)

# Enough to make the point without turning a chat message into a diff dump.
MAX_EXAMPLES = 3
_PRICE_FIELD = "قیمت"


@dataclass(frozen=True)
class Change:
    """One field one product would have had rewritten."""

    product: str
    field: str
    before: str
    after: str

    def line(self) -> str:
        return f"• {self.product} — {self.field}: «{self.before}» ← «{self.after}»"


@dataclass(frozen=True)
class Impact:
    """The result of replaying a rule over the corpus."""

    products: int
    changes: tuple[Change, ...] = ()
    variation_delta: int = 0
    price_changes: int = 0
    price_out_of_range: int = 0

    @property
    def dangerous(self) -> bool:
        """Anything that removes sellable stock or writes an absurd price."""
        return self.variation_delta < 0 or self.price_out_of_range > 0

    @property
    def touched(self) -> bool:
        return bool(self.changes) or self.price_changes > 0

    @property
    def blind(self) -> bool:
        """Nothing recent to replay on — so nothing is actually known."""
        return self.products == 0

    def summary(self) -> str:
        if self.blind:
            return "هنوز محصول تازه‌ای در حافظه نیست که رویش آزمایش کنم؛ با تأیید، از محصول بعدی اعمال می‌شود."
        bits: list[str] = []
        if self.variation_delta:
            count = abs(self.variation_delta)
            bits.append(
                f"⚠️ {count} واریژن کمتر می‌شد" if self.variation_delta < 0 else f"{count} واریژن بیشتر می‌شد"
            )
        if self.price_changes:
            bits.append(f"قیمت {self.price_changes} محصول عوض می‌شد")
        if self.price_out_of_range:
            bits.append(f"⚠️ {self.price_out_of_range} قیمت از حد مجاز بیرون می‌زد")
        text_fields = [item for item in self.changes if item.field != _PRICE_FIELD]
        if text_fields:
            bits.append(f"{len(text_fields)} فیلد متنی بازنویسی می‌شد")
        if not bits:
            return f"روی {self.products} محصول آخرِ من هیچ چیزی را عوض نمی‌کرد."
        return f"روی {self.products} محصول آخرِ من: " + "، ".join(bits) + "."

    def example_lines(self, limit: int = MAX_EXAMPLES) -> list[str]:
        return [item.line() for item in self.changes[:limit]]

    def render(self) -> str:
        """The whole preview, as one block for the chat."""
        lines = [self.summary()]
        lines += self.example_lines()
        extra = len(self.changes) - MAX_EXAMPLES
        if extra > 0:
            lines.append(f"… و {extra} تغییر دیگر")
        if self.dangerous:
            lines.append(
                "🛑 این قاعده چیزی را که فروخته می‌شود کم می‌کند؛ پیش از تأیید مطمئن شو درست فهمیده‌ام."
            )
        return "\n".join(lines)


def _label(entry: dict[str, Any]) -> str:
    title = str(entry.get("title") or "").strip()
    return f"«{title[:36]}»" if title else "محصول بدون عنوان"


def _mapped(values: list[str], wrong: str, right: str) -> list[str]:
    """What ``learning.apply_terms_to_values`` would produce for this list.

    The de-duplication is part of the behaviour being rehearsed: it is the step
    at which two values become one, and therefore where a variation disappears.
    """
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        fixed, _ = learning.replace_term(str(value), wrong, right)
        if fixed and fixed not in seen:
            seen.add(fixed)
            out.append(fixed)
    return out


def _project_terms(rule: Rule, entries: list[dict[str, Any]]) -> Impact:
    wrong, right = rule.key, rule.value
    changes: list[Change] = []
    delta = 0
    for entry in entries:
        where = f"{entry.get('text', '')}\n{entry.get('title', '')}"
        if not rule.matches(where):
            continue
        attributes = {
            str(name): [str(v) for v in (values or [])]
            for name, values in (entry.get("attributes") or {}).items()
        }
        models = [str(x) for x in (entry.get("models") or [])]

        title_before = str(entry.get("title") or "")
        title_after, fired = learning.replace_term(title_before, wrong, right)
        if fired:
            changes.append(Change(_label(entry), "عنوان", title_before, title_after))

        after_attrs: dict[str, list[str]] = {}
        for name, values in attributes.items():
            fixed = _mapped(values, wrong, right)
            after_attrs[name] = fixed
            if fixed != values:
                changes.append(Change(_label(entry), name, "، ".join(values[:3]), "، ".join(fixed[:3])))
        after_models = _mapped(models, wrong, right)
        if after_models != models:
            changes.append(
                Change(_label(entry), "مدل‌ها", " | ".join(models[:3]), " | ".join(after_models[:3]))
            )

        # Counted with the publish path's own builder, including the per-model
        # colour limits: «دو رنگ کم شد» must mean two variations fewer *sold*,
        # which is what ``plan`` decides, not what a value list suggests.
        restrictions = {
            str(name): [str(v) for v in (values or [])]
            for name, values in (entry.get("model_colors") or {}).items()
        }
        before_count = plan.plan_from_dict(
            {**entry, "models": models, "attributes": attributes, "model_colors": restrictions}
        ).count
        after_restrictions = {
            _mapped([name], wrong, right)[0]: _mapped(values, wrong, right)
            for name, values in restrictions.items()
        }
        after_count = plan.plan_from_dict(
            {
                **entry,
                "stock_matrix": {
                    _mapped([design], wrong, right)[0]: {
                        _mapped([model], wrong, right)[0]: quantity for model, quantity in values.items()
                    } for design, values in (entry.get("stock_matrix") or {}).items()
                },
                "models": after_models,
                "attributes": after_attrs,
                "model_colors": after_restrictions,
            }
        ).count
        delta += after_count - before_count
    return Impact(products=len(entries), changes=tuple(changes), variation_delta=delta)


def _project_price(rule: Rule, entries: list[dict[str, Any]]) -> Impact:
    try:
        digits = int(rule.key)
        factor = int(rule.value)
    except (TypeError, ValueError):
        return Impact(products=len(entries))
    if digits <= 0 or factor <= 1:
        return Impact(products=len(entries))
    changes: list[Change] = []
    out_of_range = 0
    for entry in entries:
        if not rule.matches(entry.get("text", "")):
            continue
        price = int(entry.get("price") or 0)
        if price <= 0:
            continue
        for value, raw in learning.bare_numbers(str(entry.get("text") or "")):
            if len(raw) != digits:
                continue
            scaled = value * factor
            # The product that taught the rule has already been corrected, so its
            # stored price is the *result* of it: «1098» still sits in its text and
            # 1098000 in its data. Requiring `value == price` would call the one
            # product that proves the rule proof of nothing, so the case where the
            # rule would have produced exactly this price counts too — the same
            # bare-amount judgement the parser makes, not a new guess.
            if value != price and scaled != price:
                continue
            if not learning.scaled_price_is_sane(scaled):
                out_of_range += 1
            changes.append(Change(_label(entry), _PRICE_FIELD, f"{value:,}", f"{scaled:,}"))
            break
    return Impact(
        products=len(entries),
        changes=tuple(changes),
        price_changes=len(changes),
        price_out_of_range=out_of_range,
    )


def project(rule: Rule, entries: list[dict[str, Any]] | None = None) -> Impact:
    """Replay ``rule`` on the corpus and describe what it would have changed.

    ``entries`` is only for tests and for callers that already loaded the corpus;
    the bot's own path reads it from disk.
    """
    if rule is None:
        return Impact(products=0)
    corpus = learning_corpus.entries() if entries is None else list(entries)
    if rule.kind == "term":
        return _project_terms(rule, corpus)
    if rule.kind == "price_scale":
        return _project_price(rule, corpus)
    # An unknown kind is a rule this version does not know how to apply either,
    # so pretending to have rehearsed it would be a lie.
    return Impact(products=len(corpus))


def preview(rule: Rule, entries: list[dict[str, Any]] | None = None) -> str:
    """The Persian block the owner reads before deciding (empty when unknown)."""
    return project(rule, entries).render()


__all__ = ["Change", "Impact", "preview", "project"]
