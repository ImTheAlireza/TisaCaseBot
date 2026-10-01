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
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Protocol
from collections.abc import Mapping, Sequence

import httpx

from bot import __version__
from bot.config import settings
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
        super().__init__(message)


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
    raise WooCommerceAPIError(response.status_code, error_message(response))


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
    ) -> None:
        """``key``/``secret`` override ``.env`` — the credential tester verifies a candidate
        pair before it is saved, so it must not be pinned to the settings in force."""
        self.audit: Sink = audit if audit is not None else _NullSink()
        self._key, self._secret = key, secret
        self.dry_run = dry_run
        self.timeout_seconds = timeout
        self.attempts = max(1, attempts)
        self._request_number = 0
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

    # — requests —
    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        basic: bool = False,
        headers: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Send ``method url``, authenticated, with the shared retry policy.

        ``kwargs`` go straight to httpx (``json=``, ``content=``, ``files=``, ``data=``):
        this class is a policy wrapper, not a second request builder, and that is the whole
        reason a rehearsal can reuse the production path.
        """
        merged: dict[str, Any] = dict(kwargs)
        query = dict(params or {})
        if basic:
            credentials = app_password()
            if credentials is not None:
                merged["auth"] = credentials
        else:
            query = {**auth_params(self._key, self._secret), **query}
        if query:
            merged["params"] = query
        merged["headers"] = {"User-Agent": USER_AGENT, **dict(headers or {})}

        method = method.upper()
        self._request_number += 1
        request_id = self._request_number
        try:
            path = httpx.URL(str(url)).path
        except Exception:  # malformed configuration: never echo a URL or query string
            path = "<مسیر نامعتبر>"

        response: httpx.Response | None = None
        for attempt in range(self.attempts):
            last_try = attempt == self.attempts - 1
            started = time.perf_counter()
            self.audit.log(
                f"[http:start] #{request_id} {method} {path} "
                f"(تلاش {attempt + 1}/{self.attempts}؛ مهلت هر فاز={self.timeout_seconds:g}s)"
            )
            try:
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
                delay = 2**attempt
                self.audit.log(
                    f"[retry] #{request_id} HTTP {response.status_code} موقت است؛ "
                    f"تلاش مجدد پس از {delay}s"
                )
                await _sleep(delay)
                continue
            return response
        assert response is not None  # pragma: no cover - unreachable with attempts >= 1
        return response

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
        if response.status_code in RETRY_ALWAYS:
            return True
        # 429 is the rate limiter saying "not processed"; a 5xx on a write may mean
        # "processed, answer lost" — that case must not be sent again.
        return response.status_code in RETRY_IF_SAFE and method.upper() in IDEMPOTENT_METHODS

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
        "attributes": [
            {"name": "مدل", "variation": True, "options": ["iPhone 13 Pro Max", "S24 Ultra"]},
            {"name": "رنگ", "variation": True, "options": ["مشکی", "سفید"]},
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
            for _chunk in sent.get("create") or []:
                ids["variation"] += 1
                created.append({"id": ids["variation"]})
            # `update` is echoed back with the values we asked for: the restock writer
            # verifies its own write from this response, and a rehearsal that skips it would
            # be rehearsing half the path.
            echoed = []
            for row in sent.get("update") or []:
                if isinstance(row, dict):
                    echoed.append({key: value for key, value in row.items() if key != "id"}
                                  | {"id": row.get("id")})
            return httpx.Response(201, json={"create": created, "update": echoed})
        if method == "POST" and "/variations" in path:
            ids["variation"] += 1
            return httpx.Response(201, json={"id": ids["variation"]})
        if method == "POST" and path.endswith("/products"):
            ids["product"] += 1
            sku_value = ""
            try:
                sku_value = str(json.loads(body).get("sku") or "")
            except ValueError:
                pass
            return httpx.Response(201, json={"id": ids["product"], "sku": sku_value})
        if method in ("PUT", "PATCH"):
            return httpx.Response(200, json={})
        if method == "DELETE":
            return httpx.Response(200, json={"deleted": True, "previous": {"status": "trash"}})
        return httpx.Response(200, json={})

    return httpx.MockTransport(handle)
