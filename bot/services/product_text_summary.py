"""Extract and format models/attributes from product captions and text.

Two renderers live here, deliberately different in size:

* :func:`format_product_summary` — the two-line answer (models/features). Nothing in the bot
  calls it since 0.19.6; its exact shape stays pinned by a test so anything that wants a
  copy-friendly line can still build one.
* :class:`TextAnalysis` — what the parser decided *and* what it refused. :meth:`TextAnalysis.report`
  is the card (the variables, then the refusals: an unread line, a warning, an AI guess);
  :meth:`TextAnalysis.detail` is the log (the variation plan, the per-model palette, the collapsed
  axes, the learning hints — everything a card has no room for).
"""
from __future__ import annotations

import html
from dataclasses import dataclass, field

from bot.services.color_matrix import is_model_attribute


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


def _actionable(note: object) -> bool:
    """Whether an AI diagnostic belongs on the seller's card instead of only in the log.

    Most of them describe a request that went fine («پاسخ AI پردازش شد»); a card that reports its
    own happy path is a card nobody reads. What stays are the lines that mean *this list may not
    be what you meant*: an error, a guess, something kept from the deterministic parser because AI
    dropped it, or a review flag. «نشد» catches «this could not be applied», which is also news.
    """
    text = str(note)
    return any(marker in text for marker in _EXCEPTION_MARKERS)


#: What makes an AI diagnostic worth a seller's attention. The rest is telemetry about a request
#: that went fine («پاسخ AI پردازش شد»), and a card that reports its own happy path is a card
#: nobody reads. "نشد" catches "this could not be applied", which *is* news.
_EXCEPTION_MARKERS = (
    "خطا", "تنظیم نیست", "حفظ شد", "رد شد", "نامعتبر", "نشد",
    "حدس", "مشکوک", "بازبینی",          # a guess is the one thing a check-card must show
)

@dataclass(frozen=True)
class TextAnalysis:
    """Everything the product parser decided about one block of text — and what it refused.

    This exists because the two entry points used to give different answers from the same parser:
    «📦 ساخت محصول» shows a card with the per-model color limits, the variation count, the
    warnings and the words it could *not* read, while «🗜️ فشرده‌سازی عکس‌ها» printed two lines of
    models and features. The card fixes that — but on this screen a *card* means "are the values
    right", so :meth:`report` renders the variables plus the refusals, and :meth:`detail` carries
    the variation plan to the log.

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
            joined = _join(list(values), sep='، ')
            if joined:
                lines.append(f"{_esc(str(name).strip())}: {joined}")
        return lines

    def _color_map_text(self) -> str:
        """The per-model palette, one entry per model — for the log, never for the card."""
        parts = [
            f"{_esc(model)}: {'/'.join(_esc(str(color).strip()) for color in colors if str(color).strip())}"
            for model, colors in self.model_colors.items()
            if [color for color in colors if str(color).strip()]
        ]
        return " | ".join(parts)

    def _variation_text(self) -> str:
        if not self.is_variable or self.variation_count < 2:
            # The flow itself names this after the plan (`simple` vs `variable`), and so do we:
            # «1 ترکیب» and «ساده» are the same product, and the second one is the sentence a
            # seller recognises.
            return "واریژن: ساده (بدون انتخاب)"
        axes = " × ".join(_esc(name) for name in self.axis_names) if self.axis_names else ""
        text = f"واریژن: {self.variation_count} ترکیب"
        if axes:
            text += f" ({_esc(axes)})"
        if self.naive_count and self.naive_count != self.variation_count:
            text += f" — محدودیتِ رنگ/سازگاری {self.naive_count - self.variation_count} ترکیب را حذف کرد"
        return text

    def report(self) -> str:
        """The card: the values that were found, and only then what was refused.

        A seller opens «🗜️ فشرده‌سازی عکس» to answer one question — *are these right* — and this
        screen never writes to the shop, so variation axes, the combination count, the color matrix
        and the learning hints are the log's business (:meth:`detail`). What stays besides the
        values is only what says "this list may not be what you meant": 🚫 a line the parser could
        not apply, ⚠️ the parser's own warning, 🤖 an AI note that reports a guess or a failure
        rather than a successful call.
        """
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
        for label, words in self.unmatched:
            # The parser's own note usually already names the same leftover word and says where to
            # fix it; repeating the advice under a second icon is noise on a card meant to be
            # skimmed. Only a word distinctive enough to be *the* reason counts — «3» or «s» shows
            # up in nearly every note and would hide the sentence that matters.
            context = " ".join((*self.notes, *self.warnings))
            covered = any(len(word) > 3 and word in context for word in words)
            # And the line the parser choked on is often a whole caption: quoting it in full turns
            # the one sentence meant to be read into a wall.
            quoted = _esc(label if len(label) <= 60 else label[:57].rstrip() + "…")
            if covered:
                lines.append(f"🚫 «{quoted}»: {_join(list(words), sep='، ')} اعمال نشد")
            else:
                lines.append(
                    f"🚫 این خط کامل خوانده نشد: «{quoted}» — {_join(list(words), sep='، ')} هیچ "
                    "مدلی را تغییر نداد؛ یک مدلِ ناخوانده نباید به مدلِ دیگری تبدیل شود. "
                    "اگر مدلِ تازه است، در «📦 ساخت محصول» اصلاحش کن"
                )
        for warning in self.warnings:
            lines.append(f"⚠️ {_esc(warning)}")
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
        """The whole answer: what :meth:`report` folded away, for the group log.

        A shorter card must not mean a less verifiable one. The variation plan, the per-model
        palette, the collapsed axes, the dictionary's guesses and the chatter of a successful AI
        call land here, so «چه چیزی به فروشنده گفته شد» and «ربات چه چیزی دید» stay two halves of
        one record. Empty string when there is nothing to add.
        """
        parts: list[str] = []
        if self.is_variable or self.variation_count or self.dropped:
            parts.append(self._variation_text())
            for name, before, after in self.dropped:
                # Same sentence the product card uses («… به N رسید و حذف شد»), so the two
                # screens never describe one event with two vocabularies.
                parts.append(f"«{_esc(name)}» با {before} مقدار به {after} رسید و حذف شد")
        colors = self._color_map_text()
        if colors:
            parts.append(f"رنگ هر مدل: {colors}")
        if self.color_summary and "نشد" not in self.color_summary:
            # No label here on purpose: this string is the flow's own summary and may be about a
            # *stock* matrix («طرح × دستهٔ گوشی»), which a color framing would misdescribe.
            parts.append(f"ماتریس: {_esc(self.color_summary)}")
        for word, target in self.suggestions:
            parts.append(f"حدس از دیکشنری: «{_esc(word)}» → «{_esc(target)}» (با تأییدِ تو یاد می‌گیرد)")
        for note in self.notes:
            parts.append(f"نکته: {_esc(note)}")
        telemetry = [note for note in self.ai_notes if not _actionable(note)]
        if telemetry:
            parts.append("یادداشت AI: " + " | ".join(_esc(note) for note in telemetry))
        return "\n".join(f"· {line}" for line in parts)
