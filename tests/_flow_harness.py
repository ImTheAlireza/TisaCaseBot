"""Shared harness for the tests that drive the product flow without Telegram.

Both ``test_dry_run.py`` and ``test_idempotency.py`` need the same three things: a
``Settings`` object swapped into every module that imported it, a stub
``callback_query`` shaped the way PTB hands one to a handler, and an isolated
publish ledger. Keeping that in one place matters for the same reason it matters in
``bot/``: two harnesses drift, and then a test passes because the second harness is
more forgiving than the first.

Import it as ``_flow_harness`` (the tests dir is on ``sys.path`` for both pytest and
``unittest discover -s tests``).
"""

from __future__ import annotations

import json as _json
import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from bot.config import Settings
from bot.services import jsonstore, products_ledger

os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("SUDO_IDS", "1234567")


def settings_with(**over: object) -> Settings:
    """A configured-looking shop: credentials complete, URLs unroutable."""
    base: dict[str, object] = {
        "bot_token": "123456:TEST",
        "woocommerce_url": "https://shop.example",
        "woocommerce_key": "ck_test",
        "woocommerce_secret": "cs_test",
        "wordpress_url": "https://shop.example",
        "wordpress_username": "admin",
        "wordpress_app_password": "aaaa bbbb",
    }
    base.update(over)
    return Settings(**base)  # type: ignore[arg-type]


class TransportScript:
    """transport ساختگی که همان جواب‌های از پیش نوشته را می‌دهد و درخواست‌ها را می‌شمارد.

    وقتی اسکریپت تمام شود عمداً به آخرین پاسخِ داده‌شده برنمی‌گردد مگر `default` را بدهی:
    «تست انتظار داشت client بیشتر تلاش نکند» خودش نتیجهٔ تست است، نه چیزی که بی‌صدا
    تکرار شود. یک نسخه در `test_dry_run` و یکی در `test_woo_client` یعنی دو درایورِ هم‌خانواده
    که می‌واگرا‌ند؛ پس اینجا است.
    """

    def __init__(self, *steps: Any, default: tuple[int, Any] | None = None) -> None:
        self.steps = list(steps)
        self.default = default
        self.requests: list[Any] = []

    @property
    def sends(self) -> int:
        return len(self.requests)

    @property
    def methods(self) -> list[str]:
        return [f"{request.method} {request.url.path}" for request in self.requests]

    def transport(self):
        import httpx

        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if not self.steps:
                if self.default is None:
                    raise AssertionError("اسکریپت تمام شد؛ client بیشتر از انتظار تلاش کرد")
                status, body = self.default
                return httpx.Response(status) if body is None else httpx.Response(status, json=body)
            step = self.steps.pop(0)
            if isinstance(step, Exception):
                raise step
            return step

        return httpx.MockTransport(handle)


class FakeStore:
    """ووکامرس ساختگی: هم مسیر موفق، هم حالت‌های بدِ سایت.

    برخلاف transport حالت آزمایشی (که «همه‌چیز خوب پیش رفت» را جواب می‌دهد) این یکی
    محصول نیمه‌ساختهٔ قبلی را یادش می‌آید، endpointی را که سایت ندارد 405 می‌دهد، و
    آپلود تصویر را رد می‌کند. یک نسخه‌اش در `_flow_harness` است چون دو فایل تست همین
    درایور را لازم دارند: `test_idempotency` (تلاش دوباره) و `test_stock_and_sale`
    (بدنهٔ درخواست‌ها) — دو درایور هم‌خانواده یعنی دو رفتار متفاوتِ کدِ یکسان.
    """

    def __init__(
        self,
        *,
        products: list[dict] | None = None,
        variations: list[dict] | None = None,
        search_status: int = 200,
        variations_status: int = 200,
        create_status: int = 201,
        batch_status: int = 201,
        variation_create_status: int = 201,
        media_status: int = 201,
    ) -> None:
        self.products = products or []
        self.variations = variations or []
        self.search_status = search_status
        self.variations_status = variations_status
        self.create_status = create_status
        self.batch_status = batch_status
        self.variation_create_status = variation_create_status
        self.media_status = media_status
        self.requests: list[tuple[str, str, dict, str]] = []
        self.media_id = 900_000

    # — کمکی‌های تست —
    def count(self, method: str, needle: str) -> int:
        return sum(1 for m, path, _params, _body in self.requests if m == method and needle in path)

    def count_created_products(self) -> int:
        """چند «POST /products» واقعاً زده شد (بچِ واریژن هم /products دارد؛ شمرده نمی‌شود)."""
        return sum(1 for m, path, _p, _b in self.requests if m == "POST" and path.endswith("/products"))

    def body(self, method: str, needle: str) -> str:
        for m, path, _params, body in self.requests:
            if m == method and needle in path:
                return body
        return ""

    def last_body(self, method: str, needle: str) -> str:
        for m, path, _params, body in reversed(self.requests):
            if m == method and needle in path:
                return body
        return ""

    @property
    def product_searches(self) -> list[dict]:
        return [
            params
            for method, path, params, _b in self.requests
            if method == "GET" and path.endswith("/products")
        ]

    def product_create_body(self) -> dict:
        """بدنهٔ «POST …/products» — مسیرِ دقیق، نه زیررشته.

        ``last_body("POST", "/products")`` بچِ واریژن را هم می‌گیرد (مسیرش هم با
        ``/products`` تمام می‌شود و بعد از محصول است): دو چیز متفاوت به‌عنوان یکی خوانده
        می‌شد. مثل ``count_created_products`` که دقیقاً برای همین هست.
        """
        for m, path, _params, body in reversed(self.requests):
            if m == "POST" and path.endswith("/products"):
                return _json.loads(body) if body else {}
        return {}

    def variation_items(self) -> list[dict]:
        """هر چه در بچِ واریژن فرستاده شد (تعداد و مقادیرش همین‌جا خوانده می‌شود)."""
        try:
            return _json.loads(self.last_body("POST", "variations/batch")).get("create") or []
        except ValueError:
            return []

    def handle(self, request) -> Any:
        import httpx

        path = request.url.path
        method = request.method.upper()
        params = dict(request.url.params)
        body = request.content.decode("utf-8", "ignore") if request.content else ""
        self.requests.append((method, path, params, body))
        if path.endswith("/tisa-health"):
            return httpx.Response(200, json={"contract": 1, "batch_fencing": True, "variation_fencing": True, "parent_cas": True, "stock_cas": True})
        if method == "GET" and path.endswith("/variations"):
            if self.variations_status != 200:
                return httpx.Response(self.variations_status, json={"code": "rest_invalid_param"})
            page, per_page = int(params.get("page", 1)), int(params.get("per_page", 100))
            return httpx.Response(200, json=self.variations[(page-1)*per_page:page*per_page])
        if method == "GET" and path.endswith("/products"):
            # Some hosts reject ``status=any`` on the products route (HTTP 400) but accept
            # the same query without it — the fallback only makes sense if the fake
            # blames the parameter, not the request as a whole.
            if self.search_status != 200 and "status" in params:
                return httpx.Response(self.search_status, json={"code": "rest_invalid_param"})
            page, per_page = int(params.get("page", 1)), int(params.get("per_page", 100))
            return httpx.Response(200, json=self.products[(page-1)*per_page:page*per_page])
        if method == "POST" and path.endswith("/media"):
            if self.media_status != 201:
                return httpx.Response(self.media_status, json={"message": "آپلود رسانه رد شد"})
            self.media_id += 1
            return httpx.Response(201, json={"id": self.media_id})
        if method == "POST" and path.endswith("/products"):
            if self.create_status != 201:
                return httpx.Response(self.create_status, json={"message": "خطای ساخت محصول"})
            row = (_json.loads(body) if body else {}) | {"id": 4321 + len(self.products)}
            self.products.append(row)
            return httpx.Response(201, json=row)
        if method == "POST" and path.endswith("/variations"):
            if self.variation_create_status != 201:
                return httpx.Response(self.variation_create_status, json={"message": "variation failed"})
            row = (_json.loads(body) if body else {}) | {"id": 9500 + len(self.variations)}
            self.variations.append(row)
            return httpx.Response(201, json=row)
        if method == "POST" and path.endswith("/variations/batch"):
            if self.batch_status != 201:
                return httpx.Response(self.batch_status, json={"message": "batch failed"})
            try:
                wanted = _json.loads(body).get("create") or []
            except ValueError:
                wanted = []
            rows = [dict(row) | {"id": 9000 + len(self.variations) + i} for i, row in enumerate(wanted)]
            self.variations.extend(rows)
            return httpx.Response(201, json={"create": rows})
        if method == "GET" and "/variations/" in path:
            identity = int(path.rsplit("/", 1)[-1])
            row = next((row for row in self.variations if int(row.get("id") or 0) == identity), None)
            return httpx.Response(200 if row else 404, json=row or {"code": "not_found"})
        if method == "GET" and "/products/" in path and path.rsplit("/", 1)[-1].isdigit():
            identity = int(path.rsplit("/", 1)[-1])
            row = next((row for row in self.products if int(row.get("id") or 0) == identity), None)
            return httpx.Response(200 if row else 404, json=row or {"code": "not_found"})
        if path.endswith("/categories"):
            return httpx.Response(200, json=[])
        if method in ("DELETE", "PUT", "PATCH"):
            return httpx.Response(200, json={"deleted": True})
        return httpx.Response(200, json={})

    @property
    def transport(self):
        import httpx

        return httpx.MockTransport(self.handle)


def respond(status: int, body: Any = None):
    """یک `httpx.Response` برای `TransportScript`."""
    import httpx

    return httpx.Response(status) if body is None else httpx.Response(status, json=body)


@contextmanager
def patched_settings(value: Settings):
    """Swap ``settings`` into every bot module that imported it.

    ``bot.config.settings`` is a singleton and each module bound the name itself, so
    patching only ``bot.config`` changes nothing where it is read. This is not
    hypothetical: the Ping gate silently did nothing until a test caught it.
    """
    import sys

    import bot.config as config

    saved: list[tuple[object, str, object]] = []
    modules = [config, *(m for name, m in list(sys.modules.items()) if name.startswith("bot.") and m)]
    for module in modules:
        current = getattr(module, "settings", None)
        if isinstance(current, Settings):
            saved.append((module, "settings", current))
            module.settings = value
    try:
        assert saved, "patched_settings: هیچ ماژولی عوض نشد"
        yield value
    finally:
        for module, name, old in reversed(saved):
            setattr(module, name, old)


@contextmanager
def temp_ledger():
    """An empty publish history, SKU cache and send-queue, for one reason.

    All three are things the bot *reads before acting* (the duplicate gate, the SKU
    high-water mark, the outbox it drains on boot). A suite that writes into the repo's real
    copies can therefore block a publish, hand out a taken SKU, or publish a queued product on
    the next real start — which the first two did. `unittest discover -s tests` has no
    conftest to redirect ``TISA_DATA_DIR``, so the isolation has to live here.
    """
    from bot import rbac
    from bot.services import outbox, sku, tracking_ledger

    tmp = Path(tempfile.mkdtemp(prefix="tisa-test-ledger-"))
    old = (products_ledger.FILE, sku.STATE_FILE, outbox.DB_PATH, outbox.FILES_DIR, tracking_ledger.FILE)
    products_ledger.FILE = tmp / "recent_products.json"
    sku.STATE_FILE = tmp / "sku_state.json"
    outbox.DB_PATH = tmp / "outbox.sqlite3"
    outbox.FILES_DIR = tmp / "outbox_files"
    tracking_ledger.FILE = tmp / "tracking_ledger.json"
    old_roles = rbac.ROLES_FILE
    rbac.ROLES_FILE = tmp / "roles.json"
    jsonstore.checked_write(rbac.ROLES_FILE, {"admins": {"7": {"name": "Test admin"}}})
    jsonstore.invalidate()
    try:
        yield products_ledger
    finally:
        (products_ledger.FILE, sku.STATE_FILE, outbox.DB_PATH, outbox.FILES_DIR, tracking_ledger.FILE) = old
        rbac.ROLES_FILE = old_roles
        jsonstore.invalidate()
        shutil.rmtree(tmp, ignore_errors=True)


@contextmanager
def no_sleep() -> list[float]:
    """خوابِ backoff را در `woo_client` صفر می‌کند و لیست تأخیرها را برمی‌گرداند.

    سیاست retry باید در تست دیده شود، نه ثانیه‌هایش: بدون این، تستِ «با حالت خاموش
    واقعاً سوکت باز می‌شود» سه بار پشت سر هم یک‌ثانیه و دوثانیه خوابید و سوئیت از
    ۱٫۸s رفت ۵s. همان دلیلی که `WooClient` یک `transport` می‌گیرد: چیزی که فقط برای
    آزمودن بیرون آمده نباید رفتاری داشته باشد.
    """
    from bot.services import woo_client

    delays: list[float] = []

    async def fake(delay: float) -> None:
        delays.append(delay)

    saved = woo_client._sleep
    woo_client._sleep = fake
    try:
        yield delays
    finally:
        woo_client._sleep = saved


def conversation_patterns() -> set[str]:
    """Patterns the bot's *conversations* answer — the only ones a click can reach.

    A callback button whose handler is not inside a ``ConversationHandler`` looks alive and is
    not: the click lands, the flow never sees the next message. Both phases 3 and 5 were caught
    by exactly this, so the check lives here and every phase re-uses it.
    """
    from telegram.ext import ConversationHandler

    from bot.app import build_application

    app = build_application()
    found: set[str] = set()
    for handlers in app.handlers.values():
        for handler in handlers:
            if not isinstance(handler, ConversationHandler):
                continue
            for group in list(handler.states.values()) + [handler.entry_points, handler.fallbacks]:
                for sub in group:
                    # PTB keeps the compiled regex on the handler, not on .callback (that is ours).
                    pattern = getattr(sub, "pattern", None)
                    if pattern:
                        found.add(str(pattern))
    return found


@contextmanager
def temp_metrics():
    """شمارنده‌ها در یک دیتابیس موقت — نه فایل واقعیِ کنارِ صفِ ارسال.

    صفحهٔ وضعیت این فایل را *می‌خواند*؛ تستی که شمارنده بنویسد و تمیز نکند، عددِ
    تست بعدی را عوض می‌کند (و «۴۰ دکمهٔ بدون دسترسی» ساخته می‌شود که هیچ‌کس نزده).
    """
    from bot.services import metrics

    tmp = Path(tempfile.mkdtemp(prefix="tisa-metrics-"))
    old = metrics.DB_PATH
    metrics.DB_PATH = tmp / "metrics.sqlite3"
    try:
        yield metrics
    finally:
        metrics.DB_PATH = old
        shutil.rmtree(tmp, ignore_errors=True)


def all_handler_patterns() -> list[str]:
    """هر الگویی که ربات واقعاً ثبت کرده — مکالمه‌ها، دکمه‌ها و دستورهای اسلش.

    ``conversation_patterns`` فقط مکالمه‌ها را می‌بیند؛ برای دکمه‌های بیرون از مکالمه
    (مثل «📊 وضعیت») باید درختِ کاملِ هندلرها گشته شود، وگرنه هر چیزی که مکالمه نیست
    «ثبت‌نشده» به‌نظر می‌رسد.
    """
    from telegram.ext import ConversationHandler

    from bot.app import build_application

    found: list[str] = []

    def walk(handler) -> None:
        if isinstance(handler, ConversationHandler):
            groups: list[object] = [handler.entry_points, handler.fallbacks, *handler.states.values()]
            for group in groups:
                items = group if isinstance(group, (list, tuple)) else [group]
                for sub in items:
                    if isinstance(sub, (list, tuple)):
                        for inner in sub:
                            walk(inner)
                    else:
                        walk(sub)
            return
        pattern = getattr(handler, "pattern", None)
        if pattern is not None:
            found.append(str(pattern))
        for command in getattr(handler, "commands", None) or ():
            found.append(f"cmd:{command}")

    for handlers in build_application().handlers.values():
        for handler in handlers:
            walk(handler)
    return found


class FakeChat:
    """Anything the flow can reply to: records the text, and answers like Telegram does.

    A reply is itself a FakeChat, because the flows edit their own «🔎 دنبال می‌گردم…» note
    afterwards — a stub that returned None would hide half the bug surface.
    """

    def __init__(
        self,
        sink: list,
        *,
        kind: str = "text",
        chat_id: int = 9,
        thread_id: int | None = None,
        text: str | None = None,
        reply_markup: object = None,
    ) -> None:
        self.sink = sink
        self.kind = kind
        self.chat_id = chat_id
        self.message_thread_id = thread_id
        self.text = text
        self.message_id = 5
        self.reply_markup = reply_markup
        self.edits = 0

    async def reply_html(self, text: str | None = None, **kwargs: object) -> FakeChat:
        # Recorded like reply_text, with the parse mode the real PTB would add:
        # a screen that answers by *replying* instead of editing has to be testable.
        kwargs.setdefault("parse_mode", "HTML")
        return await self.reply_text(text, **kwargs)

    async def reply_text(self, text: str | None = None, **kwargs: object) -> FakeChat:
        self.sink.append((self.kind, text, kwargs))
        return FakeChat(
            self.sink,
            chat_id=self.chat_id,
            thread_id=self.message_thread_id,
            text=str(text or ""),
            reply_markup=kwargs.get("reply_markup"),
        )

    async def edit_text(self, text: str | None = None, **kwargs: object) -> FakeChat:
        self.edits += 1
        self.reply_markup = kwargs.get("reply_markup")
        self.sink.append(("edit", text, kwargs))
        return self

    async def edit_message_text(self, text: str | None = None, **kwargs: object) -> FakeChat:
        return await self.edit_text(text, **kwargs)

    async def delete(self) -> None:
        self.sink.append(("delete", None, {}))

    async def answer(self, text: str | None = None, **kwargs: object) -> None:
        self.sink.append(("answer", text, kwargs))

    async def reply_document(self, document=None, **kwargs: object) -> FakeChat:
        # The bytes are recorded next to the filename: for the tracking flow the
        # attachment *is* the answer, so a test that cannot read it proves nothing.
        payload = document.getvalue() if hasattr(document, "getvalue") else document
        self.sink.append(("document", kwargs.get("filename"), {**kwargs, "bytes": payload}))
        return FakeChat(self.sink, kind="document", chat_id=self.chat_id, thread_id=self.message_thread_id)

    def documents(self) -> list[tuple[str, bytes]]:
        """(filename, bytes) of everything this chat was sent, in order."""
        return [(str(txt), dict(kw)["bytes"]) for kind, txt, kw in self.sink if kind == "document"]

    def keyboard_rows(self) -> list[list]:
        """Rows of the keyboard this chat last showed.

        A flow answers with a *reply*, so the most recent keyboard in the shared sink
        counts too — otherwise a question that was clearly asked reads as no buttons.
        """
        rows = list(getattr(self.reply_markup, "inline_keyboard", []) or [])
        if rows:
            return rows
        for _kind, _text, kwargs in reversed(self.sink):
            markup = kwargs.get("reply_markup") if isinstance(kwargs, dict) else None
            rows = list(getattr(markup, "inline_keyboard", []) or [])
            if rows:
                return rows
        return []


def message_update(text: str = "", *, user_id: int = 7, chat_id: int = 9, thread_id: int | None = None):
    """An update shaped like a plain message, with every reply recorded."""
    sent: list = []
    message = FakeChat(sent, chat_id=chat_id, thread_id=thread_id, text=text)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, username="t", first_name="t"),
        effective_message=message,
        callback_query=None,
    )
    return update, sent


def document_update(
    file_name: str,
    source: Path,
    *,
    user_id: int = 7,
    chat_id: int = 9,
    thread_id: int | None = None,
    file_size: int | None = None,
):
    """An update shaped like a Telegram Document whose download lands on disk.

    ``source`` is a real file: the tracking flow reads what it downloaded, so a stub
    would test the plumbing and not the parsing. ``file_size`` overrides what Telegram
    reports — the size guard fires *before* the download, and that is testable.
    """
    update, sent = message_update("", user_id=user_id, chat_id=chat_id, thread_id=thread_id)

    async def get_file() -> SimpleNamespace:
        async def download_to_drive(custom_path: str, **_kwargs: object) -> Path:
            shutil.copyfile(str(source), str(custom_path))
            return Path(str(custom_path))

        reported = source.stat().st_size if file_size is None else file_size
        return SimpleNamespace(file_size=reported, download_to_drive=download_to_drive)

    update.effective_message.document = SimpleNamespace(
        file_name=file_name,
        file_unique_id=f"u{abs(hash(str(source) + file_name)) % 10**8}",
        get_file=get_file,
    )
    return update, sent


def query_update(data: str, *, user_id: int = 7, chat_id: int = 9, thread_id: int | None = None, signed: bool = True):
    """An update shaped like PTB's, with every reply recorded on the query."""
    if signed and "|" not in data:
        from bot.modules import product_flow, restock_flow
        session = product_flow.sessions.get(user_id) or restock_flow.sessions.get(user_id)
        if session is not None and (data.startswith(("product:", "restock:")) or data == "nav:main"):
            data = f"{data}|{session.nonce}.{session.revision:x}"
    seen: list[tuple[str, object]] = []

    async def answer(text=None, **kwargs):
        # The same shape as FakeChat.answer (kind, text, kwargs): a toast and an
        # alert look identical unless the kwargs are recorded, and «which one did
        # the owner get?» is a real question in these tests.
        seen.append(("answer", text, kwargs))

    message = FakeChat(seen, chat_id=chat_id, thread_id=thread_id)
    query = SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id),
        message=message,
        answer=answer,
        edit_message_text=message.edit_message_text,
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=user_id, username="t", first_name="t"),
        effective_message=message,
    )
    return update, seen


class FakeBot:
    """Records every proactive send (message or document) with its routing kwargs."""

    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []
        self.documents: list[dict[str, object]] = []

    async def send_message(self, *args: object, text: str = "", **kwargs: object) -> SimpleNamespace:
        self.messages.append({"text": text, **kwargs})
        return SimpleNamespace(message_id=1)

    async def send_document(self, *args: object, **kwargs: object) -> SimpleNamespace:
        self.documents.append(dict(kwargs))
        return SimpleNamespace(message_id=2)

    async def edit_message_text(self, *args: object, **kwargs: object) -> None:
        return None


def context(bot: FakeBot | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        bot=bot or FakeBot(),
        user=SimpleNamespace(id=7),
        chat_data={},
        job_queue=SimpleNamespace(run_once=lambda *a, **k: None),
        error=lambda *a, **k: None,
    )


def write_image(directory: Path, name: str = "01_one.jpg") -> Path:
    path = directory / name
    path.write_bytes(b"z" * 64)
    return path
