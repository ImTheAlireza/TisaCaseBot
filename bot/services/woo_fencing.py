"""The bot requires the matching native WordPress safety contract before writes.

The local flock only coordinates a shared volume. The importer additionally
fences parent intents and variation combinations inside Woo's native REST hooks;
stock and parent updates use compare-and-set to reject a changed baseline.
"""
from __future__ import annotations

from typing import Any

from bot.services.woo_client import WooClient, WooCommerceAPIError
from bot.services.woo_contract import positive_id

CONTROL_FIELDS = {"tisa_fence", "tisa_expected_stock", "tisa_expected_fields"}


async def require(client: WooClient, base: str) -> None:
    if client.dry_run:
        return
    response = await client.get(base.removesuffix("/products") + "/tisa-health")
    try:
        body = response.json()
    except ValueError:
        body = None
    fields = ("batch_fencing", "variation_fencing", "parent_cas", "stock_cas")
    valid = (response.is_success and isinstance(body, dict) and body.get("contract") == 1
             and all(body.get(key) is True for key in fields))
    if not valid:
        raise WooCommerceAPIError(412, "انتشار/اپدیت امن به افزونهٔ Tisa نسخهٔ ۰.۸ و جدول‌های InnoDB نیاز دارد؛ پیش از ارسال، افزونه را به‌روزرسانی کن.")


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
