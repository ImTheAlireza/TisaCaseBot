"""Proof of REST writes, shared by creation and updates.

HTTP 200, a positive ID, or a missing echo field alone is not proof that the
approved values landed. Compare decimal prices numerically and every requested
field structurally. A missing echo may be verified by a read, never by another
POST of a creation whose outcome is ambiguous.
"""
from __future__ import annotations

import html
import re
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import unquote

import httpx

from bot.services.airpods_parser import AIRPODS_ATTRIBUTE, is_airpods_attribute
from bot.services.color_matrix import is_color_attribute, is_model_attribute


def json_body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except (ValueError, TypeError):
        return None


def positive_id(row: Any) -> int:
    raw = row.get("id") if isinstance(row, dict) else None
    if type(raw) is int:
        return max(0, raw)
    if isinstance(raw, str) and re.fullmatch(r"[0-9]+", raw):
        return int(raw)
    return 0


def text_key(value: Any) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip().casefold()


def attribute_key(value: Any) -> str:
    name = html.unescape(unquote(str(value or "")))
    if is_model_attribute(name):
        return "مدل"
    if is_color_attribute(name):
        return "رنگ"
    if is_airpods_attribute(name):
        return AIRPODS_ATTRIBUTE
    return text_key(name)


def decimal_value(value: Any) -> Decimal | None:
    if value in ("", None):
        return Decimal(0)
    if isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value).replace(",", "").replace("٬", ""))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _attributes_equal(want: Any, have: Any) -> bool:
    if not isinstance(want, list) or not isinstance(have, list) or len(want) != len(have):
        return False
    actual: dict[str, dict[str, Any]] = {}
    for row in have:
        if not isinstance(row, dict) or not row.get("name"):
            return False
        key = attribute_key(row["name"])
        if key in actual:
            return False
        actual[key] = row
    for row in want:
        if not isinstance(row, dict):
            return False
        got = actual.get(attribute_key(row.get("name")))
        if got is None:
            return False
        for flag in ("variation", "visible"):
            if flag in row and (type(got.get(flag)) is not bool or got[flag] != row[flag]):
                return False
        if "option" in row and text_key(row["option"]) != text_key(got.get("option")):
            return False
        if "options" in row:
            options = got.get("options")
            if not isinstance(options, list) or {text_key(x) for x in options} != {text_key(x) for x in row["options"]}:
                return False
    return True


def field_errors(
    sent: dict[str, Any], got: Any, *, expected_id: int | None = None,
    require_id: bool = True, variation: bool = False,
) -> list[str]:
    """Names of missing/different requested values (never accepts an error row)."""
    if not isinstance(got, dict) or got.get("error"):
        return ["پاسخ JSON معتبر"]
    bad: list[str] = []
    if require_id and (not positive_id(got) or (expected_id is not None and positive_id(got) != expected_id)):
        bad.append("id")
    for key, want in sent.items():
        if key == "id":
            if positive_id(got) != positive_id(sent):
                bad.append(key)
            continue
        # `visible` is not in the wc/v3 variation schema. `status=publish`
        # controls visibility there; parent attribute visibility is checked above.
        if key in {"tisa_fence", "tisa_expected_stock", "tisa_expected_fields"} or (variation and key == "visible"):
            continue
        if key not in got:
            bad.append(key)
            continue
        have = got[key]
        if key in {"regular_price", "sale_price", "weight", "menu_order"}:
            a, b = decimal_value(want), decimal_value(have)
            same = a is not None and b is not None and a == b
        elif key == "stock_quantity":
            same = have is None if want is None else decimal_value(want) == decimal_value(have) and have is not None
        elif key in {"manage_stock", "virtual", "downloadable"}:
            same = type(have) is bool and have == want
        elif key == "attributes":
            same = _attributes_equal(want, have)
        elif key in {"images", "categories"}:
            valid = isinstance(have, list) and all(isinstance(row, dict) and positive_id(row) for row in have)
            wanted = [positive_id(row) for row in want]
            actual = [positive_id(row) for row in have] if valid else []
            same = valid and (wanted == actual if key == "images" else set(wanted) == set(actual))
        elif key == "image":
            same = positive_id(want) == positive_id(have)
        elif key == "meta_data":
            actual_meta = {str(row.get("key")): row.get("value") for row in have if isinstance(row, dict)} if isinstance(have, list) else {}
            same = all(str(row.get("key")) in actual_meta and actual_meta[str(row["key"])] == row.get("value") for row in want)
        elif key in {"name", "description"}:
            same = text_key(want) == text_key(have)
        else:
            same = want == have
        if not same:
            bad.append(key)
    return list(dict.fromkeys(bad))


def same_values(sent: dict[str, Any], got: Any, *, variation: bool = True) -> bool:
    return not field_errors(sent, got, variation=variation)


_FIELD_LABELS = {
    "name": "عنوان", "attributes": "ویژگی‌ها", "regular_price": "قیمت عادی", "sale_price": "قیمت ویژه",
    "manage_stock": "مدیریت موجودی", "stock_quantity": "تعداد موجودی", "stock_status": "وضعیت موجودی",
    "status": "وضعیت انتشار", "images": "تصاویر", "image": "تصویر", "categories": "دسته‌بندی",
    "meta_data": "اطلاعات پیگیری", "menu_order": "ترتیب", "id": "شناسه", "sku": "SKU",
}


def human_fields(errors: list[str]) -> str:
    return "، ".join(_FIELD_LABELS.get(error, error) for error in errors)
