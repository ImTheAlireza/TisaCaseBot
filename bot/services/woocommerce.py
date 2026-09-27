"""Small WooCommerce REST API check used by the diagnostics button."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

from bot.services.woo_client import Audit, WooClient, body_snippet, describe_exception, products_base

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WooCommerceResult:
    ok: bool
    status_code: int | None
    message: str
    elapsed_ms: float


async def ping_woocommerce(
    url: str,
    consumer_key: str,
    consumer_secret: str,
    version: str = "wc/v3",
    timeout: float = 10.0,
) -> WooCommerceResult:
    """Authenticate against WooCommerce and request its system status.

    The request is deliberately made server-side so credentials never reach Telegram, and
    it is authenticated the way the site owner verified it in a browser: WooCommerce's
    query-string auth (some shared hosts / ModSecurity setups reject HTTP Basic). Both are
    the client's job now — see :mod:`bot.services.woo_client`.

    ``attempts=1``: a diagnostics button must answer quickly instead of fighting a busy
    host with backoff, so a 502 here is reported as what it is rather than retried.
    """
    # Read one product: it exercises the same authenticated REST route that
    # the site owner verified in the browser, without downloading the catalog.
    started = time.perf_counter()
    audit = Audit()
    try:
        async with WooClient(
            audit=audit, timeout=timeout, attempts=1, key=consumer_key, secret=consumer_secret
        ) as client:
            response = await client.get(products_base(url, version), params={"per_page": 1})
        elapsed = (time.perf_counter() - started) * 1000
        if response.is_success:
            logger.info("WooCommerce ping trace: %s", audit.text())
            return WooCommerceResult(True, response.status_code, "Connected", elapsed)
        # The response body is useful for distinguishing WooCommerce permissions
        # from a hosting/WAF block. It is sent only to the private log chat and
        # never includes the request URL (which contains no credentials here).
        # The body distinguishes a WooCommerce permission problem from a hosting/WAF block.
        # It goes to the log only (never to the chat) and is redacted + single-lined by the
        # client helper rather than sliced by hand.
        logger.warning("WooCommerce response body: %s", body_snippet(response, 800))
        logger.warning("WooCommerce ping trace: %s", audit.text())
        if response.status_code == 401:
            message = "Authentication failed (check the consumer key and secret)."
        elif response.status_code == 403:
            message = "Forbidden (check key permissions, security plugins, or WAF/Cloudflare rules)."
        elif response.status_code == 404:
            message = "Endpoint not found (check the store URL or API version)."
        else:
            message = f"WooCommerce returned HTTP {response.status_code}."
        return WooCommerceResult(False, response.status_code, message, elapsed)
    except httpx.TimeoutException as exc:
        elapsed = (time.perf_counter() - started) * 1000
        logger.warning("WooCommerce ping timeout after %.0f ms: %s | %s", elapsed, describe_exception(exc), audit.text())
        return WooCommerceResult(False, None, describe_exception(exc), elapsed)
    except httpx.HTTPError as exc:
        elapsed = (time.perf_counter() - started) * 1000
        logger.warning("WooCommerce ping transport error after %.0f ms: %s | %s", elapsed, describe_exception(exc), audit.text())
        return WooCommerceResult(False, None, describe_exception(exc), elapsed)
    except Exception:
        elapsed = (time.perf_counter() - started) * 1000
        return WooCommerceResult(False, None, "Could not connect to WooCommerce.", elapsed)
