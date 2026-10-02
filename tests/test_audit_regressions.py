"""Corrected, isolated regressions for the 2026-10-02 audit (never live-site tests)."""
from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import multiprocessing
import os
import sqlite3
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("SUDO_IDS", "1234567")
try:
    import httpx
    import pytest
    from openpyxl import load_workbook
    from PIL import Image
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from _flow_harness import context as fake_context, patched_settings, query_update, settings_with, temp_ledger
    from bot import rbac
    from bot.modules import outbox_flow, product_flow as PF, product_tools
    from bot.services import (
        airpods_parser, barcodes, callbacks, color_matrix, jsonstore, learning_corpus,
        outbox, plan, processor, products_ledger, publish_batch, update_plan, woo_contract, woo_fencing,
        worker, workspace,
    )
    from bot.services.product_extractor import ProductData
    from bot.services.product_match import ShopProduct, ShopVariation
    from bot.services.woo_client import WooClient, WooCommerceAPIError, check
    from bot.utils.logging import _RedactingFormatter
except ImportError as exc:
    raise unittest.SkipTest("Audit regressions require the installed runtime/dev dependencies") from exc


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    with temp_ledger(), patched_settings(settings_with()):
        monkeypatch.setattr(learning_corpus, "CORPUS_FILE", tmp_path / "corpus.json")
        PF.sessions.clear()
        PF.album_buffers.clear()
        PF.album_tasks.clear()
        yield tmp_path
        PF.sessions.clear()
        PF.album_buffers.clear()
        PF.album_tasks.clear()


@pytest.fixture
def photo(isolated):
    path = isolated / "01_مشکی.png"
    Image.new("RGB", (20, 20), "black").save(path)
    return path


def draft():
    return {"title": "قاب آزمایش", "price": 698000, "sku_prefix": "LP", "models": ["iPhone 13", "iPhone 14"],
            "attributes": {"رنگ": ["مشکی", "سفید"]}, "stock": 20}


def enqueue(photo, **over):
    args = {"batch_id": "a" * 24, "chat_id": 7, "user_id": 7, "payload": draft(), "images": [photo], "delay": 0}
    args.update(over)
    assert outbox.enqueue(**args)
    return outbox.due()[0]


def product():
    parent = ShopProduct.from_row({"id": 42, "name": "قاب", "type": "variable", "status": "draft",
                                  "attributes": [{"name": "مدل", "variation": True, "visible": True,
                                                  "options": ["iPhone 13", "iPhone 14"]}]})
    parent.variations = [ShopVariation.from_row({"id": 80 + index, "sku": f"VENDOR-{index}", "weight": "0.1",
                         "tax_class": "reduced-rate", "backorders": "yes", "description": "vendor detail",
                         "regular_price": "600000", "status": "publish", "manage_stock": True,
                         "stock_quantity": 5, "stock_status": "instock",
                         "attributes": [{"name": "مدل", "option": label}]})
                         for index, label in enumerate(["iPhone 13", "iPhone 14"])]
    return parent


@pytest.mark.parametrize("identity", [0, -1, None, True, False, "nan", "1.5", {}, []])
def test_A01_invalid_ids_never_acknowledge_writes(identity):
    assert woo_contract.field_errors({"regular_price": "600000"}, {"id": identity, "regular_price": "600000"})


@pytest.mark.parametrize("field,value", [("regular_price", "nan"), ("regular_price", "Infinity"),
                                        ("stock_quantity", None), ("stock_quantity", 5), ("status", "private")])
def test_A01_semantic_or_numeric_mismatch_is_not_success(field, value):
    requested = {"regular_price": "600000", "stock_quantity": 20, "status": "publish"}
    echoed = {"id": 9, **requested, field: value}
    assert woo_contract.field_errors(requested, echoed, variation=True)


def test_A08_no_supported_native_contract_means_no_external_write(isolated):
    seen = []
    def handle(request):
        seen.append(request.method)
        return httpx.Response(404, json={"code": "rest_no_route"})
    async def run():
        async with WooClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(WooCommerceAPIError) as exc:
                await woo_fencing.require(client, "https://example.invalid/wp-json/wc/v3/products")
            assert exc.value.status_code == 412
    asyncio.run(run())
    assert seen == ["GET"]


def test_A08_variation_conflict_requires_matching_readback(isolated):
    wanted = {"regular_price": "600000", "stock_quantity": 20, "status": "publish", "attributes": [{"name": "مدل", "option": "iPhone 13"}]}
    conflict = {"error": {"code": "tisa_variation_exists", "data": {"existing_variation_id": 91}}}
    async def run():
        async with WooClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"id": 91, **wanted}))) as client:
            assert (await woo_fencing.recover_variation(client, "https://example.invalid/variations", wanted, conflict))["id"] == 91
        async with WooClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"id": 91, **wanted, "stock_quantity": 19}))) as client:
            with pytest.raises(WooCommerceAPIError) as exc:
                await woo_fencing.recover_variation(client, "https://example.invalid/variations", wanted, conflict)
            assert exc.value.status_code == 409
    asyncio.run(run())


def test_A10_hash_is_ordered_and_semantic_not_mtime_or_arbitrary_name(photo, isolated):
    other = isolated / "02_سفید.png"
    other.write_bytes(photo.read_bytes())
    body = draft()
    initial = publish_batch.batch_id(body, [photo, other], chat_id=7)
    os.utime(photo, (1, 1))
    assert initial == publish_batch.batch_id(body, [photo, other], chat_id=7)
    assert initial != publish_batch.batch_id(body, [other, photo], chat_id=7)
    plain = isolated / "random.png"
    plain.write_bytes(photo.read_bytes())
    renamed = isolated / "another.png"
    renamed.write_bytes(photo.read_bytes())
    assert publish_batch.batch_id(body, [plain]) == publish_batch.batch_id(body, [renamed])
    assert publish_batch.batch_id(body, [plain]) != publish_batch.batch_id(body, [photo])


def test_A10_missing_images_do_not_have_an_identity(isolated):
    with pytest.raises(FileNotFoundError):
        publish_batch.batch_id(draft(), [isolated / "missing.png"])


def test_A11_enqueue_is_all_or_nothing_and_private(photo, isolated):
    assert not outbox.enqueue(batch_id="b" * 24, user_id=7, chat_id=7, payload=draft(), images=[photo, isolated / "missing.png"])
    assert outbox.pending() == 0
    row = enqueue(photo)
    assert len(row.images) == 1 and row.images[0].read_bytes() == photo.read_bytes()
    assert row.images[0].stat().st_mode & 0o777 == 0o600
    assert row.images[0].parent.stat().st_mode & 0o777 == 0o700
    assert outbox.DB_PATH.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("problem", ["missing", "expired", "attempts", "revoked", "invalid"])
def test_A11_A12_A44_preflight_rejects_without_calling_the_store(photo, isolated, monkeypatch, problem):
    row = enqueue(photo, user_id=404 if problem == "revoked" else 7,
                  payload=draft() | ({"sku_prefix": ""} if problem == "invalid" else {}))
    if problem == "missing":
        row.images[0].unlink()
    elif problem in ("expired", "attempts"):
        with outbox._db() as conn:
            conn.execute("UPDATE outbox SET created_at = ?, attempts = ?", (time.time()-90000 if problem == "expired" else time.time(), 8 if problem == "attempts" else 1))
    publisher = AsyncMock(return_value=(91, "https://example.invalid/edit"))
    monkeypatch.setattr(outbox_flow, "create_draft", publisher)
    app = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
    assert asyncio.run(outbox_flow.drain_once(app)) == 1
    publisher.assert_not_awaited()
    assert outbox.pending() == 0


def test_A13_sql_ack_failure_does_not_delete_spooled_files(photo, isolated, monkeypatch):
    row = enqueue(photo)
    @contextlib.contextmanager
    def broken():
        raise sqlite3.OperationalError("disk full")
        yield  # pragma: no cover
    monkeypatch.setattr(outbox, "_db", broken)
    with pytest.raises(sqlite3.OperationalError):
        outbox.succeed(row.batch_id, expected_generation=row.generation)
    assert row.images[0].is_file()


def test_A13_generation_not_a_timestamp_protects_refresh(photo, isolated):
    moment = time.time()
    old = enqueue(photo, now=moment)
    fresh = enqueue(photo, now=moment)
    assert old.updated_at == fresh.updated_at and old.generation != fresh.generation
    with pytest.raises(RuntimeError):
        outbox.succeed(old.batch_id, expected_updated_at=old.updated_at, expected_generation=old.generation)
    assert fresh.images[0].is_file() and outbox.pending() == 1


def test_A13_overlapping_workers_claim_a_row_once(photo, isolated):
    enqueue(photo)
    claimed = outbox.claim_due()
    assert claimed is not None and claimed.claim_token
    assert outbox.claim_due() is None
    assert not outbox.enqueue(batch_id=claimed.batch_id, user_id=7, chat_id=7, payload=draft(), images=[photo])
    assert outbox.renew_claim(claimed)
    outbox.release_claim(claimed)
    assert outbox.claim_due() is not None


def test_A13_success_history_failure_keeps_pending_bytes(photo, isolated, monkeypatch):
    row = enqueue(photo)
    monkeypatch.setattr(outbox_flow, "create_draft", AsyncMock(return_value=(91, "https://example.invalid/edit")))
    def broken(*args, **kwargs):
        raise jsonstore.StateWriteError("disk full")
    monkeypatch.setattr(outbox_flow, "_finish_card", broken)
    app = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
    asyncio.run(outbox_flow.drain_once(app))
    assert outbox.pending() == 1 and row.images[0].is_file()
    app.bot.send_message.assert_not_awaited()


def test_A14_missing_primary_can_recover_backup_and_cache_values_are_independent(isolated):
    path = isolated / "state.json"
    assert jsonstore.write_json(path, {"data": [1]})
    assert jsonstore.write_json(path, {"data": [2]})
    copied = jsonstore.read_json(path)
    copied["data"].append(3)
    assert jsonstore.read_json(path) == {"data": [2]}
    path.unlink()
    assert jsonstore.read_json(path) == {"data": [1]}


def test_A14_security_state_never_regrants_from_an_old_backup(isolated):
    assert rbac.is_admin(7)
    assert jsonstore.write_json(rbac.ROLES_FILE, {"admins": {}})
    rbac.ROLES_FILE.write_text("broken", encoding="utf-8")
    assert not rbac.is_admin(7)


def test_A15_checked_writer_failure_is_not_a_transient_publish_error(isolated, monkeypatch):
    with pytest.raises(jsonstore.StateWriteError):
        jsonstore.checked_write(isolated / "state.json", {}, writer=lambda *args: False)
    assert not outbox.is_transient(jsonstore.StateWriteError("disk full"))


def test_A20_exception_tracebacks_are_redacted():
    stream = io.StringIO()
    logger = logging.Logger("audit-isolated")
    handler = logging.StreamHandler(stream)
    handler.setFormatter(_RedactingFormatter("%(message)s"))
    logger.addHandler(handler)
    try:
        raise RuntimeError("consumer_secret=cs_DO_NOT_SHOW token=123456:FAKE_TOKEN")
    except RuntimeError:
        logger.exception("failed")
    assert "cs_DO_NOT_SHOW" not in stream.getvalue()
    assert "123456:FAKE_TOKEN" not in stream.getvalue()


@pytest.mark.parametrize("stale", ["session", "revision"])
def test_A22_old_session_or_revision_cannot_publish(photo, isolated, monkeypatch, stale):
    current = PF.ProductSession(data=ProductData(**draft()), files=[photo], user_id=7, proof_required=True)
    PF.sessions[7] = current
    update, replies = query_update("product:confirm")
    if stale == "session":
        PF.sessions[7] = PF.ProductSession(data=ProductData(**draft()), files=[photo], user_id=7, proof_required=True)
    else:
        current.revision += 1
    publisher = AsyncMock(return_value=(91, "https://example.invalid/edit"))
    monkeypatch.setattr(PF, "create_draft", publisher)
    asyncio.run(PF.confirm(update, fake_context()))
    publisher.assert_not_awaited()
    assert any("پیش‌نمایش قبلی" in str(item[1]) for item in replies)


def test_A50_pending_album_is_not_discarded_by_confirmation(photo, isolated, monkeypatch):
    session = PF.ProductSession(data=ProductData(**draft()), files=[photo], user_id=7)
    PF.sessions[7] = session
    PF.album_buffers[(7, "album")] = [object()]
    publisher = AsyncMock()
    monkeypatch.setattr(PF, "create_draft", publisher)
    update, replies = query_update("product:confirm")
    asyncio.run(PF.confirm(update, fake_context()))
    publisher.assert_not_awaited()
    assert PF.sessions[7] is session and PF.album_buffers
    assert any("آلبوم" in str(item) for item in replies)


def test_A22_all_keyboard_callbacks_are_revision_bound_and_fit_telegram():
    original = InlineKeyboardMarkup([[InlineKeyboardButton("تأیید", callback_data="product:confirm")]])
    bound = callbacks.bind(original, "a" * 16, 100)
    data = bound.inline_keyboard[0][0].callback_data
    assert callbacks.split(data) == ("product:confirm", "a" * 16, 100)
    assert len(data.encode()) <= 64
    handler = callbacks.SessionCallbackHandler(lambda: None, pattern="^product:confirm$")
    assert handler.pattern.match(data)


def test_A24_batches_of_one_draft_are_serialized(isolated, monkeypatch):
    session = PF.ProductSession(user_id=7)
    PF.sessions[7] = session
    active = maximum = finished = 0
    async def prepare(*args):
        nonlocal active, maximum, finished
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        finished += 1
        active -= 1
    monkeypatch.setattr(PF, "_prepare_files_impl", prepare)
    async def run():
        await asyncio.gather(PF._prepare_files(7, [], fake_context()), PF._prepare_files(7, [], fake_context()))
    asyncio.run(run())
    assert maximum == 1 and finished == 2 and not session.processing_media


def test_A24_text_changed_during_extraction_is_reprocessed(isolated, monkeypatch):
    session = PF.ProductSession(info_text="old")
    values = []
    async def extract(current, **kwargs):
        captured = current.info_text
        values.append(captured)
        if captured == "old":
            current.info_text = "new"
        return ProductData(title=captured)
    monkeypatch.setattr(PF, "_extract_once", extract)
    assert asyncio.run(PF._extract(session)).title == "new"
    assert values == ["old", "new"]


def test_A27_network_variants_have_distinct_identity():
    assert color_matrix.model_signature("Redmi Note 14 Pro 4G") != color_matrix.model_signature("Redmi Note 14 Pro 5G")
    assert color_matrix.model_signature("Redmi Note 14 Pro 5G") != color_matrix.model_signature("Redmi Note 14 Pro")


def test_A28_zero_feasible_combinations_stays_empty():
    attrs = [("مدل", ["iPhone 13", "iPhone 14"]), ("ایرپاد", ["AirPods 1/2", "AirPods Pro 2"]), ("رنگ", ["مشکی", "سفید"])]
    restrictions = {"iPhone 13": ["مشکی"], "iPhone 14": ["مشکی"], "AirPods 1/2": ["سفید"], "AirPods Pro 2": ["سفید"]}
    assert color_matrix.build_combinations(attrs, restrictions) == []


def test_A29_english_airpods_axis_is_reused_not_duplicated():
    current = product()
    current.attributes.append({"name": "AirPods", "variation": True, "visible": True, "options": ["AirPods 1/2", "AirPods Pro 2"]})
    result = update_plan.build(current, {"models": ["iPhone 13", "iPhone 14"], "attributes": {"ایرپاد": ["AirPods 1/2", "AirPods Pro 2"]}})
    assert len([axis for axis in (result.attributes or current.attributes) if airpods_parser.is_airpods_attribute(axis["name"])]) == 1


def test_A30_common_variation_ids_keep_all_unmentioned_metadata():
    current = product()
    result = update_plan.build(current, {"models": ["iPhone 13", "iPhone 14", "iPhone 15"]})
    assert not result.deletes and not result.regenerate
    assert result.kept == 2 and len(result.creates) == 1
    assert result.creates[0].model == "iPhone 15"
    assert not result.updates


def test_A31_review_files_do_not_execute_input_formulas():
    rows = [{"rownum": "=1+1", "barcode": "19357000001111222233334444", "code": "=2+2", "code_state": "error", "code_note": "bad", "barcode_state": "ok"}]
    workbook = load_workbook(io.BytesIO(processor.build_review_workbook(rows)))
    assert workbook.active["A2"].data_type == "s" and workbook.active["C2"].data_type == "s"
    assert "'=1+1" in processor.build_review_csv(rows)
    assert "'=2+2" in processor.build_review_csv(rows)


def test_A32_mobile_number_is_not_an_order_id():
    assert processor._RE_CODE_IN_TEXT.search("09123456789") is None
    assert processor._RE_CODE_IN_TEXT.search("گیرنده 305175 / 09123456789")[1] == "305175"


@pytest.mark.parametrize("text", ["²" * 24, "¹²³⁴⁵⁶⁷⁸", "½" * 24])
def test_A33_non_decimal_unicode_digits_do_not_crash_or_enter_output(text):
    assert barcodes.classify(text)[0] == "error"


def _slow_write(path: str):
    time.sleep(2)
    Path(path).write_text("late write", encoding="utf-8")
    return "finished"


def test_A34_timeout_stops_actual_worker_and_late_disk_writes(isolated):
    marker = isolated / "late.txt"
    async def run():
        with pytest.raises(TimeoutError):
            await worker.run(_slow_write, str(marker), timeout=0.15)
        assert not any(process.name == "TisaFileWorker" for process in multiprocessing.active_children())
        await asyncio.sleep(0.2)
    asyncio.run(run())
    assert not marker.exists()


def test_A36_history_keys_do_not_depend_on_the_clock(isolated):
    keys = [products_ledger.record(user_id=7, status="pending", title="قاب")["key"] for _ in range(50)]
    assert len(set(keys)) == len(keys)
    assert all(len(key) == 24 for key in keys)


def test_A37_corpus_keeps_all_models_and_inventory_matrix(isolated):
    body = ProductData(title="قاب", models=[f"iPhone {number}" for number in range(1, 16)], attributes={"طرح": ["A", "B"]})
    body.stock_matrix = {"A": dict.fromkeys(body.models, 2), "B": dict.fromkeys(body.models)}
    assert learning_corpus.record("iPhone 13", body)
    row = learning_corpus.entries()[0]
    assert len(row["models"]) == 15
    assert row["stock_matrix"] == body.stock_matrix
    assert plan.plan_from_dict(row).count == row["variation_count"]


def test_A38_compact_and_multiple_section_airpods_models_are_read():
    assert airpods_parser.extract_airpods_models("AirpodsPro2") == ["AirPods Pro 2"]
    assert airpods_parser.extract_airpods_models("AirPods:\n1/2 | 3 | Pro2") == ["AirPods 1/2", "AirPods 3", "AirPods Pro 2"]


def test_A43_history_is_guarded_before_reading(isolated, monkeypatch):
    monkeypatch.setattr(products_ledger, "recent", lambda *args: pytest.fail("unauthorized history read"))
    update, replies = query_update("product:recent", user_id=404)
    asyncio.run(product_tools.cb_recent(update, fake_context()))
    assert any("دسترسی" in str(item) for item in replies)


def test_B03_retry_after_does_not_keep_a_handler_sleeping_for_hours(isolated):
    response = httpx.Response(429, headers={"Retry-After": "7200"}, json={"message": "busy"})
    assert not WooClient._retry_status("POST", response)
    assert WooClient._retry_delay(response, 0) <= 30
    with pytest.raises(WooCommerceAPIError) as exc:
        check(response)
    assert exc.value.retry_after == 7200
    for bad in ["nan", "inf", "1e309"]:
        response = httpx.Response(429, headers={"Retry-After": bad})
        assert WooClient._retry_delay(response, 0) <= 30


def test_B09_active_workspaces_survive_the_janitor(isolated):
    root = isolated / "temp"
    directory = workspace.new_dir(root, 7)
    workspace.protect(directory)
    os.utime(directory, (1, 1))
    assert workspace.sweep(root, 1) == 0 and directory.is_dir()
    workspace.remove(directory)
    assert not directory.exists()


def test_C03_matrix_is_bounded_before_allocating_millions_of_rows():
    result = plan.build_plan([f"iPhone {number}" for number in range(100)], {"طرح": [str(number) for number in range(100)]})
    assert result.capacity_error and result.count == 0
