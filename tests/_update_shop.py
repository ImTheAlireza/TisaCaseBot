"""A WooCommerce that remembers — for the tests of «🔄 اپدیت محصول».

``_flow_harness.FakeStore`` answers every write with «fine» and forgets it; that is enough to
count requests, and useless for the question an update has to answer: *after this sequence of
requests, what does the shop hold?* So this one keeps a product, its variations and a media
counter, applies the writes the way WooCommerce does (batch order: create → update → delete; a
failed batch row carries an ``error`` object instead of an id; a hundred rows per batch), and
exposes the knobs a real host turns: a batch route that does not exist, a row the batch swallows,
an echo that disagrees, a delete that is not echoed.

Used by ``test_update_apply`` and ``test_update_flow``. The row builders live here too, so the
plan tests and the shop tests argue about the same product.
"""
from __future__ import annotations

import json
import re
from typing import Any

import httpx

from bot.services import product_match

BATCH_LIMIT = 100


def product_row(**over: object) -> dict:
    row: dict = {
        "id": 1201, "name": "قاب سیلیکونی آیفون", "sku": "BO7", "type": "variable",
        "status": "publish", "regular_price": "698000", "sale_price": "", "price": "698000",
        "manage_stock": False, "stock_quantity": None, "stock_status": "instock",
        "images": [{"id": 11}, {"id": 12}, {"id": 13}],
        "attributes": [
            {"id": 0, "name": "مدل", "position": 0, "visible": True, "variation": True,
             "options": ["iPhone 13 Pro Max", "S24 Ultra"]},
            {"id": 0, "name": "رنگ", "position": 1, "visible": True, "variation": True,
             "options": ["مشکی", "سفید", "سبز"]},
        ],
    }
    row.update(over)
    return row


def variation_row(index: int, model: str, color: str, *, stock: int | None = 0,
                  status: str = "instock", price: str = "698000", sale: str = "",
                  manage: bool = True) -> dict:
    return {
        "id": 9000 + index, "type": "variation", "status": "publish",
        "attributes": [{"id": 0, "name": "مدل", "option": model},
                       {"id": 0, "name": "رنگ", "option": color}],
        "regular_price": price, "sale_price": sale, "price": sale or price,
        "manage_stock": manage, "stock_quantity": stock, "stock_status": status,
    }


def default_variations() -> list[dict]:
    """iPhone 13 Pro Max × مشکی/سفید at 698,000 (stock 0); S24 Ultra × سبز at 598,000 (stock 7)."""
    return [
        variation_row(1, "iPhone 13 Pro Max", "مشکی"),
        variation_row(2, "iPhone 13 Pro Max", "سفید"),
        variation_row(3, "S24 Ultra", "سبز", stock=7, price="598000"),
    ]


def shop_product(*, extra: list[dict] | None = None, **product_over: object) -> product_match.ShopProduct:
    product = product_match.ShopProduct.from_row(product_row(**product_over))
    product.variations = [product_match.ShopVariation.from_row(row)
                          for row in [*default_variations(), *(extra or [])]]
    return product


class UpdateShop:
    """One product and its variations, behind an ``httpx.MockTransport``."""

    def __init__(self, product: dict | None = None, variations: list[dict] | None = None) -> None:
        self.product: dict = dict(product or product_row())
        self.variations: dict[int, dict] = {
            int(row["id"]): dict(row) for row in (variations if variations is not None
                                                  else default_variations())}
        self.requests: list[tuple[str, str, dict, str]] = []
        self.next_variation = 9100
        self.next_media = 900_000
        # — what the host does wrong —
        self.product_put_status: int | None = None     # e.g. 400: the parent is refused
        self.product_put_ignores: set[str] = set()      # body keys the PUT accepts but does not keep
        self.batch_status: int | None = None            # e.g. 404: no such route
        self.swallow_batch_updates: set[int] = set()    # rows only the *batch* drops (a PUT still works)
        self.swallow_updates: set[int] = set()          # rows every write drops: not applied, not echoed
        self.stale_echo: set[int] = set()               # applied late: the echo still shows the old row
        self.blank_echo: set[int] = set()               # applied, but the echo carries only the id
        self.reject_creates = False                     # create rows come back with an `error` object
        self.create_limit: int | None = None            # a batch makes this many rows; the rest fail
        self.omit_delete_echo = False                   # deleted, but `delete` comes back empty
        self.refuse_delete: set[int] = set()            # rows the shop refuses to delete
        self.media_status = 201
        self.variations_status: int | None = None       # GET variations fails
        self.search_status: int | None = None           # product search / SKU lookup fails (down)
        self.fail_page: int | None = None               # GET variations fails on this page only

    # — assertions' helpers —
    def calls(self, method: str, needle: str = "") -> list[tuple[str, str, dict, str]]:
        return [call for call in self.requests if call[0] == method and needle in call[1]]

    @property
    def methods(self) -> list[str]:
        """``METHOD /path-after-wp-json`` for every request, in order."""
        return [f"{method} {path.split('/wp-json', 1)[-1]}" for method, path, _params, _body in self.requests]

    def body(self, method: str, needle: str, *, nth: int = 0) -> dict:
        found = self.calls(method, needle)
        return json.loads(found[nth][3]) if found and found[nth][3] else {}

    def options(self, name: str) -> list[str]:
        for item in self.product.get("attributes") or []:
            if item.get("name") == name:
                return list(item.get("options") or [])
        return []

    def grid(self) -> set[tuple[str, ...]]:
        """The attribute values of every variation that exists — the thing a seller sees."""
        return {tuple(item["option"] for item in row["attributes"]) for row in self.variations.values()}

    def by_values(self, *values: str) -> dict | None:
        for row in self.variations.values():
            if tuple(item["option"] for item in row["attributes"]) == values:
                return row
        return None

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # — the shop —
    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method.upper()
        params = dict(request.url.params)
        text = request.content.decode("utf-8", "ignore") if request.content else ""
        self.requests.append((method, path, params, text))
        if path.endswith("/media") and method == "POST":
            if self.media_status != 201:
                return httpx.Response(self.media_status, json={"message": "آپلود رسانه رد شد"})
            self.next_media += 1
            return httpx.Response(201, json={"id": self.next_media})
        sku = re.search(r"/products/sku/([^/]+)$", path)
        if sku and method == "GET":
            wanted = sku.group(1).casefold()
            if self.search_status is not None:
                return httpx.Response(self.search_status, json={"message": "down"})
            if str(self.product.get("sku") or "").casefold() == wanted:
                return httpx.Response(200, json=self.product)
            return httpx.Response(404, json={"code": "woocommerce_rest_product_invalid_sku"})
        if path.endswith("/products") and method == "GET":
            if self.search_status is not None:
                return httpx.Response(self.search_status, json={"message": "down"})
            term = str(params.get("search") or "").casefold()
            hay = f"{self.product.get('name')} {self.product.get('sku')}".casefold()
            return httpx.Response(200, json=[self.product] if term and term in hay else [])
        found = re.search(r"/products/(\d+)(/variations(?:/batch|/(\d+))?)?$", path)
        if not found:
            return httpx.Response(200, json=[])
        suffix, single = found.group(2), found.group(3)
        try:
            body = json.loads(text) if text else {}
        except ValueError:
            body = {}
        if suffix is None:
            return self._product(method, body)
        if suffix == "/variations":
            return self._variations(method, params, body)
        if suffix == "/variations/batch":
            return self._batch(body)
        return self._single(method, int(single), body)

    def _product(self, method: str, body: dict) -> httpx.Response:
        if method == "GET":
            return httpx.Response(200, json=self.product)
        if self.product_put_status is not None:
            return httpx.Response(self.product_put_status, json={"message": "نمی‌شود"})
        for key, value in body.items():
            if key in self.product_put_ignores:
                continue
            if key == "images":
                self.product["images"] = [{"id": item["id"]} for item in value]
            else:
                self.product[key] = value
        return httpx.Response(200, json=self.product)

    def _variations(self, method: str, params: dict, body: dict) -> httpx.Response:
        if method == "GET":
            if self.variations_status is not None:
                return httpx.Response(self.variations_status, json={"message": "no"})
            page = int(params.get("page") or 1)
            if self.fail_page == page:
                return httpx.Response(500, json={"message": "page failed"})
            per_page = int(params.get("per_page") or 10)
            rows = [self.variations[key] for key in sorted(self.variations)]
            return httpx.Response(200, json=rows[(page - 1) * per_page: page * per_page])
        if self.batch_status == 400 or self.reject_creates:
            return httpx.Response(400, json={"message": "رد شد"})
        row = self._make(body)
        return httpx.Response(201, json=row)

    def _single(self, method: str, variation_id: int, body: dict) -> httpx.Response:
        row = self.variations.get(variation_id)
        if row is None:
            return httpx.Response(404, json={"message": "Invalid ID"})
        if method == "DELETE":
            if variation_id in self.refuse_delete:
                return httpx.Response(403, json={"message": "نمی‌شود حذف کرد"})
            return httpx.Response(200, json=self.variations.pop(variation_id))
        if variation_id in self.swallow_updates:
            return httpx.Response(200, json={})
        self._merge(row, body)
        return httpx.Response(200, json=row)

    def _batch(self, body: dict) -> httpx.Response:
        if self.batch_status is not None:
            return httpx.Response(self.batch_status, json={"message": "batch not available"})
        creates, updates, deletes = (body.get("create") or [], body.get("update") or [],
                                     body.get("delete") or [])
        if len(creates) + len(updates) + len(deletes) > BATCH_LIMIT:
            return httpx.Response(400, json={"message": "Maximum 100 items per batch"})
        out: dict[str, list] = {"create": [], "update": [], "delete": []}
        for index, item in enumerate(creates):
            if self.reject_creates or (self.create_limit is not None and index >= self.create_limit):
                out["create"].append({"id": 0, "error": {"code": "woocommerce_rest_invalid", "message": "رد شد"}})
            else:
                out["create"].append(self._make(item))
        for item in updates:
            variation_id = int(item["id"])
            row = self.variations.get(variation_id)
            if row is None:
                out["update"].append({"id": variation_id, "error": {"code": "invalid_id", "message": "no"}})
                continue
            if variation_id in self.swallow_updates or variation_id in self.swallow_batch_updates:
                continue
            before = dict(row)
            self._merge(row, {key: value for key, value in item.items() if key != "id"})
            if variation_id in self.stale_echo:
                out["update"].append(before)
            elif variation_id in self.blank_echo:
                out["update"].append({"id": variation_id})
            else:
                out["update"].append(dict(row))
        for variation_id in deletes:
            if int(variation_id) in self.variations and int(variation_id) not in self.refuse_delete:
                removed = self.variations.pop(int(variation_id))
                if not self.omit_delete_echo:
                    out["delete"].append(removed)
            elif int(variation_id) in self.refuse_delete:
                out["delete"].append({"id": int(variation_id), "error": {"code": "cannot_delete", "message": "no"}})
        return httpx.Response(200, json=out)

    def _make(self, payload: dict) -> dict:
        self.next_variation += 1
        row: dict[str, Any] = {
            "id": self.next_variation, "type": "variation", "status": payload.get("status", "publish"),
            "attributes": [{"id": 0, "name": item["name"], "option": item["option"]}
                           for item in payload.get("attributes") or []],
            "regular_price": payload.get("regular_price", ""), "sale_price": payload.get("sale_price", ""),
            "manage_stock": bool(payload.get("manage_stock", False)),
            "stock_quantity": payload.get("stock_quantity"),
            "stock_status": payload.get("stock_status", "instock"),
            "image": payload.get("image") or {},
            "menu_order": payload.get("menu_order", 0),
        }
        row["price"] = row["sale_price"] or row["regular_price"]
        self.variations[row["id"]] = row
        return dict(row)

    @staticmethod
    def _merge(row: dict, body: dict) -> None:
        row.update({key: value for key, value in body.items() if key != "id"})
        row["price"] = row.get("sale_price") or row.get("regular_price")
