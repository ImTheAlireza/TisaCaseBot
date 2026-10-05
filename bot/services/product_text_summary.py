"""Extract and format models/attributes from product captions and text.

Two renderers live here, deliberately different in size:

* :func:`format_product_summary` — the old two-line answer (models/features), kept because it is
  the copy-friendly one and other callers still use it.
* :class:`TextAnalysis` + :meth:`TextAnalysis.report` — the whole answer the «📦 ساخت محصول» card
  is built from, including the refusals (unmatched model words, dropped options, warnings), so
  that a check of «درست تشخیص داد؟» can actually be done from the compress screen.
"""
from __future__ import annotations

import html
from dataclasses import dataclass, field

from bot.services.color_matrix import is_color_attribute, is_model_attribute


def format_product_summary(models: list[str], attributes: dict[str, list[str]]) -> str:
    """Compact admin-facing result; pipe-separated values are easy to copy."""
    clean_models = list(dict.fromkeys(html.escape(str(model).strip()) for model in models if str(model).strip()))
    feature_lines: list[str] = []
    for name, raw_values in attributes.items():
        values = list(dict.fromkeys(html.escape(str(value).strip()) for value in raw_values if str(value).strip()))
        if values:
            feature_lines.append(f"{html.escape(str(name).strip())}: {' | '.join(values)}")
    return (
        "🔎 <b>نتیجهٔ تشخیص از کپشن و متن</b>\n"
        f"مدل‌ها: {(' | '.join(clean_models) if clean_models else 'پیدا نشد')}\n"
        f"ویژگی‌ها: {('؛ '.join(feature_lines) if feature_lines else 'پیدا نشد')}"
    )


# --- the rich answer: what «📦 ساخت محصول» would build, said out loud ----------------


def _esc(value: object) -> str:
    return html.escape(str(value).strip(), quote=False)


def _join(values: list[str] | tuple[str, ...], sep: str = " | ") -> str:
    clean = [value for value in (_esc(item) for item in values) if value]
    return sep.join(dict.fromkeys(clean))


#: What makes an AI diagnostic worth a seller's attention. The rest is telemetry about a request
#: that went fine («پاسخ AI پردازش شد»), and a card that reports its own happy path is a card
#: nobody reads. "نشد" catches "this could not be applied", which *is* news.
_EXCEPTION_MARKERS = (
    "خطا", "تنظیم نیست", "حفظ شد", "رد شد", "نامعتبر", "نشد",
    "حدس", "مشکوک", "بازبینی",          # a guess is the one thing a check-card must show
)

#: What makes an informational note belong on this card. The card exists to check the models and
#: the variations, so a note about the AI-written title or the price stays in the log — the
#: compress screen writes nothing to the shop and cannot act on it.
_CARD_WORDS = ("مدل", "رنگ", "فام", "واریژن", "variation", "طرح", "سازگار", "ایرپاد", "airpods", "matrix")


def _actionable(note: str) -> bool:
    text = str(note)
    return any(marker in text for marker in _EXCEPTION_MARKERS)


def _card_relevant(note: str) -> bool:
    text = str(note).casefold()
    return any(word in text for word in _CARD_WORDS)


@dataclass(frozen=True)
class TextAnalysis:
    """Everything the product parser decided about one block of text — and what it refused.

    This exists because the two entry points used to give different answers from the same parser:
    «📦 ساخت محصول» shows a card with the per-model color limits, the variation count, the
    warnings and the words it could *not* read, while «🗜️ فشرده‌سازی عکس‌ها» printed two lines of
    models and features. A person checking «تشخیص درست بود؟» needs the refusals as much as the
    hits: an empty list and a rejected list looked identical.

    Every field is plain data (no session, no PTB), so the renderer below stays a service.
    """

    phone_models: tuple[str, ...] = ()
    accessories: tuple[str, ...] = ()
    attributes: dict[str, list[str]] = field(default_factory=dict)
    model_colors: dict[str, list[str]] = field(default_factory=dict)
    color_summary: str = ""
    variation_count: int = 0
    naive_count: int = 0
    is_variable: bool = False
    axis_names: tuple[str, ...] = ()
    dropped: tuple[tuple[str, int, int], ...] = ()
    unmatched: tuple[tuple[str, tuple[str, ...]], ...] = ()
    suggestions: tuple[tuple[str, str], ...] = ()
    warnings: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    ai_notes: tuple[str, ...] = ()
    has_text: bool = True

    @property
    def found_anything(self) -> bool:
        return bool(self.phone_models or self.accessories or self.attributes)

    def _feature_lines(self) -> list[str]:
        lines: list[str] = []
        for name, values in self.attributes.items():
            if is_model_attribute(name):
                continue                                   # the model axis has its own line
            joined = _join(list(values), sep="، ")
            if joined:
                lines.append(f"{_esc(str(name).strip())}: {joined}")
        return lines

    def _color_groups(self) -> list[tuple[tuple[str, ...], list[str]]]:
        """models that share *exactly* the same palette collapse into one group."""
        groups: dict[tuple[str, ...], list[str]] = {}
        for model, colors in self.model_colors.items():
            clean = tuple(str(color).strip() for color in colors if str(color).strip())
            if clean:
                groups.setdefault(clean, []).append(str(model).strip())
        return list(groups.items())

    def _global_colors(self) -> set[str]:
        out: set[str] = set()
        for name, values in self.attributes.items():
            if is_color_attribute(str(name)):
                out |= {str(value).strip() for value in values if str(value).strip()}
        return out

    def _color_lines(self) -> tuple[list[str], bool]:
        """(what the card shows, whether the full mapping had to be folded away).

        Fourteen models with the same two colors are *one* fact, not fourteen; writing them out
        turned the card into a wall and hid the lines that matter. A fold is never a deletion:
        :meth:`detail` keeps the enumeration for the group log.
        """
        groups = self._color_groups()
        if not groups:
            return [], False
        known = len([colors for colors in self.model_colors.values() if colors])
        if len(groups) == 1:
            colors, models = groups[0]
            if len(models) == known and set(colors) <= self._global_colors():
                return [], True                      # the «رنگ» feature line already says this
            shown = _join(list(colors), sep="، ")
            if len(models) == known:
                label = f"رنگ همهٔ {known} مدل" if known > 1 else "رنگ مدل"
                return [f"<b>{label}:</b> {shown}"], False
            named = _join(models[:5], sep="، ") + ("، …" if len(models) > 5 else "")
            return [f"<b>رنگ {len(models)} مدل از {known}:</b> {shown} ({named})"], True
        pieces: list[str] = []
        folded = False
        for colors, models in groups:
            if len(models) > 3:
                pieces.append(f"{_esc('/'.join(colors))}: {len(models)} مدل")
                folded = True
            else:
                pieces.append(f"{_esc('/'.join(colors))}: {_join(models, sep='، ')}")
        return [f"<b>رنگ هر مدل:</b> {' · '.join(pieces)}"], folded

    def _variations_line(self) -> str:
        if not self.is_variable or self.variation_count < 2:
            # The flow itself names this after the plan (`simple` vs `variable`), and so do we:
            # «1 ترکیب» and «ساده» are the same product, and the second one is the sentence a
            # seller recognises.
            return "<b>واریژن:</b> ساده (بدون انتخاب)"
        axes = " × ".join(_esc(name) for name in self.axis_names) if self.axis_names else ""
        count = f"<b>واریژن:</b> {self.variation_count} ترکیب"
        if axes:
            count += f" ({_esc(axes)})"
        if self.naive_count and self.naive_count != self.variation_count:
            count += f" — محدودیتِ رنگ/سازگاری {self.naive_count - self.variation_count} ترکیب را حذف کرد"
        return count

    def report(self) -> str:
        """The card: hits first, then what was dropped, then what to do about it."""
        if not self.has_text:
            return (
                "🔎 <b>نتیجهٔ تشخیص از کپشن و متن</b>\n\n"
                "📝 متنی نبوده که خوانده شود — کپشن عکس یا یک پیام متنیِ جدا بفرست "
                "(عکسِ بی‌متن قابل تشخیص نیست)."
            )
        lines = ["🔎 <b>نتیجهٔ تشخیص از کپشن و متن</b>", ""]
        if self.phone_models:
            lines.append(f"<b>مدل گوشی:</b> {_join(list(self.phone_models))}")
        if self.accessories:
            lines.append(f"<b>لوازم جانبی:</b> {_join(list(self.accessories))}")
        if not (self.phone_models or self.accessories):
            lines.append("<b>مدل گوشی:</b> پیدا نشد")
        features = self._feature_lines()
        if features:
            lines.append(f"<b>ویژگی‌ها:</b> {'؛ '.join(features)}")
        color_lines, folded = self._color_lines()
        lines.extend(color_lines)
        if not color_lines and not folded and self.color_summary and "نشد" not in self.color_summary:
            # «ماتریس رنگ تشخیص داده نشد» is the default state of a caption without a matrix.
            # Saying it on every card turns an absence into something that reads like a failure.
            # No bold label here on purpose: this string is the flow's own summary and may be
            # about a *stock* matrix («طرح × دستهٔ گوشی»), which «رنگ‌بندی» would misdescribe.
            lines.append(f"🎨 {_esc(self.color_summary)}")
        lines.append(self._variations_line())
        for name, before, after in self.dropped:
            # Same sentence the product card uses («… به N رسید و حذف شد»), so the two screens
            # never describe one event with two vocabularies.
            lines.append(f"✂️ «{_esc(name)}» با {before} مقدار به {after} رسید و حذف شد")
        for label, words in self.unmatched:
            # The parser's own note usually already names the same leftover word and says where to
            # fix it; repeating the advice under a second icon is noise on a card meant to be
            # skimmed. Only a word distinctive enough to be *the* reason counts — «3» or «s» shows
            # up in nearly every note and would hide the sentence that matters.
            context = " ".join((*self.notes, *self.warnings))
            covered = any(len(word) > 3 and word in context for word in words)
            # The line the parser choked on is usually a whole caption; quoting all of it makes the
            # one line that matters unreadable. What is quoted is what the sentence is about.
            quoted = _esc(label if len(label) <= 60 else label[:57].rstrip() + "…")
            if covered:
                lines.append(f"🚫 «{quoted}»: {_join(list(words), sep='، ')} اعمال نشد")
            else:
                lines.append(
                    f"🚫 این خط کامل خوانده نشد: «{quoted}» — {_join(list(words), sep='، ')} هیچ "
                    "مدلی را تغییر نداد؛ یک مدلِ ناخوانده نباید به مدلِ دیگری تبدیل شود. "
                    "اگر مدلِ تازه است، در «📦 ساخت محصول» اصلاحش کن"
                )
        for word, target in self.suggestions:
            lines.append(f"❔ حدس از دیکشنری: «{_esc(word)}» → «{_esc(target)}» (با تأییدِ تو یاد می‌گیرد)")
        for warning in self.warnings:
            lines.append(f"⚠️ {_esc(warning)}")
        for note in self.notes:
            if _card_relevant(note):
                lines.append(f"📌 {_esc(note)}")
        for note in self.ai_notes:
            if _actionable(note):
                lines.append(f"🤖 {_esc(note)}")
        if not self.found_anything:
            lines += [
                "",
                "هیچ مدلی از این متن درآمدنی نبود. اسم گوشی را بنویس (مثلاً «۱۳promax»، «a54»، "
                "«s24 اولترا») — رقم‌های فارسی یا انگلیسی هر دو خوانده می‌شوند.",
            ]
        return "\n".join(lines)

    def detail(self) -> str:
        """What :meth:`report` folded away — the audit log's second half.

        A shorter card may not mean a less verifiable one: everything dropped for being
        repetitive (the full per-model palette) or merely informational (a successful AI call, a
        note about the title) is written here instead, HTML-escaped, and the compress flow appends
        it to the line it logs to the group.
        """
        parts: list[str] = []
        _lines, folded = self._color_lines()
        if folded and self.model_colors:
            parts.append("رنگ هر مدل: " + " | ".join(
                f"{_esc(model)}: {'/'.join(_esc(str(color).strip()) for color in colors if str(color).strip())}"
                for model, colors in self.model_colors.items() if colors
            ))
        telemetry = [note for note in self.ai_notes if not _actionable(note)]
        if telemetry:
            parts.append("یادداشت AI: " + " | ".join(_esc(note) for note in telemetry))
        off_card = [note for note in self.notes if not _card_relevant(note)]
        if off_card:
            parts.append("نکته: " + " | ".join(_esc(note) for note in off_card))
        return "\n".join(parts)
