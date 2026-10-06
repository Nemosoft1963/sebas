"""OCR-3 Phase 3: 帳票アダプター・三値状態・解決ゲート・採用。実OCR/GPU/実PDFは使わない。"""
from __future__ import annotations

import pytest

from app.ocr_invoice_adapter import (
    CHAPTER6_FIELDS,
    OCR_STATE_TO_PARSE_MONEY,
    adapt_ocr_invoice,
    adoptable_charge_dicts,
    current_month_source_control,
    evaluate_ocr_resolution_gate,
    is_ocr_adoptable,
    ocr_state_to_parse_money,
    printed_total_from_adapted,
    to_adapter_result,
    to_invoice_document,
)
from app.ocr_schema import OcrErrorCode, field_as_zero_amount, make_ocr_field
from app.ocr_store import OcrStore
from app.vehicle_auto import (
    Extractor,
    InvoiceDocument,
    adopt_ocr_for_normalization,
    apply_ocr_adoption,
    classify_invoice_relation,
    extract,
)
from app.vehicle_reconciliation import reconcile_sources
from app.vehicle_workflow import evaluate_ocr_resolution_gate as workflow_gate


def _block(block_id="p1-b001", text="", bbox=None, label="table"):
    return {
        "block_id": block_id,
        "label": label,
        "bbox": bbox or [10, 20, 30, 40],
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
        "evidence_text": extra.pop("evidence_text", ""),
        "validation": extra.pop("validation", ""),
    }
    item.update(extra)
    return item


def _payload(*, fields=None, pages=None, status="passed", run_id="run-exp", file_absent=False, extra_pages_blocks=None):
    blocks = extra_pages_blocks or [
        _block("p1-b001", "前月ご請求額 48,229"),
        _block("p1-b002", "当月ご入金額 48,229"),
        _block("p1-b003", "当月お買上額 0"),
        _block("p1-b004", "当月ご請求額 0"),
        _block("p1-b005", "当月燃料使用量"),
        _block("p1-b006", "5月25日 入金"),
    ]
    return {
        "schema_version": "ocr-fallback/v1",
        "status": status,
        "trigger": "unreadable",
        "run_id": run_id,
        "source": {
            "context_file_id": "cf-eneos",
            "filename": "ENEOS20260531_関東ロジ.pdf",
            "sha256": "a" * 64,
            "page_count": 1,
        },
        "engine": {
            "pipeline": "PaddleOCR-VL-1.6",
            "vlm_model": "PaddleOCR-VL-1.6-0.9B",
            "layout_model": "PP-DocLayoutV3",
            "image_digest": "",
            "parameters_hash": "",
        },
        "pages": pages if pages is not None else [{
            "page": 1, "width": 1653, "height": 2339, "blocks": blocks,
        }],
        "fields": fields if fields is not None else [],
        "checks": [],
        "review": {"required": False, "decision": None, "reviewer": None, "reviewed_at": None},
        "file_absent": file_absent,
    }


def experiment_fields():
    """仕様 13.3 の合成OCR結果。実PDFは使わない。"""
    return [
        _field("previous_invoice_amount", "48229", block_id="p1-b001",
               evidence_text="前月ご請求額 48,229円"),
        _field("payment_amount", "48229", block_id="p1-b002",
               evidence_text="当月ご入金額 48,229円"),
        _field("current_purchase_amount", "0", state="zero", block_id="p1-b003",
               evidence_text="当月お買上額 0円"),
        _field("current_invoice_amount", "0", state="zero", block_id="p1-b004",
               evidence_text="当月ご請求額 0円"),
        _field("fuel_usage", "", state="blank", field_type="string", block_id="p1-b005",
               evidence_text="当月燃料使用量"),
        _field("payment_date", "2026-05-25", state="value", field_type="date", block_id="p1-b006",
               evidence_text="5月25日 入金"),
    ]


def experiment_payload(**overrides):
    payload = _payload(fields=experiment_fields())
    payload.update(overrides)
    return payload


# --- 1. 実験正解 ---

def test_experiment_previous_payment_not_allocated_to_current_fuel():
    adapted = adapt_ocr_invoice(experiment_payload())
    assert adapted.current_month_new_fuel_amount == 0
    not_names = {row["name"] for row in adapted.not_current_month}
    assert "previous_invoice_amount" in not_names
    assert "payment_amount" in not_names
    prev = next(row for row in adapted.not_current_month if row["name"] == "previous_invoice_amount")
    pay = next(row for row in adapted.not_current_month if row["name"] == "payment_amount")
    assert prev["amount"] == 48229
    assert pay["amount"] == 48229
    assert "前月" in prev["reason"] or "配賦しない" in prev["reason"]
    assert "入金" in pay["reason"]
    charge_names = {item.name for item in adapted.current_month_charges}
    assert "previous_invoice_amount" not in charge_names
    assert "payment_amount" not in charge_names
    charge_amounts = {ocr_state_to_parse_money(item).amount for item in adapted.current_month_charges}
    assert 48229 not in charge_amounts
    assert adapted.vehicle_lines == []
    adopted = adopt_ocr_for_normalization(experiment_payload(), run_id="run-exp")
    assert all(row.get("name") not in {"previous_invoice_amount", "payment_amount", "carryover_amount"} for row in adopted)
    assert all("48229" not in str(row.get("value")) for row in adopted)


# --- 2. 項目の分離 ---

def test_chapter6_fields_are_separate_and_ambiguous_stops():
    blocks = [_block(f"p1-b{i:03d}") for i in range(1, 17)]
    fields = [
        _field("previous_invoice_amount", "100", block_id="p1-b001"),
        _field("payment_amount", "40", block_id="p1-b002"),
        _field("carryover_amount", "60", block_id="p1-b003"),
        _field("current_purchase_amount", "10", block_id="p1-b004"),
        _field("current_invoice_amount", "70", block_id="p1-b005"),
        _field("tax_amount", "1", block_id="p1-b006"),
        _field("discount_amount", "0", state="zero", block_id="p1-b007"),
        _field("purchase_date", "2026-05-10", field_type="date", block_id="p1-b008"),
        _field("payment_date", "2026-05-25", field_type="date", block_id="p1-b009"),
        _field("vehicle_number", "1234", field_type="string", block_id="p1-b010", row_key="r1"),
        _field("product", "軽油", field_type="string", block_id="p1-b011", row_key="r1"),
        _field("quantity", "1", block_id="p1-b012", row_key="r1"),
        _field("unit_price", "10", block_id="p1-b013", row_key="r1"),
        _field("line_amount", "10", block_id="p1-b014", row_key="r1"),
        _field("receipt_number", "R-1", field_type="string", block_id="p1-b015", row_key="r1"),
        _field("amount", "99999", block_id="p1-b016"),
    ]
    adapted = adapt_ocr_invoice(_payload(fields=fields, extra_pages_blocks=blocks))
    for name in CHAPTER6_FIELDS:
        assert name in adapted.fields, name
        item = adapted.fields[name]
        assert item.page == 1
        assert item.block_id
        assert item.bbox is not None
        assert item.state in {"value", "zero", "blank", "unreadable"}
    assert adapted.fields["previous_invoice_amount"] is not adapted.fields["payment_amount"]
    assert adapted.fields["current_purchase_amount"] is not adapted.fields["current_invoice_amount"]
    assert adapted.ambiguous
    assert all(item.error_code == OcrErrorCode.OCR_FIELD_AMBIGUOUS.value for item in adapted.ambiguous)
    assert adapted.roles_ambiguous is True
    codes = {issue["code"] for issue in adapted.issues}
    assert OcrErrorCode.OCR_FIELD_AMBIGUOUS.value in codes
    payment_only = adapt_ocr_invoice(_payload(fields=[
        _field("payment_amount", "48229", block_id="p1-b002", row_key="pay1"),
        _field("line_amount", "48229", block_id="p1-b002", row_key="pay1"),
    ]))
    assert payment_only.vehicle_lines == []
    assert payment_only.unallocated_lines
    assert all(not line.used_for_vehicle_cost for line in payment_only.unallocated_lines)
    assert "車両番号" in payment_only.unallocated_lines[0].reason


# --- 3. 状態 ---

def test_blank_zero_unreadable_stay_distinct_downstream():
    blank = make_ocr_field("fuel_usage", "", page=1, block_id="p1-b005")
    zero = make_ocr_field("current_purchase_amount", "0", page=1, block_id="p1-b003")
    unreadable = make_ocr_field("line_amount", None, page=1, block_id="p1-b014", unreadable=True)
    assert blank.value_state == "blank"
    assert zero.value_state == "zero"
    assert unreadable.value_state == "unreadable"
    assert OCR_STATE_TO_PARSE_MONEY["blank"] == "blank"
    assert OCR_STATE_TO_PARSE_MONEY["zero"] == "value"
    assert OCR_STATE_TO_PARSE_MONEY["unreadable"] == "invalid"
    parsed_u = ocr_state_to_parse_money({
        "value_state": "unreadable", "value": "", "name": "line_amount",
    })
    assert parsed_u.status == "invalid"
    assert parsed_u.reason == "unreadable"
    assert parsed_u.amount is None
    assert field_as_zero_amount(unreadable) is False
    adapted = adapt_ocr_invoice(_payload(fields=[
        _field("current_purchase_amount", "", state="unreadable", block_id="p1-b003"),
        _field("current_invoice_amount", "0", state="zero", block_id="p1-b004"),
        _field("fuel_usage", "", state="blank", field_type="string", block_id="p1-b005"),
    ]))
    assert adapted.fields["current_purchase_amount"].state == "unreadable"
    assert adapted.fields["current_invoice_amount"].state == "zero"
    assert adapted.fields["fuel_usage"].state == "blank"
    assert adapted.current_month_new_fuel_amount is None
    assert any(issue.get("kind") == "read" or issue.get("code") == "unreadable" for issue in adapted.issues)
    ex = Extractor(["2026-05"])
    source = {"id": "f", "filename": "燃料/202605/ENEOS_関東.pdf"}
    apply_ocr_adoption(ex, source, "", {
        "payload": _payload(fields=[
            _field("line_amount", "", state="unreadable", block_id="p1-b014", row_key="r1"),
            _field("vehicle_number", "1234", field_type="string", block_id="p1-b010", row_key="r1"),
            _field("current_invoice_amount", "0", state="zero", block_id="p1-b004"),
        ], status="passed"),
        "run_id": "run-u",
        "review": None,
    })
    reads = [i for i in ex.issues if i["kind"] == "read"]
    assert reads
    assert any(i.get("reason_code") == "unreadable" for i in reads)
    assert not any(r["amount"] == 0 and r.get("quality") == "actual" and "unreadable" in str(r) for r in ex.records)
    assert not any(r.get("quality") == "assumed_zero" for r in ex.records)

    absent = adapt_ocr_invoice(_payload(fields=experiment_fields(), file_absent=True, status="failed"))
    present_unread = adapt_ocr_invoice(_payload(fields=[
        _field("current_purchase_amount", "", state="unreadable", block_id="p1-b003"),
    ], file_absent=False, status="failed"))
    assert absent.file_absent is True
    assert present_unread.file_absent is False
    assert any(i["code"] == "file_absent" for i in absent.issues)
    assert not any(i["code"] == "file_absent" for i in present_unread.issues)


# --- 4. InvoiceDocument / AdapterResult / reconcile ---

def test_printed_total_is_current_invoice_not_previous_or_payment():
    adapted = adapt_ocr_invoice(experiment_payload())
    source = {"id": "eneos", "filename": "燃料/202605/ENEOS_関東.pdf"}
    invoice = to_invoice_document(adapted, source, "")
    result = to_adapter_result(adapted)
    assert isinstance(invoice, InvoiceDocument)
    assert invoice.printed_total == 0
    assert result.printed_total == 0
    assert printed_total_from_adapted(adapted) == 0
    assert invoice.printed_total != 48229
    other = InvoiceDocument(
        source_ref="context:csv", vendor="wing", company="関東", billing_month="2026-05",
        invoice_number=None, invoice_date=None, printed_total=0,
        detail_count_printed=0, document_role="detail",
        detail_fingerprints=[], extraction_complete=True, extraction_issues=[],
    )
    assert classify_invoice_relation(invoice, other) in {
        "exact_duplicate", "same_invoice_complement", "different_invoice", "unknown", "partial_overlap",
    }
    control = current_month_source_control(adapted, source, mon="2026-05")
    assert control is not None
    assert control["amount"] == 0
    assert control["origin"] == "source_total"
    rows = reconcile_sources(
        records=[],
        controls=[control],
        dispositions=[{"source_ref": "context:eneos", "status": "included", "evidence": "ocr"}],
        excluded_records=[],
        read_issues=[],
    )
    assert rows
    assert rows[0]["source_total"] == 0
    assert rows[0]["source_total"] != 48229


# --- 5. OCR解決ゲート ---

def _seed_run(store, *, run_id, project_id, file_id, status, error_code=""):
    store.create_run(
        project_id=project_id, context_file_id=file_id, source_sha256="a" * 64,
        execution_key=run_id, status=status, run_id=run_id, error_code=error_code,
    )
    store.update_run(run_id, status=status, error_code=error_code, finished=True)
    return run_id


def test_ocr_resolution_gate_pass_and_block(tmp_path):
    store = OcrStore(tmp_path / "ocr.db")
    sources = [{
        "id": "cf1", "filename": "fuel.pdf", "quality": "unreadable",
        "requires_ocr": True, "original_available": True,
    }]
    off = evaluate_ocr_resolution_gate(sources, project_id="p", store=store, feature_enabled=False)
    assert off.noop is True
    assert off.blocked is False
    assert off.downstream_values is None or off.reason == "feature_disabled"

    missing = evaluate_ocr_resolution_gate(sources, project_id="p", store=store, feature_enabled=True)
    assert missing.blocked is True
    assert missing.downstream_values is None
    assert any("未実行" in (i.get("message") or "") for i in missing.issues)

    _seed_run(store, run_id="r-pass", project_id="p", file_id="cf1", status="passed")
    ok = evaluate_ocr_resolution_gate(sources, project_id="p", store=store, feature_enabled=True)
    assert ok.blocked is False
    assert "r-pass" in ok.allowed_run_ids
    assert ok.downstream_values is not None

    store2 = OcrStore(tmp_path / "ocr2.db")
    _seed_run(store2, run_id="r-nr", project_id="p", file_id="cf1", status="needs_review")
    blocked_nr = evaluate_ocr_resolution_gate(sources, project_id="p", store=store2, feature_enabled=True)
    assert blocked_nr.blocked is True
    assert blocked_nr.downstream_values is None
    store2.save_review("r-nr", decision="approved", reviewer="human", reviewed_at="2026-05-01T00:00:00Z")
    approved = evaluate_ocr_resolution_gate(sources, project_id="p", store=store2, feature_enabled=True)
    assert approved.blocked is False
    assert "r-nr" in approved.allowed_run_ids

    store3 = OcrStore(tmp_path / "ocr3.db")
    _seed_run(store3, run_id="r-fail", project_id="p", file_id="cf1", status="failed", error_code="OCR_TIMEOUT")
    failed = evaluate_ocr_resolution_gate(sources, project_id="p", store=store3, feature_enabled=True)
    assert failed.blocked is True
    assert failed.downstream_values is None

    store4 = OcrStore(tmp_path / "ocr4.db")
    _seed_run(store4, run_id="r-unapp", project_id="p", file_id="cf1", status="needs_review")
    unapproved = evaluate_ocr_resolution_gate(sources, project_id="p", store=store4, feature_enabled=True)
    assert unapproved.blocked is True
    assert any("未承認" in (i.get("message") or "") or "needs_review" in (i.get("message") or "") for i in unapproved.issues)

    wf = workflow_gate([], project_id="p", feature_enabled=False)
    assert wf.noop is True and wf.blocked is False


# --- 6. 採用 ---

def test_only_passed_or_approved_ocr_enters_normalization():
    payload = experiment_payload(status="passed", run_id="run-ok")
    rows = adopt_ocr_for_normalization(payload, run_id="run-ok", context_file_id="cf-eneos")
    assert rows
    assert all(row["ocr_run_id"] == "run-ok" for row in rows)
    assert all(row["context_file_id"] == "cf-eneos" for row in rows)
    assert all(row.get("ocr_page") and row.get("ocr_block_id") for row in rows)
    assert all(row["name"] not in {"previous_invoice_amount", "payment_amount", "carryover_amount"} for row in rows)

    review_payload = experiment_payload(status="needs_review", run_id="run-nr")
    assert adopt_ocr_for_normalization(review_payload, run_id="run-nr") == []
    approved = adopt_ocr_for_normalization(
        review_payload, run_id="run-nr", review={"decision": "approved"},
    )
    assert approved
    assert adopt_ocr_for_normalization(experiment_payload(status="failed", run_id="run-f"), run_id="run-f") == []
    assert is_ocr_adoptable({"status": "needs_review"}, {"decision": "rejected"}) is False
    assert adoptable_charge_dicts(adapt_ocr_invoice(experiment_payload())) 
    names = {r["name"] for r in adoptable_charge_dicts(adapt_ocr_invoice(experiment_payload()))}
    assert "previous_invoice_amount" not in names
    assert "payment_amount" not in names


# --- 7. flag 無効 ---

def test_feature_flag_disabled_gate_and_adoption_are_noop(monkeypatch):
    monkeypatch.delenv("LOCALSAPORTER_OCR_ENABLED", raising=False)
    from app.capability_registry import ocr_feature_enabled
    from app.vehicle_profit import calculate
    from test_vehicle_auto import ledger
    assert ocr_feature_enabled() is False
    sources = [{
        "id": "cf1", "filename": "fuel.pdf", "quality": "unreadable",
        "requires_ocr": True, "original_available": True,
    }]
    gate = evaluate_ocr_resolution_gate(sources, project_id="p", feature_enabled=None)
    assert gate.noop is True
    assert gate.blocked is False
    data = extract([ledger()], ["2026-01", "2026-02"], True)
    assert [r["profit"] for r in calculate(data)["rows"]] == [620, 1400]
    assert not any(r.get("ocr_run_id") for r in data["records"])


def test_extract_without_ocr_adoptions_matches_legacy_csv():
    from test_vehicle_auto import ledger
    docs = [
        ledger(),
        ({"id": "fuel", "filename": "関東202601請求明細.csv"}, None,
         "給油日付,車番,金額\n20260102,1234,100"),
    ]
    data = extract(docs, ["2026-01"], True)
    fuel = [r for r in data["records"] if r["source_ref"] == "context:fuel"]
    assert len(fuel) == 1 and fuel[0]["amount"] == 100
    assert not any(r.get("ocr_run_id") for r in data["records"])
