"""📊 وضعیت و 🩺 عیب‌یابی — دو صفحه برای «چیزی می‌چرخد؟» و «چرا نمی‌چرخد؟».

سه قانون، همان‌هایی که بقیهٔ صفحه‌ها هم رعایت می‌کنند:

* **وضعیت هیچ درخواست شبکه‌ای نمی‌زند.** همان لحظه که سایت از دست در رفته باید بشود
  وضعیت را خواند، و باید فوری باشد: همه‌چیز از روی دیسک و ``.env`` خوانده می‌شود.
* **هر خط فایده‌اش را هم می‌گوید.** «۳ در صف» تصمیم نمی‌دهد؛ «۳ در صف، بعدی ۴۰ ثانیه
  دیگر» تصمیم می‌دهد. چیزی که هم نداریم صفر گزارش نمی‌شود — «—» می‌خورد.
* **عیب‌یابی هیچ بررسی را «موفق» گزارش نمی‌کند که انجام نشده باشد.** رد شدنِ دسترسی،
  تایم‌اوت و ناقص‌بودن ``.env`` سه حالت جدا هستند: 🟡 یعنی «ربات کار می‌کند ولی این
  فلج است»، 🔴 یعنی «همین الان چیزی منتشر نمی‌شود». هر خط هم یک راه‌حل دارد، نه فقط
  یک شکایت.
"""

from __future__ import annotations

import asyncio
import html
import io
import logging
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from bot import rbac
from bot.__init__ import __version__
from bot.buttons import feature_allowed
from bot.config import data_dir, settings
from bot.constants import CB
from bot.services import flow_state, importer_contract, metrics, outbox, sku, tracking_ledger
from bot.services.woo_client import WooClient
from bot.services.woocommerce import ping_woocommerce
from bot.services.wordpress_media import test_wordpress_media
from bot.utils.text import clip_html

logger = logging.getLogger(__name__)

_MARKS = {"ok": "🟢", "warn": "🟡", "bad": "🔴"}
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DISK_WARN_BYTES = 1024 ** 3
_DISK_BAD_BYTES = 200 * 1024 ** 2


@dataclass(frozen=True)
class Check:
    """One line of a diagnostic card: what was checked, how it went, what to do."""

    name: str
    state: str  # "ok" | "warn" | "bad"
    detail: str = ""
    fix: str = ""

    def line(self) -> str:
        mark = _MARKS.get(self.state, "⚪")
        text = f"{mark} <b>{html.escape(self.name)}</b>"
        if self.detail:
            text += f" — {html.escape(self.detail)}"
        if self.fix:
            text += f"\n    ↳ {html.escape(self.fix)}"
        return text


def _bytes_text(value: float) -> str:
    return f"{value / 1024 ** 3:.1f} GB"


def _disk_check(root: Path) -> Check:
    try:
        usage = shutil.disk_usage(root)
    except OSError:  # pragma: no cover — a directory that vanished mid-check
        return Check("دیسک", "bad", f"{root} خوانده نشد", "از روی سرور بررسیش کن.")
    state = "ok"
    if usage.free < _DISK_BAD_BYTES:
        state = "bad"
    elif usage.free < _DISK_WARN_BYTES:
        state = "warn"
    fix = ("جا برای تصویرهای موقت و لاگ لازم است؛ فایل‌های قدیمیِ logs/ را ببر."
           if state != "ok" else "")
    return Check("دیسک", state, f"{_bytes_text(usage.free)} آزاد از {_bytes_text(usage.total)}", fix)


def _state_dir_check() -> Check:
    """A data directory inside the code directory dies with the next deploy."""
    state = data_dir()
    inside = state == _REPO_ROOT / "data" or _REPO_ROOT in state.parents
    if not inside:
        return Check("جای داده‌ها", "ok", str(state))
    return Check(
        "جای داده‌ها",
        "warn",
        f"داخل پوشهٔ کد: {state}",
        "TISA_DATA_DIR را بیرون بده (مثلاً /var/lib/tisaposttowp) تا git clean یا "
        "دیپلوی بعدی تاریخچهٔ فروشگاه را نپرد.",
    )


def _store_checks() -> list[Check]:
    """سه فایلی که خرابی‌شان فقط «چیزی را کم» می‌کند — پس بررسی می‌شوند، نه فرض."""
    checks: list[Check] = []
    problem = outbox.probe()
    stats = outbox.stats()
    if problem:
        checks.append(Check("صف ارسال", "bad", problem, "مجوز نوشتن روی data/، یا دیسک پر؟"))
    else:
        waiting = int(stats.get("pending") or 0)
        detail = f"{waiting} در انتظار"
        if settings.woo_dry_run:
            # «۰ در صف» در حالت آزمایشی دو معنا دارد: یا چیزی نیست، یا زهکشی خاموش است.
            # فرقی که برای «چرا محصول من منتشر نشد؟» حیاتی است، پس گفته می‌شود.
            detail += " · در حالت آزمایشی صف تخلیه نمی‌شود"
        if waiting:
            detail += f" · بعدی تا {int(stats.get('waiting_seconds') or 0)} ثانیه"
            if stats.get("last_error"):
                detail += f" · آخرین علت: {str(stats['last_error'])[:80]}"
        else:
            detail += " · چیزی در انتظار نیست"
        dropped = int(stats.get("dropped") or 0)
        if dropped:
            detail += f" · {dropped} تا پس از همهٔ تلاش‌ها رها شدند"
        checks.append(Check("صف ارسال", "ok" if not dropped else "warn", detail))

    rows, ledger_problem = tracking_ledger.probe()
    checks.append(
        Check(
            "دفتر فایل‌های ردیابی",
            "warn" if ledger_problem else "ok",
            ledger_problem or f"{rows} فایل ثبت‌شده",
            "فایل را پاک کن؛ از نو پر می‌شود (فقط هشدار «این فایل قبلاً آمده» کم می‌شود)."
            if ledger_problem
            else "",
        )
    )

    seen, metric_problem = metrics.probe()
    checks.append(
        Check(
            "متریک‌ها",
            "warn" if metric_problem else "ok",
            metric_problem or (f"{seen} شمارنده" if seen else "هنوز چیزی ثبت نشده"),
            "data/metrics.sqlite3 را پاک کن؛ از نو ساخته می‌شود." if metric_problem else "",
        )
    )

    stuck = flow_state.pending()
    if stuck:
        checks.append(
            Check(
                "جریان‌های نیمه‌کاره",
                "warn",
                f"{len(stuck)} جریان با ری‌استارتِ قبلی نیمه ماند",
                "وضعیتشان را در «🧾 آخرین محصولات» ببین؛ دوباره‌سازی فقط با «🔁» انجام می‌شود.",
            )
        )
    return checks


def _role_check() -> Check:
    sudo = sorted(settings.sudo_ids)
    if not sudo:
        return Check("دسترسی", "bad", "SUDO_IDS خالی است",
                     "شناسهٔ عددیِ مالک را در SUDO_IDS (فایل .env) بگذار.")
    return Check("دسترسی", "ok", f"سودو: {len(sudo)} · ادمین تأییدشده: {len(rbac.admins())}")


def _log_check() -> Check:
    if not settings.log_chat_id:
        return Check("چت لاگ", "warn", "LOG_CHAT_ID تنظیم نشده",
                     "LOG_CHAT_ID را در .env بگذار؛ تا پیش از آن کارت‌ها فقط در "
                     "logs/bot.log نوشته می‌شوند.")
    if settings.verbose_log:
        return Check("چت لاگ", "ok", f"{settings.log_chat_id} · trace کامل هم می‌رود (VERBOSE_LOG)")
    return Check("چت لاگ", "ok", f"{settings.log_chat_id} · یک کارت به‌ازای هر محصول",
                 "برای دیدنِ خط‌به‌خطِ یک محصول، VERBOSE_LOG=1 را روشن کن.")


def _importer_check() -> Check:
    """آیا افزونهٔ ZIP همان چیزی را می‌خواند که ربات داخل بسته می‌گذارد؟

    آفلاین است (فقط هدرِ فایلِ افزونه کنارِ ربات)، پس جای آن در «📊 وضعیت» هم هست:
    این تنها موردی است که خرابی‌اش **بی‌صدا** است — محصول ساخته می‌شود، فقط بدون
    تخفیف و موجودی.
    """
    state, detail, fix = importer_contract.describe()
    return Check("افزونهٔ واردکنندهٔ ZIP", state, detail, fix)


def _limit_check() -> Check:
    """همان سه عددی که مبدل فایل قبل از دانلود به کاربر می‌گوید — از یک منبع."""
    return Check("سقف‌ها", "ok", settings.limits_line)


def status_text() -> str:
    """همهٔ کارتِ «📊 وضعیت» — بی‌شبکه، بی‌انتظار."""
    lines = [
        "📊 <b>وضعیت</b>",
        "",
        f"🤖 نسخه <code>{html.escape(__version__)}</code> · پایتون <code>{sys.version_info.major}"
        f".{sys.version_info.minor}.{sys.version_info.micro}</code>",
        f"🧪 حالت آزمایشی: {'روشن — چیزی روی سایت نوشته نمی‌شود' if settings.woo_dry_run else 'خاموش (انتشار واقعی)'}",
        "",
        *[_check.line() for _check in [_state_dir_check(), _disk_check(data_dir()), _role_check(),
                                       _log_check(), _importer_check(), _limit_check(),
                                       *_store_checks()]],
    ]
    counters = metrics.status_lines()
    lines += ["", "📈 <b>از وقتی شمارنده‌ها روشن شده‌اند</b>"] if counters else [
        "", "📈 هنوز شمارنده‌ای ثبت نشده (محصولی منتشر نشده، یا data/ تازه است)."]
    if counters:
        lines += counters
    if settings.problems:
        lines += ["", "⚠️ <b>پیکربندی</b>", *[f"   · {html.escape(str(problem))}" for problem in settings.problems]]
    clipped, _ = clip_html("\n".join(lines))
    return clipped


# --- The live checks: only «🩺 عیب‌یابی» runs these ----------------------------


async def _check_token(bot) -> Check:
    try:
        me = await bot.get_me()
    except Exception as exc:
        return Check("توکن تلگرام", "bad", f"{type(exc).__name__}: {str(exc)[:120]}",
                     "BOT_TOKEN را از @BotFather دوباره بردار؛ اینترنتِ سرور را هم چک کن.")
    return Check("توکن تلگرام", "ok", f"@{getattr(me, 'username', '?')}")


async def _check_log_chat(bot) -> Check:
    target = settings.log_chat_id
    if not target:
        return Check("چت لاگ", "warn", "تنظیم نشده", "LOG_CHAT_ID را در .env بگذار (آیدی عددیِ گروه/کانال).")
    try:
        chat = await bot.get_chat(chat_id=target)
    except Exception:
        return Check("چت لاگ", "bad", f"{target} پاسخ نداد",
                     "ربات را در آن گروه/کانال ادمین کن؛ آیدی گروه‌های تازه با -100 شروع می‌شود.")
    return Check("چت لاگ", "ok", f"«{getattr(chat, 'title', None) or getattr(chat, 'username', target)}»")


async def _check_woocommerce() -> Check:
    if not (settings.woocommerce_url and settings.woocommerce_key and settings.woocommerce_secret):
        return Check("WooCommerce", "bad", "سه‌تاییِ WooCommerce در .env کامل نیست",
                     "WOOCOMMERCE_URL / _KEY / _SECRET.")
    result = await ping_woocommerce(settings.woocommerce_url, settings.woocommerce_key,
                                    settings.woocommerce_secret, settings.woocommerce_version)
    if result.ok:
        return Check("WooCommerce", "ok", f"HTTP {result.status_code} · {result.elapsed_ms:.0f} ms")
    return Check("WooCommerce", "bad", f"{result.status_code or '—'} · {result.message}",
                 "کلید و سِر (خواندن‌ونوشتن) و permalink سایت را بررسی کن.")


async def _check_wordpress() -> Check:
    if settings.woo_dry_run:
        return Check("رسانهٔ وردپرس", "warn", "🧪 در dry-run آپلود/حذف زنده انجام نمی‌شود؛ تست رسانه اجرا نشد.")
    if not (settings.wordpress_url and settings.wordpress_username and settings.wordpress_app_password):
        return Check("رسانهٔ وردپرس", "bad", "WORDPRESS_URL/_USERNAME/_APP_PASSWORD کامل نیست",
                     "Application Password از صفحهٔ کاربران وردپرس ساخته می‌شود.")
    result = await test_wordpress_media()
    if result.ok:
        extra = "" if result.deleted else " (آپلود شد ولی حذف نشد؛ یک فایل تستی در رسانه ماند)"
        return Check("رسانهٔ وردپرس", "ok" if result.deleted else "warn", f"{result.message}{extra}")
    return Check("رسانهٔ وردپرس", "bad", result.message,
                 "مسیر wp-json، نام کاربری و Application Password را دوباره امتحان کن.")


async def _check_sku_plugin() -> Check:
    """The optional next-SKU plugin: absent is fine (the catalog is scanned), broken is not."""
    if not (settings.wordpress_url and settings.wordpress_app_password):
        return Check("افزونهٔ SKU", "warn", "بدون WORDPRESS_APP_PASSWORD بررسی نمی‌شود",
                     "اختیاری است؛ بدون آن SKU از خودِ کاتالوگ خوانده می‌شود.")
    endpoint = f"{settings.wordpress_url.rstrip('/')}{sku.PLUGIN_ROUTE}"
    try:
        async with WooClient(timeout=10.0, attempts=1) as client:
            response = await client.get(endpoint, params={"prefix": "TISA"}, basic=True)
    except Exception as exc:
        return Check("افزونهٔ SKU", "warn", f"پاسخ نگرفتیم ({type(exc).__name__})",
                     "اختیاری است؛ ربات با اسکنِ کاتالوگ ادامه می‌دهد.")
    if response.status_code == 404:
        return Check("افزونهٔ SKU", "warn", "نصب نیست (HTTP 404)",
                     "اگر شمارش SKU را به افزونه می‌سپاری، الان فعال نیست.")
    if response.status_code in (401, 403):
        return Check("افزونهٔ SKU", "warn", f"دسترسی رد شد (HTTP {response.status_code})",
                     "Application Password باید نقش edit_products داشته باشد.")
    if response.status_code >= 400:
        return Check("افزونهٔ SKU", "bad", f"HTTP {response.status_code}",
                     "لاگ وردپرس را ببین؛ تا درست شدنش SKU از کاتالوگ خوانده می‌شود.")
    return Check("افزونهٔ SKU", "ok", f"SKU بعدی: {response.json().get('sku', '?')}")


def _check_supervisor() -> Check:
    if not shutil.which(settings.supervisorctl_bin):
        return Check("ری‌استارت", "warn", f"«{settings.supervisorctl_bin}» روی PATH نیست",
                     "با systemd دکمهٔ «🔄 ری‌استارت» کار نمی‌کند؛ واحد `deploy/tisaposttowp.service` در مخزن است.")
    return Check("ری‌استارت", "ok", f"program: {settings.supervisor_program}")


async def run_checks(bot) -> list[Check]:
    """همهٔ بررسی‌های زنده، هم‌زمان — یک کارت، نه چهار دکمه."""
    results = await asyncio.gather(
        _check_token(bot),
        _check_log_chat(bot),
        _check_woocommerce(),
        _check_wordpress(),
        _check_sku_plugin(),
        return_exceptions=True,
    )
    checks: list[Check] = []
    for result in results:
        if isinstance(result, BaseException):
            # A check that could not run is a finding, not a pass: «بی‌خطا» دیدنش،
            # خودش یک جوابِ اشتباه به مالک است.
            checks.append(Check("بررسی ناتمام", "bad", f"{type(result).__name__}: {str(result)[:120]}",
                                "این مورد اصلاً اندازه‌گیری نشد؛ لاگ ربات را ببین."))
        else:
            checks.append(result)
    checks += [_importer_check(), _disk_check(data_dir()), _state_dir_check(),
               _check_supervisor(), *_store_checks()]
    return checks


def diagnose_text(checks: list[Check], *, seconds: float) -> str:
    bad = sum(1 for check in checks if check.state == "bad")
    warn = sum(1 for check in checks if check.state == "warn")
    verdict = ("✅ همه‌چیز سبز است." if not bad and not warn else
               f"🔴 {bad} مورد بلا‌است · 🟡 {warn} مورد نیمه‌کاره" if bad else
               f"🟡 {warn} مورد نیازمندِ توجه — هیچ‌کدام ربات را متوقف نمی‌کند")
    lines = [
        "🩺 <b>عیب‌یابی</b>",
        "",
        verdict,
        "",
        *[check.line() for check in checks],
        "",
        f"<i>{len(checks)} بررسی · {seconds:.1f} ثانیه · چیزی روی سایت نوشته نشد"
        " (تست ساختار شکست، اگر لازم داری، از «🏓 تست‌های تک‌تک»)</i>",
    ]
    clipped, _ = clip_html("\n".join(lines))
    return clipped


# --- Screens ------------------------------------------------------------------


def _keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🩺 عیب‌یابی کامل", callback_data=CB.OPS_DIAGNOSE),
                InlineKeyboardButton("📥 فایل متریک‌ها", callback_data=CB.OPS_METRICS),
            ],
            [InlineKeyboardButton("🏓 تست‌های تک‌تک", callback_data=CB.PING)],
            [InlineKeyboardButton("⬅️ بازگشت به منو", callback_data=CB.MAIN_MENU)],
        ]
    )


def _denied(query) -> str | None:
    """The alert to answer with when this user may not see the screen; None when they may.

    The alert is *returned*, not sent: a callback query may be answered exactly once, and
    a second answer is the kind of error that only shows up on a real device.
    """
    user = query.from_user
    if user and feature_allowed(user.id, "ops_status"):
        return None
    metrics.note_denial("ops")
    return "⛔ دسترسی ندارید."


async def cb_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«📊 وضعیت» — همان لحظه، بدون هیچ درخواستی به بیرون."""
    query = update.callback_query
    denied = _denied(query)
    if denied:
        await query.answer(denied, show_alert=True)
        return
    await query.answer()
    await query.message.reply_html(status_text(), reply_markup=_keyboard())


async def cb_diagnose(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«🩺 عیب‌یابی» — هر بررسیِ زنده یک‌جا، با یک خط راه‌حل برای هر کدام."""
    query = update.callback_query
    denied = _denied(query)
    if denied:
        await query.answer(denied, show_alert=True)
        return
    await query.answer()
    status = await query.message.reply_text("⏳ دارم هم‌زمان چند جا را چک می‌کنم…")
    started = time.perf_counter()
    text = diagnose_text(await run_checks(context.bot), seconds=time.perf_counter() - started)
    try:
        await status.edit_text(text, parse_mode="HTML", reply_markup=_keyboard())
    except Exception:
        # The «⏳» note can be gone (a long timeout, a scrolled-away chat); the answer
        # must still arrive, just as its own message.
        await query.message.reply_html(text, reply_markup=_keyboard())


async def send_metrics_file(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """The counters as a CSV file: numbers to open in Excel, not to read in a chat."""
    await context.bot.send_document(
        chat_id=chat_id,
        document=io.BytesIO(metrics.export_csv().encode("utf-8-sig")),
        filename=f"tisa-metrics-{time.strftime('%Y%m%d-%H%M')}.csv",
        caption="📈 شمارنده‌ها از وقتی `data/metrics.sqlite3` ساخته شده می‌آیند، نه فقط دیشب. "
                "نسبت‌ها از خودِ شمارنده‌ها حساب شده‌اند.",
    )


async def cb_metrics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    denied = _denied(query)
    if denied:
        await query.answer(denied, show_alert=True)
        return
    await query.answer()
    chat_id = query.message.chat_id if query.message is not None else (
        query.from_user.id if query.from_user else 0)
    await send_metrics_file(context, int(chat_id))


async def cmd_export_metrics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/export_metrics`` — همان فایل، وقتی چت عوض شده. (نامِ دستور با «-» ممکن نیست.)"""
    user = update.effective_user
    if not user or not rbac.is_sudo(user.id):
        metrics.note_denial("ops:/export_metrics")
        await update.effective_message.reply_text("🔒 فقط مالک (سودو) می‌تواند متریک‌ها را بگیرد.")
        return
    chat = update.effective_chat
    await send_metrics_file(context, int(chat.id if chat else user.id))


def register(app: Application) -> None:
    app.add_handler(CallbackQueryHandler(cb_status, pattern=f"^{CB.OPS_STATUS}$"))
    app.add_handler(CallbackQueryHandler(cb_diagnose, pattern=f"^{CB.OPS_DIAGNOSE}$"))
    app.add_handler(CallbackQueryHandler(cb_metrics, pattern=f"^{CB.OPS_METRICS}$"))
    app.add_handler(CommandHandler("export_metrics", cmd_export_metrics))


__all__ = [
    "Check",
    "cb_diagnose",
    "cb_metrics",
    "cb_status",
    "cmd_export_metrics",
    "diagnose_text",
    "register",
    "run_checks",
    "send_metrics_file",
    "status_text",
]
