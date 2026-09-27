"""宇佐美OCR補完と failed 再実行。実OCR/GPU/実PDF/実在の会計情報は使わない。"""
from __future__ import annotations

import threading

import pytest

from app.ocr_invoice_adapter import is_ocr_adoptable
from app.ocr_review import MAX_OCR_ATTEMPTS, OCR_RUNTIME, OcrApiError, enqueue_ocr_run
from app.ocr_store import OcrStore
from app.ocr_usami_adapter import (
    adoptable_usami_blocks,
    extract_usami_blocks,
    parse_table_rows,
    usami_adoptions_from_ocr,
)
from app.vehicle_auto import Extractor, adopt_ocr_for_normalization, apply_ocr_adoption, extract
from test_ocr_phase3 import experiment_payload
from test_vehicle_auto import ledger


TABLE_9901_A = (
    "<table><tr><td>お客様名</td><td colspan=\"6\">テスト運輸（有）</td><td colspan=\"2\">御中</td></tr>"
    "<tr><td>伝票番号</td><td>車番</td><td>月</td><td>日</td><td>給油所名</td><td>代行</td>"
    "<td colspan=\"2\">商品名</td><td>数量</td><td>単価</td><td colspan=\"2\">金額</td></tr>"
    "<tr><td>1111</td><td>9901</td><td>1</td><td>13</td><td>東京TT</td><td>自</td>"
    "<td>軽油</td><td>100</td><td>800</td><td>80000</td></tr>"
    "<tr><td>2222</td><td>9901</td><td>1</td><td>20</td><td>東京TT</td><td>自</td>"
    "<td>軽油</td><td>50</td><td>800</td><td>40000</td></tr>"
    "<tr><td colspan=\"2\">小計</td><td>120000</td></tr>"
    "<tr><td colspan=\"2\">参考消費税（10%）</td><td>(12000)</td></tr>"
    "<tr><td colspan=\"3\">【消費税対象外商品】</td></tr></table>"
)
TABLE_9901_B = (
    "<table><tr><td colspan=\"2\">軽油引取税</td><td>150</td><td>321</td><td>48150</td></tr>"
    "<tr><td colspan=\"2\">車番：東京 999あ9901</td></tr>"
    "<tr><td>品名</td><td>無鉛ハイオク</td><td>レギュラー</td><td>軽油</td><td>灯油</td>"
    "<td>自動車用潤滑油</td><td>合計</td><td>168150</td></tr>"
    "<tr><td>合計</td><td></td><td></td><td>150000</td></tr></table>"
)
TABLE_9902 = (
    "<table>"
    "<tr><td>3333</td><td>9902</td><td>1</td><td>5</td><td>東京TT</td><td>自</td>"
    "<td>軽油</td><td>60</td><td>800</td><td>48000</td></tr>"
    "<tr><td colspan=\"2\">小計</td><td>48000</td></tr>"
    "<tr><td colspan=\"2\">参考消費税（10%）</td><td>(4800)</td></tr>"
    "<tr><td colspan=\"2\">軽油引取税</td><td>50</td><td>321</td><td>16050</td></tr>"
    "<tr><td colspan=\"2\">車番：東京 999あ9902</td></tr>"
    "<tr><td>品名</td><td>無鉛ハイオク</td><td>レギュラー</td><td>軽油</td><td>灯油</td>"
    "<td>自動車用潤滑油</td><td>合計</td><td>64050</td></tr>"
    "<tr><td>合計</td><td></td><td></td><td>60000</td></tr></table>"
)

PARTIAL_TEXT_9901 = (
    "00001 9901 1\n品目\n軽油引取税 0\n参考消費税 (0)\n自動車用潤滑油 100\n"
)
COMPLETE_TEXT_9902 = (
    "00002 9902 1\n品目\n小計 48000\n軽油引取税 16050\n参考消費税 (4800)\n自動車用潤滑油 64050\n"
)
COMPLETE_TEXT_9901 = (
    "00001 9901 1\n品目\n小計 100\n軽油引取税 0\n参考消費税 (0)\n自動車用潤滑油 100\n"
)


def _block(block_id, html, confidence=0.9, label="table"):
    return {
        "block_id": block_id,
        "label": label,
        "bbox": [10, 20, 30, 40],
        "text": html,
        "confidence": confidence,
    }


def usami_payload(*, status="needs_review", run_id="run-usami", blocks=None, confidence=0.9):
    rows = blocks if blocks is not None else [
        _block("p1-b001", TABLE_9901_A, confidence),
        _block("p1-b002", TABLE_9901_B, confidence),
        _block("p1-b003", TABLE_9902, confidence),
    ]
    return {
        "schema_version": "ocr-fallback/v1",
        "status": status,
        "trigger": "unreadable",
        "run_id": run_id,
        "source": {
            "context_file_id": "cf-usami",
            "filename": "燃料/202601/宇佐美_関東.pdf",
            "sha256": "a" * 64,
            "page_count": 1,
        },
        "engine": {},
        "pages": [{"page": 1, "width": 100, "height": 100, "blocks": rows}],
        "fields": [],
        "checks": [],
        "review": {"required": True, "decision": None, "reviewer": None, "reviewed_at": None},
    }


def approved_review():
    return {"decision": "approved", "reviewer": "alice"}


def adoption_bundle(payload, review):
    return {"payload": payload, "run_id": payload.get("run_id") or "run-usami", "review": review}


def test_html_parser_yields_rows_and_skips_empty_cells():
    rows = parse_table_rows(TABLE_9901_B)
    cells = [[c.strip() for c in row if str(c).strip()] for row in rows]
    assert cells[0][0] == "軽油引取税"
    assert cells[0][-1] == "48150"
    assert any(c.startswith("車番") for row in cells for c in row)
    assert ["品名", "無鉛ハイオク", "レギュラー", "軽油", "灯油", "自動車用潤滑油", "合計", "168150"] in cells
    assert cells[-1][0] == "合計"
    assert "150000" in cells[-1]


def test_us01_extracts_from_html_tables_not_line_amounts():
    garbled = TABLE_9901_A.replace("80000", "99999").replace("<td>100</td><td>800</td>", "<td>あ</td><td>?</td>")
    payload = usami_payload(blocks=[
        _block("p1-b001", garbled),
        _block("p1-b002", TABLE_9901_B),
        _block("p1-b003", TABLE_9902),
    ])
    blocks = extract_usami_blocks(payload, run_id="run-usami")
    by_plate = {item.plate4: item for item in blocks}
    assert set(by_plate) == {"9901", "9902"}
    first = by_plate["9901"]
    assert first.subtotal == 120000
    assert first.diesel_tax == 48150
    assert first.tax == 12000
    assert first.printed_total == 168150
    assert first.identity_ok is True
    assert first.adopted is True
    assert first.charged_amount == 180150
    assert first.printed_total != 150000
    second = by_plate["9902"]
    assert second.subtotal == 48000
    assert second.diesel_tax == 16050
    assert second.printed_total == 64050
    assert second.identity_ok is True
    assert second.adopted is True
    assert second.charged_amount == 68850


def test_us02_rejects_identity_gap_mismatch_duplicate_low_confidence():
    identity = usami_payload(blocks=[
        _block("p1-b001", TABLE_9901_A),
        _block("p1-b002", TABLE_9901_B.replace("168150", "168151")),
    ])
    bad_identity = extract_usami_blocks(identity)[0]
    assert bad_identity.identity_ok is False
    assert bad_identity.adopted is False
    assert bad_identity.reason_code == "identity"
    assert bad_identity.adopted is False

    missing = usami_payload(blocks=[
        _block("p1-b001", TABLE_9901_A.replace('<tr><td colspan="2">小計</td><td>120000</td></tr>', "")),
        _block("p1-b002", TABLE_9901_B),
    ])
    missing_block = extract_usami_blocks(missing)[0]
    assert missing_block.adopted is False
    assert missing_block.subtotal is None
    assert missing_block.reason_code == "missing_subtotal"

    non_numeric = usami_payload(blocks=[
        _block("p1-b001", TABLE_9901_A.replace("120000", "不明")),
        _block("p1-b002", TABLE_9901_B),
    ])
    unread = extract_usami_blocks(non_numeric)[0]
    assert unread.adopted is False
    assert unread.subtotal is None
    assert unread.reason_code in {"non_numeric", "missing_subtotal"}

    mismatch = usami_payload(blocks=[
        _block("p1-b001", TABLE_9901_A),
        _block("p1-b002", TABLE_9901_B.replace("9901", "8801")),
    ])
    plate_bad = extract_usami_blocks(mismatch)[0]
    assert plate_bad.adopted is False
    assert plate_bad.reason_code == "plate_mismatch"

    dup = usami_payload(blocks=[
        _block("p1-b001", TABLE_9901_A),
        _block("p1-b002", TABLE_9901_B),
        _block("p1-b003", TABLE_9901_A),
        _block("p1-b004", TABLE_9901_B),
    ])
    dups = extract_usami_blocks(dup)
    assert len(dups) == 2
    assert all(item.reason_code == "duplicate_plate" for item in dups)
    assert all(item.adopted is False for item in dups)

    low = usami_payload(confidence=0.1)
    low_blocks = extract_usami_blocks(low)
    assert all(item.adopted is False for item in low_blocks)
    assert all(item.reason_code == "low_confidence" for item in low_blocks)


def _fuel_source():
    return {"id": "u", "filename": "燃料/202601/宇佐美_関東.pdf"}


def test_us03_approved_ocr_fills_missing_text_block():
    payload = usami_payload(status="needs_review")
    adoptions = usami_adoptions_from_ocr({
        "u": adoption_bundle(payload, approved_review()),
    })
    assert "9901" in adoptions["u"]
    ex = Extractor(["2026-01"])
    ex.usami_ocr_adoptions = adoptions
    text_layer = PARTIAL_TEXT_9901 + COMPLETE_TEXT_9902
    result = ex.fuel(_fuel_source(), text_layer)
    assert not any(i.get("reason_code") == "partial_block" for i in result.issues)
    amounts = [r["amount"] for r in ex.records]
    assert 180150 in amounts
    assert 68850 in amounts
    ocr_row = next(r for r in ex.records if r["amount"] == 180150)
    assert ocr_row.get("ocr_run_id") == "run-usami"
    assert ocr_row.get("ocr_block_id")
    assert "ocr:" in ocr_row["source_locator"]
    controls = [c for c in ex.source_controls if c.get("origin") == "source_total"]
    assert controls
    assert controls[0]["amount"] == 180150 + 68850
    assert controls[0]["extraction_method"] == "usami_block_printed_plus_tax:v1"


def test_us04_passed_only_failed_and_identity_fail_do_not_fill():
    text_layer = PARTIAL_TEXT_9901 + COMPLETE_TEXT_9902
    payload = usami_payload(status="passed")
    none_review = usami_adoptions_from_ocr({"u": adoption_bundle(payload, None)})
    assert none_review == {}
    passed_only = usami_adoptions_from_ocr({
        "u": adoption_bundle(payload, {"decision": None}),
    })
    assert passed_only == {}

    ex = Extractor(["2026-01"])
    ex.usami_ocr_adoptions = usami_adoptions_from_ocr({
        "u": adoption_bundle(payload, None),
    })
    result = ex.fuel(_fuel_source(), text_layer)
    assert any(i.get("reason_code") == "partial_block" for i in result.issues)
    assert not any(r.get("amount") == 180150 for r in ex.records)

    failed_payload = usami_payload(status="failed")
    failed = usami_adoptions_from_ocr({
        "u": adoption_bundle(failed_payload, approved_review()),
    })
    assert failed == {}

    identity = usami_payload(blocks=[
        _block("p1-b001", TABLE_9901_A),
        _block("p1-b002", TABLE_9901_B.replace("168150", "1")),
        _block("p1-b003", TABLE_9902),
    ])
    identity_adopt = usami_adoptions_from_ocr({
        "u": adoption_bundle(identity, approved_review()),
    })
    assert "9901" not in identity_adopt.get("u", {})
    ex2 = Extractor(["2026-01"])
    ex2.usami_ocr_adoptions = identity_adopt
    result2 = ex2.fuel(_fuel_source(), text_layer)
    assert any(i.get("reason_code") == "partial_block" for i in result2.issues)


def test_us05_complete_text_block_is_not_overwritten():
    payload = usami_payload(status="needs_review")
    ex = Extractor(["2026-01"])
    ex.usami_ocr_adoptions = usami_adoptions_from_ocr({
        "u": adoption_bundle(payload, approved_review()),
    })
    result = ex.fuel(_fuel_source(), COMPLETE_TEXT_9901)
    assert result.records_added == 1
    assert ex.records[0]["amount"] == 100
    assert ex.records[0].get("ocr_run_id") is None
    assert not any(r.get("amount") == 180150 for r in ex.records)


def test_us06_invoice_ocr_adoption_unchanged():
    assert is_ocr_adoptable({"status": "passed"}, None) is True
    assert is_ocr_adoptable({"status": "needs_review"}, {"decision": "approved"}) is True
    assert is_ocr_adoptable({"status": "needs_review"}, None) is False
    payload = experiment_payload(status="passed", run_id="run-ok")
    rows = adopt_ocr_for_normalization(payload, run_id="run-ok", context_file_id="cf-eneos")
    assert rows
    assert all(row["name"] not in {"previous_invoice_amount", "payment_amount"} for row in rows)
    ex = Extractor(["2026-05"])
    source = {"id": "f", "filename": "燃料/202605/ENEOS_関東.pdf"}
    applied = apply_ocr_adoption(ex, source, "", {
        "payload": payload, "run_id": "run-ok", "review": None,
    })
    assert applied is not None


def test_us03_extract_pipeline_records_ocr_fill():
    payload = usami_payload(status="needs_review")
    docs = [
        ledger(),
        (_fuel_source(), None, PARTIAL_TEXT_9901 + COMPLETE_TEXT_9902),
    ]
    data = extract(
        docs, ["2026-01"], True,
        ocr_adoptions={"u": adoption_bundle(payload, approved_review())},
    )
    fuel = [r for r in data["records"] if r["source_ref"] == "context:u"]
    assert any(r["amount"] == 180150 and r.get("ocr_run_id") == "run-usami" for r in fuel)
    assert not any(
        i.get("reason_code") == "partial_block" for i in data["auto_extraction"]["issues"]
        if i.get("source_ref") == "context:u"
    )
    assert any(
        c.get("source_ref") == "context:u" and c.get("origin") == "source_total" and c.get("amount") == 180150 + 68850
        and c.get("extraction_method") == "usami_block_printed_plus_tax:v1"
        for c in data["source_controls"]
    )


def _seed_run(store, *, run_id, project_id, file_id, status, key, execution_key=""):
    store.create_run(
        project_id=project_id, context_file_id=file_id, source_sha256="a" * 64,
        status=status, run_id=run_id, idempotency_key=key, execution_key=execution_key or run_id,
        error_code="OCR_TIMEOUT" if status == "failed" else "",
    )
    store.update_run(run_id, status=status, finished=True, error_code="OCR_TIMEOUT" if status == "failed" else "")


def _tax_variant_payload(subtotal="120000", tax="(12000)", diesel="48150", printed="168150", include_tax_row=True):
    tax_row = f'<tr><td colspan="2">参考消費税（10%）</td><td>{tax}</td></tr>' if include_tax_row else ""
    table_a = (
        "<table>"
        "<tr><td>1111</td><td>9901</td><td>1</td><td>13</td><td>東京TT</td><td>自</td>"
        "<td>軽油</td><td>100</td><td>800</td><td>80000</td></tr>"
        f'<tr><td colspan="2">小計</td><td>{subtotal}</td></tr>'
        f"{tax_row}"
        "</table>"
    )
    table_b = (
        "<table>"
        f'<tr><td colspan="2">軽油引取税</td><td>150</td><td>321</td><td>{diesel}</td></tr>'
        "<tr><td colspan=\"2\">車番：東京 999あ9901</td></tr>"
        f'<tr><td>品名</td><td>無鉛ハイオク</td><td>レギュラー</td><td>軽油</td><td>灯油</td>'
        f'<td>自動車用潤滑油</td><td>合計</td><td>{printed}</td></tr>'
        "<tr><td>合計</td><td></td><td></td><td>150000</td></tr></table>"
    )
    return usami_payload(blocks=[_block("p1-b001", table_a), _block("p1-b002", table_b)])


def test_usami_ocr_tax_must_be_readable_and_ten_percent():
    # (a) 括弧だけで数値欠落 → tax_unreadable。通常経路の欠落は partial_block のまま。
    unread = extract_usami_blocks(_tax_variant_payload(tax="("))[0]
    assert unread.adopted is False
    assert unread.tax is None
    assert unread.charged_amount is None
    assert unread.reason_code == "tax_unreadable"
    ex = Extractor(["2026-01"])
    ex.usami_ocr_adoptions = usami_adoptions_from_ocr({
        "u": adoption_bundle(_tax_variant_payload(tax="("), approved_review()),
    })
    result = ex.fuel(_fuel_source(), PARTIAL_TEXT_9901)
    assert any(i.get("reason_code") == "partial_block" for i in result.issues)
    assert not any(r.get("ocr_run_id") for r in ex.records)

    # (b) 小計120000に対し 120001 / 1200 は 10% 不整合。
    for bad_tax in ("120001", "1200"):
        inconsistent = extract_usami_blocks(_tax_variant_payload(tax=bad_tax))[0]
        assert inconsistent.adopted is False
        assert inconsistent.reason_code == "tax_inconsistent"
        assert inconsistent.charged_amount is None

    # (c) 120000 に対し 12000 は採用。計上は 小計+軽油+税。
    ok = extract_usami_blocks(_tax_variant_payload(tax="(12000)"))[0]
    assert ok.adopted is True
    assert ok.tax == 12000
    assert ok.charged_amount == 120000 + 48150 + 12000

    # (d) 参考消費税の行が無い → tax_missing。
    missing = extract_usami_blocks(_tax_variant_payload(include_tax_row=False))[0]
    assert missing.adopted is False
    assert missing.tax is None
    assert missing.charged_amount is None
    assert missing.reason_code == "tax_missing"

    # (e) 端数 ±1。floor(113019*0.10)=11301。11301 は採用、11303 は不採用。
    edge_ok = extract_usami_blocks(_tax_variant_payload(
        subtotal="113019", tax="11301", diesel="100", printed="113119",
    ))[0]
    assert edge_ok.adopted is True
    assert edge_ok.tax == 11301
    assert edge_ok.charged_amount == 113019 + 100 + 11301
    edge_low = extract_usami_blocks(_tax_variant_payload(
        subtotal="113019", tax="11300", diesel="100", printed="113119",
    ))[0]
    assert edge_low.adopted is True
    edge_high = extract_usami_blocks(_tax_variant_payload(
        subtotal="113019", tax="11302", diesel="100", printed="113119",
    ))[0]
    assert edge_high.adopted is True
    edge_out = extract_usami_blocks(_tax_variant_payload(
        subtotal="113019", tax="11303", diesel="100", printed="113119",
    ))[0]
    assert edge_out.adopted is False
    assert edge_out.reason_code == "tax_inconsistent"
    assert edge_out.charged_amount is None


def test_usami_ocr_token_scan_handles_split_cells_and_identity_oracle():
    detail = "<tr><td>1111</td><td>9901</td><td>1</td><td>13</td><td>東京TT</td><td>自</td><td>軽油</td><td>10</td><td>800</td><td>8000</td></tr>"
    product_close = (
        "<tr><td>品名</td><td>無鉛ハイオク</td><td>レギュラー</td><td>軽油</td><td>灯油</td>"
        "<td>自動車用潤滑油</td><td>合計</td><td>168150</td></tr>"
        "<tr><td>合計</td><td></td><td></td><td>150000</td></tr>"
    )
    plate_fullwidth = "<tr><td colspan=\"2\">車番：東京 999あ9901</td></tr>"

    # (a) 参考消費税の数値が別セル。括弧が閉じない / 欠ける → 最初の数値 12000。
    split_tax = (
        f"<table>{detail}<tr><td colspan=\"2\">小計</td><td>120000</td></tr>"
        "<tr><td>参考消費税(10%)</td><td>(</td><td>12000</td></tr></table>"
    )
    diesel_std = (
        f"<table><tr><td colspan=\"2\">軽油引取税</td><td>150</td><td>321</td><td>48150</td></tr>"
        f"{plate_fullwidth}{product_close}</table>"
    )
    split_ok = extract_usami_blocks(usami_payload(blocks=[
        _block("p1-b001", split_tax), _block("p1-b002", diesel_std),
    ]))[0]
    assert split_ok.adopted is True
    assert split_ok.tax == 12000
    assert split_ok.diesel_tax == 48150
    assert split_ok.charged_amount == 120000 + 48150 + 12000

    missing_paren = (
        f"<table>{detail}<tr><td colspan=\"2\">小計</td><td>120000</td></tr>"
        "<tr><td>参考消費税（10%）</td><td>(</td><td>12000)</td></tr></table>"
    )
    missing_close = extract_usami_blocks(usami_payload(blocks=[
        _block("p1-b001", missing_paren), _block("p1-b002", diesel_std),
    ]))[0]
    assert missing_close.adopted is True
    assert missing_close.tax == 12000

    # (a') 数値が欠落 → tax_unreadable。
    tax_empty = extract_usami_blocks(usami_payload(blocks=[
        _block("p1-b001", f"<table>{detail}<tr><td colspan=\"2\">小計</td><td>120000</td></tr>"
               "<tr><td>参考消費税(10%)</td><td>(</td></tr></table>"),
        _block("p1-b002", diesel_std),
    ]))[0]
    assert tax_empty.adopted is False
    assert tax_empty.reason_code == "tax_unreadable"

    # (b) 1セルに改行で複数項目。数値は後続セル。
    combined = (
        f"<table>{detail}<tr><td colspan=\"2\">小計</td><td>120000</td></tr>"
        "<tr><td>参考消費税(10%)\n【消費税対象外商品】\n軽油引取税\n車番:足立 999か9901</td>"
        "<td>(</td><td>12000)</td><td>150</td><td>321</td><td>48150</td></tr>"
        f"{product_close}</table>"
    )
    combined_block = extract_usami_blocks(usami_payload(blocks=[_block("p1-b001", combined)]))[0]
    assert combined_block.adopted is True
    assert combined_block.tax == 12000
    assert combined_block.diesel_tax == 48150
    assert combined_block.labeled_plate4 == "9901"
    assert combined_block.charged_amount == 180150

    # (c) 軽油引取税が5セル / 金額が無く expected が周辺に無い → identity。
    diesel_five = (
        f"<table><tr><td colspan=\"2\">軽油引取税</td><td>150</td><td>32</td><td>1</td><td>4815</td></tr>"
        f"{plate_fullwidth}{product_close}</table>"
    )
    five = extract_usami_blocks(usami_payload(blocks=[
        _block("p1-b001", f"<table>{detail}<tr><td colspan=\"2\">小計</td><td>120000</td></tr>"
               "<tr><td colspan=\"2\">参考消費税（10%）</td><td>(12000)</td></tr></table>"),
        _block("p1-b002", diesel_five),
    ]))[0]
    assert five.adopted is False
    assert five.reason_code == "identity"

    diesel_no_amount = (
        f"<table><tr><td colspan=\"2\">軽油引取税</td><td>99999</td><td>99</td></tr>"
        f"{plate_fullwidth}{product_close}</table>"
    )
    no_amt = extract_usami_blocks(usami_payload(blocks=[
        _block("p1-b001", f"<table>{detail}<tr><td colspan=\"2\">小計</td><td>120000</td></tr>"
               "<tr><td colspan=\"2\">参考消費税（10%）</td><td>(12000)</td></tr></table>"),
        _block("p1-b002", diesel_no_amount),
    ]))[0]
    assert no_amt.adopted is False
    assert no_amt.reason_code == "identity"

    # (c') ラベル無しで expected==0 なら diesel=0 で採用。expected>0 は missing_diesel。
    zero_diesel = (
        f"<table>{detail}<tr><td colspan=\"2\">小計</td><td>48000</td></tr>"
        "<tr><td colspan=\"2\">参考消費税(10%)</td><td>(4800)</td></tr>"
        "<tr><td colspan=\"2\">車番:東京 999あ9901</td></tr>"
        "<tr><td>品名</td><td>無鉛ハイオク</td><td>レギュラー</td><td>軽油</td><td>灯油</td>"
        "<td>自動車用潤滑油</td><td>合計</td><td>48000</td></tr>"
        "<tr><td>合計</td><td></td><td></td><td>40000</td></tr></table>"
    )
    zero_ok = extract_usami_blocks(usami_payload(blocks=[_block("p1-b001", zero_diesel)]))[0]
    assert zero_ok.adopted is True
    assert zero_ok.diesel_tax == 0
    assert zero_ok.charged_amount == 48000 + 0 + 4800

    missing_diesel = (
        f"<table>{detail}<tr><td colspan=\"2\">小計</td><td>120000</td></tr>"
        "<tr><td colspan=\"2\">参考消費税（10%）</td><td>(12000)</td></tr>"
        f"{plate_fullwidth}{product_close}</table>"
    )
    miss_d = extract_usami_blocks(usami_payload(blocks=[_block("p1-b001", missing_diesel)]))[0]
    assert miss_d.adopted is False
    assert miss_d.reason_code == "missing_diesel"

    # (d) 空白・全角半角の揺れ。
    mixed = (
        f"<table>{detail}<tr><td colspan=\"2\">小 計</td><td>120000</td></tr>"
        "<tr><td>参考消費税（10%）</td><td>(12000)</td></tr>"
        "<tr><td>軽 油</td><td>10</td></tr></table>"
    )
    mixed_b = (
        "<table><tr><td colspan=\"2\">軽油引取税</td><td>150</td><td>321</td><td>48150</td></tr>"
        "<tr><td colspan=\"2\">車番:東京 999あ9901</td></tr>"
        f"{product_close}</table>"
    )
    mixed_block = extract_usami_blocks(usami_payload(blocks=[
        _block("p1-b001", mixed), _block("p1-b002", mixed_b),
    ]))[0]
    assert mixed_block.adopted is True
    assert mixed_block.labeled_plate4 == "9901"
    assert mixed_block.tax == 12000

    # (e) 周辺に expected があれば採用、無ければ不採用。
    oracle_hit = extract_usami_blocks(usami_payload(blocks=[
        _block("p1-b001", f"<table>{detail}<tr><td colspan=\"2\">小計</td><td>120000</td></tr>"
               "<tr><td colspan=\"2\">参考消費税（10%）</td><td>(12000)</td></tr></table>"),
        _block("p1-b002", diesel_std),
    ]))[0]
    assert oracle_hit.adopted is True
    assert oracle_hit.diesel_tax == 48150
    oracle_miss = extract_usami_blocks(usami_payload(blocks=[
        _block("p1-b001", f"<table>{detail}<tr><td colspan=\"2\">小計</td><td>120000</td></tr>"
               "<tr><td colspan=\"2\">参考消費税（10%）</td><td>(12000)</td></tr></table>"),
        _block("p1-b002", f"<table><tr><td colspan=\"2\">軽油引取税</td><td>150</td><td>321</td><td>48000</td></tr>"
               f"{plate_fullwidth}{product_close}</table>"),
    ]))[0]
    assert oracle_miss.adopted is False
    assert oracle_miss.reason_code == "identity"


def test_rt01_retry_failed_only_with_new_key_and_limit(tmp_path):
    store = OcrStore(tmp_path / "ocr.db")
    OCR_RUNTIME.update({
        "feature_enabled": True,
        "page_count_fn": lambda _data: 1,
        "artifact_base": tmp_path / "ocr_jobs",
        "job_hold": threading.Event(),
        "client_factory": None,
        "renderer": None,
    })
    original = b"%PDF-usami-retry"
    _seed_run(store, run_id="old-fail", project_id="p1", file_id="cf1", status="failed", key="k-old")

    same_key, dup, _code = enqueue_ocr_run(
        store, project_id="p1", context_file_id="cf1", original_bytes=original,
        expected_sha256="a" * 64, filename="invoice.pdf", idempotency_key="k-old",
        feature_enabled=True,
    )
    assert dup is True
    assert same_key["run_id"] == "old-fail"

    new_view, new_dup, new_code = enqueue_ocr_run(
        store, project_id="p1", context_file_id="cf1", original_bytes=original,
        expected_sha256="a" * 64, filename="invoice.pdf", idempotency_key="k-retry-1",
        feature_enabled=True,
    )
    assert new_dup is False
    assert new_code == 202
    assert new_view["run_id"] != "old-fail"
    assert store.get_run("old-fail")["status"] == "failed"

    store2 = OcrStore(tmp_path / "ocr2.db")
    _seed_run(store2, run_id="passed-1", project_id="p1", file_id="cf1", status="passed", key="k-pass")
    passed_view, passed_dup, _ = enqueue_ocr_run(
        store2, project_id="p1", context_file_id="cf1", original_bytes=original,
        expected_sha256="a" * 64, filename="invoice.pdf", idempotency_key="k-new",
        feature_enabled=True,
    )
    assert passed_dup is True
    assert passed_view["run_id"] == "passed-1"

    store3 = OcrStore(tmp_path / "ocr3.db")
    _seed_run(store3, run_id="nr-1", project_id="p1", file_id="cf1", status="needs_review", key="k-nr")
    nr_view, nr_dup, _ = enqueue_ocr_run(
        store3, project_id="p1", context_file_id="cf1", original_bytes=original,
        expected_sha256="a" * 64, filename="invoice.pdf", idempotency_key="k-nr-new",
        feature_enabled=True,
    )
    assert nr_dup is True
    assert nr_view["run_id"] == "nr-1"

    store4 = OcrStore(tmp_path / "ocr4.db")
    for index in range(MAX_OCR_ATTEMPTS):
        _seed_run(
            store4, run_id=f"fail-{index}", project_id="p1", file_id="cf1",
            status="failed", key=f"k-f-{index}", execution_key=f"exec-{index}",
        )
    with pytest.raises(OcrApiError) as err:
        enqueue_ocr_run(
            store4, project_id="p1", context_file_id="cf1", original_bytes=original,
            expected_sha256="a" * 64, filename="invoice.pdf", idempotency_key="k-too-many",
            feature_enabled=True,
        )
    assert err.value.status_code == 409
    assert err.value.error_code == "ocr_retry_limit"
    OCR_RUNTIME["job_hold"].set()
    OCR_RUNTIME.update({
        "feature_enabled": None, "page_count_fn": None, "artifact_base": None,
        "job_hold": None, "client_factory": None, "renderer": None,
    })
