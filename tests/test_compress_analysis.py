"""Fase ۱۵: «🗜️ فشرده‌سازی عکس» must show the *same* intelligence «📦 ساخت محصول» has.

Both entry points always shared one parser (``product_flow._extract``); what differed was the
output surface — the compress flow printed two lines and threw the rest away, so a half-read or
outright wrong detection was indistinguishable from silence. These tests pin the report that closes
the gap: per-model colors, the variation count, the words the parser refused, and the learning
hints — and they pin that no part of it can break a compression that already succeeded.
"""
from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace

from bot.modules import product_flow as flow
from bot.services.product_text_summary import TextAnalysis, format_product_summary


def _fake_extract(models, *, attributes=None, model_colors=None, warnings=(), suggestions=(),
                  to_dict=None, ai_notes=()):
    """Stand in for the product parser, keeping only what the report is built from.

    ``to_dict`` is deliberately optional: a partial result must still render. The report is a
    courtesy on top of a finished compression, so it may not be the thing that raises.
    """

    async def _extract(session, **kwargs):
        session.models = list(models)
        session.color_summary = " / ".join(f"{m}: {'/'.join(c)}" for m, c in (model_colors or {}).items())
        session.ai_diagnostics[:] = list(ai_notes)
        data = SimpleNamespace(
            attributes=dict(attributes or {}),
            model_colors=dict(model_colors or {}),
            warnings=list(warnings),
            notes=[],
            suggestions=list(suggestions),
        )
        if to_dict is not None:
            data.to_dict = to_dict
        return data

    return _extract


def _call(monkeypatch, factory, *, caption="قاب چرم", info="رنگ: مشکی"):
    monkeypatch.setattr(flow, "_extract", factory)
    return asyncio.run(flow.analyze_text(caption, info))


def test_the_compress_report_shows_colors_variations_and_refusals(monkeypatch):
    # ``to_dict`` is what ProductData hands the *same* builder the product card uses, so the
    # fixture speaks that shared shape (models + attributes + per-model colors), not a private one.
    plan_dict = {
        "models": ["iPhone 13", "iPhone 14"],
        "attributes": {"رنگ": ["مشکی", "سفید"], "جنس": ["چرم"]},
        "model_colors": {"iPhone 13": ["مشکی"]},
    }
    factory = _fake_extract(
        ["iPhone 13", "iPhone 14"],
        attributes={"رنگ": ["مشکی", "سفید"], "جنس": ["چرم"]},
        model_colors={"iPhone 13": ["مشکی"], "iPhone 14": ["مشکی", "سفید"]},
        warnings=("قیمت برای یک مدل پیدا نشد",),
        suggestions=([{"kind": "model", "word": "الین", "target": "iPhone 11"}]),
        to_dict=lambda: plan_dict,
    )

    report = _call(monkeypatch, factory, caption="قاب چرم مشکی").report()

    assert "<b>مدل گوشی:</b> iPhone 13 | iPhone 14" in report
    assert "<b>رنگ هر مدل:</b> مشکی: iPhone 13 · مشکی/سفید: iPhone 14" in report
    # The flow's own summary sentence must not restate the same palette a second time.
    assert report.count("iPhone 13") == 2
    # 2 models × 2 colors, minus the one combination the color matrix forbids.
    assert "<b>واریژن:</b> 3 ترکیب (مدل × رنگ)" in report
    assert "1 ترکیب را حذف کرد" in report
    assert "جنس: چرم" in report
    assert "⚠️ قیمت برای یک مدل پیدا نشد" in report
    # The learning corpus has a guess but no veto — it is shown as a question, not a fact.
    assert "«الین» → «iPhone 11»" in report


def test_a_model_that_was_read_and_rejected_is_not_reported_as_absent(monkeypatch):
    factory = _fake_extract(["iPhone 13"], attributes={})
    analysis = _call(monkeypatch, factory, caption="iPhone 13 پرو پلاس و A54", info="")
    assert analysis.unmatched, "the tripwire needs a sample the parser can still apply"
    report = analysis.report()
    assert "🚫" in report and "plus" in report and "کامل خوانده نشد" in report


def test_an_empty_detection_says_so_and_tells_the_seller_what_to_write(monkeypatch):
    factory = _fake_extract([], attributes={})
    report = _call(monkeypatch, factory, caption="قاب شیک", info="قیمت: ۴۵۰۰۰۰").report()
    assert "<b>مدل گوشی:</b> پیدا نشد" in report
    assert "اسم گوشی را بنویس" in report
    # A miss is an explanation, not a complaint about the photo.
    assert "فشرده" not in report


def test_a_message_without_any_text_is_its_own_answer(monkeypatch):
    factory = _fake_extract([], attributes={})
    report = _call(monkeypatch, factory, caption="", info="").report()
    assert "متنی نبوده که خوانده شود" in report


def test_a_partial_parser_result_cannot_break_the_report(monkeypatch):
    # The adapter contract with callers: a stand-in that only knows ``attributes`` still renders,
    # and the model axis is never duplicated into the feature line.
    factory = _fake_extract(["iPhone 13"], attributes={"مدل": ["iPhone 13"], "رنگ": ["مشکی"]})
    report = _call(monkeypatch, factory).report()
    assert report.count("iPhone 13") == 1
    assert "<b>واریژن:</b> ساده (بدون انتخاب)" in report


def test_ai_notes_travel_with_the_report_and_with_the_legacy_tuple(monkeypatch):
    factory = _fake_extract(["iPhone 13"], attributes={}, ai_notes=("AI: حدس از روی context",))
    analysis = _call(monkeypatch, factory)
    # A *guess* is the one AI outcome a verification card must never hide.
    assert "🤖 AI: حدس از روی context" in analysis.report()
    diagnostics: list[str] = []
    models, attributes = asyncio.run(
        flow.extract_product_metadata("قاب", "", diagnostics=diagnostics)
    )
    assert models == ["iPhone 13"] and attributes == {}
    assert diagnostics == ["AI: حدس از روی context"]


def test_a_successful_ai_call_is_not_news_on_the_card(monkeypatch):
    # «پاسخ AI پردازش شد» and «پاسخ AI شامل 14 مدل بود» describe a request that went fine. On the
    # seller's card they are two lines of noise; in the group log they are the trace of what ran.
    factory = _fake_extract(
        ["iPhone 13"],
        attributes={},
        ai_notes=("نرمال‌سازی مدل: پاسخ AI شامل 14 مدل بود", "استخراج جزئیات: پاسخ AI پردازش شد",
                  "نرمال‌سازی مدل: خطای timeout؛ پارسر قطعی حفظ شد"),
        to_dict=lambda: {"models": ["iPhone 13"], "attributes": {}},
    )
    analysis = _call(monkeypatch, factory)
    report = analysis.report()
    assert "پردازش شد" not in report and "شامل 14 مدل بود" not in report
    assert "🤖 نرمال‌سازی مدل: خطای timeout" in report           # the failure is still on the card
    detail = analysis.detail()
    assert "پردازش شد" in detail and "شامل 14 مدل بود" in detail


def test_a_uniform_palette_is_one_fact_not_fourteen(monkeypatch):
    models = [f"iPhone {n}" for n in range(13, 27)]
    same = {model: ["قهوه‌ای", "صورتی"] for model in models}
    factory = _fake_extract(
        models,
        attributes={"رنگ": ["قهوه‌ای", "صورتی"]},
        model_colors=same,
        to_dict=lambda: {"models": models, "attributes": {"رنگ": ["قهوه‌ای", "صورتی"]},
                         "model_colors": same},
    )
    analysis = _call(monkeypatch, factory, caption="قاب چرم", info="رنگ: قهوه‌ای، صورتی")
    report = analysis.report()
    assert "قهوه‌ای/صورتی" not in report                 # not enumerated 14 times …
    assert "<b>ویژگی‌ها:</b> رنگ: قهوه‌ای، صورتی" in report        # … because the color axis already says it
    assert "28 ترکیب" in report or "26 ترکیب" in report   # 13 models × 2 colours is still counted
    # The audit keeps the full mapping, so the fold is never a loss.
    assert "iPhone 26: قهوه‌ای/صورتی" in analysis.detail()


def test_a_long_rejected_line_is_quoted_as_much_as_it_matters():
    # One long caption line must not turn the one line meant to be *read* into a wall.
    label = "قاب " + "، ".join(f"iPhone {n} pro max" for n in range(13, 30))
    report = TextAnalysis(phone_models=("iPhone 13",), unmatched=((label, ("max",)),)).report()
    quoted = next(line for line in report.splitlines() if line.startswith("🚫"))
    assert len(quoted) < 260 and "…" in quoted


def test_a_palette_that_actually_differs_still_names_the_groups(monkeypatch):
    colors = {"iPhone 13": ["مشکی"], "iPhone 14": ["مشکی", "سفید"], "A54": ["شفاف"]}
    factory = _fake_extract(["iPhone 13", "iPhone 14", "A54"], attributes={},
                            model_colors=colors,
                            to_dict=lambda: {"models": ["iPhone 13", "iPhone 14", "A54"],
                                             "attributes": {}, "model_colors": colors})
    report = _call(monkeypatch, factory).report()
    assert "رنگ هر مدل:" in report
    assert "مشکی: iPhone 13" in report and "شفاف: A54" in report


def test_report_escapes_the_shop_text():
    report = TextAnalysis(
        phone_models=("<b>13</b>",),
        attributes={"رنگ": ["<script>x</script>"]},
    ).report()
    assert "<b>مدل گوشی:</b> &lt;b&gt;13&lt;/b&gt;" in report
    assert "&lt;script&gt;" in report and "<script>" not in report
    assert "✂️" not in report


def test_an_axis_that_collapses_names_itself_instead_of_vanishing(monkeypatch):
    # One color repeated: the axis is dropped as a variation, and says so in the same words the
    # product card uses — «it went from 2 values to 1 and was removed», not silence.
    plan_dict = {"models": ["iPhone 13"], "attributes": {"رنگ": ["مشکی", "مشکی"]}}
    factory = _fake_extract(["iPhone 13"], attributes={"رنگ": ["مشکی", "مشکی"]},
                            to_dict=lambda: plan_dict)
    report = _call(monkeypatch, factory).report()
    assert "✂️ «رنگ» با 2 مقدار به 1 رسید و حذف شد" in report


def test_an_absent_color_matrix_is_silence_and_a_present_one_is_a_line():
    # «ماتریس رنگ تشخیص داده نشد» is the default state of a plain caption; repeating it on every
    # card reads like a failure. An affirmative summary is shown as the flow's own sentence.
    quiet = TextAnalysis(phone_models=("iPhone 13",), color_summary="ماتریس رنگ تشخیص داده نشد.")
    loud = TextAnalysis(phone_models=("iPhone 13",), color_summary="۲ رنگ روی ۲ مدل اعمال شد")
    assert "رنگ" not in quiet.report().replace("رنگ هر مدل", "")
    assert "🎨 ۲ رنگ روی ۲ مدل اعمال شد" in loud.report()


def test_the_short_summary_stays_the_short_summary():
    # A second, richer renderer was added; the old one was not quietly repurposed.
    text = format_product_summary(["iPhone 13"], {"رنگ": ["مشکی"]})
    assert text.splitlines()[0].startswith("🔎")
    assert "واریژن" not in text and "رنگ هر مدل" not in text
    assert "مدل گوشی" not in text


def test_compress_flow_sends_the_rich_report_not_the_two_lines(monkeypatch):
    from bot.modules import image_compress as ic

    factory = _fake_extract(
        ["iPhone 13", "iPhone 14"],
        attributes={"رنگ": ["مشکی", "سفید"]},
        model_colors={"iPhone 13": ["مشکی"], "iPhone 14": ["مشکی", "سفید"]},
        to_dict=lambda: {
            "models": ["iPhone 13", "iPhone 14"],
            "attributes": {"رنگ": ["مشکی", "سفید"]},
            "model_colors": {"iPhone 13": ["مشکی"]},
        },
    )
    monkeypatch.setattr(flow, "_extract", factory)
    monkeypatch.setattr(ic, "_can_compress", lambda user_id: True)

    sent: list[str] = []
    logged: list[str] = []

    async def _fake_log(context, text, *, parse_mode=None):
        logged.append(text)

    monkeypatch.setattr(ic, "_log_to_group", _fake_log)

    class _Bot:
        async def send_message(self, *, chat_id, text, **kwargs):
            sent.append(text)

    context = SimpleNamespace(
        bot=_Bot(),
        user_data={
            ic.ANALYSIS_CAPTIONS_KEY: ["قاب آیفون ۱۳ و ۱۴"],
            ic.ANALYSIS_INFO_KEY: ["رنگ: مشکی و سفید"],
        },
    )

    asyncio.run(ic._send_analysis(context, 7))

    joined = "\n".join(sent)
    assert "<b>مدل گوشی:</b> iPhone 13 | iPhone 14" in joined
    assert "<b>رنگ هر مدل:</b>" in joined
    assert re.search(r"<b>واریژن:</b> \d+ ترکیب", joined)
    # The group log carries the same body, so an audit can verify what a seller was told.
    assert logged and "خروجی همان پارسرِ ساخت محصول" in logged[0]
    assert "<b>رنگ هر مدل:</b>" in logged[0]
    # The old two-line renderer must not also be sent — one card, not a repeat.
    assert sum("نتیجهٔ تشخیص از کپشن و متن" in text for text in sent) == 1


def test_a_real_caption_is_parsed_by_the_real_parser():
    """No fake: what the deterministic parser (no AI configured) actually decides about a caption.

    The user's requirement was «باید درست تشخیص بده», so at least one test reads a seller-shaped
    caption with Persian digits and checks the models, colors and variation count that come out.
    """
    from dataclasses import replace

    from _flow_harness import patched_settings
    from bot.config import settings

    # Offline on purpose: a test that reaches a real AI endpoint is a test that fails
    # differently on every machine. The deterministic parser is what must be right.
    offline = replace(settings, ai_base_url="", ai_token="", ai_model="")

    with patched_settings(offline):
        analysis = asyncio.run(flow.analyze_text(
            "قاب چرم مشکی برای آیفون ۱۳ پرو مکس و سامسونگ A54",
            "رنگ: مشکی، سفید\nقیمت: ۴۵۰۰۰۰ تومان",
        ))
    report = analysis.report()
    joined_models = " | ".join(analysis.phone_models)
    assert "13" in joined_models and "Pro Max" in joined_models, joined_models
    assert "A54" in joined_models, joined_models
    assert len(analysis.phone_models) == 2
    assert "مشکی" in report and "سفید" in report
    assert re.search(r"<b>واریژن:</b> \d+ ترکیب", report), report
    # The count is what the seller would otherwise only learn after publishing.
    assert analysis.variation_count == 4, analysis.variation_count
    assert "پیدا نشد" not in report
