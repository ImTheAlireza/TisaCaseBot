#!/usr/bin/env python3
"""شبیه‌سازی فلوی «🔄 اپدیت محصول» — هندلرهای واقعی، تلگرام ساختگی، فروشگاهی که یادش می‌ماند.

هر صحنه یک گفتگوی کامل است: دکمه ← جست‌وجو ← انتخاب محصول ← فرستادن عکس/متن ← کارتِ
مقایسه ← «✅ اعمال تغییرات»؛ و در پایانِ هر صحنه، آنچه فروشگاه *واقعاً* نگه داشته چاپ می‌شود.

* صحنهٔ ۱ مسیر اصلی: عکس + مدل‌های تازه + قیمت + موجودی ← همهٔ واریژن‌ها پاک و از نو ساخته می‌شوند.
* صحنهٔ ۲ «ننوشتن یعنی دست‌نخوردن»: فقط موجودی، قیمت همان قبلی می‌ماند.
* صحنهٔ ۳ خطِ تصادفی («سلام») عنوان محصول را عوض نمی‌کند.
* صحنهٔ ۴ فروشگاه وسط کار عوض شده: کارت دوباره نشان داده می‌شود و چیزی نوشته نمی‌شود.
* صحنهٔ ۵ ساختنِ واریژن‌ها وسط کار می‌شکند؛ دوباره زدنِ همان دکمه شبکه را کامل می‌کند، بی‌تکرار.
* صحنهٔ ۶ فهرستِ مدل بدون قیمت و موجودی: واریژن‌ها از نو ساخته می‌شوند، قیمت/موجودی قبلی می‌ماند.
* صحنهٔ ۷ فهرستی که همان فهرستِ فروشگاه است: هیچ بازسازی‌ای نیست، فقط قیمت درجا عوض می‌شود.

اجرا:

    .venv/bin/python tools/simulate_update_flow.py

هیچ درخواستی به تلگرام یا سایت نمی‌رود. داده‌های موقت در ``/tmp/tisa-update-sim`` می‌مانند.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPO = Path(__file__).resolve().parents[1]
for path in (str(REPO), str(REPO / "tests")):
    if path not in sys.path:
        sys.path.insert(0, path)

SIM_DATA = Path("/tmp/tisa-update-sim")
shutil.rmtree(SIM_DATA, ignore_errors=True)
SIM_DATA.mkdir(parents=True)
os.environ.setdefault("BOT_TOKEN", "123456:SIM")
os.environ.setdefault("SUDO_IDS", "1234567")
os.environ["TISA_DATA_DIR"] = str(SIM_DATA)
os.environ["TISA_CONTRACT"] = "0"

import logging

logging.disable(logging.CRITICAL)

from _flow_harness import message_update, no_sleep, patched_settings, query_update, settings_with, temp_ledger
from _update_shop import UpdateShop
from PIL import Image

from bot.constants import CB
from bot.modules import product_flow as PF
from bot.modules import restock_flow as RF
from bot.services import product_match, update_apply

W = 78
USER, CHAT, LOG_CHAT = 7, 9, -100777


def strip_tags(text: object) -> str:
    return re.sub(r"<[^>]+>", "", str(text or "")).replace("&amp;", "&")


def title(text: str) -> None:
    print("\n" + "═" * W)
    print(f"  {text}")
    print("═" * W)


def say(who: str, text: str) -> None:
    print(f"\n{who}")
    for line in str(text).splitlines() or [""]:
        print(f"   │ {line}")


def keyboard(markup: Any) -> str:
    rows = getattr(markup, "inline_keyboard", None) or []
    return "   ".join("[" + button.text + "]" for row in rows for button in row)


class Chat:
    """The seller's chat: every message, edit, deletion, reaction — printed as it happens."""

    def __init__(self) -> None:
        self.next_id = 100
        self.sent_to_log: list[str] = []

    def _where(self, kwargs: dict) -> str:
        return "گروه لاگ" if kwargs.get("chat_id") == LOG_CHAT else "چت فروشنده"

    async def send_message(self, text=None, **kwargs):
        self.next_id += 1
        if kwargs.get("chat_id") == LOG_CHAT:
            self.sent_to_log.append(strip_tags(text))
            first = strip_tags(text).splitlines()[0]
            print(f"\n   ⮕ {self._where(kwargs)} · پیام #{self.next_id}: {first[:70]}")
            return SimpleNamespace(message_id=self.next_id)
        say(f"🤖 پیام تازه #{self.next_id}", strip_tags(text))
        if kwargs.get("reply_markup") is not None:
            print(f"   ⌨️  {keyboard(kwargs['reply_markup'])}")
        return SimpleNamespace(message_id=self.next_id)

    async def edit_message_text(self, text=None, **kwargs):
        say(f"✏️  ویرایش پیام #{kwargs.get('message_id')}", strip_tags(text))
        if kwargs.get("reply_markup") is not None:
            print(f"   ⌨️  {keyboard(kwargs['reply_markup'])}")
        return SimpleNamespace(message_id=kwargs.get("message_id"))

    async def delete_message(self, **kwargs):
        print(f"\n🗑  پیام #{kwargs.get('message_id')} پاک شد (کارتِ قبلی)")
        return True

    async def send_chat_action(self, **kwargs):
        return None

    async def set_message_reaction(self, **kwargs):
        print(f"\n⚡ reaction روی پیام #{kwargs.get('message_id')}")
        return True

    async def get_file(self, file_id):
        async def download_to_drive(*, custom_path):
            Image.new("RGB", (16, 16), (200, 30, 30)).save(custom_path, "JPEG")

        return SimpleNamespace(file_size=100, download_to_drive=download_to_drive)


class Scene:
    def __init__(self, label: str, shop: UpdateShop | None = None) -> None:
        title(label)
        self.shop = shop or UpdateShop()
        self.chat = Chat()
        self.ctx = SimpleNamespace(bot=self.chat, chat_data={}, job_queue=None)
        PF.sessions.clear()
        RF.sessions.clear()
        self._install()

    def _install(self) -> None:
        real_find, real_read, real_apply = _REAL
        shop = self.shop

        async def find(query, **kwargs):
            kwargs.pop("dry_run", None)
            return await real_find(query, transport=shop.transport, **kwargs)

        async def read(product_id, **kwargs):
            kwargs.pop("dry_run", None)
            return await real_read(product_id, transport=shop.transport, **kwargs)

        async def apply(plan, files=(), **kwargs):
            kwargs.pop("dry_run", None)
            return await real_apply(plan, files, transport=shop.transport, **kwargs)

        product_match.find, product_match.read, update_apply.apply = find, read, apply

    # — the seller's moves —
    def tap(self, handler, data: str, label: str):
        print(f"\n👤 [تپ] {label}")
        update, seen = query_update(data, user_id=USER, chat_id=CHAT)
        result = asyncio.run(handler(update, self.ctx))
        for kind, text, kwargs in seen:
            if kind == "answer" and text:
                print(f"   🔔 toast{' (هشدار)' if kwargs.get('show_alert') else ''}: {text}")
        return result

    def type(self, text: str, handler=None, message_id: int = 40):
        say("👤 فروشنده می‌نویسد", text)
        update, seen = message_update(text, user_id=USER, chat_id=CHAT)
        update.effective_message.message_id = message_id
        update.effective_message.from_user = SimpleNamespace(id=USER)
        result = asyncio.run((handler or PF.on_text)(update, self.ctx))
        for kind, body, _ in seen:
            if kind == "text":
                say("🤖 پاسخ", strip_tags(body))
        return result

    def photos(self, count: int) -> None:
        print(f"\n👤 [{count} عکس می‌فرستد]")
        messages = [SimpleNamespace(message_id=70 + i, photo=[SimpleNamespace(file_id=f"f{i}")],
                                    document=None, caption=None, text=None, media_group_id=None)
                    for i in range(count)]
        asyncio.run(PF._prepare_files(USER, messages, self.ctx))

    def open_product(self) -> None:
        self.tap(PF.entry, CB.PHONE_RESTOCK, "🔄 اپدیت محصول موجود")
        self.type("BO7", handler=RF.handle_search)
        self.tap(RF.pick, f"{CB.RESTOCK_PICK}:1201", "کاندیدای اول")

    def confirm(self):
        return self.tap(PF.confirm, "product:confirm", "✅ اعمال تغییرات")

    # — the shop —
    def shop_table(self, label: str) -> None:
        rows = sorted(self.shop.variations.values(),
                      key=lambda row: tuple(item["option"] for item in row["attributes"]))
        print(f"\n🏬 فروشگاه — {label}")
        print(f"   عنوان: {self.shop.product['name']} · گالری: {len(self.shop.product['images'])} تصویر")
        print(f"   مدل‌ها: {' | '.join(self.shop.options('مدل'))}   رنگ‌ها: {' | '.join(self.shop.options('رنگ'))}")
        for row in rows:
            values = " × ".join(item["option"] for item in row["attributes"])
            stock = row.get("stock_quantity")
            picture = f"  🖼{row['image']['id']}" if row.get("image") else ""
            print(f"   • #{row['id']}  {values:<30} قیمت {int(row['regular_price'] or 0):>9,}   "
                  f"موجودی {stock if stock is not None else '—'}{picture}")

    def writes(self) -> int:
        return len([call for call in self.shop.requests if call[0] != "GET"])


_REAL = (product_match.find, product_match.read, update_apply.apply)


def scene_main_path() -> None:
    scene = Scene("صحنهٔ ۱ — مسیر اصلی: عکس + مدل‌های تازه + قیمت + موجودی")
    scene.shop_table("قبل")
    old_ids = set(scene.shop.variations)
    scene.open_product()
    scene.photos(2)
    scene.type("iPhone 13 Pro Max\niPhone 15\niPhone 15 Pro\nقیمت 720000 تومان\nموجودی 12", message_id=41)
    print(f"\n   (تا اینجا درخواستِ نوشتن به فروشگاه: {scene.writes()})")
    scene.confirm()
    scene.shop_table("بعد")
    print(f"\n   شناسهٔ واریژن‌های قبلی که هنوز مانده: {sorted(old_ids & set(scene.shop.variations)) or 'هیچ‌کدام'}"
          f" · واریژن‌های فعلی: {len(scene.shop.variations)}")
    print(f"   درخواست‌های نوشتن: {scene.writes()} · پیام‌هایی که به «گروه لاگ» رفت: {len(scene.chat.sent_to_log)}")


def scene_silence() -> None:
    scene = Scene("صحنهٔ ۲ — «ننوشتن یعنی دست‌نخوردن»: فقط موجودی، بدون قیمت")
    scene.open_product()
    scene.type("موجودی 12")
    scene.confirm()
    scene.shop_table("بعد (قیمت‌ها همان قبلی؛ فقط موجودی عوض شد)")
    body = scene.shop.body("POST", "/variations/batch")
    print(f"\n   ردیفِ ارسالی به فروشگاه برای اولین واریژن: {body['update'][0]}")


def scene_stray_line() -> None:
    scene = Scene("صحنهٔ ۳ — «سلام» عنوان محصول نمی‌شود")
    scene.open_product()
    scene.type("سلام\nقیمت 720000 تومان")
    scene.confirm()
    scene.shop_table("بعد (عنوان همان قبلی)")


def scene_shop_moved() -> None:
    scene = Scene("صحنهٔ ۴ — فروشگاه بین پیش‌نمایش و تأیید عوض شده")
    scene.open_product()
    scene.type("قیمت 720000 تومان")
    print("\n🏬 (همین حالا یک نفر در پیشخوان وردپرس قیمتِ یک واریژن را ۷۲۰٬۰۰۰ کرد)")
    scene.shop.variations[9001]["regular_price"] = "720000"
    scene.confirm()
    print(f"\n   درخواست‌های نوشتن تا اینجا: {scene.writes()}  ← هیچ‌چیزِ تأییدنشده‌ای نوشته نشد")
    scene.confirm()
    scene.shop_table("بعد از تأیید دوم")


def scene_half_failed() -> None:
    scene = Scene("صحنهٔ ۵ — ساختنِ واریژن‌ها وسط کار می‌شکند؛ دوباره زدن شبکه را کامل می‌کند")
    scene.open_product()
    scene.type("iPhone 13 Pro Max\niPhone 15\nقیمت 720000 تومان\nموجودی 12")
    print("\n🏬 (batch وسط کار قطع می‌شود: فروشگاه فقط ۳ ردیف اول را ساخت و بقیه را نپذیرفت)")
    scene.shop.create_limit = 3
    scene.confirm()
    scene.shop_table("بعد از شکست (قدیمی‌ها هنوز هستند؛ محصول بی‌واریژن نماند)")
    print("\n🏬 (فروشگاه خوب شد)")
    scene.shop.create_limit = None
    scene.shop.requests.clear()
    scene.confirm()
    scene.shop_table("بعد از دوباره زدن")
    print(f"\n   واریژن‌ها: {len(scene.shop.variations)} · ترکیب‌های متفاوت: {len(scene.shop.grid())}"
          " ← هیچ ترکیبی دوبار نیست و iPhone 15 هر سه رنگ را دارد")


def scene_rebuild_keeps_what_was_not_written() -> None:
    scene = Scene("صحنهٔ ۶ — فهرست مدل بدون قیمت و موجودی: واریژن‌ها از نو، قیمت/موجودی قبلی می‌ماند")
    scene.shop_table("قبل")
    scene.open_product()
    scene.type("iPhone 13 Pro Max\nS24 Ultra\nS25 Ultra")
    scene.confirm()
    scene.shop_table("بعد (شناسه‌ها تازه‌اند؛ ۶۹۸٬۰۰۰/۵۹۸٬۰۰۰ و موجودی ۰/۷ همان قبلی است)")


def scene_same_list_is_no_rebuild() -> None:
    scene = Scene("صحنهٔ ۷ — فهرستی که همان فهرستِ فروشگاه است: هیچ بازسازی‌ای نیست")
    scene.shop_table("قبل")
    old_ids = set(scene.shop.variations)
    scene.open_product()
    scene.type("iPhone 13 Pro Max\nS24 Ultra\nقیمت 720000 تومان")
    scene.confirm()
    scene.shop_table("بعد")
    deletes = [call for call in scene.shop.requests
               if call[0] == "DELETE" or '"delete"' in call[3]]
    same = "بله" if old_ids == set(scene.shop.variations) else "نه"
    print(f"\n   همان شناسه‌ها؟ {same} · درخواستِ حذف: {len(deletes)}")


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="tisa-update-sim-"))
    PF.TEMP_DIR = tmp
    PF.feature_allowed = lambda user_id, key: True
    try:
        with patched_settings(settings_with(log_chat_id=LOG_CHAT)), no_sleep(), temp_ledger():
            for scene in (scene_main_path, scene_silence, scene_stray_line, scene_shop_moved, scene_half_failed,
                          scene_rebuild_keeps_what_was_not_written, scene_same_list_is_no_rebuild):
                scene()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
