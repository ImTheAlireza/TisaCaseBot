"""The bot requires the matching native WordPress safety contract before writes.

The local flock only coordinates a shared volume. The importer additionally
fences parent intents and variation combinations inside Woo's native REST hooks;
stock and parent updates use compare-and-set to reject a changed baseline.

The preflight itself is the part that keeps going wrong in production, so it is written to be
unable to hide a diagnosis: it is short, it repeats (a read-only GET can), and when it fails it
says *which* thing failed — a wedged host, a firewall eating credentialed URLs, or a site whose
plugin does not register the route at all.
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx

from bot.services.outbox import TRANSIENT_STATUS_CODES
from bot.services.woo_client import (
    BLOCKED_STATUS_CODES,
    WooClient,
    WooCommerceAPIError,
    error_message,
    origin_of,
)
from bot.services.woo_contract import positive_id

CONTROL_FIELDS = {"tisa_fence", "tisa_expected_stock", "tisa_expected_fields"}

#: The preflight is one read-only GET that runs *before* anything is written, which makes it the
#: cheapest request of a publish — and, on a wedged shop, the most expensive thing in the chat:
#: with the writer's 45 s socket the seller watched «📤 در حال آپلود عکس‌ها…» for three quarters of
#: a minute and the queued retry spent one of its eight attempts the same way. So the probe gets
#: its own budget, and repeats inside the same publish. :meth:`WooClient.request` refuses to
#: retry a timeout because a repeated ``POST /products`` is a duplicate product; this request
#: cannot duplicate anything, so the refusal does not apply here.
#:
#: The budget is deliberately *small*: two tries of eight seconds plus one liveness question is
#: under half a minute, because a shop that is genuinely down stays down for minutes and the
#: queue — not this request — is what waits it out.
PROBE_TIMEOUT_SECONDS = 8.0
PROBE_ATTEMPTS = 2
#: …with a breath between attempts, because a stalled PHP worker is not unstuck by hammering it.
PROBE_BACKOFF_SECONDS = 1.5
#: The «is anything listening at all?» request. No credentials, no side effect, no dawdling.
LIVENESS_TIMEOUT_SECONDS = 8.0

#: A module-level sleeper, for the same reason :mod:`bot.services.woo_client` has one: the suite
#: watches the backoff instead of paying it.
_sleep = asyncio.sleep


def _namespace(base: str) -> str:
    """``…/wp-json/wc/v3/products`` → ``…/wp-json/wc/v3`` — where the fence's own route lives."""
    return base.removesuffix("/products")


def _detail(response: httpx.Response) -> str:
    """The shop's own one-line reason, when it bothered to send one (never a URL)."""
    try:
        reason = (error_message(response) or "").strip().replace("\n", " ")
    except Exception:                                       # a body httpx cannot decode
        return ""
    return f": {reason[:160]}" if reason and reason != f"HTTP {response.status_code}" else ""


def _silent_host(error: WooCommerceAPIError) -> WooCommerceAPIError:
    """Tag an error that means «nothing answered», so the queue can wait it out.

    The queue has to choose between «retry on the usual backoff» and «the host is down; knock
    every half hour and do not spend the seller's attempts» (:func:`bot.services.outbox.is_silent`).
    A ``503`` on its own cannot make that call — LiteSpeed answers 503 when it is merely busy —
    so the branch that *knows* it got silence says so here, once, at the source.
    """
    error.host_silent = True
    return error


def _blocked_host(error: WooCommerceAPIError) -> WooCommerceAPIError:
    """Tag an error that means «the answer came from a security layer, not from WordPress».

    Same reason as :func:`_silent_host`: the queue has to pick between «wait this out and do not
    spend the seller's attempts» and «this will repeat tomorrow, ask a human»
    (:func:`bot.services.outbox.is_blocked`), and a bare ``503`` cannot say which. Both tags end in
    the same 30-minute wait; they differ in the sentence the operator reads — «وایتلیست کن» here,
    «به مصرفِ هاست نگاه کن» there.
    """
    error.host_blocked = True
    return error


def _connect_level(exc: BaseException | None) -> bool:
    """Did the failure happen *before* the shop could see anything?

    :class:`~bot.services.woo_client.WooClient` already knocks three times on a connect error
    (nothing arrived, so repeating is safe — that is its own retry policy), and a socket that was
    never established tells us everything the liveness probe would. That is why the probe stops
    here instead of asking a fourth question to a port that is not listening.
    """
    return isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))


async def _health(client: WooClient, base: str) -> tuple[httpx.Response | None, BaseException | None]:
    """Ask the shop for its contract: exactly one of ``(response, error)`` comes back.

    Silence and refusal are different facts and the caller splits them: a refusal is a deployment
    problem that will repeat forever, while a shop that answers nothing is a host having a bad
    minute — which is exactly what :mod:`bot.services.outbox` is for.
    """
    timeout = min(client.timeout_seconds, PROBE_TIMEOUT_SECONDS)
    last: BaseException | None = None
    for attempt in range(PROBE_ATTEMPTS):
        if attempt:
            await _sleep(PROBE_BACKOFF_SECONDS * attempt)
        try:
            return await client.get(f"{_namespace(base)}/tisa-health", timeout=timeout), None
        except httpx.TransportError as exc:
            last = exc
            client.audit.log(
                f"[fence] بررسی ایمنی بی‌پاسخ ماند (تلاش {attempt + 1}/{PROBE_ATTEMPTS}، "
                f"مهلت {timeout:g}s): {type(exc).__name__}"
            )
            if _connect_level(exc):
                break                                        # the client has already retried this
    return None, last


async def _awake(client: WooClient, base: str) -> bool:
    """Did the site answer a request that carries **no** credentials? ``True`` = «it is awake».

    Any HTTP status counts as an answer: a 404 still proves PHP, the web server and the route
    rewriting are alive, which is the whole question. This is what separates «the shop is down»
    from «something between us and the shop dislikes URLs containing ``consumer_secret``».
    """
    origin = origin_of(base)
    if not origin:
        return False
    try:
        response = await client.get(f"{origin}/wp-json/", timeout=LIVENESS_TIMEOUT_SECONDS,
                                    public=True)
    except httpx.TransportError as exc:
        client.audit.log("[fence] یک GET بدون کلید هم بی‌پاسخ ماند "
                         f"({type(exc).__name__}) → سایت/هاست از دسترس خارج است")
        return False
    client.audit.log(
        f"[fence] سایت به درخواستِ بدون کلید پاسخ داد (HTTP {response.status_code}) "
        "پس وب‌سرور و PHP بالا هستند؛ گیرِ کار مسیرِ افزونه یا فایروال است"
    )
    return True


async def _stranger_status(client: WooClient, base: str) -> int | None:
    """Ask the *same* safety URL with no credentials: «would it have said that to anyone?»

    Two very different problems arrive as one ``418``, and they are two different phone calls to
    the host: a rule that refuses *us* (the unkeyed request is refused with the same code — the fix
    is a whitelist entry for the bot's IP) and a rule that fires *because* the URL carries
    ``consumer_key``/``consumer_secret`` (WordPress answers the unkeyed request, so only the
    credentialed URL is being eaten). One GET, on a path that has already failed, tells them apart.
    ``None`` — the unkeyed request got no answer either — is read as the first case, because the
    whitelist is the fix that covers both.
    """
    try:
        response = await client.get(f"{_namespace(base)}/tisa-health",
                                    timeout=LIVENESS_TIMEOUT_SECONDS, public=True)
    except httpx.TransportError:
        return None
    return response.status_code


async def _blocked(client: WooClient, base: str, response: httpx.Response) -> WooCommerceAPIError:
    """The 418 sentence, and *which* rule is behind it — measured, not guessed.

    :data:`bot.services.woo_client.BLOCKED_STATUS_CODES` is what a security layer in front of
    WordPress answers when it decides a client is a robot. It is the one refusal that must never be
    read as «the plugin is missing»: the request never reached PHP, so the deployment is fine and
    re-uploading the zip is a wasted afternoon. It is also the one refusal that is *not* a config
    error of ours — so it is queued (``503``) and waited out, not dropped (``412``).

    Reaching this function at all is now itself a finding: :class:`…woo_client.WooClient` answers
    the gate's JavaScript challenge on its own (see ``woo_client.CHALLENGE_COOKIE``), so a 418 that
    still arrives here is the harder, *challenge-less* refusal — not the page a browser shrugs at.
    """
    status = response.status_code
    stranger = await _stranger_status(client, base)
    detail = _detail(response)
    if stranger is None or stranger == status:
        said = "بی‌پاسخ ماند" if stranger is None else f"HTTP {stranger}"
        client.audit.log(
            f"[fence] سایت درخواستِ دارای کلید را با HTTP {status} رد کرد و درخواستِ بدون کلید "
            f"هم {said} → تصمیم‌گیرنده لایهٔ امنیتیِ میزبان است، نه ووکامرس و نه افزونه"
        )
        return _blocked_host(WooCommerceAPIError(503, (
            f"میزبان درخواست‌های ربات را رد می‌کند (HTTP {status}{detail}): همین کد را به درخواستِ "
            "بدون کلید هم می‌دهد، پس تصمیم‌گیرنده لایهٔ امنیتی جلوی وردپرس است — ضدِرباتِ میزبان، "
            "Imunify360، ModSecurity — نه ووکامرس و نه افزونهٔ تیسا. این لایه معمولاً با یک "
            "چالشِ جاوااسکریپتی راه می‌دهد و ربات همان را خودکار انجام می‌دهد؛ پس ۴۱۸ بدون چالش "
            "یعنی این‌بار چالش هم داده نشده. درخواست به وردپرس نرسیده و هیچ‌چیز نوشته نشده، پس "
            "این بسته در صف می‌ماند و بدون سوزاندنِ تلاش‌ها نیم‌ساعت یک‌بار امتحان می‌شود. IP "
            "سرورِ ربات را در همان لایه وایت‌لیست کن (Imunify360 ← White List ← افزودن IP)؛ اگر "
            "سایت پشت Cloudflare است «Bot Fight Mode» را برای ‎/wp-json/*‎ استثنا کن. برای تأیید، "
            "همان لحظه سایت را با مرورگر باز کن: بالا می‌آید — این «پایین بودنِ سایت» نیست."
        )))
    client.audit.log(
        f"[fence] مسیر بررسی ایمنی با کلید HTTP {status} گرفت ولی همان مسیر بدون کلید "
        f"HTTP {stranger} جواب داد → قاعدهٔ فایروال روی consumer_key/consumer_secret است"
    )
    return _blocked_host(WooCommerceAPIError(503, (
        f"سایت درخواستِ دارای کلید را رد می‌کند (HTTP {status}{detail}) در حالی که همان مسیرِ "
        f"بدون کلید جواب داد (HTTP {stranger}) — پس قاعدهٔ فایروال/ModSecurity روی "
        "«consumer_key/consumer_secret» است، نه خرابیِ سایت و نه افزونه. کوکیِ چالش هم روی هر "
        "دو درخواست فرستاده می‌شود، پس فرق از خودِ آدرسِ دارای کلید است. هیچ‌چیز نوشته نشده و "
        "این بسته در صف می‌ماند. در پنلِ همان لایه قاعدهٔ «consumer» را برای ‎/wp-json/wc/v3/*‎ "
        "بردار، یا IP سرورِ ربات را استثنا کن."
    )))


async def require(client: WooClient, base: str) -> None:
    """Refuse to write until the shop's own safety contract has answered.

    Every failure carries the status the queue understands, because that status decides whether a
    human has to act:

    * the socket never opened → ``503`` with «DNS/فایروال/سایت خاموش», and no further asking;
    * nothing answers, not even an unauthenticated request → ``503`` — «the host is not serving»;
      queued, and the trace says to look at CPU/entry-processes/PHP pool/firewall;
    * the site answers but the plugin route stays silent *only when our keys are in the URL*
      → ``503`` said out loud, because the fix is a host rule, not a WordPress setting;
    * the shop replies with a busy status (``5xx``/``429``/…) → ``503``, queued like any 5xx;
    * the shop replies with a robot-protection status (:data:`BLOCKED_STATUS_CODES`, the 418 a
      host's bot layer answers with — *after* :mod:`bot.services.woo_client` has already answered
      the layer's JavaScript challenge) → ``503`` tagged ``host_blocked``: queued and waited out
      without spending attempts, because the request never reached PHP and no plugin on earth
      fixes it — the whitelist does;
    * the route is absent, refused, or answers without the fields we need → ``412`` — a plugin
      problem that will repeat tomorrow, so it is never queued.

    In every branch the original transport error stays as ``__cause__``: the flow's cards print a
    sentence, and the log group keeps the chain.
    """
    if client.dry_run:
        return
    response, failure = await _health(client, base)
    if response is None:
        assert failure is not None  # _health returns exactly one of the two
        if _connect_level(failure):
            raise _silent_host(WooCommerceAPIError(503, (
                "اتصال به فروشگاه برقرار نشد — نه DNS حل می‌شود و نه پورت جواب می‌دهد. هیچ "
                "درخواستی به سایت نرسیده، پس نیم‌ساخته‌ای هم وجود ندارد؛ انتشار در صفِ تلاشِ "
                "دوباره می‌ماند. از سمت سرورِ ربات: `curl -m 15 https://…/wp-json/`."
            ))) from failure
        if await _awake(client, base):
            raise _silent_host(WooCommerceAPIError(503, (
                "سایت زنده است ولی مسیر افزونهٔ تیسا (tisa-health) به درخواستی که کلید در آدرس "
                "دارد پاسخ نداد. این شکلِ کارِ فایروال/ModSecurity میزبان است، نه وردپرس. از همان "
                "سرورِ ربات امتحان کن: `curl -m 15` همان آدرسِ بررسی ایمنی را با "
                "`consumer_key`/`consumer_secret` بزن؛ اگر آن هم گیر کرد، قاعدهٔ «consumer» را از "
                "فایروال بردار."
            ))) from failure
        raise _silent_host(WooCommerceAPIError(503, (
            "فروشگاه به هیچ درخواستی پاسخ نداد — نه بررسی ایمنی، نه یک GET سادهٔ بدون کلید. "
            "چیزی نوشته نشده و نیم‌ساخته‌ای وجود ندارد؛ انتشار در صفِ تلاشِ دوباره می‌ماند. "
            "از سمت میزبان ببین: CPU/RAM و سقف «entry processes»، استخر PHP-FPM/LiteSpeed، "
            "و اینکه وردپرس اصلاً به ربات پاسخ می‌دهد یا فایروال/IP را بسته است."
        ))) from failure
    status = response.status_code
    if status in TRANSIENT_STATUS_CODES:
        raise WooCommerceAPIError(503, (
            f"فروشگاه هنگام بررسی ایمنی شلوغ بود (HTTP {status}{_detail(response)}). هیچ چیزی "
            "نوشته نشد و این بسته در صفِ تلاشِ دوباره می‌ماند."
        ))
    if status in (401, 403):
        raise WooCommerceAPIError(403, (
            f"کلید WooCommerce برای بررسی ایمنی پذیرفته نشد (HTTP {status}{_detail(response)}). "
            "کلید باید متعلق به کاربری با دسترسی «ویرایش محصول» باشد؛ «🏓 Ping» ← «🌐 تست اتصال ووکامرس» را بزن."
        ))
    if status in BLOCKED_STATUS_CODES:
        # A security layer answered, not WordPress (:data:`…woo_client.BLOCKED_STATUS_CODES`).
        # This is the one non-2xx that must be *queued*: no byte reached PHP, the deployment is
        # fine — and the 412 below would send the operator to re-upload a plugin that is already
        # on the site, once per blocked attempt.
        raise await _blocked(client, base, response)
    if not response.is_success:
        raise WooCommerceAPIError(412, (
            f"مسیر بررسی ایمنی روی سایت جواب نداد (HTTP {status}{_detail(response)}). یعنی "
            "افزونهٔ تیسا نصب/فعال نیست یا آن‌قدر قدیمی است که این مسیر را ثبت نکرده؛ "
            "tisa-product-importer.zip این ریپو را در وردپرس آپلود و فعال کن."
        ))
    try:
        body = response.json()
    except ValueError:
        body = None
    fields = ("batch_fencing", "variation_fencing", "parent_cas", "stock_cas")
    valid = (response.is_success and isinstance(body, dict) and body.get("contract") == 1
             and all(body.get(key) is True for key in fields))
    if not valid:
        # The plugin reports its own version, so say which build the site is actually running:
        # «update the plugin» is not actionable when nobody knows what is installed out there.
        installed = body.get("version") if isinstance(body, dict) else None
        where = f" (نسخهٔ فعال روی سایت: {installed})" if isinstance(installed, str) and installed else ""
        raise WooCommerceAPIError(412, (
            "انتشار/اپدیت امن به افزونهٔ Tisa نسخهٔ ۰.۸ و جدول‌های InnoDB نیاز دارد؛ پیش از "
            f"ارسال، افزونه را به‌روزرسانی کن{where}."
        ))


def conflict_id(body: Any, code: str, field: str) -> int:
    if not isinstance(body, dict):
        return 0
    error = body["error"] if isinstance(body.get("error"), dict) else body
    if error.get("code") != code:
        return 0
    data = error.get("data")
    return positive_id({"id": data.get(field)}) if isinstance(data, dict) else 0


async def recover_variation(client: WooClient, endpoint: str, sent: dict[str, Any], body: Any) -> dict[str, Any] | None:
    """Only a matching, fully read-back 409 is an acknowledged create."""
    from bot.services.woo_contract import field_errors

    identity = conflict_id(body, "tisa_variation_exists", "existing_variation_id")
    if not identity:
        return None
    response = await client.get(f"{endpoint}/{identity}")
    try:
        got = response.json()
    except ValueError:
        got = None
    if not response.is_success or field_errors(sent, got, expected_id=identity, variation=True):
        raise WooCommerceAPIError(409, "واریژن این بسته موجود است اما با پیش‌نمایش یکسان نیست (ممکن است موجودی فروش رفته باشد)؛ بدون بازنویسی متوقف شد.")
    return got


class ExistingBatch(WooCommerceAPIError):
    def __init__(self, product_id: int) -> None:
        self.product_id = product_id
        super().__init__(409, "بسته قبلاً ساخته شده؛ ادامه از همان محصول")
