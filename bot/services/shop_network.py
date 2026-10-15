"""Four read-only questions that tell an operator *what* is broken, with no shell involved.

Why these four and not one: the shop can fail in five shapes that look identical from a chat —
the host serves nothing, a firewall rule drops only requests carrying our keys, a security layer
in front of WordPress refuses the bot outright, the plugin is not installed, or the keys are wrong.
Each probe below separates one pair of those, and they run at the same time, so the whole card
answers in about `PROBE_TIMEOUT_SECONDS` even when the shop is fully hung. Every request is a
``GET`` with no side effect: pressing this during an outage is safe, which is exactly when it is
useful.

The fifth shape is the one that costs the most when it is mis-read, because the site is *up*: the
host's bot protection answers the third and fourth probes with the same refusal
(:func:`_is_blocked`). Saying «سایت از دسترس خارج شد» there sends the owner to the host's load
panel for a problem that is fixed by one whitelist entry — and saying «افزونه را دوباره نصب کن»
sends them to WordPress for a request that never reached it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import httpx

from bot.services.woo_client import (
    Audit, BLOCKED_STATUS_CODES, WooClient, body_snippet, describe_exception, origin_of,
    products_base,
)

logger = logging.getLogger(__name__)

#: Each probe's own socket budget. The client's publish timeout (45 s) is deliberately not used:
#: a diagnosis that hangs for 45 seconds per question is the bug this reports on.
PROBE_TIMEOUT_SECONDS = 10.0

#: Codes that mean «refused before WordPress saw it»: the certain one
#: (:data:`bot.services.woo_client.BLOCKED_STATUS_CODES` — the 418 a host's bot layer answers with)
#: plus the two shapes the same decision arrives in elsewhere — a challenge page's ``403`` and a
#: rate limiter's ``429``. They only count as a block *here* when the same code comes back with and
#: without our keys, or when the homepage itself is refused: a ``403`` that answers the unkeyed
#: request differently is WooCommerce doing its job, and reading that as a block would send the
#: operator to the host for a problem in their own key.
REFUSAL_STATUS_CODES = frozenset({*BLOCKED_STATUS_CODES, 403, 429})


@dataclass(frozen=True)
class ShopProbe:
    """One question and what came back.

    ``status`` is ``None`` when nothing arrived at all. «No answer» and «answered 404» are the two
    most different facts in this whole diagnosis, so they are never collapsed into one.
    """

    label: str
    path: str
    status: int | None
    ms: float
    detail: str = ""
    answered: bool = True

    def line(self) -> str:
        timing = f"{self.ms:.0f} ms"
        head = (f"⏳ {self.label}: بی‌پاسخ پس از {timing}" if self.status is None
                else f"↩️ {self.label}: HTTP {self.status} در {timing}")
        return f"{head} — {self.detail}" if self.detail else head


@dataclass(frozen=True)
class ShopNetwork:
    """The four probes, read as one sentence and one next step."""

    ok: bool
    verdict: str
    fix: str
    probes: tuple[ShopProbe, ...]
    elapsed_ms: float
    #: «the shop answered, and the answer was *no*» — a security layer in front of WordPress
    #: refused us (:func:`_is_blocked`). Kept next to ``ok`` because they are not opposites: a
    #: block is still «not publishing», but it is *not* an outage, and the watchdog says so out
    #: loud — «سایت از دسترس خارج شد» about a site that is up for every browser sends the owner
    #: to the wrong panel and to the wrong morning.
    blocked: bool = False

    def report(self) -> str:
        """The text of the Telegram card (plain: the handlers wrap it in their own HTML)."""
        mark = "✅" if self.ok else ("🔒" if self.blocked else "❌")
        lines = [f"{mark} {self.verdict}", "", *[probe.line() for probe in self.probes]]
        if self.fix:
            lines += ["", f"👉 {self.fix}"]
        return "\n".join(lines)

    @property
    def summary(self) -> str:
        """One line for «🩺 عیب‌یابی»: what was measured, no prose."""
        return " · ".join(
            f"{probe.path}={'بی‌پاسخ' if probe.status is None else probe.status}" for probe in self.probes
        )


def _fence_detail(response: httpx.Response) -> str:
    """What the plugin said about itself, in one line — the only body that is worth reading."""
    try:
        body = response.json()
    except ValueError:
        return "بدنه JSON نیست"
    if not isinstance(body, dict):
        return "بدنهٔ غیرمنتظره"
    if body.get("contract") != 1:
        return "قرارداد ایمنی در پاسخ نیست"
    version = str(body.get("version") or "")
    missing = [key for key in ("batch_fencing", "variation_fencing", "parent_cas", "stock_cas")
               if body.get(key) is not True]
    head = f"افزونه {version}" if version else "افزونهٔ تیسا"
    return f"{head} · قرارداد کامل" if not missing else f"{head} · ناقص: {'، '.join(missing)}"


async def _ask(client: WooClient, url: str, *, label: str, path: str, public: bool,
               describe: bool = False) -> ShopProbe:
    """One timed GET, turned into a probe whatever happens.

    Nothing escapes: a probe that could not get an answer *is* an answer, and comparing four of
    them is the entire method here.
    """
    started = time.perf_counter()
    try:
        response = await client.get(url, timeout=PROBE_TIMEOUT_SECONDS, public=public)
    except Exception as exc:
        return ShopProbe(label, path, None, (time.perf_counter() - started) * 1000,
                         describe_exception(exc)[:120], answered=False)
    return ShopProbe(
        label, path, response.status_code, (time.perf_counter() - started) * 1000,
        _fence_detail(response) if describe else body_snippet(response, 110),
    )


def _is_blocked(home: ShopProbe, open_rest: ShopProbe, open_fence: ShopProbe,
                keyed: ShopProbe) -> bool:
    """Was the refusal written by something *in front of* WordPress, or by WordPress?

    One comparison does it, and it is the whole reason the keyed question has a twin: the same
    refusal with the keys and without them cannot be about the keys. ``open_rest`` is deliberately
    not part of the test — a ``401`` on ``wc/v3/products`` for an anonymous caller is what a
    healthy shop says, so it is the one answer here that is never evidence of a block.
    """
    if keyed.status is None:
        return False                     # «بی‌پاسخ» حکایتِ خودش را دارد: هاست یا قاعدهٔ فایروال
    if keyed.status in REFUSAL_STATUS_CODES and keyed.status == open_fence.status:
        return True
    return home.status in REFUSAL_STATUS_CODES


def _read_verdict(home: ShopProbe, open_rest: ShopProbe, open_fence: ShopProbe,
                  keyed: ShopProbe) -> tuple[bool, str, str]:
    """Which of four very different problems is this? Decided by answers and timings only."""
    if not (home.answered or open_rest.answered or open_fence.answered):
        return False, (
            "سایت به هیچ‌کدام از درخواست‌ها جواب نداد — نه صفحهٔ اصلی، نه مسیر REST بدون کلید، نه "
            "پیش‌آزمونِ ربات. پس کارِ کلید و افزونه و ربات نیست: هاست در این لحظه سرویس نمی‌دهد."
        ), (
            "در پنل میزبان (cPanel/DirectAdmin/Plesk) این سه را ببین: مصرف CPU و RAM، «سقف "
            "پردازش‌های هم‌زمان» (entry processes / PHP workers)، و اینکه سایت با مرورگر هم باز "
            "می‌شود یا نه. دسترسی نداری؟ همین جمله را تیکت کن: «از IP سرورِ ربات، هر درخواستی به "
            "wp-json بدون پاسخ می‌ماند و اتصال برقرار می‌شود ولی جواب نمی‌آید». تا درست شدن، "
            "هرچه تأیید کنی در صفِ تلاشِ دوباره می‌ماند و محصول دومی ساخته نمی‌شود."
        )
    if _is_blocked(home, open_rest, open_fence, keyed):
        # The 2026‑10‑15 signature: 418 with the keys, 418 without them, in cycles that matched
        # the watchdog's own cadence. Before this branch existed the sentence was «پاسخ‌ها
        # غیرمنتظره است … افزونه را غیرفعال و فعال کن» — advice for a WordPress that was never
        # consulted, about a site that was serving every browser normally the whole time.
        return False, (
            f"پاسخ‌ها مسدودسازی است، نه قطعی و نه افزونه: پیش‌آزمون با کلید HTTP {keyed.status}، "
            f"بدون کلید هم HTTP {open_fence.status}. یک کدِ یکسان با کلید و بی‌کلید یعنی "
            "تصمیم‌گیرنده خودِ ووکامرس نیست — یک لایهٔ امنیتی جلوی وردپرس (ضدِرباتِ میزبان، "
            "Imunify360، ModSecurity) درخواست‌های ربات را رد می‌کند. سایت همان لحظه برای مرورگر "
            "بالا است؛ «از دسترس خارج شدن» توهمِ همین پیام بود، نه واقعیت."
        ), (
            "IP سرورِ ربات را در همان لایه وایت‌لیست کن: Imunify360 ← White List ← افزودن IP؛ در "
            "ModSecurity/cPHulk هم همین، و اگر سایت پشت Cloudflare است «Bot Fight Mode»/«Managed "
            "Challenge» را برای ‎/wp-json/*‎ استثنا کن. دسترسی نداری؟ همین جمله را تیکت کن: "
            "«درخواست‌های API از IP سرورِ ربات با HTTP 418 رد می‌شوند؛ IP را وایت‌لیست کنید». "
            "تا آن وقت همین پیام تکرار می‌شود، چون هر درخواستِ تازه می‌تواند بلوک را تمدید کند — "
            "پس ربات هم وقتی مسدود است ساعتی یک‌بار می‌پرسد، نه هر پانزده دقیقه."
        )
    if keyed.status in BLOCKED_STATUS_CODES:
        # The stranger got *something* (else `_is_blocked` would have caught it), so the refusal
        # is about what is in the URL — the ModSecurity shape, now with a real status instead of a
        # 45-second timeout. Only the certain code is read this way: a 403 with the keys and a 401
        # without them is WooCommerce refusing the credentials, and that keeps its own branch below.
        return False, (
            f"سایت درخواستِ دارای کلید را رد می‌کند (HTTP {keyed.status}) در حالی که همان مسیرِ "
            f"بدون کلید جواب داد (HTTP {open_fence.status}): این امضای قاعدهٔ فایروال/ModSecurity "
            "روی consumer_key‏/consumer_secret است، نه خرابیِ سایت."
        ), (
            "به میزبان بگو برای «/wp-json/wc/v3/*» قاعده‌ای که consumer_key‏/consumer_secret را در "
            "کوئری استرینگ بلاک می‌کند کنار بگذارد (یا IP سرورِ ربات را استثنا کند). تا آن، "
            "«🌐 تست اتصال ووکامرس» هم همین را می‌بیند و انتشار در صف می‌ماند."
        )
    if keyed.status is None:
        return False, (
            "سایت به درخواست‌های بدونِ کلید جواب می‌دهد، ولی همان مسیر با کلیدِ ووکامرس در آدرس "
            "بی‌پاسخ می‌ماند. این امضای فایروال/ModSecurity میزبان است، نه وردپرس."
        ), (
            "به میزبان بگو برای «/wp-json/wc/v3/*» قاعده‌ای که consumer_key/consumer_secret را در "
            "کوئری استرینگ بلاک می‌کند کنار بگذارد (یا IP ربات را استثنا کند). تا آن، «🌐 تست "
            "اتصال ووکامرس» هم همین را می‌بیند."
        )
    if keyed.status in (401, 403):
        return False, (
            f"پیش‌آزمون با کلیدها رد شد (HTTP {keyed.status}) در حالی که سایت جواب می‌دهد، "
            "پس ایرادِ ارتباط نیست."
        ), (
            "کلید/سِر را با «🔑 تست کلید» بسنج؛ کلید باید به کاربری با نقش «مدیریت فروشگاه» یا "
            "دست‌کم «ویرایش محصول» وصل باشد."
        )
    if keyed.status == 404 or open_fence.status == 404:
        return False, (
            "سایت جواب می‌دهد ولی مسیر tisa-health ثبت نشده (HTTP 404): افزونهٔ تیسا نصب نیست، "
            "غیرفعال است، یا آن‌قدر قدیمی است که این مسیر را ندارد — و تا آن نباشد ربات چیزی "
            "منتشر نمی‌کند."
        ), (
            "از همان wp-admin، بدون ترمینال: افزونه‌ها ← افزودن جدید ← بارگذاری، و "
            "tisa-product-importer.zip را آپلود و فعال کن؛ بعد «🩺 عیب‌یابی کامل»."
        )
    if keyed.status == 200 and "قرارداد کامل" in keyed.detail:
        return True, f"مسیر ایمنی جواب داد و قرارداد کامل است ({keyed.detail}).", ""
    return False, (
        f"پاسخ‌ها غیرمنتظره است: پیش‌آزمون با کلید HTTP {keyed.status}، بدون کلید "
        f"HTTP {open_fence.status}."
    ), (
        "«🩺 عیب‌یابی کامل» را دوباره بزن؛ اگر باز همین بود، افزونه را یک بار غیرفعال و فعال کن "
        "(و در تنظیمات←پیوندهای یکتا یک «ذخیرهٔ مجدد» هم معجزه می‌کند)."
    )


async def probe_shop_network(*, transport: httpx.BaseTransport | None = None) -> ShopNetwork:
    """Ask the shop four read-only questions at once, and name the thing that is broken.

    * the homepage and the products route **without** any credentials — «is anything listening?»
      (a 401/403 is a *good* answer here: it proves web server, PHP and pretty permalinks are up);
    * ``tisa-health`` **with** the keys — the exact request that precedes every publish;
    * ``tisa-health`` **without** them — which separates «no plugin» (404) from «the route exists
      and wants auth» (401/403) **and** «a security layer refuses us» (the same 418 twice, see
      :func:`_is_blocked`).

    ``transport`` is the test seam (the same one :class:`WooClient` takes); production never uses it.
    """
    started = time.perf_counter()
    root = products_base().removesuffix("/products")
    origin = origin_of(root)
    audit = Audit()
    async with WooClient(audit=audit, timeout=PROBE_TIMEOUT_SECONDS, attempts=1,
                         transport=transport) as client:
        home, open_rest, open_fence, keyed = await asyncio.gather(
            _ask(client, f"{origin}/" if origin else f"{root}/",
                 label="صفحهٔ اصلی سایت", path="/", public=True),
            _ask(client, f"{root}/products?per_page=1", label="REST ووکامرس، بدون کلید",
                 path="/wp-json/wc/v3/products", public=True),
            _ask(client, f"{root}/tisa-health", label="tisa-health بدون کلید",
                 path="/wp-json/wc/v3/tisa-health", public=True),
            _ask(client, f"{root}/tisa-health", label="tisa-health با کلید (پیش‌آزمون انتشار)",
                 path="/wp-json/wc/v3/tisa-health", public=False, describe=True),
        )
    ok, verdict, fix = _read_verdict(home, open_rest, open_fence, keyed)
    result = ShopNetwork(ok, verdict, fix, (home, open_rest, open_fence, keyed),
                         (time.perf_counter() - started) * 1000,
                         blocked=_is_blocked(home, open_rest, open_fence, keyed))
    logger.info("shop network probe: ok=%s | %s", ok, " ;; ".join(
        f"{probe.path}={probe.status or 'no-answer'} ({probe.ms:.0f}ms)" for probe in result.probes
    ))
    return result
