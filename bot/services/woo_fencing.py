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
from bot.services.woo_client import WooClient, WooCommerceAPIError, error_message
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


def _origin(base: str) -> str:
    """``https://shop/wp-json/wc/v3`` → ``https://shop`` (``""`` when the config is nonsense).

    Built from the parts, not from ``URL.netloc`` (which is bytes in httpx) and not from the
    whole URL: a probe must not carry anything but scheme/host/port, whatever the configured
    base happened to include.
    """
    try:
        url = httpx.URL(base)
        host = url.host or ""
        port = f":{url.port}" if url.port else ""
    except Exception:                                       # malformed configuration
        return ""
    return f"{url.scheme}://{host}{port}" if url.scheme and host else ""


def _detail(response: httpx.Response) -> str:
    """The shop's own one-line reason, when it bothered to send one (never a URL)."""
    try:
        reason = (error_message(response) or "").strip().replace("\n", " ")
    except Exception:                                       # a body httpx cannot decode
        return ""
    return f": {reason[:160]}" if reason and reason != f"HTTP {response.status_code}" else ""


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
    origin = _origin(base)
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
            raise WooCommerceAPIError(503, (
                "اتصال به فروشگاه برقرار نشد — نه DNS حل می‌شود و نه پورت جواب می‌دهد. هیچ "
                "درخواستی به سایت نرسیده، پس نیم‌ساخته‌ای هم وجود ندارد؛ انتشار در صفِ تلاشِ "
                "دوباره می‌ماند. از سمت سرورِ ربات: `curl -m 15 https://…/wp-json/`."
            )) from failure
        if await _awake(client, base):
            raise WooCommerceAPIError(503, (
                "سایت زنده است ولی مسیر افزونهٔ تیسا (tisa-health) به درخواستی که کلید در آدرس "
                "دارد پاسخ نداد. این شکلِ کارِ فایروال/ModSecurity میزبان است، نه وردپرس. از همان "
                "سرورِ ربات امتحان کن: `curl -m 15` همان آدرسِ بررسی ایمنی را با "
                "`consumer_key`/`consumer_secret` بزن؛ اگر آن هم گیر کرد، قاعدهٔ «consumer» را از "
                "فایروال بردار."
            )) from failure
        raise WooCommerceAPIError(503, (
            "فروشگاه به هیچ درخواستی پاسخ نداد — نه بررسی ایمنی، نه یک GET سادهٔ بدون کلید. "
            "چیزی نوشته نشده و نیم‌ساخته‌ای وجود ندارد؛ انتشار در صفِ تلاشِ دوباره می‌ماند. "
            "از سمت میزبان ببین: CPU/RAM و سقف «entry processes»، استخر PHP-FPM/LiteSpeed، "
            "و اینکه وردپرس اصلاً به ربات پاسخ می‌دهد یا فایروال/IP را بسته است."
        )) from failure
    status = response.status_code
    if status in TRANSIENT_STATUS_CODES:
        raise WooCommerceAPIError(503, (
            f"فروشگاه هنگام بررسی ایمنی شلوغ بود (HTTP {status}{_detail(response)}). هیچ چیزی "
            "نوشته نشد و این بسته در صفِ تلاشِ دوباره می‌ماند."
        ))
    if status in (401, 403):
        raise WooCommerceAPIError(403, (
            f"کلید WooCommerce برای بررسی ایمنی پذیرفته نشد (HTTP {status}{_detail(response)}). "
            "کلید باید متعلق به کاربری با دسترسی «ویرایش محصول» باشد؛ «🔑 تست کلید» را بزن."
        ))
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
