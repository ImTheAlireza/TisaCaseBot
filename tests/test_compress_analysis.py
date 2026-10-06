"""فاز ۱۵: کارتِ «🗜️ فشرده‌سازی عکس» = فقط *مقادیر*؛ بقیه در لاگ.

دو بار این کارت بازبینی شد: اول «هیچ‌چیز جز دو خط نبود» (پارسر یکی بود، خروجی نه)، بعد «هرچیز
 روی کارت بود» (چهارده بارِ یک فام، دو خطِ «AI پردازش شد»). نتیجهٔ نهایی همان چیزی است که
فروشنده می‌خواهد: مدل‌ها و ویژگی‌ها را نشان بده، اگر جایی را *نخوانده‌ای* بگو، و بقیه را برای
بازرسی در لاگِ گروه بنویس. تست‌های همین فایل این مرز را می‌بندند — نه به‌خاطرِ سلیقه، به‌خاطرِ
اینکه «شلوغ» یعنی «خوانده نمی‌شود»، و کارتی که خوانده نشود هیچی را تأیید نمی‌کند.
"""
from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace

from bot.modules import product_flow as flow
from bot.services.product_text_summary import TextAnalysis, format_product_summary


def _fake_extract(models, *, attributes=None, model_colors=None, warnings=(), suggestions=(),
                  notes=(), color_summary="", to_dict=None, ai_notes=()):
    """Stand in for the product parser, keeping only what the renderers are built from.

    ``to_dict`` is deliberately optional: a partial result must still render. The report is a
    courtesy on top of a finished compression, so it may not be the thing that raises.
    """

    async def _extract(session, **kwargs):
        session.models = list(models)
        session.color_summary = color_summary
        session.ai_diagnostics[:] = list(ai_notes)
        data = SimpleNamespace(
            attributes=dict(attributes or {}),
            model_colors=dict(model_colors or {}),
            warnings=list(warnings),
            notes=list(notes),
            suggestions=list(suggestions),
        )
        if to_dict is not None:
            data.to_dict = to_dict
        return data

    return _extract


def _call(monkeypatch, factory, *, caption="قاب چرم", info="رنگ: مشکی"):
    monkeypatch.setattr(flow, "_extract", factory)
    return asyncio.run(flow.analyze_text(caption, info))


def test_the_card_is_the_variables_and_nothing_else(monkeypatch):
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
        notes=("عنوان را هوش مصنوعی نوشته؛ اگر لازم شد اصلاحش کن",),
        color_summary="ماتریس رنگ اعمال شد",
        to_dict=lambda: plan_dict,
    )

    analysis = _call(monkeypatch, factory, caption="قاب چرم مشکی")
    report = analysis.report()

    assert "<b>مدل گوشی:</b> iPhone 13 | iPhone 14" in report
    assert "<b>ویژگی‌ها:</b> رنگ: مشکی، سفید؛ جنس: چرم" in report
    assert "⚠️ قیمت برای یک مدل پیدا نشد" in report          # a warning is still worth the card
    # …and that is the whole card. No variation maths, no repeated palette, no AI chatter.
    for noise in ("واریژن", "ترکیب", "✂️", "❔", "🎨", "نکته:", "رنگ هر مدل", "هوش مصنوعی نوشته"):
        assert noise not in report, noise
    assert len(report.splitlines()) <= 6, report

    # Everything the card folded away is in the log, once.
    detail = analysis.detail()
    assert "<b>واریژن:</b>" not in detail                      # the log is plain, not a second card
    assert "· واریژن: 3 ترکیب (مدل × رنگ)" in detail
    assert "محدودیتِ رنگ/سازگاری 1 ترکیب را حذف کرد" in detail
    assert "رنگ هر مدل: iPhone 13: مشکی | iPhone 14: مشکی/سفید" in detail
    assert "ماتریس: ماتریس رنگ اعمال شد" in detail
    assert "«الین» → «iPhone 11»" in detail
    assert "نکته: عنوان را هوش مصنوعی نوشته" in detail


def test_a_model_that_was_read_and_rejected_is_not_reported_as_absent(monkeypatch):
    factory = _fake_extract(["iPhone 13"], attributes={})
    analysis = _call(monkeypatch, factory, caption="iPhone 13 پرو پلاس و A54", info="")
    assert analysis.unmatched, "the tripwire needs a sample the parser can still apply"
    report = analysis.report()
    assert "🚫" in report and "plus" in report and "کامل خوانده نشد" in report


def test_a_long_rejected_line_is_quoted_as_much_as_it_matters():
    # One long caption line must not turn the one line meant to be *read* into a wall.
    label = "قاب " + "، ".join(f"iPhone {n} pro max" for n in range(13, 30))
    report = TextAnalysis(phone_models=("iPhone 13",), unmatched=((label, ("max",)),)).report()
    quoted = next(line for line in report.splitlines() if line.startswith("🚫"))
    assert len(quoted) < 260 and "…" in quoted


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


def test_a_partial_parser_result_renders_and_folds_nothing(monkeypatch):
    # The adapter contract with callers: a stand-in that only knows ``attributes`` still renders,
    # the model axis is never duplicated into the feature line, and a card with nothing folded
    # away produces no log tail at all (no empty «جزئیات» header).
    factory = _fake_extract(["iPhone 13"], attributes={"مدل": ["iPhone 13"], "رنگ": ["مشکی"]})
    analysis = _call(monkeypatch, factory)
    assert analysis.report().count("iPhone 13") == 1
    assert analysis.detail() == ""


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
    # seller's card they are noise; in the group log they are the trace of what ran.
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
    assert "خطای timeout" not in detail                        # never twice


def test_an_axis_that_collapses_is_said_in_the_log(monkeypatch):
    # One color repeated: the axis is dropped as a variation. Worth auditing, not worth a card.
    plan_dict = {"models": ["iPhone 13"], "attributes": {"رنگ": ["مشکی", "مشکی"]}}
    factory = _fake_extract(["iPhone 13"], attributes={"رنگ": ["مشکی", "مشکی"]},
                            to_dict=lambda: plan_dict)
    analysis = _call(monkeypatch, factory)
    assert "✂️" not in analysis.report()
    assert "«رنگ» با 2 مقدار به 1 رسید و حذف شد" in analysis.detail()


def test_report_escapes_the_shop_text():
    report = TextAnalysis(
        phone_models=("<b>13</b>",),
        attributes={"رنگ": ["<script>x</script>"]},
    ).report()
    assert "<b>مدل گوشی:</b> &lt;b&gt;13&lt;/b&gt;" in report
    assert "&lt;script&gt;" in report and "<script>" not in report
    assert "✂️" not in report


def test_the_short_summary_stays_the_short_summary():
    # The two-line renderer is no longer called by the bot, but its shape is still pinned.
    text = format_product_summary(["iPhone 13"], {"رنگ": ["مشکی"]})
    assert text.splitlines()[0].startswith("🔎")
    assert "واریژن" not in text and "رنگ هر مدل" not in text


def test_compress_flow_sends_the_card_and_logs_the_rest(monkeypatch):
    from bot.modules import image_compress as ic

    factory = _fake_extract(
        ["iPhone 13", "iPhone 14"],
        attributes={"رنگ": ["مشکی", "سفید"]},
        model_colors={"iPhone 13": ["مشکی"], "iPhone 14": ["مشکی", "سفید"]},
        ai_notes=("استخراج جزئیات: پاسخ AI پردازش شد",),
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

    card = "\n".join(sent)
    assert "<b>مدل گوشی:</b> iPhone 13 | iPhone 14" in card
    assert "رنگ: مشکی، سفید" in card
    assert "واریژن" not in card and "رنگ هر مدل" not in card
    assert sum("نتیجهٔ تشخیص از کپشن و متن" in text for text in sent) == 1   # one card, not two

    # The group log carries the card *plus* what was folded, so an audit can still verify the plan.
    assert logged and "خروجی همان پارسرِ ساخت محصول" in logged[0]
    assert "جزئیات (فشرده‌شده در کارت)" in logged[0]
    assert re.search(r"واریژن: \d+ ترکیب", logged[0])
    assert "رنگ هر مدل: iPhone 13: مشکی" in logged[0]
    assert "یادداشت AI: استخراج جزئیات" in logged[0]


def test_a_real_caption_is_parsed_by_the_real_parser():
    """No fake: what the deterministic parser (no AI configured) decides about a caption.

    The requirement was «باید درست تشخیص بده», so at least one test reads a seller-shaped caption
    with Persian digits and checks the values that come out of it.
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
    # The maths is still computed (so the log has it) — it just is not on the card.
    assert analysis.variation_count == 4, analysis.variation_count
    assert "واریژن" not in report and "واریژن: 4 ترکیب" in analysis.detail()
    assert "پیدا نشد" not in report
