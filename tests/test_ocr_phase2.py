"""OCR-2 Phase 2: 起動判定・検算・強制停止・永続化・ジョブ。実OCR/GPU/実PDFは使わない。"""
from __future__ import annotations

import sqlite3
import threading
import time
from decimal import Decimal

import httpx
import pytest

from app.memory.short_term import ShortTermMemory, ensure_ocr_tables
from app.ocr_artifacts import sha256_hex
from app.ocr_client import OcrClient
from app.ocr_fallback import (
    amounts_for_downstream,
    decide_final_status,
    evaluate_ocr_triggers,
    max_consecutive_short_repeats,
    ocr_active_count,
    run_ocr_fallback,
    validate_ocr_checks,
)
from app.ocr_schema import OcrErrorCode, field_as_zero_amount, make_ocr_field
from app.ocr_store import OCR_TABLES, OcrStore

READABLE_TEXT = "あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほまみむめもやゆよ"  # 50 chars


def _readable_page(page=1, extra=None):
    row = {
        "page": page,
        "text": READABLE_TEXT,
        "quality": "readable",
        "char_count": 50,
        "replacement_control_rate": 0.0,
        "image_area_ratio": 0.1,
        "has_table_structure": True,
    }
    if extra:
        row.update(extra)
    return row


def _engine():
    return {
        "pipeline": "PaddleOCR-VL-1.6",
        "vlm_model": "PaddleOCR-VL-1.6-0.9B",
        "layout_model": "PP-DocLayoutV3",
        "image_digest": "sha256:" + "a" * 64,
        "parameters_hash": "b" * 64,
    }


def _source(sha, page_count=1):
    return {
        "context_file_id": "cf-1",
        "filename": "invoice.pdf",
        "sha256": sha,
        "page_count": page_count,
    }


def _block(page=1, block_id="p1-b001", text="当月ご請求額 0", bbox=None):
    return {
        "block_id": block_id,
        "label": "table",
        "bbox": bbox or [10, 10, 100, 100],
        "text": text,
        "confidence": 0.9,
    }


def _field(name, value, *, state="value", page=1, block_id="p1-b001", field_type="decimal", **extra):
    item = {
        "name": name,
        "value": value,
        "type": field_type,
        "page": page,
        "block_id": block_id,
        "value_state": state,
        "evidence_text": "",
        "validation": "",
    }
    item.update(extra)
    return item


def _payload(original, *, pages=None, fields=None, status="needs_review", trigger="garbled", page_count=1):
    sha = sha256_hex(original)
    return {
        "schema_version": "ocr-fallback/v1",
        "status": status,
        "trigger": trigger,
        "source": _source(sha, page_count),
        "engine": _engine(),
        "pages": pages if pages is not None else [{
            "page": 1, "width": 100, "height": 100, "blocks": [_block()],
        }],
        "fields": fields if fields is not None else [],
        "checks": [],
        "review": {"required": True, "decision": None, "reviewer": None, "reviewed_at": None},
    }


def _by_id(checks):
    return {item["check_id"]: item for item in checks}


def _fake_renderer(_data, page):
    return f"PNG-PAGE-{page}".encode("utf-8")


def _page_from_request(request) -> int:
    body = request.content.decode("latin1", errors="replace")
    marker = "PNG-PAGE-"
    if marker in body:
        after = body.split(marker, 1)[1]
        digits = ""
        for char in after:
            if char.isdigit():
                digits += char
            else:
                break
        if digits:
            return int(digits)
    if 'name="page"' in body:
        after = body.split('name="page"', 1)[1]
        for token in after.replace("\r", "\n").split("\n"):
            token = token.strip()
            if token.isdigit():
                return int(token)
    return 1


def _client_for(pages):
    def handler(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/ocr/recognize":
            form_page = _page_from_request(request)
            payload = pages.get(form_page, pages.get(1, {"text": "ok"}))
            return httpx.Response(200, json=payload)
        return httpx.Response(404)

    return OcrClient(transport=httpx.MockTransport(handler))


def _page_recognition(text="読取本文", fields=None, page=1):
    block = {
        "block_id": f"p{page}-b001",
        "label": "text",
        "bbox": [1, 2, 3, 4],
        "text": text,
        "confidence": 0.95,
    }
    if fields:
        block["fields"] = fields
    return {"page": page, "width": 100, "height": 200, "blocks": [block]}


# --- TRG ---

def test_readable_pdf_is_skipped_ocr_not_required():
    decision = evaluate_ocr_triggers([_readable_page()])
    assert decision.skipped is True
    assert decision.pages == []
    assert decision.skip_reason == OcrErrorCode.OCR_NOT_REQUIRED.value


def test_trg01_unreadable_starts_immediately():
    decision = evaluate_ocr_triggers([{
        "page": 1, "quality": "unreadable", "text": READABLE_TEXT, "char_count": 50,
    }])
    assert decision.skipped is False
    assert "TRG-01" in decision.reason_codes
    assert decision.trigger == "unreadable"


def test_trg02_garbled_starts_immediately():
    decision = evaluate_ocr_triggers([{
        "page": 2, "quality": "garbled", "text": READABLE_TEXT, "char_count": 50,
    }])
    assert "TRG-02" in decision.reason_codes
    assert decision.trigger == "garbled"


def test_trg03_replacement_rate_boundary():
    below = evaluate_ocr_triggers([_readable_page(extra={"replacement_control_rate": 0.00499})])
    assert below.skipped is True
    exact = evaluate_ocr_triggers([_readable_page(extra={"replacement_control_rate": 0.005})])
    assert "TRG-03" in exact.reason_codes
    above = evaluate_ocr_triggers([_readable_page(extra={"replacement_control_rate": 0.0051})])
    assert "TRG-03" in above.reason_codes


def test_trg04_char_density_boundary():
    fifty = evaluate_ocr_triggers([_readable_page(extra={"char_count": 50})])
    assert fifty.skipped is True
    forty_nine = evaluate_ocr_triggers([_readable_page(extra={"char_count": 49})])
    assert "TRG-04" in forty_nine.reason_codes
    assert forty_nine.trigger == "low_text_density"


def test_trg05_image_area_boundary():
    below = evaluate_ocr_triggers([_readable_page(extra={"image_area_ratio": 0.799})])
    assert below.skipped is True
    exact = evaluate_ocr_triggers([_readable_page(extra={"image_area_ratio": 0.80})])
    assert "TRG-05" in exact.reason_codes
    above = evaluate_ocr_triggers([_readable_page(extra={"image_area_ratio": 0.81})])
    assert "TRG-05" in above.reason_codes


def test_trg06_table_request_without_matrix():
    hit = evaluate_ocr_triggers(
        [_readable_page(extra={"has_table_structure": False})], purpose="table",
    )
    assert "TRG-06" in hit.reason_codes
    ok = evaluate_ocr_triggers(
        [_readable_page(extra={"has_table_structure": True})], purpose="table",
    )
    assert ok.skipped is True


def test_trg07_unmapped_amounts():
    decision = evaluate_ocr_triggers([_readable_page(extra={
        "amount_candidates": ["1234"], "amount_field_mapping": None,
    })])
    assert "TRG-07" in decision.reason_codes


def test_trg08_known_total_mismatch_one_yen():
    equal = evaluate_ocr_triggers(
        [_readable_page()], known_total="100", extracted_total="100",
    )
    assert equal.skipped is True
    mismatch = evaluate_ocr_triggers(
        [_readable_page()], known_total="100", extracted_total="99",
    )
    assert "TRG-08" in mismatch.reason_codes


def test_trg09_user_original_match():
    decision = evaluate_ocr_triggers(
        [_readable_page()], user_requested_original_match=True,
    )
    assert "TRG-09" in decision.reason_codes
    assert decision.trigger == "manual"


def test_triggers_are_page_local_and_not_always_on():
    decision = evaluate_ocr_triggers([
        _readable_page(1),
        {"page": 2, "quality": "unreadable", "text": "", "char_count": 0},
    ])
    assert decision.pages == [2]
    assert "TRG-01" in decision.reason_codes


def test_empty_pages_with_known_page_count_are_unreadable_not_skipped():
    for pages in (None, []):
        decision = evaluate_ocr_triggers(pages, page_count=3)
        assert decision.skipped is False
        assert decision.pages == [1, 2, 3]
        assert "TRG-01" in decision.reason_codes
        assert decision.trigger == "unreadable"
        assert decision.skip_reason != OcrErrorCode.OCR_NOT_REQUIRED.value


def test_no_pages_and_no_page_count_is_not_ocr_not_required():
    decision = evaluate_ocr_triggers(None, page_count=None)
    assert decision.skipped is False
    assert decision.skip_reason != OcrErrorCode.OCR_NOT_REQUIRED.value
    assert decision.trigger == "unreadable"
    empty = evaluate_ocr_triggers([])
    assert empty.skipped is False
    assert empty.skip_reason != OcrErrorCode.OCR_NOT_REQUIRED.value


# --- VAL ---

def test_val01_source_hash_match_and_mismatch():
    original = b"%PDF-1.4 sample"
    payload = _payload(original)
    ok = _by_id(validate_ocr_checks(payload, original_bytes=original, expected_sha256=sha256_hex(original)))
    assert ok["VAL-01"]["status"] == "passed"
    bad = _by_id(validate_ocr_checks(payload, original_bytes=original, expected_sha256="0" * 64))
    assert bad["VAL-01"]["status"] == "failed"


def test_val02_page_coverage_and_missing():
    original = b"pdf"
    payload = _payload(original, page_count=2, pages=[{
        "page": 1, "width": 1, "height": 1, "blocks": [_block()],
    }])
    missing = _by_id(validate_ocr_checks(payload, original_bytes=original, expected_sha256=sha256_hex(original), target_pages=[1, 2]))
    assert missing["VAL-02"]["status"] == "failed"
    payload["pages"].append({"page": 2, "width": 1, "height": 1, "blocks": [_block(page=2, block_id="p2-b001")]})
    ok = _by_id(validate_ocr_checks(payload, original_bytes=original, expected_sha256=sha256_hex(original), target_pages=[1, 2]))
    assert ok["VAL-02"]["status"] == "passed"


def test_val03_requires_page_block_bbox():
    original = b"pdf"
    payload = _payload(original, fields=[_field("x", "1", state="value")])
    ok = _by_id(validate_ocr_checks(payload, original_bytes=original, expected_sha256=sha256_hex(original)))
    assert ok["VAL-03"]["status"] == "passed"
    payload["fields"][0].pop("block_id")
    bad = _by_id(validate_ocr_checks(payload, original_bytes=original, expected_sha256=sha256_hex(original)))
    assert bad["VAL-03"]["status"] == "failed"


def test_val04_decimal_lossless():
    original = b"pdf"
    payload = _payload(original, fields=[_field("unit_price", "1234.50")])
    ok = _by_id(validate_ocr_checks(payload, original_bytes=original, expected_sha256=sha256_hex(original)))
    assert ok["VAL-04"]["status"] == "passed"
    payload["fields"] = [_field("unit_price", "12.3e1")]
    bad = _by_id(validate_ocr_checks(payload, original_bytes=original, expected_sha256=sha256_hex(original)))
    assert bad["VAL-04"]["status"] == "failed"


def test_val05_line_total_zero_tolerance_and_tax_rule_review():
    original = b"pdf"
    fields = [
        _field("line_amount", "100", row_key="r1"),
        _field("line_amount", "50", block_id="p1-b002", row_key="r2"),
        _field("current_invoice_amount", "150"),
    ]
    pages = [{"page": 1, "width": 1, "height": 1, "blocks": [_block(), _block(block_id="p1-b002")]}]
    payload = _payload(original, pages=pages, fields=fields)
    ok = _by_id(validate_ocr_checks(payload, original_bytes=original, expected_sha256=sha256_hex(original)))
    assert ok["VAL-05"]["status"] == "passed"
    fields[-1]["value"] = "151"
    mismatch = _by_id(validate_ocr_checks(payload, original_bytes=original, expected_sha256=sha256_hex(original)))
    assert mismatch["VAL-05"]["status"] == "failed"
    fields[-1]["value"] = "150"
    review = _by_id(validate_ocr_checks(
        payload, original_bytes=original, expected_sha256=sha256_hex(original),
        tax_rounding_rule="unknown",
    ))
    assert review["VAL-05"]["status"] == "needs_review"


def test_val06_invoice_formula_and_ambiguous_roles():
    original = b"pdf"
    pages = [{"page": 1, "width": 1, "height": 1, "blocks": [
        _block(), _block(block_id="p1-b002"), _block(block_id="p1-b003"), _block(block_id="p1-b004"),
    ]}]
    fields = [
        _field("previous_invoice_amount", "100"),
        _field("payment_amount", "40", block_id="p1-b002"),
        _field("current_purchase_amount", "10", block_id="p1-b003"),
        _field("current_invoice_amount", "70", block_id="p1-b004"),
    ]
    payload = _payload(original, pages=pages, fields=fields)
    ok = _by_id(validate_ocr_checks(payload, original_bytes=original, expected_sha256=sha256_hex(original)))
    assert ok["VAL-06"]["status"] == "passed"
    fields[-1]["value"] = "71"
    bad = _by_id(validate_ocr_checks(payload, original_bytes=original, expected_sha256=sha256_hex(original)))
    assert bad["VAL-06"]["status"] == "failed"
    mixed = _payload(original, fields=[_field("amount", "100")])
    mixed_checks = _by_id(validate_ocr_checks(mixed, original_bytes=original, expected_sha256=sha256_hex(original)))
    assert mixed_checks["VAL-06"]["status"] == "needs_review"


def test_val07_repetition_boundary():
    nine = "お客様コード " * 9
    ten = "お客様コード " * 10
    assert max_consecutive_short_repeats(nine) < 10
    assert max_consecutive_short_repeats(ten) >= 10
    original = b"pdf"
    payload = _payload(original, pages=[{
        "page": 1, "width": 1, "height": 1,
        "blocks": [_block(text=ten)],
    }])
    checks = _by_id(validate_ocr_checks(payload, original_bytes=original, expected_sha256=sha256_hex(original)))
    assert checks["VAL-07"]["status"] == "failed"


def test_val07_html_table_empty_cells_and_repetitions():
    # (a) 空セルが30個並ぶ <table> のブロックは反復に数えられず VAL-07 が passed
    empty_cells = "<table>" + "".join("<tr>" + "<td></td>" * 10 + "</tr>" for _ in range(3)) + "</table>"
    original = b"pdf"
    payload_a = _payload(original, pages=[{
        "page": 1, "width": 100, "height": 100,
        "blocks": [_block(text=empty_cells)],
    }])
    checks_a = _by_id(validate_ocr_checks(payload_a, original_bytes=original, expected_sha256=sha256_hex(original)))
    assert checks_a["VAL-07"]["status"] == "passed"
    # (d) 保存される text はHTMLのまま変わらない
    assert payload_a["pages"][0]["blocks"][0]["text"] == empty_cells

    # (b) <td>合計</td> を12回連続させた表は、可視テキストで「合計」が12回連続するので failed
    repeat_cells = "<table><tr>" + "<td>合計</td>" * 12 + "</tr></table>"
    payload_b = _payload(original, pages=[{
        "page": 1, "width": 100, "height": 100,
        "blocks": [_block(text=repeat_cells)],
    }])
    checks_b = _by_id(validate_ocr_checks(payload_b, original_bytes=original, expected_sha256=sha256_hex(original)))
    assert checks_b["VAL-07"]["status"] == "failed"

    # (c) text ブロックの通常の反復(「あ」の12回連続)は従来どおり failed
    repeat_text = "あ " * 12
    payload_c = _payload(original, pages=[{
        "page": 1, "width": 100, "height": 100,
        "blocks": [_block(text=repeat_text)],
    }])
    checks_c = _by_id(validate_ocr_checks(payload_c, original_bytes=original, expected_sha256=sha256_hex(original)))
    assert checks_c["VAL-07"]["status"] == "failed"


def test_val07_paddlex_fixture_max_repetition_below_limit():
    # (e) 実機のサンプル tests/fixtures/paddlex_layout_parsing_sample.json の全ブロックで、反復の最大値が10未満(passed 側)
    from pathlib import Path
    import json
    fixture_path = Path(__file__).resolve().parent / "fixtures" / "paddlex_layout_parsing_sample.json"
    data = json.loads(fixture_path.read_text(encoding="utf-8"))
    pruned = data["result"]["layoutParsingResults"][0]["prunedResult"]
    for block in pruned["parsing_res_list"]:
        text = block.get("block_content", "")
        repeats = max_consecutive_short_repeats(text)
        assert repeats < 10, f"Block {block.get('block_id')} has repetition {repeats} >= 10"


def test_val08_replacement_rate_half_percent():
    original = b"pdf"
    clean = _payload(original)
    ok = _by_id(validate_ocr_checks(clean, original_bytes=original, expected_sha256=sha256_hex(original)))
    assert ok["VAL-08"]["status"] == "passed"
    garbled = "A" * 199 + "\ufffd"
    payload = _payload(original, pages=[{
        "page": 1, "width": 1, "height": 1, "blocks": [_block(text=garbled)],
    }])
    bad = _by_id(validate_ocr_checks(payload, original_bytes=original, expected_sha256=sha256_hex(original)))
    assert bad["VAL-08"]["status"] == "failed"


def test_val09_vehicle_number_unknown_and_master():
    original = b"pdf"
    pages = [{"page": 1, "width": 1, "height": 1, "blocks": [_block()]}]
    missing = _payload(original, pages=pages, fields=[
        _field("vehicle_number", "", state="blank", used_for_vehicle_cost=True),
    ])
    checks = _by_id(validate_ocr_checks(missing, original_bytes=original, expected_sha256=sha256_hex(original)))
    assert checks["VAL-09"]["status"] == "failed"
    known = _payload(original, pages=pages, fields=[
        _field("vehicle_number", "品川100あ12-34", used_for_vehicle_cost=True),
    ])
    ok = _by_id(validate_ocr_checks(
        known, original_bytes=original, expected_sha256=sha256_hex(original),
        vehicle_master=["品川100あ12-34"],
    ))
    assert ok["VAL-09"]["status"] == "passed"


def test_val10_date_roles_must_be_distinct():
    original = b"pdf"
    unlabeled = _payload(original, fields=[_field("date", "2026-05-01", field_type="date")])
    checks = _by_id(validate_ocr_checks(unlabeled, original_bytes=original, expected_sha256=sha256_hex(original)))
    assert checks["VAL-10"]["status"] == "needs_review"
    labeled = _payload(original, fields=[
        _field("target_month", "2026-05", field_type="month", role="target_month"),
        _field("transaction_date", "2026-05-25", field_type="date", block_id="p1-b002", role="transaction_date"),
        _field("closing_date", "2026-05-31", field_type="date", block_id="p1-b003", role="closing_date"),
    ])
    labeled["pages"][0]["blocks"].extend([_block(block_id="p1-b002"), _block(block_id="p1-b003")])
    ok = _by_id(validate_ocr_checks(labeled, original_bytes=original, expected_sha256=sha256_hex(original)))
    assert ok["VAL-10"]["status"] == "passed"


def test_val11_duplicate_row_keys():
    original = b"pdf"
    pages = [{"page": 1, "width": 1, "height": 1, "blocks": [_block(), _block(block_id="p1-b002")]}]
    dup = _payload(original, pages=pages, fields=[
        _field("line_amount", "10", row_key="line-1"),
        _field("line_amount", "10", block_id="p1-b002", row_key="line-1"),
    ])
    checks = _by_id(validate_ocr_checks(dup, original_bytes=original, expected_sha256=sha256_hex(original)))
    assert checks["VAL-11"]["status"] == "failed"


def test_val12_blank_zero_unreadable_stay_distinct():
    original = b"pdf"
    payload = _payload(original, fields=[
        _field("fuel", "", state="blank"),
        _field("tax", "0", state="zero", block_id="p1-b002"),
        _field("other", "", state="unreadable", block_id="p1-b003"),
    ])
    payload["pages"][0]["blocks"].extend([_block(block_id="p1-b002"), _block(block_id="p1-b003")])
    checks = _by_id(validate_ocr_checks(payload, original_bytes=original, expected_sha256=sha256_hex(original)))
    assert checks["VAL-12"]["status"] == "passed"
    payload["fields"][-1]["value"] = "0"
    bad = _by_id(validate_ocr_checks(payload, original_bytes=original, expected_sha256=sha256_hex(original)))
    assert bad["VAL-12"]["status"] == "failed"


# --- 強制停止 ---

@pytest.mark.parametrize("code", [
    "OCR_SOURCE_HASH_MISMATCH",
    "OCR_PAGE_RENDER_FAILED",
    "OCR_REPETITION_DETECTED",
    "OCR_TOTAL_MISMATCH",
    "OCR_SCHEMA_INVALID",
    "OCR_ARTIFACT_TAMPERED",
    "OCR_SERVICE_UNAVAILABLE",
])
def test_any_force_stop_prevents_passed(code):
    status, _ = decide_final_status([{"check_id": "VAL-05", "status": "passed"}], extra_codes=[code])
    assert status != "passed"


def test_failed_and_needs_review_are_not_converted_to_zero():
    unreadable = make_ocr_field("current_invoice_amount", None, page=1, block_id="p1-b001", unreadable=True)
    zero = make_ocr_field("current_invoice_amount", "0", page=1, block_id="p1-b001")
    assert field_as_zero_amount(unreadable) is False
    assert field_as_zero_amount(zero) is True
    failed = {"status": "failed", "fields": [{
        "name": "current_invoice_amount", "value": "", "value_state": "unreadable",
    }]}
    assert amounts_for_downstream(failed) is None
    review = {"status": "needs_review", "fields": [{
        "name": "current_invoice_amount", "value": "0", "value_state": "zero",
    }]}
    assert amounts_for_downstream(review) is None


def test_file_absent_is_not_same_as_unreadable_file(tmp_path):
    store = OcrStore(tmp_path / "ocr.db")
    absent = run_ocr_fallback(
        original_bytes=None, expected_sha256="0" * 64, project_id="p",
        context_file_id="cf", store=store, feature_enabled=True,
        pages_quality=[_readable_page()],
    )
    original = b"%PDF unreadable"
    present = run_ocr_fallback(
        original_bytes=original, expected_sha256=sha256_hex(original),
        project_id="p", context_file_id="cf2", store=store, feature_enabled=True,
        pages_quality=[{"page": 1, "quality": "unreadable", "text": "", "char_count": 0}],
        client=_client_for({1: _page_recognition()}),
        renderer=_fake_renderer,
    )
    assert absent.get("file_absent") is True
    assert present.get("file_absent") is not True
    assert absent["status"] == "failed"
    assert present["status"] in {"failed", "needs_review", "passed"}


# --- 永続化 ---

def test_ocr_migration_is_idempotent_and_does_not_copy_blob(tmp_path):
    path = tmp_path / "memory.db"
    memory = ShortTermMemory(path)
    store = OcrStore(path)
    store.migrate()
    store.migrate()
    names = store.table_names()
    for table in OCR_TABLES:
        assert table in names
    assert store.has_original_blob_column() is False
    with sqlite3.connect(path) as db:
        ensure_ocr_tables(db)
        turns_cols = {row[1] for row in db.execute("PRAGMA table_info(turns)")}
    assert "user_text" in turns_cols
    memory.save("s", "hello", "world", "m", 1.0)
    assert memory.recent("s")[0]["content"] == "hello"


def test_store_saves_run_page_block_field_validation(tmp_path):
    store = OcrStore(tmp_path / "ocr.db")
    run, duplicate = store.create_run(
        project_id="p1", context_file_id="cf", source_sha256="a" * 64,
        execution_key="key-1", status="running",
    )
    assert duplicate is False
    store.save_page(run["run_id"], 1, image_sha256="b" * 64, width=10, height=20, status="done")
    store.save_block(run["run_id"], 1, "p1-b001", label="text", bbox=[1, 2, 3, 4], text="hello", confidence=0.8)
    store.save_field(run["run_id"], "f001", name="current_invoice_amount", value_text="0", value_type="decimal", page_no=1, block_id="p1-b001")
    store.save_validation(run["run_id"], "VAL-01", status="passed", expected="a" * 64, actual="a" * 64)
    again, dup = store.create_run(
        project_id="p1", context_file_id="cf", source_sha256="a" * 64,
        execution_key="key-1",
    )
    assert dup is True
    assert again["run_id"] == run["run_id"]
    bundle = store.get_bundle(run["run_id"])
    assert bundle["pages"][0]["page_no"] == 1
    assert bundle["blocks"][0]["block_id"] == "p1-b001"
    assert bundle["fields"][0]["name"] == "current_invoice_amount"
    assert bundle["validations"][0]["check_id"] == "VAL-01"
    assert "original_data" not in bundle["run"]


# --- ジョブ ---

def _run_kwargs(tmp_path, original, **extra):
    store = extra.pop("store", OcrStore(tmp_path / "ocr.db"))
    kwargs = dict(
        original_bytes=original,
        expected_sha256=sha256_hex(original),
        project_id="p1",
        context_file_id="cf-1",
        filename="doc.pdf",
        page_count=1,
        pages_quality=[{"page": 1, "quality": "unreadable", "text": "", "char_count": 0}],
        store=store,
        renderer=_fake_renderer,
        feature_enabled=True,
        artifact_root=tmp_path / "artifacts",
    )
    kwargs.update(extra)
    return kwargs, store


def test_idempotent_execution_key_returns_existing(tmp_path):
    original = b"%PDF same"
    client = _client_for({1: _page_recognition()})
    kwargs, store = _run_kwargs(tmp_path, original, client=client)
    first = run_ocr_fallback(**kwargs)
    second = run_ocr_fallback(**kwargs)
    assert first["run_id"] == second["run_id"]
    assert second["duplicate"] is True
    with store.connect() as db:
        count = db.execute("SELECT COUNT(*) AS n FROM ocr_runs").fetchone()["n"]
    assert count == 1


def test_concurrency_limit_is_one(tmp_path):
    original = b"%PDF slow"
    started = threading.Event()
    release = threading.Event()
    seen = []

    def handler(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"ok": True})
        seen.append(ocr_active_count())
        started.set()
        release.wait(timeout=2)
        return httpx.Response(200, json=_page_recognition())

    client = OcrClient(transport=httpx.MockTransport(handler))
    kwargs, _ = _run_kwargs(tmp_path, original, client=client, artifact_root=None)
    errors = []

    def worker():
        try:
            run_ocr_fallback(**kwargs)
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    assert started.wait(timeout=2)
    assert ocr_active_count() == 1
    release.set()
    thread.join(timeout=2)
    assert not errors
    assert max(seen) == 1


def test_page_checkpoint_resumes_unprocessed_only(tmp_path):
    original = b"%PDF two pages"
    store = OcrStore(tmp_path / "ocr.db")
    calls = []

    def handler(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"ok": True})
        page = _page_from_request(request)
        calls.append(page)
        return httpx.Response(200, json=_page_recognition(page=page, text=f"page{page}"))

    client = OcrClient(transport=httpx.MockTransport(handler))
    kwargs, store = _run_kwargs(
        tmp_path, original, store=store, client=client, artifact_root=None,
        page_count=2,
        pages_quality=[
            {"page": 1, "quality": "unreadable", "text": "", "char_count": 0},
            {"page": 2, "quality": "unreadable", "text": "", "char_count": 0},
        ],
    )
    first = run_ocr_fallback(**kwargs)
    run_id = first["run_id"]
    store.update_run(run_id, status="running")
    with store.connect() as db:
        db.execute("DELETE FROM ocr_pages WHERE run_id=? AND page_no=2", (run_id,))
        db.execute("DELETE FROM ocr_blocks WHERE run_id=? AND page_no=2", (run_id,))
    calls.clear()
    resumed = run_ocr_fallback(**kwargs, resume=True)
    assert resumed["resumed"] is True
    assert calls == [2]
    assert set(resumed["processed_pages"]) >= {1, 2}


def test_resume_stops_on_hash_mismatch(tmp_path):
    original = b"%PDF v1"
    store = OcrStore(tmp_path / "ocr.db")
    run, _ = store.create_run(
        project_id="p1", context_file_id="cf-1", source_sha256=sha256_hex(original),
        execution_key="pending-key", status="running",
    )
    store.save_page(run["run_id"], 1, status="done")
    changed = b"%PDF v2"
    result = run_ocr_fallback(
        original_bytes=changed, expected_sha256=sha256_hex(changed),
        project_id="p1", context_file_id="cf-1", store=store,
        feature_enabled=True, resume=True,
        pages_quality=[{"page": 1, "quality": "unreadable", "text": "", "char_count": 0}],
        renderer=_fake_renderer,
        client=_client_for({1: _page_recognition()}),
    )
    assert result["status"] == "failed"
    assert result["error_code"] == "OCR_SOURCE_HASH_MISMATCH"


def test_service_unavailable_keeps_regular_extraction(tmp_path):
    original = b"%PDF keep"
    regular = {"text": "通常抽出", "quality": "garbled"}

    def handler(request):
        raise httpx.ConnectError("down", request=request)

    client = OcrClient(transport=httpx.MockTransport(handler))
    kwargs, store = _run_kwargs(
        tmp_path, original, client=client, artifact_root=None,
        regular_extraction=regular,
    )
    result = run_ocr_fallback(**kwargs)
    assert result["status"] == "failed"
    assert result["error_code"] == "OCR_SERVICE_UNAVAILABLE"
    assert result["regular_extraction"] == regular
    saved = store.get_run(result["run_id"])
    assert saved["error_code"] == "OCR_SERVICE_UNAVAILABLE"


def test_retry_only_on_transient_via_client(tmp_path):
    original = b"%PDF retry"
    calls = {"n": 0}

    def handler(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"ok": True})
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, json=_page_recognition())

    client = OcrClient(transport=httpx.MockTransport(handler))
    kwargs, _ = _run_kwargs(tmp_path, original, client=client, artifact_root=None)
    result = run_ocr_fallback(**kwargs)
    assert calls["n"] == 2
    assert result["status"] in {"passed", "needs_review", "failed"}

    calls["n"] = 0

    def poor(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"ok": True})
        calls["n"] += 1
        return httpx.Response(200, json={"text": "", "quality": "bad"})

    client = OcrClient(transport=httpx.MockTransport(poor))
    kwargs, _ = _run_kwargs(tmp_path, original + b"2", client=client, artifact_root=None, context_file_id="cf-poor")
    run_ocr_fallback(**kwargs)
    assert calls["n"] == 1


def test_feature_flag_disabled_skips_without_ocr(tmp_path, monkeypatch):
    monkeypatch.delenv("LOCALSAPORTER_OCR_ENABLED", raising=False)
    original = b"%PDF flag"
    called = {"n": 0}

    def renderer(data, page):
        called["n"] += 1
        return b"PNG"

    result = run_ocr_fallback(
        original_bytes=original, expected_sha256=sha256_hex(original),
        project_id="p", context_file_id="cf",
        pages_quality=[{"page": 1, "quality": "unreadable", "text": "", "char_count": 0}],
        renderer=renderer, feature_enabled=False,
        store=OcrStore(tmp_path / "ocr.db"),
    )
    assert result["status"] == "skipped"
    assert result["skip_reason"] == "feature_disabled"
    assert called["n"] == 0


# --- フロー ---

def test_end_to_end_passed_needs_review_failed(tmp_path):
    original = b"%PDF flow"

    passed_fields = [
        {"name": "note", "value": "ok", "type": "string", "value_state": "value"},
    ]
    client_ok = _client_for({1: _page_recognition(fields=passed_fields)})
    kwargs, _ = _run_kwargs(tmp_path, original, client=client_ok)
    passed = run_ocr_fallback(**kwargs)
    assert passed["status"] in {"passed", "needs_review"}
    if passed["status"] == "passed":
        assert passed["downstream_allowed"] is True

    review_fields = [
        {"name": "amount", "value": "100", "type": "decimal", "value_state": "value"},
        {"name": "date", "value": "2026-05-01", "type": "date", "value_state": "value"},
    ]
    client_review = _client_for({1: _page_recognition(fields=review_fields)})
    kwargs, _ = _run_kwargs(tmp_path, original + b"r", client=client_review, context_file_id="cf-review")
    reviewed = run_ocr_fallback(**kwargs)
    assert reviewed["status"] in {"needs_review", "failed"}
    assert reviewed["downstream_allowed"] is False

    def fail_handler(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json=_page_recognition(text="お客様コード " * 12))

    client_fail = OcrClient(transport=httpx.MockTransport(fail_handler))
    kwargs, _ = _run_kwargs(tmp_path, original + b"f", client=client_fail, context_file_id="cf-fail")
    failed = run_ocr_fallback(**kwargs)
    assert failed["status"] == "failed"
    assert failed["downstream_allowed"] is False
    assert amounts_for_downstream(failed) is None
