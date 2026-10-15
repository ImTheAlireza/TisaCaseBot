"""The one HTTP layer for the shop: auth, timeouts, retry policy, redaction (plan 4.1).

Before this module existed, four files each opened their own ``httpx.AsyncClient``:

* :mod:`bot.services.woocommerce_direct` — the writer (query-string auth, 45 s, its own
  retry loop, ``follow_redirects``, connection limits);
* :mod:`bot.services.woocommerce` — the diagnostics ping (10 s, the same two auth params
  copy-pasted, and *no* retry, so a 502 from a busy host read as "credentials broken");
* :mod:`bot.services.woocommerce_product_test` and :mod:`bot.services.wordpress_media` —
  the 🔧 tools (20 s / 15 s, Basic auth for the media route).

That meant four definitions of "how we talk to WooCommerce": one shared ``User-Agent``
string written two different ways, the consumer secret assembled by hand in two places,
and a retry policy that only existed in the writer. The client below owns all of it, and
the modules above now differ only in *what* they ask for.

The retry policy is the one part where this file deliberately changes behaviour. The old
``_post_transient`` retried ``POST /products`` on 500/502/504 — and a 502 from a proxy in
front of PHP usually means *the request was applied and the answer was lost*. Retrying it
created a second product. So the rule here is narrow and the same for every caller:

* a **connect error** is retried for any method — nothing was sent, nothing was applied;
* a **429** is retried for any method — the shop said "not now", and processed nothing;
* **502/503/504** are retried for idempotent methods only (a repeated read or a repeated
  ``PUT`` of the same payload cannot double a change);
* a **timeout is never retried**. On a 45-second socket that is minutes of the admin
  staring at nothing, and on a write it is exactly the ambiguous case.

The ambiguous write is handled one layer up instead, where it belongs: the content-addressed
batch id and the resume hunt in :mod:`bot.services.publish_batch` top up the half-made
product rather than adding a second one.

One more thing belongs here and nowhere else: :data:`CHALLENGE_COOKIE`. The shop's host stands
behind a gate that answers ``418`` to any client without its cookie and hands that client a
JavaScript page that sets it (2026‑10‑15 — the whole «سایت پایین است» saga). Only the layer that
opens the socket can answer it once for every caller, so that is what :meth:`WooClient._handshake`
does, and :class:`ChallengeJar` keeps the answer across restarts.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Protocol
from collections.abc import Mapping, Sequence

import httpx

from bot import __version__
from bot.config import data_dir, settings
from bot.utils.logging import redact

logger = logging.getLogger(__name__)

#: One spelling of the User-Agent for every shop request (it used to be two, and one of
#: them was a bare ``TisaPostToWP/1.0``). The version is in there so the shop's own access
#: log can tell which build sent a request — the reason `tisa_source` carries it too.
USER_AGENT = f"TisaPostToWP/{__version__} (+https://tisacase.com)"

#: Statuses that mean "busy, ask again" for every method — 429 is a deferral, and
#: 500 is deliberately absent: on WooCommerce it is usually a PHP fatal that a second
#: request one second later will not fix, while it doubles the time the admin waits.
RETRY_ALWAYS = (429,)

#: Statuses retried only when resending cannot double a change (see ``IDEMPOTENT_METHODS``).
RETRY_IF_SAFE = (502, 503, 504)

#: Sending these again cannot double a change. ``POST`` is deliberately absent.
IDEMPOTENT_METHODS = ("GET", "HEAD", "OPTIONS", "PUT", "DELETE")

#: Statuses WordPress never writes on our routes and a security layer in front of it does.
#:
#: ``418`` is the one this shop actually met (2026‑10‑15): the host's bot protection answered the
#: "teapot" — instead of 403 — to every request the watchdog sent, with the keys and without them
#: alike, in fifteen-minute cycles that matched the watchdog's own cadence. Nothing downstream may
#: read it as «the plugin is missing» or «the seller typed something wrong»: the request never
#: reached PHP and no byte was written.
#:
#: What that answer *is* changed once somebody finally looked at its body (see
#: :data:`CHALLENGE_COOKIE`): a JavaScript handshake, not a refusal. :class:`WooClient` now
#: completes the handshake itself, so this code only ever reaches the layers above when the gate
#: refused **without** offering one — the residual, harder block the 🔒 sentence is for.
BLOCKED_STATUS_CODES = frozenset({418})

#: The cookie the host's anti-bot gate plants with JavaScript, and the page that plants it.
#:
#: The 2026‑10‑15 mystery, end to end. Every request that arrived *without* this cookie — the
#: bot's, ``curl``'s, a third-party checker's — was answered ``418`` with a 663-byte page whose
#: whole body is «set ``_dgjsc`` to ``<32 hex>:<14-digit stamp>``, then reload». A browser runs
#: that page, reloads once and never notices: which is why every «پایین بودن» report came from the
#: bot while the shop served customers the whole time. ``curl`` and ``httpx`` execute no
#: JavaScript, so they read ``418`` for as long as the cookie (``Max-Age=7200``) is missing.
CHALLENGE_COOKIE = "_dgjsc"
#: The value, exactly as the page builds it (`_dgjsc=<hex>:<stamp>`), plus the page's own lifetime.
CHALLENGE_PATTERN = re.compile(rf"{CHALLENGE_COOKIE}=([a-f0-9]{{32}}:[0-9]{{14}})")
CHALLENGE_MAX_AGE_PATTERN = re.compile(r"Max-Age=(\d+)")
#: What the observed page asks for (``Max-Age=7200``); used when a page does not say.
CHALLENGE_MAX_AGE_DEFAULT = 7200.0


def challenge_cookie(response: httpx.Response) -> tuple[str, float] | None:
    """The gate's handshake, when this response is the page that asks for it.

    Returns ``(value, max_age)``. ``None`` means «this 418 is a refusal, not a question» — the
    distinction the fence, the queue and the seller's card are all built on.
    """
    if response.status_code not in BLOCKED_STATUS_CODES:
        return None
    try:
        body = response.text
    except Exception:                                       # a body httpx cannot decode
        return None
    match = CHALLENGE_PATTERN.search(body or "")
    if match is None:
        return None
    max_age = CHALLENGE_MAX_AGE_PATTERN.search(body or "")
    return match.group(1), (float(max_age.group(1)) if max_age else CHALLENGE_MAX_AGE_DEFAULT)


class ChallengeJar:
    """Remembers each origin's gate cookie between requests *and* restarts.

    Without it every restart would begin with a 418 (the first request has no cookie) — and a
    single 418 seen by :mod:`bot.services.woo_fencing` is enough to paint the shop 🔒 «مسدود» for
    a whole publish. Two hours of handshake are worth a small JSON file next to the outbox.

    The value is bound to the client the gate issued it to, so a stale entry costs one extra round
    trip (the gate asks again) and never a wrong answer.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path if path is not None else data_dir() / "shop-challenge.json"
        self._entries: dict[str, tuple[str, float]] | None = None

    def _all(self) -> dict[str, tuple[str, float]]:
        if self._entries is None:
            self._entries = self._read()
        return self._entries

    def _read(self) -> dict[str, tuple[str, float]]:
        """Read whatever previous builds left, dropping anything expired or malformed.

        Never raises: a missing, unreadable or hand-mangled file must not be able to stop a
        publish. The worst it can cost is one handshake.
        """
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        now = time.time()
        entries: dict[str, tuple[str, float]] = {}
        for origin, item in (raw.items() if isinstance(raw, dict) else ()):
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                continue
            value, expires = item
            if isinstance(value, str) and isinstance(expires, (int, float)) and float(expires) > now:
                entries[str(origin)] = (value, float(expires))
        return entries

    def value(self, origin: str) -> str | None:
        """The cookie to send to ``origin`` right now, or ``None`` if there is none to send."""
        if not origin:
            return None
        entry = self._all().get(origin)
        if entry is None or entry[1] <= time.time():
            return None
        return entry[0]

    def remember(self, origin: str, value: str, max_age: float) -> None:
        """Keep a freshly solved handshake, and write it down for the next process."""
        if not origin:
            return
        self._all()[origin] = (value, time.time() + max(60.0, float(max_age)))
        self._write()

    def _write(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self._all(), ensure_ascii=False), "utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:  # a read-only data dir must not break publishing
            logger.warning("ذخیرهٔ کوکیِ چالش ناکام ماند (%s): %s", self.path, exc)

#: How many times a request may be sent at all (1 = never retry).
DEFAULT_ATTEMPTS = 3

#: The backoff sleeper. A module-level name, not a direct ``asyncio.sleep`` call, so the
#: suite can watch the retry policy without paying real seconds (see
#: ``tests/test_woo_client.py``) — the same reason :class:`WooClient` takes a ``transport``.
#: Production never touches it; nothing here must grow behaviour of its own.
_sleep = asyncio.sleep


class Sink(Protocol):
    """Anything that can receive one audit line (see :class:`Audit`)."""

    def log(self, line: str) -> None: ...


class Audit:
    """Collects a step-by-step trace that is both logged and kept for errors.

    The lines are shown to the admin on a failed publish, so they are written for a human
    who has to decide the next action — and never contain the request URL, which carries
    the consumer secret (the log filter redacts it anyway: belt and brace).
    """

    __slots__ = ("lines",)

    def __init__(self) -> None:
        self.lines: list[str] = []

    def log(self, line: str) -> None:
        self.lines.append(line)
        logger.info("%s", line)

    def text(self) -> str:
        return "\n".join(self.lines)


class WooCommerceAPIError(RuntimeError):
    """A WooCommerce REST request failed; carries the parsed error message.

    The message comes from the WooCommerce JSON error body (e.g. "Invalid or
    duplicated SKU."), which is far more actionable than httpx's default
    "Client error '400 Bad Request' for url '...'". The URL is deliberately NOT
    stored here: it contains the consumer secret. ``diagnostics`` holds the
    step-by-step audit of the failed request.
    """

    def __init__(self, status_code: int, message: str, diagnostics: Sequence[str] | None = None):
        self.status_code = status_code
        self.diagnostics = list(diagnostics or [])
        self.retry_after: float = 0.0
        #: «nothing answered» — set by the preflight on its own silence, and the only thing that
        #: lets :func:`bot.services.outbox.is_silent` tell a dead host from a busy one (both 503).
        self.host_silent: bool = False
        #: «answered, and the answer was *no*» — the preflight met a security layer in front of
        #: WordPress (:data:`BLOCKED_STATUS_CODES`) that refused *without* offering its
        #: challenge page — the solvable shape of that answer never gets this far, because
        #: :meth:`WooClient._handshake` answers it. The queue waits this out exactly like
        #: silence (nothing was written, so nothing needs a human tonight), but the sentence and
        #: the next step are a different pair — «وایتلیست کن» rather than «به هاست نگاه کن» —
        #: which is why it is a second flag and not a second meaning for the first.
        self.host_blocked: bool = False
        #: The queue's own deadline, said on the error: «مهلت ۲۴ ساعت/تلاش‌ها تمام شد». Only the
        #: queue sets it, so the message can tell that expiry apart from a 410 the shop itself
        #: answered for some other reason.
        self.queue_expired: bool = False
        super().__init__(redact(message))


def retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After", "").strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        try:
            value = parsedate_to_datetime(raw)
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            seconds = (value - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


def auth_params(key: str | None = None, secret: str | None = None) -> dict[str, str]:
    """WooCommerce's query-string credentials (defaults: ``.env``).

    Some shared hosts / ModSecurity setups reject HTTP Basic while allowing WooCommerce's
    query-string auth (the method verified in a browser), so this is the default for every
    ``wc/v3`` route — including the diagnostics ping.
    """
    return {
        "consumer_key": key or settings.woocommerce_key,
        "consumer_secret": secret or settings.woocommerce_secret,
    }


def app_password() -> tuple[str, str] | None:
    """WordPress application-password credentials, or ``None`` when unconfigured.

    Needed for routes that are *not* ``wc/v3`` (the media upload and the SKU plugin): those
    check ``current_user_can``, which a WooCommerce consumer key cannot satisfy.
    """
    if not all((settings.wordpress_url, settings.wordpress_username, settings.wordpress_app_password)):
        return None
    return (settings.wordpress_username, settings.wordpress_app_password)


def products_base(url: str | None = None, version: str | None = None) -> str:
    """``…/wp-json/wc/v3/products`` — the single place the API version is spliced in."""
    root = (url if url is not None else settings.woocommerce_url or "").rstrip("/")
    api = (version if version is not None else settings.woocommerce_version or "wc/v3").strip("/")
    return f"{root}/wp-json/{api}/products"


def origin_of(url: str) -> str:
    """``https://shop.example/wp-json/wc/v3`` → ``https://shop.example`` (``""`` if nonsense).

    Built from the parts rather than ``URL.netloc`` (bytes in httpx) and never from the whole
    URL: what comes back must carry no path and no credentials, whatever the configured base
    happened to include.
    """
    try:
        parsed = httpx.URL(url)
        host = parsed.host or ""
        port = f":{parsed.port}" if parsed.port else ""
    except Exception:                                       # malformed configuration
        return ""
    return f"{parsed.scheme}://{host}{port}" if parsed.scheme and host else ""


def media_base(url: str | None = None) -> str:
    """``…/wp-json/wp/v2/media`` — the WordPress upload endpoint."""
    root = (url if url is not None else settings.wordpress_url or "").rstrip("/")
    return f"{root}/wp-json/wp/v2/media"


def error_message(response: httpx.Response) -> str:
    """Pull the human-readable reason out of a WooCommerce/WordPress error body.

    Never includes the URL: it carries the consumer secret.
    """
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        for key in ("message", "code", "error"):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        data = body.get("data")
        if isinstance(data, dict) and data.get("status"):
            return f"HTTP {data['status']}"
    text = (response.text or "").strip()
    return text if text and len(text) <= 400 else f"HTTP {response.status_code}"


def describe_exception(exc: BaseException) -> str:
    """Render transport errors usefully; httpx timeout exceptions often have empty strings.

    Do not include the raw transport message for network errors: it may contain a request URL,
    and WooCommerce credentials are query parameters. The request method/path are logged
    separately by :class:`WooClient` without query parameters.
    """
    if isinstance(exc, httpx.ReadTimeout):
        detail = "پاسخ فروشگاه نرسید؛ ممکن است درخواست اعمال شده باشد"
    elif isinstance(exc, httpx.ConnectTimeout):
        detail = "مهلت اتصال به فروشگاه تمام شد"
    elif isinstance(exc, httpx.WriteTimeout):
        detail = "ارسال درخواست کامل نشد"
    elif isinstance(exc, httpx.PoolTimeout):
        detail = "اتصال آزاد در دسترس نبود"
    elif isinstance(exc, httpx.TimeoutException):
        detail = "مهلت درخواست تمام شد"
    elif isinstance(exc, httpx.ConnectError):
        detail = "اتصال به فروشگاه برقرار نشد"
    elif isinstance(exc, httpx.RemoteProtocolError):
        detail = "ارتباط پیش از پاسخ قطع شد"
    elif isinstance(exc, httpx.TransportError):
        detail = "خطای شبکه"
    elif isinstance(exc, TimeoutError):
        # ``asyncio.timeout`` raises a bare TimeoutError with an empty message — and it is not
        # an httpx error, so without this branch the card read «TimeoutError: » and nothing else.
        detail = "مهلت کلِ درخواست تمام شد (پاسخ فروشگاه در زمان مقرر نرسید)"
    else:
        detail = redact(str(exc).strip()) or "جزئیات خطا در دسترس نیست"
    return f"{type(exc).__name__}: {detail}"


def body_snippet(response: httpx.Response, limit: int = 400) -> str:
    """A compact, single-line snippet of the raw response body for the audit."""
    text = (response.text or "").replace("\n", " ").replace("\r", " ").strip()
    return redact(text[:limit])


def body_for_log(body: str) -> str:
    """A request body as one log line: JSON stays readable, files do not.

    A media POST is ``multipart/form-data`` and its first bytes are raw JPEG — quoting them
    printed a wall of control characters into the log group, which is the opposite of a
    diagnostic.
    """
    if not body:
        return ""
    head = body[:400]
    if any(ord(ch) < 9 or 11 <= ord(ch) <= 12 or 14 <= ord(ch) < 32 for ch in head):
        return f"<{len(body):,} بایت دادهٔ دودویی (فایل ارسالی) — بدنه در لاگ نمی‌آید>"
    return redact(head)


def check(response: httpx.Response) -> httpx.Response:
    """Raise a readable :class:`WooCommerceAPIError` instead of httpx's URL-leaking one."""
    if response.is_success:
        return response
    error = WooCommerceAPIError(response.status_code, error_message(response))
    error.retry_after = retry_after(response) or 0.0
    raise error


class WooClient:
    """Async client for the shop's REST APIs, with the policy every caller used to redo.

    ``dry_run`` swaps in a fake transport (see :func:`dry_run_transport`); ``transport`` is
    a seam for the test suite to hand the writer a store that answers with 500s or a
    half-created product. When both are given, ``dry_run`` wins — a rehearsal must never be
    able to talk to the real shop by accident.

    Credentials are injected per request (query string for ``wc/v3``, application password
    for the other routes) so no caller builds an auth dict again.
    """

    def __init__(
        self,
        *,
        audit: Sink | None = None,
        timeout: float = 45.0,
        transport: httpx.BaseTransport | None = None,
        dry_run: bool = False,
        attempts: int = DEFAULT_ATTEMPTS,
        key: str | None = None,
        secret: str | None = None,
        min_request_interval: float = 0.0,
        max_concurrent_requests: int = 20,
        challenges: ChallengeJar | None = None,
    ) -> None:
        """``key``/``secret`` override ``.env`` — the credential tester verifies a candidate
        pair before it is saved, so it must not be pinned to the settings in force.

        ``challenges`` is the gate's cookie jar; the suite hands in one of its own so that no test
        writes into the real ``data/``.
        """
        self.audit: Sink = audit if audit is not None else _NullSink()
        self._key, self._secret = key, secret
        self.dry_run = dry_run
        self.challenges = challenges if challenges is not None else ChallengeJar()
        self.timeout_seconds = timeout
        self.attempts = max(1, attempts)
        self._request_number = 0
        self.min_request_interval = max(0.0, float(min_request_interval))
        self._request_slots = asyncio.Semaphore(max(1, int(max_concurrent_requests)))
        self._pace_lock = asyncio.Lock()
        self._last_request_started = 0.0
        kwargs: dict[str, Any] = {
            "timeout": timeout,
            "follow_redirects": True,
            "limits": httpx.Limits(max_connections=20, max_keepalive_connections=10),
        }
        if dry_run:
            kwargs["transport"] = dry_run_transport(self.audit)
        elif transport is not None:
            kwargs["transport"] = transport
        self._client = httpx.AsyncClient(**kwargs)

    # — lifecycle —
    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> WooClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def _pace(self) -> None:
        """Keep this client's outgoing request starts at least the configured gap apart."""
        if self.min_request_interval <= 0:
            return
        async with self._pace_lock:
            now = time.monotonic()
            delay = self.min_request_interval - (now - self._last_request_started)
            if delay > 0:
                await _sleep(delay)
            self._last_request_started = time.monotonic()

    # — requests —
    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        basic: bool = False,
        public: bool = False,
        headers: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Send ``method url``, authenticated, with the shared retry policy.

        ``kwargs`` go straight to httpx (``json=``, ``content=``, ``files=``, ``data=``, and
        ``timeout=`` to shorten one phase): this class is a policy wrapper, not a second
        request builder, and that is the whole reason a rehearsal can reuse the production
        path.

        ``public=True`` sends the request with **no credentials at all** — the liveness probe
        in :mod:`bot.services.woo_fencing` needs it. Two reasons: a URL without
        ``consumer_secret`` in the query string cannot leak it into the shop's access log, and
        a probe that answers while the authenticated request hangs is itself the diagnosis
        («the host is dropping requests that carry our keys», not «the site is down»).
        """
        merged: dict[str, Any] = dict(kwargs)
        query = dict(params or {})
        if not public:
            if basic:
                credentials = app_password()
                if credentials is not None:
                    merged["auth"] = credentials
            else:
                query = {**auth_params(self._key, self._secret), **query}
        if query:
            merged["params"] = query
        merged["headers"] = {"User-Agent": USER_AGENT, **dict(headers or {})}
        # A solved handshake rides along on every request, exactly as a browser replays its
        # cookies: without it each request after the first would meet the gate's 418 again and
        # pay a second round trip. It goes through the client's own jar (not a header of ours) so
        # that it would merge with any other cookie instead of replacing it.
        held = self.challenges.value(origin_of(str(url)))
        if held:
            self._install_cookie(str(url), held)

        method = method.upper()
        self._request_number += 1
        request_id = self._request_number
        # A caller may shorten one phase (the safety preflight does). The outer guard and the
        # audit line must speak the same number as the socket, not the client-wide default —
        # a trace that says «مهلت هر فاز=45s» for a request abandoned at 8s sends
        # whoever is debugging this to the wrong timeout.
        try:
            phase = float(merged.get("timeout", self.timeout_seconds))
        except (TypeError, ValueError):
            phase = float(self.timeout_seconds)
        try:
            path = httpx.URL(str(url)).path
        except Exception:  # malformed configuration: never echo a URL or query string
            path = "<مسیر نامعتبر>"

        response: httpx.Response | None = None
        for attempt in range(self.attempts):
            last_try = attempt == self.attempts - 1
            started = time.perf_counter()
            try:
                # Product publishing configures one in-flight request per client. That keeps
                # image and variation fallbacks from turning into bursts on a shared host.
                async with self._request_slots:
                    await self._pace()
                    started = time.perf_counter()
                    self.audit.log(
                        f"[http:start] #{request_id} {method} {path} "
                        f"(تلاش {attempt + 1}/{self.attempts}؛ مهلت هر فاز={phase:g}s)"
                    )
                    async with asyncio.timeout(max(1.0, phase * 2)):
                        response = await self._client.request(method, str(url), **merged)
            except httpx.TransportError as exc:
                elapsed_ms = (time.perf_counter() - started) * 1000
                retry = not last_try and self._retry_network(exc)
                self.audit.log(
                    f"[http:error] #{request_id} {method} {path} "
                    f"پس از {elapsed_ms:.0f} ms: {describe_exception(exc)} "
                    f"(تلاش {attempt + 1}/{self.attempts}؛ "
                    f"{'تلاش مجدد می‌شود' if retry else 'تلاش مجدد در همین درخواست انجام نمی‌شود'})"
                )
                if not retry:
                    raise
                delay = 2**attempt
                self.audit.log(f"[retry] #{request_id} خطای اتصال؛ تلاش مجدد پس از {delay}s")
                await _sleep(delay)
                continue

            elapsed_ms = (time.perf_counter() - started) * 1000
            self.audit.log(
                f"[http:done] #{request_id} {method} {path} → HTTP {response.status_code} "
                f"در {elapsed_ms:.0f} ms (تلاش {attempt + 1}/{self.attempts})"
            )
            if not last_try and self._retry_status(method, response):
                delay = self._retry_delay(response, attempt)
                self.audit.log(
                    f"[retry] #{request_id} HTTP {response.status_code} موقت است؛ "
                    f"تلاش مجدد پس از {delay:g}s"
                )
                await _sleep(delay)
                continue
            return await self._handshake(method, str(url), merged, response, request_id, phase, path)
        # Unreachable while ``attempts >= 1``, but ``assert`` vanishes under ``python -O``
        # and the caller would then get ``None`` as if it were a response.
        raise RuntimeError("no request was attempted (attempts must be at least 1)")  # pragma: no cover

    def _install_cookie(self, url: str, value: str) -> None:
        """Put the gate's cookie in the client's jar, scoped to the URL's host."""
        try:
            host = httpx.URL(url).host or ""
        except Exception:                                   # malformed configuration
            return
        if host:
            self._client.cookies.set(CHALLENGE_COOKIE, value, domain=host, path="/")

    async def _handshake(
        self,
        method: str,
        url: str,
        merged: dict[str, Any],
        response: httpx.Response,
        request_id: int,
        phase: float,
        path: str,
    ) -> httpx.Response:
        """Answer the gate's JavaScript handshake for it, once, and send the request again.

        The page that arrived is not a refusal — it is the gate saying «set this cookie and come
        back», which a browser does without anyone noticing and ``httpx`` never does. Doing it here,
        in the one HTTP layer, means every caller above (the safety fence, the four probes, the
        media test, the writer) sees the real answer instead of a 418 nobody could explain.

        Sending again is safe **even for a POST**: this answer proves PHP never ran — the gate
        stands in front of it — so no byte of the request has been applied. That is the same
        argument the retry policy makes for a connect error, and the reason this repeat does not
        spend one of ``attempts``: the first request was never delivered.

        A second 418 is returned as it is. Looping would hammer the gate that is already unhappy,
        and the fence above is the layer that knows how to *report* a real, challenge-less block.
        """
        challenge = challenge_cookie(response)
        if challenge is None:
            return response
        value, max_age = challenge
        self.challenges.remember(origin_of(url), value, max_age)
        self._install_cookie(url, value)
        self.audit.log(
            f"[http:gate] #{request_id} لایهٔ ضدِرباتِ میزبان چالشِ جاوااسکریپتی داد؛ "
            f"کوکی {CHALLENGE_COOKIE} ست شد (اعتبار {max_age:g}s) و همان درخواست یک‌بار تکرار می‌شود"
        )
        again = dict(merged)
        started = time.perf_counter()
        try:
            async with self._request_slots:
                await self._pace()
                async with asyncio.timeout(max(1.0, phase * 2)):
                    second = await self._client.request(method, url, **again)
        except httpx.TransportError as exc:
            self.audit.log(
                f"[http:gate] #{request_id} تکرارِ پس از چالش بی‌پاسخ ماند: {describe_exception(exc)}"
            )
            return response
        self.audit.log(
            f"[http:done] #{request_id} {method} {path} → HTTP {second.status_code} "
            f"در {(time.perf_counter() - started) * 1000:.0f} ms (پس از چالش)"
        )
        return second

    def _retry_network(self, exc: httpx.TransportError) -> bool:
        """May this failed request be sent again?

        Only a connect error, which proves the shop never received the request. A read
        timeout may have landed and been applied — and a second ``POST /products`` is the
        duplicate this whole layer exists to avoid. It is also slow (45 s socket × attempts),
        which in a chat reads as a hung bot.
        """
        return isinstance(exc, httpx.ConnectError)

    @staticmethod
    def _retry_status(method: str, response: httpx.Response) -> bool:
        if (retry_after(response) or 0) > 30:
            return False  # Do not retry EARLIER than a long server deferral. Queue it instead.
        if response.status_code in RETRY_ALWAYS:
            return True
        # 429 is the rate limiter saying "not processed"; a 5xx on a write may mean
        # "processed, answer lost" — that case must not be sent again.
        return response.status_code in RETRY_IF_SAFE and method.upper() in IDEMPOTENT_METHODS

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        fallback = float(2 ** min(max(attempt, 0), 5))
        return min(30.0, max(fallback, retry_after(response) or 0.0))

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", url, **kwargs)

    async def put(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("PUT", url, **kwargs)

    async def delete(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("DELETE", url, **kwargs)


class _NullSink:
    """Audit target for callers that do not care about the trace (the 🔧 test tools)."""

    __slots__ = ()

    def log(self, line: str) -> None:  # noqa: ARG002 - the point is to drop it
        return



DEMO_PRODUCT_ID = 850_001
DEMO_VARIATION_IDS = (860_001, 860_002)
#: What a seller can type in rehearsal and get this product back. Deliberately not the
#: product the publisher creates (its SKU must stay free) and deliberately narrow: only
#: these search terms resolve, so nothing else in the codebase sees a phantom row.
DEMO_SEARCH_TERMS = ("دمو", "demo", "مشکی", "سفید")


def _demo_product() -> dict[str, Any]:
    return {
        "id": DEMO_PRODUCT_ID,
        "name": "محصول آزمایشیِ دمو (dry-run)",
        "sku": "DEMO1",
        "type": "variable",
        "status": "draft",
        "regular_price": "698000",
        "sale_price": "",
        "price": "698000",
        "manage_stock": False,
        "stock_quantity": None,
        "stock_status": "instock",
        "purchasable": True,
        # Two pictures, so a rehearsal of an update can say «جایگزین ۲ تصویر فعلی».
        "images": [{"id": 700_001, "src": "https://dry.run/demo-1.jpg"},
                   {"id": 700_002, "src": "https://dry.run/demo-2.jpg"}],
        "attributes": [
            {"id": 0, "name": "مدل", "position": 0, "visible": True, "variation": True,
             "options": ["iPhone 13 Pro Max", "S24 Ultra"]},
            {"id": 0, "name": "رنگ", "position": 1, "visible": True, "variation": True,
             "options": ["مشکی", "سفید"]},
        ],
    }


def _demo_variations() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, color in enumerate(["مشکی", "سفید"]):
        rows.append({
            "id": DEMO_VARIATION_IDS[index],
            "sku": "",
            "type": "variation",
            "status": "publish",
            "parent_id": DEMO_PRODUCT_ID,
            "attributes": [
                {"variation": "مدل", "option": "iPhone 13 Pro Max"},
                {"variation": "رنگ", "option": color},
            ],
            "regular_price": "698000",
            "sale_price": "",
            "price": "698000",
            "manage_stock": True,
            "stock_quantity": 0,
            "stock_status": "instock",
            "visible": True,
        })
    return rows


def _dry_answers(url: httpx.URL) -> bool:
    """True for the read calls that should return the demo product, and only those."""
    path = url.path
    if path.endswith("/variations") and f"/products/{DEMO_PRODUCT_ID}" in path:
        return True
    if re.search(rf"/products/{DEMO_PRODUCT_ID}$", path):
        return True
    if path.endswith("/products"):
        search = (url.params.get("search") or "").strip().lower()
        return bool(search) and any(term in search or search in term for term in DEMO_SEARCH_TERMS)
    return False


def _dry_response(method: str, path: str, body: str) -> httpx.Response:
    """Answer a read/write against the demo product the way WooCommerce would."""
    if method == "GET":
        if path.endswith("/variations"):
            return httpx.Response(200, json=_demo_variations())
        if path.endswith("/products"):
            return httpx.Response(200, json=[_demo_product()])       # a collection is a list
        return httpx.Response(200, json=_demo_product())
    try:
        sent = json.loads(body) if body else {}
    except ValueError:
        sent = {}
    if not isinstance(sent, dict):
        sent = {}
    # Echo the fields back, the way WooCommerce does: a caller that verifies its own write from
    # the response gets the same verification in rehearsal as in production.
    row = _demo_product() if not path.endswith("/variations") else {}
    row.update({key: value for key, value in sent.items() if key != "id"})
    return httpx.Response(200, json=row)


def dry_run_transport(audit: Sink) -> httpx.MockTransport:
    """A transport that answers like WooCommerce/WordPress — and touches nothing.

    The point of a dry run is that it is *the same code path*: the real builder makes the
    payload, the real client signs and sends it, and only the socket is replaced. A second,
    simplified “preview publisher” would drift from production inside a release — the exact
    failure this repo keeps documenting.
    """
    ids = {"product": 800_000, "media": 900_000, "variation": 700_000, "category": 600_000}

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method.upper()
        body = request.content.decode("utf-8", "ignore") if request.content else ""
        audit.log(f"[dry-run] {method} {path}" + (f" {body_for_log(body)}" if body else ""))
        if "next-sku" in path:
            # The plugin counter is deliberately absent: the fallback (scan the
            # catalog) must be what a dry run exercises too.
            return httpx.Response(404, json={"message": "dry-run: افزونهٔ SKU صدا زده نشد"})
        if method == "GET" and path.endswith("/categories"):
            wanted = str(request.url.params.get("search") or "").strip()
            if not wanted:
                return httpx.Response(200, json=[])
            ids["category"] += 1
            return httpx.Response(200, json=[{"id": ids["category"], "name": wanted, "parent": 0}])
        if _dry_answers(request.url):
            # A rehearsal of «شارژ محصول موجود» has to read a product with variations, or the
            # diff it prints would be a diff of nothing. Only the demo row is ever answered:
            # an open-ended fake catalog would make every SKU candidate look taken, which is
            # precisely the dry-run case the empty-catalog answer protects.
            return _dry_response(method, path, body)
        if method == "GET":
            return httpx.Response(200, json=[])
        if method == "POST" and "/media" in path:
            ids["media"] += 1
            return httpx.Response(201, json={"id": ids["media"], "source_url": f"https://dry.run/{ids['media']}.jpg"})
        if method == "POST" and path.endswith("/variations/batch"):
            try:
                sent = json.loads(body)
            except ValueError:
                sent = {}
            created = []
            for chunk in sent.get("create") or []:
                ids["variation"] += 1
                created.append(dict(chunk) | {"id": ids["variation"]})
            # `update` is echoed back with the values we asked for: the restock writer
            # verifies its own write from this response, and a rehearsal that skips it would
            # be rehearsing half the path.
            echoed = []
            for row in sent.get("update") or []:
                if isinstance(row, dict):
                    echoed.append({key: value for key, value in row.items() if key != "id"}
                                  | {"id": row.get("id")})
            # `delete` is echoed the way WooCommerce does (the removed objects), so the update
            # writer verifies deletions in a rehearsal exactly as it does for real.
            removed = [{"id": variation_id} for variation_id in sent.get("delete") or []]
            return httpx.Response(201, json={"create": created, "update": echoed, "delete": removed})
        if method == "POST" and "/variations" in path:
            ids["variation"] += 1
            return httpx.Response(201, json=(json.loads(body) if body else {}) | {"id": ids["variation"]})
        if method == "POST" and path.endswith("/products"):
            ids["product"] += 1
            sku_value = ""
            try:
                sku_value = str(json.loads(body).get("sku") or "")
            except ValueError:
                pass
            return httpx.Response(201, json=(json.loads(body) if body else {}) | {"id": ids["product"], "sku": sku_value})
        if method in ("PUT", "PATCH"):
            return httpx.Response(200, json=(json.loads(body) if body else {}) | {"id": int(path.rsplit("/", 1)[-1])})
        if method == "DELETE":
            return httpx.Response(200, json={"deleted": True, "previous": {"status": "trash"}})
        return httpx.Response(200, json={})

    return httpx.MockTransport(handle)
