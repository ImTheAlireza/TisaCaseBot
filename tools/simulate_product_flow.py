#!/usr/bin/env python3
"""شبیه‌سازی کاملِ فلوی «🆕 محصول جدید» — بدون تلگرام، بدون سایت، بدون AI.

این اسکریپت همان هندلرهای واقعی (`entry`, `on_media`, `on_text`, `field_value`,
`undo`, `confirm`) را با یک تلگرامِ ساختگی اجرا می‌کند و در هر گام نشان می‌دهد:

* چه ورودی‌ای پذیرفته می‌شود و به چه چیزی ترجمه می‌شود،
* در هر لحظه کدام بخشِ تحلیل اجرا شده (پارسر قطعی، ماتریس موجودی، AI، اعتبارسنجی)،
* کارت پیش‌نمایش مرحله‌به‌مرحله چه شکلی می‌شود،
* و در پایان، خروجیِ نهایی و همان چیزی که به ووکامرس می‌رفت (با ``TISA_DRY_RUN``).

اجرا:

    .venv/bin/python tools/simulate_product_flow.py

هیچ درخواستی به تلگرام یا سایت نمی‌رود: نه شبکه‌ای خوانده می‌شود و نه چیزی نوشته
می‌شود. داده‌های موقت در ``/tmp/tisa-product-sim`` می‌مانند، نه در ``data/`` مخزن.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPO = Path(__file__).resolve().parents[1]
for path in (str(REPO), str(REPO / "tests")):
    if path not in sys.path:
        sys.path.insert(0, path)

SIM_DATA = Path("/tmp/tisa-product-sim")
shutil.rmtree(SIM_DATA, ignore_errors=True)
SIM_DATA.mkdir(parents=True)

os.environ.setdefault("BOT_TOKEN", "123456:SIM")
os.environ.setdefault("SUDO_IDS", "1234567")
os.environ["TISA_DATA_DIR"] = str(SIM_DATA)
os.environ["TISA_DRY_RUN"] = "yes"
os.environ["TISA_CONTRACT"] = "0"

from _flow_harness import patched_settings, settings_with
from bot.modules import product_flow as PF
from bot.services import woocommerce_direct as WOO
from bot.services.plan import plan_from_dict
from bot.services.product_extractor import _fallback
from bot.services.phone_parser import normalize_caption
from bot.services.stock_matrix import parse_stock_matrix_sources
from bot.services.validation import validate_draft

W = 78
CHAT_ID = 7
LOG_CHAT_ID = -1001234567890
STATE_NAMES = {PF.COLLECT: "COLLECT", PF.EDITING_FIELD: "EDITING_FIELD", PF.REVIEW: "REVIEW",
               PF.ConversationHandler.END: "END"}


# ---------------------------------------------------------------------------
# خروجی: چاپ خوانا
# ---------------------------------------------------------------------------
def title(text: str) -> None:
    print("\n" + "═" * W)
    print(f"  {text}")
    print("═" * W)


def section(text: str) -> None:
    print("\n" + "─" * W)
    print(f"▪️ {text}")
    print("─" * W)


def block(label: str, body: str, *, indent: str = "│ ") -> None:
    print(f"┌─ {label}")
    for line in (body or "").rstrip().splitlines():
        print(indent + line)
    print("└" + "─" * (W - 1))


def kv(label: str, value: object) -> None:
    print(f"   {label:<34} {value}")


def strip_tags(text: str) -> str:
    text = re.sub(r"<pre>(.*?)</pre>", r"\1", text or "", flags=re.S)
    text = re.sub(r"<[^>]+>", "", text)
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def card_preview(text: str, limit: int = 10) -> str:
    lines = strip_tags(text).splitlines()
    if len(lines) > limit:
        lines = [*lines[:limit], "…"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# تلگرام ساختگی: چت را نگه می‌دارد تا آخر «از نگاه فروشنده» چاپ شود
# ---------------------------------------------------------------------------
class Chat:
    def __init__(self) -> None:
        self.messages: dict[int, str] = {}
        self.versions: dict[int, int] = {}
        self.order: list[int] = []
        self.deleted: set[int] = set()
        self.next_id = 500
        self.wire: list[str] = []
        self.events: list[tuple[str, int, str]] = []
        self.log_group: list[str] = []

    def new_id(self) -> int:
        self.next_id += 1
        return self.next_id

    def render(self) -> str:
        lines = []
        for mid in self.order:
            if mid in self.deleted:
                lines.append(f"#{mid}  🗑 (کارتِ قبلی — حذف شد)")
                continue
            text = strip_tags(self.messages.get(mid, ""))
            head = text.splitlines()[0] if text else "(خالی)"
            lines.append(f"#{mid}  {head}")
        return "\n".join(lines) or "(چت خالی)"


CHAT = Chat()


class FakeBot:
    """همان قراردادِ باتِ تلگرام، فقط به‌جای شبکه روی یک چتِ ساختگی."""

    async def send_message(self, text: str | None = None, **kw: Any) -> Any:
        if int(kw.get("chat_id") or CHAT_ID) != CHAT_ID:
            CHAT.log_group.append(text or "")
            CHAT.wire.append("LOG    (گروه لاگ)")
            return SimpleNamespace(message_id=CHAT.new_id())
        mid = CHAT.new_id()
        CHAT.messages[mid] = text or ""
        CHAT.order.append(mid)
        CHAT.versions[mid] = 1
        CHAT.events.append(("SEND", mid, text or ""))
        CHAT.wire.append(f"SEND   #{mid}")
        return SimpleNamespace(message_id=mid)

    async def edit_message_text(self, text: str | None = None, **kw: Any) -> Any:
        mid = int(kw.get("message_id") or 0)
        CHAT.messages[mid] = text or ""
        CHAT.versions[mid] = CHAT.versions.get(mid, 1) + 1
        CHAT.events.append(("EDIT", mid, text or ""))
        CHAT.wire.append(f"EDIT   #{mid}")
        return SimpleNamespace(message_id=mid)

    async def delete_message(self, **kw: Any) -> bool:
        mid = int(kw.get("message_id") or 0)
        CHAT.deleted.add(mid)
        CHAT.messages.pop(mid, None)
        CHAT.wire.append(f"DELETE #{mid}")
        return True

    async def send_chat_action(self, **kw: Any) -> None:
        CHAT.wire.append("typing… (گذرا)")

    async def set_message_reaction(self, **kw: Any) -> bool:
        CHAT.wire.append(f"REACT  #{kw.get('message_id')} {kw.get('reaction')}")
        return True

    async def send_document(self, **kw: Any) -> Any:
        mid = CHAT.new_id()
        CHAT.wire.append(f"DOC    #{mid}")
        return SimpleNamespace(message_id=mid)


BOT = FakeBot()
CONTEXT = SimpleNamespace(bot=BOT, chat_data={})
USER = SimpleNamespace(id=7, username="seller", first_name="فروشنده")
ANALYSIS: list[str] = []
TOASTS: list[str] = []


def _message(text: str = "", *, caption: str = "", photo: bool = False, mid: int = 10,
             group: str | None = None) -> SimpleNamespace:
    async def reply_text(body: str, **kw: Any) -> Any:
        return await BOT.send_message(body, **kw)

    media = [SimpleNamespace(file_id=f"file-{mid}")] if photo else None
    return SimpleNamespace(
        text=text, caption=caption or None, photo=media, document=None,
        chat_id=CHAT_ID, message_thread_id=None, media_group_id=group, message_id=mid,
        reply_text=reply_text,
    )


def user_text(text: str, mid: int) -> SimpleNamespace:
    return SimpleNamespace(effective_user=USER, effective_message=_message(text, mid=mid))


def user_photo(caption: str, mid: int, group: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        effective_user=USER,
        effective_message=_message(caption=caption, photo=True, mid=mid, group=group),
    )


def callback(data: str, label: str, *, card_id: int) -> SimpleNamespace:
    async def answer(text_: str | None = None, **kw: Any) -> None:
        if text_:
            TOASTS.append(f"{label}: {text_}")

    async def edit_message_text(body: str | None = None, **kw: Any) -> Any:
        return await BOT.edit_message_text(body, message_id=card_id, **kw)

    message = SimpleNamespace(chat_id=CHAT_ID, message_thread_id=None, message_id=card_id,
                              text="", reply_text=lambda *a, **k: None,
                              edit_message_text=edit_message_text)
    query = SimpleNamespace(data=data, from_user=USER, message=message, answer=answer,
                            edit_message_text=edit_message_text)
    return SimpleNamespace(callback_query=query, effective_user=USER, effective_message=message)


# ---------------------------------------------------------------------------
# ردیابِ تحلیل: هر تابعِ واقعیِ تحلیل را می‌پیچد و می‌گوید چه شد و چقدر طول کشید
# ---------------------------------------------------------------------------
def _record(name: str, detail: str, ms: float) -> None:
    ANALYSIS.append(f"{name:<26} {detail}   [{ms:.0f} ms]")


def trace_sync(module: Any, name: str, fmt: Any) -> None:
    original = getattr(module, name)

    def wrapper(*a: Any, **k: Any) -> Any:
        started = time.perf_counter()
        result = original(*a, **k)
        _record(name, fmt(a, k, result), (time.perf_counter() - started) * 1000)
        return result

    setattr(module, name, wrapper)


def trace_async(module: Any, name: str, fmt: Any) -> None:
    original = getattr(module, name)

    async def wrapper(*a: Any, **k: Any) -> Any:
        started = time.perf_counter()
        result = await original(*a, **k)
        _record(name, fmt(a, k, result), (time.perf_counter() - started) * 1000)
        return result

    setattr(module, name, wrapper)


def _short(value: Any, limit: int = 44) -> str:
    text = str(value).replace("\n", " ⏎ ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _ai_note() -> str:
    return "" if PF.settings.ai_base_url else "  (AI خاموش → همان نتیجهٔ پارسر قطعی)"


def install_tracers() -> None:
    trace_sync(PF, "normalize_caption",
               lambda a, k, r: f"متن {len(str(a[0]))} کاراکتری → {_short(r) or '—'}")
    trace_sync(PF, "parse_stock_matrix_sources",
               lambda a, k, r: (f"جدول پیدا شد: {len(r.designs)} طرح × {len(r.models)} مدل"
                                if r.found else "جدولی پیدا نشد"))
    trace_sync(PF, "_fallback",
               lambda a, k, r: f"قیمت {r.price:,} · موجودی {r.stock} · عنوان «{_short(r.title, 22)}»")
    trace_async(PF, "ai_normalize",
                lambda a, k, r: f"ورودی «{_short(a[0], 22)}» → {_short(r)}{_ai_note()}")
    trace_async(PF, "extract_product",
                lambda a, k, r: (f"عنوان «{_short(r.title, 22)}» · قیمت {r.price:,} · "
                                 f"SKU {r.sku_prefix or '—'} · {len(r.models)} مدل{_ai_note()}"))
    trace_sync(PF, "validate_draft",
               lambda a, k, r: f"{len(r.errors)} خطا · {len(r.warnings)} هشدار"
                               + (" ← مسدودکننده" if r.blocking else ""))
    trace_sync(PF.draft_edits, "apply_edit",
               lambda a, k, r: f"فیلد «{a[1]}» → " + (f"رد شد: {_short(r, 26)}" if r else "اعمال شد"))


def drain_analysis() -> str:
    text = "\n".join(ANALYSIS)
    ANALYSIS.clear()
    return text


# ---------------------------------------------------------------------------
# استاب‌های فایل: دانلود و فشرده‌سازی تلگرامی بدون شبکه
# ---------------------------------------------------------------------------
async def fake_download(context: Any, file_id: str, target: Path, attempts: int = 3) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\xff\xd8\xff\xe0" + b"sim" * 800)
    return target.stat().st_size


def fake_compress(source: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / Path(source).name
    shutil.copy(source, target)
    return target


# ---------------------------------------------------------------------------
# گام‌ها
# ---------------------------------------------------------------------------
async def step(number: str, label: str, coro: Any) -> Any:
    section(f"گام {number} — {label}")
    wire_before = len(CHAT.wire)
    events_before = len(CHAT.events)
    toasts_before = len(TOASTS)
    result = await coro
    analysis = drain_analysis()
    events = CHAT.events[events_before:]
    if analysis:
        print("🧠 تحلیلِ اجراشده:")
        for line in analysis.splitlines():
            print(f"   • {line}")
    if events:
        print("🃏 کارت (به ترتیبِ نسخه‌ها):")
        for index, (op, mid, text) in enumerate(events, 1):
            tag = "فرستاده شد" if op == "SEND" else "ویرایش شد"
            print(f"   ── #{mid} · نسخهٔ {index} ({tag})")
            for line in card_preview(text, limit=9).splitlines():
                print(f"      {line}")
    wire = CHAT.wire[wire_before:]
    if wire:
        print("📡 ربات: " + " · ".join(wire))
    for toast in TOASTS[toasts_before:]:
        print(f"   • TOAST گذرا: {toast}")
    if result is not None:
        print(f"🔀 وضعیت مکالمه پس از این گام: {STATE_NAMES.get(result, str(result))}")
    return result


def current_card() -> int:
    return PF.sessions[USER.id].status_message_id


# ---------------------------------------------------------------------------
# بخش ۰: کاتالوگ ورودی‌ها — با همان توابعی که جریان استفاده می‌کند
# ---------------------------------------------------------------------------
def schematic() -> None:
    print("""
   ┌── ورودیِ فروشنده ─────────────────────────────────────────────────────┐
   │  🖼 عکس/آلبوم + کپشن مدل‌ها      ✍️ متن آزاد: عنوان، قیمت، SKU،        │
   │                                    موجودی، رنگ، جدول طرح×مدل          │
   │  🔘 دکمه‌های کارت: اصلاح فیلد · افزودن عکس/متن · جدا کردن رنگ ·        │
   │                    برگرداندن به حالت قبل · تأیید · لغو                 │
   └───────────────┬───────────────────────────────────────┬───────────────┘
                   ▼                                       ▼
        مسیر رسانه (on_media)                     مسیر متن (on_text)
        دانلود → فشرده‌سازی                       افزودن به info_text
                   └───────────────┬───────────────────────┘
                                   ▼
                    ┌───────────────────────────────┐
                    │  تحلیل (_extract) — محلی اول   │
                    │  واژه‌نامه → ماتریس موجودی →   │
                    │  پارسر مدل → (AI در صورت بودن) │
                    │  → قیمت هر واریژن → پلن →      │
                    │  اعتبارسنجی → شواهد (evidence) │
                    └──────────────┬────────────────┘
                                   ▼
        کارت پیش‌نمایش مرحله‌ای (پایین چت)  →  ✅ تأیید → پیش‌نویس/زیپ
""")


def inputs_catalogue() -> None:
    section("بخش ۰ — چه ورودی‌هایی می‌گیرد و هر کدام کجا تحلیل می‌شود")
    rows = [
        ("عکس / آلبوم عکس", "on_media → دانلود → فشرده‌سازی → کارت"),
        ("کپشن عکس", "نرمال‌سازی واژه‌نامه + normalize_caption + AI"),
        ("متن آزاد", "on_text → اطلاعات محصول (عنوان، قیمت، SKU، موجودی، ویژگی)"),
        ("جدول «موجودی ماتریسی:»", "parse_stock_matrix_sources → طرح × مدل"),
        ("دکمه‌های کارت", "اصلاح فیلد · افزودن · جداکردن رنگ · برگرداندن · تأیید · لغو"),
    ]
    for name, where in rows:
        print(f"   {name:<26} → {where}")

    print("\n   نمونه‌ها (خروجیِ همین حالا از خودِ توابع جریان):\n")
    for text in ("قیمت ۶۹۸ت", "SKU: BO147", "موجودی ۲۰", "قیمت ویژه ۴۹۸", "ناموجود",
                 "رنگ: مشکی | سفید"):
        data = _fallback(text, [])
        bits = []
        if data.price:
            bits.append(f"قیمت {data.price:,}")
        if data.sale_price:
            bits.append(f"قیمت ویژه {data.sale_price:,}")
        if data.stock is not None:
            bits.append(f"موجودی {data.stock}")
        if data.stock_status:
            bits.append(data.stock_status)
        if data.sku_prefix:
            bits.append(f"SKU {data.sku_prefix}")
        if data.attributes:
            bits.append(" ، ".join(f"{k}: {'، '.join(v)}" for k, v in data.attributes.items()))
        print(f"   «{text}»".ljust(34) + " → " + (" · ".join(bits) or "—"))

    caption = "آیفون ۱۳ پرو | ۱۳ پرو مکس"
    print(f"\n   کپشن «{caption}»".ljust(34) + f" → مدل: {normalize_caption(caption) or '—'}")
    matrix_text = ("موجودی ماتریسی:\nطرح | iPhone 13 | iPhone 13 Pro/13 Pro Max\n"
                   "پروانه آبی | ۷ | ۰\nپاپیون صورتی | - | ۳")
    matrix = parse_stock_matrix_sources([("info", matrix_text)])
    print("   جدول طرح × مدل".ljust(34)
          + f" → {len(matrix.designs)} طرح × {len(matrix.models)} مدل · "
            f"{matrix.total_stock} عدد · {matrix.sellable_cells} خانهٔ قابل فروش")
    print("\n   قاعدهٔ قیمت هر واریژن: قیمت اختصاصی مدل → گروه (iphone/android) → قیمت پایه")


# ---------------------------------------------------------------------------
# اجرای سناریو
# ---------------------------------------------------------------------------
async def send_album(caption: str) -> Any:
    """دو عکسِ یک آلبوم؛ دقیقاً همان مسیری که تایمر آلبوم در ربات می‌رود."""
    await PF.on_media(user_photo(caption, 11, group="g1"), CONTEXT)
    await PF.on_media(user_photo(caption, 12, group="g1"), CONTEXT)
    task = PF.album_tasks.pop((USER.id, "g1"), None)
    if task is not None:
        task.cancel()
    await PF._flush_album((USER.id, "g1"), CONTEXT)
    return PF.REVIEW if PF.sessions[USER.id].data is not None else PF.COLLECT


async def run() -> None:
    title("شبیه‌سازی فلوی «🆕 محصول جدید» — بدون تلگرام، بدون سایت")
    kv("AI", "خاموش (پارسر قطعی)")
    kv("TISA_DRY_RUN", "yes — هیچ درخواستی به ووکامرس نمی‌رود")
    kv("فایل‌های موقت", str(SIM_DATA))
    schematic()
    inputs_catalogue()

    title("اجرای گام‌به‌گامِ یک محصول واقعی")

    await step("۱", "تپ «🆕 محصول جدید» (mode=new)",
               PF.entry(callback("phone:new", "🆕", card_id=1), CONTEXT))
    guide = PF.sessions[USER.id].guide_message_id
    kv("پیام راهنما", f"#{guide} — از این لحظه هرگز ویرایش/حذف نمی‌شود")

    await step("۲", "آلبوم دو عکس با کپشن «آیفون ۱۳ پرو | ۱۳ پرو مکس»",
               send_album("آیفون ۱۳ پرو | ۱۳ پرو مکس"))

    await step("۳", "متن اطلاعات: عنوان، SKU، قیمت، موجودی",
               PF.on_text(user_text("قاب سیلیکونی مگنتی آیفون 13 پرو\nSKU: BO\nقیمت ۶۹۸ت\nموجودی ۲۰", 20),
                          CONTEXT))
    kv("قیمت روی پیش‌نویس", f"{PF.sessions[USER.id].data.price:,} تومان")

    await step("۴", "اصلاحِ بدفهمیده: «قیمت ۱۲۰»",
               PF.on_text(user_text("قیمت ۱۲۰", 21), CONTEXT))
    broken = PF.sessions[USER.id].data.price
    kv("کارت خراب شد", f"{broken:,} تومان")

    await step("۵", "تپ «↩️ برگرداندن به حالت قبل»",
               PF.undo(callback("product:undo", "↩️", card_id=current_card()), CONTEXT))
    kv("بعد از undo", f"{PF.sessions[USER.id].data.price:,} تومان (بدون هیچ درخواست تازه)")

    await step("۶", "پیام رنگ‌ها: «رنگ: مشکی | سفید»",
               PF.on_text(user_text("رنگ: مشکی | سفید", 22), CONTEXT))

    await step("۷", "تپ «✏️ اصلاح فیلد خاص»",
               PF.edit(callback("product:edit", "✏️", card_id=current_card()), CONTEXT))
    field_keys = PF.sessions[USER.id].field_keys
    price_index = field_keys.index("price") if "price" in field_keys else 0
    await step("۸", f"تپ فیلد «قیمت» (index={price_index})",
               PF.pick_field(callback(f"product:field:{price_index}", "قیمت", card_id=current_card()),
                             CONTEXT))
    await step("۹", "تایپ مقدار تازهٔ قیمت (وضعیت EDITING_FIELD)",
               PF.field_value(user_text("712000", 23), CONTEXT))

    await step("۱۰", "همان اصلاح بد، این بار بعد از ویرایش دستی: «قیمت ۱۲۰»",
               PF.on_text(user_text("قیمت ۱۲۰", 24), CONTEXT))
    kv("قیمت پس از متن بد", f"{PF.sessions[USER.id].data.price:,} تومان "
                            "← قفلِ ویرایش دستی اجازهٔ خراب‌شدن نمی‌دهد")

    session = PF.sessions[USER.id]
    data = session.data
    title("تحلیل نهایی: از ورودی به پیش‌نویسِ ووکامرس")

    section("۱) دادهٔ استخراج‌شده (ProductData → product.json)")
    block("product.json", json.dumps(data.to_dict(), ensure_ascii=False, indent=2))

    plan = plan_from_dict(data.to_dict())
    section("۲) پلن واریژن‌ها (plan_from_dict)")
    kv("نوع محصول", "متغیر" if plan.is_variable else "ساده")
    kv("تعداد واریژن", plan.count)
    for axis, values in plan.axes:
        kv(f"محور {axis}", " | ".join(values))

    attributes = WOO._attributes(data.to_dict())
    combinations = WOO._combinations(attributes)
    section("۳) همان چیزی که به ووکامرس می‌رود — مدلِ داخلی")
    kv("attributes", ", ".join(f"{a['name']}(variation={a['variation']})" for a in attributes) or "—")
    kv("ترکیب واریژن‌ها", f"{len(combinations)} عدد → " + " ، ".join(
        "/".join(sorted(c.values())) for c in combinations[:4])
       + (" …" if len(combinations) > 4 else ""))
    issues = validate_draft(data.to_dict(), mode="new", image_count=len(session.files),
                            price_min=PF.settings.price_min, price_max=PF.settings.price_max,
                            require_models=PF.settings.require_models)
    kv("اعتبارسنجی", f"{len(issues.errors)} خطا · {len(issues.warnings)} هشدار"
                     + (" ← مسدودکننده" if issues.blocking else " ← عبور می‌کند"))

    section("۴) تأیید و ساخت (اجرای آزمایشی — چیزی روی سایت ساخته نمی‌شود)")
    wire_before = len(CHAT.wire)
    await PF.confirm(callback("product:confirm", "✅ تأیید", card_id=current_card()), CONTEXT)
    print("📡 ربات: " + " · ".join(CHAT.wire[wire_before:]))
    for toast in TOASTS[-1:]:
        print(f"   • TOAST گذرا: {toast}")

    title("چت از نگاه فروشنده (در پایان)")
    print(CHAT.render())
    print("\n   ↑ کارت‌های 🗑 همان «کارت قبلی» هستند که با هر پیام پاک شدند تا فقط یک")
    print("     پیش‌نمایش زنده بماند؛ پیام راهنما و کارت نتیجه سر جای خودشان‌اند.")

    if CHAT.log_group:
        title("گروه لاگ (LOG_CHAT_ID) — همان‌جا که لاگ‌ها می‌روند، نه در پیوی کاربر")
        for message in CHAT.log_group[:2]:
            print(strip_tags(message))


def main() -> int:
    logging.getLogger("bot").setLevel(logging.ERROR)
    install_tracers()
    PF._download_with_retry = fake_download
    PF.compress_image = fake_compress
    PF.feature_allowed = lambda user_id, key: True

    shop = settings_with(woo_dry_run=True, album_wait_seconds=0.0, log_chat_id=LOG_CHAT_ID,
                         ai_base_url="", ai_token="", ai_model="")
    with patched_settings(shop):
        asyncio.run(run())
    print(f"\nپایان شبیه‌سازی — هیچ درخواستی به تلگرام یا سایت نرفت. فایل‌های موقت: {SIM_DATA}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
