"""Allowed WooCommerce category taxonomy and SKU-driven category rules."""
from __future__ import annotations

import re

TAXONOMY = """فروش ویژه
💥 بلک فرایدی
محصولات عمده
اکسسوری
  استند و هولدر
  بند قاب
  پاپ سوکت و بند آویز
  پک محافظ شارژر
  جا کارتی
  جاکلیدی
  سرکلیدی
  کابل جمع کن
  کیف سیلیکونی
  محافظ کابل
  هوک ایرپاد
قاب و کاور گوشی و تبلت
  آیفون iphone
  سامسونگ samsung
  شیائومی xiaomi
  چاپی
  قاب تبلت
  گلس
    گلس تبلت و آیپد
    گلس صفحه نمایش
    گلس لنز دوربین
  عروسکی
  ست دو نفره
  سیلیکونی
کیف پول ووکامرس
لوازم جانبی اپل واچ
  بند اپل واچ و اسمارت واچ
  گارد اپل واچ
  گلس اپل واچ
  یراق و اکسسوری اپل واچ
لوازم جانبی ایرپاد
  Apple AirPods
    Airpods 1/2
    Airpods 3
    Airpods 4
    Airpods Pro
    Airpods Pro 2
    Airpods Pro 3
  QCY
  Samsung Galaxy Buds
محصولات اورجینال
  برند Blueo
  KZDOO
  VAWL
  KUZOOM
محصولات روود
محصولات سیلیکونی"""
FORBIDDEN = {"فروش ویژه", "💥 بلک فرایدی", "محصولات عمده"}

_PRINT_CATEGORY_LABELS = {"چاپی", "چاپ", "پرینت"}
_PRINT_SKU_RE = re.compile(r"(?:CH|SB)\d*", re.IGNORECASE)


def sku_uses_printed_category(sku_prefix: str) -> bool:
    """Whether the supplied SKU/prefix belongs to the CH or SB print family."""
    sku = re.sub(r"[^A-Z0-9]", "", str(sku_prefix or "").upper())
    return bool(_PRINT_SKU_RE.fullmatch(sku))


def is_printed_category(category: str) -> bool:
    """Whether a taxonomy path names «چاپی» (or one of its AI aliases)."""
    normalized = str(category or "").replace("&gt;", ">").replace(" ← ", ">")
    parts = [part.strip().casefold() for part in normalized.split(">") if part.strip()]
    return any(part in _PRINT_CATEGORY_LABELS for part in parts)


def apply_sku_category_policy(categories: list[str], sku_prefix: str) -> list[str]:
    """Allow the «چاپی» category only for CH/SB product identifiers.

    Printing mentioned in marketing copy (for example «چاپ IMD») describes a
    design, not the store category. The shop's SKU prefix is the authority: CH
    and SB products always get the category; every other SKU loses it, even if
    the AI inferred it from text or returned the full nested taxonomy path.
    """
    allow_print = sku_uses_printed_category(sku_prefix)
    result: list[str] = []
    for raw in categories:
        category = str(raw or "").replace("&gt;", ">").replace(" ← ", ">")
        if is_printed_category(category):
            continue
        if category.strip() and category not in result:
            result.append(category)
    if allow_print:
        result.append("چاپی")
    return result
