"""🛰 Watchdog: «سایت پایین است؟» جوابش را خودِ ربات می‌دهد، هر ۱۵ دقیقه، بدون دکمه.

چرا این ماژول هست: ردپای ۲۰۲۶-۱۰-۰۵ نشان داد ساعت‌ها بین «ساخت محصول گیر می‌کند» و
«آها، خودِ هاست سرویس نمی‌داد» فاصله افتاده — چون هر پاسخ با یک آدمِ دکمه‌زننده درک می‌شد.
ارزیابیِ وضعیت چهار GETِ بی‌عارضه است (``bot/services/shop_network.py``)، پس کسی که باید
بپرسد ربات است؛ آدم فقط *تغییر* را می‌خواند:

* **هرگز نمی‌نویسد.** همان چهار پرسشِ read-only؛ وسطِ قطعی هم زدنش بی‌خطر است.
* **ساکت است وقتی چیزی عوض نشده.** میزبانی که نه ساعت پایین است دو پیام تولید می‌کند
  (افتادن، و یک یادآوری هر شش ساعت)، نه سی‌وشش تا.
* **بازگشت را *اقدام* می‌کند، نه فقط اعلام:** در همان پاس، صفِ ارسال تخلیه می‌شود تا محصولی
  که نیم‌ساعت صبر کرده منتظرِ موعدِ بعدیِ backoff نماند.
* **«مسدود» را از «پایین» جدا می‌گوید.** اگر سایت جواب بدهد و جوابش «نه» باشد
  (``ShopNetwork.blocked``)، پیام «🔒 ربات به سایت راه نمی‌یابد» است، نه «⛔ سایت از دسترس
  خارج شد» — و در این حال دیگر هر ۱۵ دقیقه نمی‌کوبد: هر درخواست می‌تواند بلوک را تمدید کند،
  پس ساعتی یک‌بار می‌پرسد تا وایت‌لیست شود. ۲۰۲۶‑۱۰‑۱۵ نشان داد چه خبر است: چرخهٔ ۱۵دقیقه‌ایِ
  «افتاد/برگشت» دقیقاً به همان اندازه بود که خودمان می‌پرسیدیم.

``/watch`` هم همین کارت را با یک پرسشِ تازه نشان می‌دهد (فقط سودو) — برای لحظه‌ای که آدم
می‌خواهد *همین حالا* بداند، نه اینکه صبر کند. آن یکی استراحت نمی‌کند: کسی که می‌پرسد آدم است،
نه یک زمان‌بندِ ۱۵دقیقه‌ای.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import html

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from bot import rbac
from bot.config import data_dir, settings
from bot.modules import outbox_flow
from bot.services import jsonstore, product_journal
from bot.services.shop_network import ShopNetwork, probe_shop_network
from bot.utils import timeutil

logger = logging.getLogger(__name__)

#: Name under ``TISA_DATA_DIR``. Plain JSON because the state is one small dict and the daily
#: backup already zips every JSON store — a fourth SQLite file would be one more thing to
#: corrupt and nothing to gain.
STATE_FILE = "shop_watch.json"

#: How long the bot stays quiet about an outage that is not changing before it says «هنوز پایین
#: است» once. Six hours: long enough to never be noise, short enough that nobody finishes the day
#: believing the site came back.
REMINDER_AFTER_SECONDS = 6 * 3600

#: The cadence *while the answer is «مسدود»*. A block is the one failure that our own questions can
#: make worse: the bot's IP sits on the host's bot-protection list, and every new request is another
#: incident that can renew or lengthen the block — which is exactly the fifteen-minute on/off cycle
#: of 2026‑10‑15, where each pass was refused and the next one, fifteen minutes later, got through.
#: So one knock an hour instead of four, until a whitelist entry makes the question free again.
#: A host that is *down* gets no such rest: it is not listening, and noticing the return quickly is
#: worth more than four saved requests.
BLOCKED_RETRY_SECONDS = 3600

#: A restart must not look like an outage ending: the first pass after boot only records.
_UNKNOWN = None


def state_path() -> Path:
    return data_dir() / STATE_FILE


def read_state() -> dict[str, Any]:
    """The last known state, or ``{}`` when there is none (a corrupt file reads as none)."""
    try:
        data = jsonstore.read_json(state_path(), default={})
    except Exception as exc:                                # never let a state file stop a job
        logger.warning("shop watch: حالتِ قبلی خوانده نشد: %s", exc)
        return {}
    return dict(data) if isinstance(data, dict) else {}


def evaluate(state: dict[str, Any], *, ok: bool, verdict: str, now: float, blocked: bool = False
             ) -> tuple[dict[str, Any], str]:
    """What to say about one more pass — the whole watchdog in a pure function.

    Only *transitions* are worth a chat message, and a test has to be able to prove that a host
    staying down for nine hours is one sentence. ``was is None`` is the boot case: the first pass
    after a restart records the state and keeps quiet about a green site, but still says so when
    the site was already down, because that is exactly the moment the bot is being blamed for.

    ``blocked`` (:attr:`bot.services.shop_network.ShopNetwork.blocked`) is not a shade of the same
    failure, it is a different one, and it is stored so that the two sentences on either side of it
    can tell the truth: this is not «سایت از دسترس خارج شد» — the site is up — and the end of it is
    «مسدودسازی برداشته شد», not «سایت برگشت».
    """
    was = state.get("ok")
    was_blocked = bool(state.get("blocked"))
    # نه ``or now``: یک حالتِ با ``since=0.0`` (و هر «شروعِ مبهمِ دیگر») falsy است و قطعی
    # بی‌مقدار به نظر می‌رسد؛ صراحتاً ``None`` چک می‌شود.
    since_raw = state.get("since")
    began = now if since_raw is None else float(since_raw)
    if was is _UNKNOWN:
        if ok:
            return {"ok": True, "since": now, "verdict": verdict, "reminded": 0.0,
                    "blocked": False}, ""
        return ({"ok": False, "since": now, "verdict": verdict, "reminded": now, "blocked": blocked},
                f"{_first(blocked)}\n{verdict}")
    if bool(was) == ok:
        if ok:
            return state, ""
        down = now - began
        reminded_raw = state.get("reminded")
        if now - (0.0 if reminded_raw is None else float(reminded_raw)) >= REMINDER_AFTER_SECONDS:
            return ({**state, "reminded": now, "blocked": blocked},
                    f"🛰 {_still_saying(blocked)}: {timeutil.human_duration(down)} است.\n{verdict}")
        return {**state, "blocked": blocked}, ""
    if ok:
        return ({"ok": True, "since": now, "verdict": verdict, "reminded": 0.0, "blocked": False},
                f"🟢 {_goodbye(was_blocked)} — {timeutil.human_duration(now - began)} "
                f"{'طول کشید' if was_blocked else 'پایین بود'}.")
    return ({"ok": False, "since": now, "verdict": verdict, "reminded": now, "blocked": blocked},
            f"{_hello(blocked)}\n{verdict}")


#: The pairs of sentences. Written out here rather than inlined in :func:`evaluate` because they are
#: the product of this module: «⛔ سایت از دسترس خارج شد» and «🔒 مسدودسازی» are two different
#: mornings for whoever reads them, and a block reported as an outage is how the wrong one starts.
_HELLO = {False: "⛔ سایت از دسترس خارج شد.",
          True: "🔒 ربات به سایت راه نمی‌یابد — سایت جواب می‌دهد، مسدودسازی جوابش است."}
_FIRST = {False: "🛰 پایشِ سایت: از اولین بررسی هم سایت پاسخ نمی‌داد.",
          True: "🔒 پایشِ سایت: از اولین بررسی هم پاسخ‌ها مسدودسازی است."}
_STILL = {False: "سایت هنوز پایین است",
          True: "ربات هنوز مسدود است"}
_GOODBYE = {False: "سایت برگشت",
            True: "مسدودسازی برداشته شد"}


def _hello(blocked: bool) -> str:
    return _HELLO[bool(blocked)]


def _first(blocked: bool) -> str:
    return _FIRST[bool(blocked)]


def _still_saying(blocked: bool) -> str:
    return _STILL[bool(blocked)]


def _goodbye(blocked: bool) -> str:
    return _GOODBYE[bool(blocked)]


def _usable() -> bool:
    """Should the watchdog be asking at all? Two cases where asking would lie.

    In dry-run the fake transport answers everything (a green light would be a false promise),
    and with no keys configured the probe's own verdict is «کلید» — a fact /check-config already
    says. Neither is worth a repeating chat message.
    """
    return bool(
        settings.shop_watch_minutes > 0
        and not settings.woo_dry_run
        and settings.woocommerce_url and settings.woocommerce_key and settings.woocommerce_secret
    )


def watch_card(result: ShopNetwork, state: dict[str, Any], *, now: float | None = None) -> str:
    """The ``/watch`` answer: this pass, plus what the watchdog has been seeing."""
    moment = time.time() if now is None else now
    lines = [f"🛰 <b>پایشِ سایت — {timeutil.strftime('%H:%M', moment)}</b>", ""]
    lines += [html.escape(line, quote=False) for line in result.report().splitlines()]
    minutes = settings.shop_watch_minutes
    was = state.get("ok")
    if was is None:
        lines += ["", "<i>پایشِ خودکار هنوز پاسی انجام نداده (ربات تازه بالا آمده است).</i>"]
    else:
        since = float(state.get("since") or moment)
        age = timeutil.human_duration(max(0.0, moment - since))
        blocked = bool(state.get("blocked"))
        if was:
            mark = "🟢 پایین نبود"
        elif blocked:
            mark = "🔒 مسدود است"
        else:
            mark = "⛔ پایین است"
        cadence = (f"ساعتی یک‌بار تا رفعِ مسدودسازی (به‌جای {minutes} دقیقه‌ایِ عادی)"
                   if blocked and not was else f"هر {minutes} دقیقه")
        lines += ["", f"<i>پایشِ خودکار {cadence} · وضعیتِ فعلی: {mark} · از {age} پیش</i>"]
    return "\n".join(lines)


async def _announce(app: Application, text: str) -> None:
    """One sentence to the log group and to the owner — never a stack of them.

    The log group is the record, the owner's private chat is the alert; both are best-effort,
    because a watchdog whose only failure mode is «the alert could not be delivered» must not add
    a second failure on top of the outage.
    """
    try:
        await product_journal.send_log_message(app.bot, text)
    except Exception as exc:
        logger.warning("shop watch: پیام به لاگ‌چت نرفت: %s", exc)
    for chat_id in sorted(rbac.sudo_ids()):
        if int(chat_id) == int(settings.log_chat_id or 0):
            continue
        try:
            await app.bot.send_message(chat_id=chat_id, text=text)
        except Exception as exc:
            logger.debug("shop watch: پیام به %s نرفت (%s)", chat_id, exc)


def _resting(state: dict[str, Any], now: float) -> float:
    """Seconds left of a rest that a *block* earns, or ``0`` when the next pass may ask.

    Only a block rests. A host that is down is not listening, so asking costs nothing and noticing
    the return quickly is worth more than the four saved requests; a host that has *blocked us* is
    listening to every knock, and each one can renew the block
    (:data:`BLOCKED_RETRY_SECONDS`). The clock is the last pass that actually asked
    (``checked_at``), so a bot that was restarted mid-block does not restart the wait from scratch
    — but it does ask immediately, because a state file with no ``checked_at`` came from a version
    of the bot that never counted, and one extra question after a deploy is cheaper than an hour of
    silence about a site that came back.
    """
    if state.get("ok") is not False or not state.get("blocked"):
        return 0.0
    last = state.get("checked_at")
    if last is None:
        return 0.0
    return max(0.0, BLOCKED_RETRY_SECONDS - (now - float(last)))


async def check(context: ContextTypes.DEFAULT_TYPE) -> None:
    """One pass: four GETs, a state write, and a sentence only when the answer changed."""
    app = getattr(context, "application", None)
    if app is None:                                           # pragma: no cover - PTB wiring
        return
    if not _usable():
        return
    state = read_state()
    now = time.time()
    rest = _resting(state, now)
    if rest:
        logger.info("shop watch: آخرین پاسخ مسدودسازی بود؛ %s دقیقه دیگر دوباره می‌پرسد",
                    round(rest / 60))
        return
    result = await probe_shop_network()
    now = time.time()
    new_state, message = evaluate(state, ok=result.ok, verdict=result.verdict, now=now,
                                  blocked=result.blocked)
    # «کِی آخرین بار پرسیدیم» تنها چیزی است که حالت را در هر پاس عوض می‌کند — و همان چیزی است که
    # به یک مسدودسازی استراحت می‌دهد. یک فایلِ JSONِ کوچک هر پاس، بهایی که «مسدود را چهار بار در
    # ساعت نکوبیم» می‌ارزد.
    new_state = {**new_state, "checked_at": now}
    if new_state != state:
        try:
            await jsonstore.write_json_async(state_path(), new_state)
        except Exception as exc:
            logger.warning("shop watch: حالت نوشته نشد: %s", exc)
    if not message:
        return
    logger.info("shop watch: %s", " ".join(message.split()))
    await _announce(app, message)
    if bool(state.get("ok")) is False and result.ok:
        # The reason the queue stretched its waits to half an hour was precisely to survive an
        # outage; now that the shop answers, waiting for the next window would be self-inflicted.
        try:
            attempted = await outbox_flow.drain_once(app)
        except Exception as exc:
            logger.warning("shop watch: تخلیهٔ صف پس از بازگشت انجام نشد: %s", exc)
            return
        if attempted:
            await _announce(app, f"📤 با بازگشتِ سایت، {attempted} مورد از صفِ ارسال تلاش شد.")


async def cmd_watch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/watch`` — ask the four questions now and show what the watchdog knows."""
    message = update.effective_message
    user = update.effective_user
    if user is None or not rbac.is_sudo(user.id):
        if message is not None:
            await message.reply_text("⛔ این فقط برای مدیر ربات است.")
        return
    if message is not None:
        await message.reply_text("⏳ دارم چهار سؤالِ بی‌عارضه از سایت می‌پرسم…")
    result = await probe_shop_network()
    text = watch_card(result, read_state())
    if message is None:
        return
    await message.reply_text(text, parse_mode="HTML")


async def start(app: Application) -> int:
    """Schedule the sweep. Called from ``bot/app.py`` right after the outbox is scheduled."""
    minutes = settings.shop_watch_minutes
    if minutes <= 0:
        logger.info("shop watch: خاموش (TISA_SHOP_WATCH_MINUTES=0)")
        return 0
    if not _usable():
        logger.info("shop watch: خاموش — %s",
                    "حالت آزمایشی" if settings.woo_dry_run else "کلید ووکامرس کامل نیست")
        return 0
    if app.job_queue is None:                                   # pragma: no cover - no APScheduler
        logger.warning("shop watch: JobQueue نصب نیست؛ پایشِ خودکار زمان‌بندی نشد")
        return 0
    app.job_queue.run_repeating(
        check, interval=minutes * 60, first=min(60.0, minutes * 60.0), name="shop_watch",
    )
    logger.info("shop watch: هر %s دقیقه، فقط هنگام تغییر خبر می‌دهد", minutes)
    return 0


def register(app: Application) -> None:
    app.add_handler(CommandHandler("watch", cmd_watch))
