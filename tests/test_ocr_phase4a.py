"""OCR-4A: 承認・公開・RAG・API。実OCR/GPU/実PDF/本番経験RAGは使わない。"""
from __future__ import annotations

import io
import json
import threading
import time

import httpx
import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pypdf import PdfWriter

from app.experience_store import ExperienceStore
from app.memory.short_term import ShortTermMemory
from app.ocr_artifacts import generate_manifest, sha256_hex, write_artifact
from app.ocr_client import OcrClient
from app.ocr_invoice_adapter import evaluate_ocr_resolution_gate, is_ocr_adoptable
from app.ocr_review import (
    OCR_RUNTIME,
    adoption_status_of,
    enqueue_ocr_run,
    field_evidence,
    list_ocr_artifacts,
    ocr_rag_lesson,
    publish_ocr_run,
    rag_registered_of,
    register_ocr_rag,
    revoke_ocr_rag,
    run_status_view,
    submit_ocr_review,
)
from app.ocr_schema import OcrErrorCode
from app.ocr_store import OcrStore


def _engine():
    return {
        "pipeline": "PaddleOCR-VL-1.6",
        "vlm_model": "PaddleOCR-VL-1.6-0.9B",
        "layout_model": "PP-DocLayoutV3",
        "image_digest": "sha256:" + "a" * 64,
        "parameters_hash": "b" * 64,
    }


def _block(block_id="p1-b001", text="当月ご請求額 0", bbox=None, label="table"):
    return {
        "block_id": block_id,
        "label": label,
        "bbox": bbox or [10, 20, 30, 40],
        "text": text,
        "confidence": 0.9,
    }


def _field(name, value, *, state="value", page=1, block_id="p1-b001", field_type="decimal", field_id=None, **extra):
    item = {
        "name": name,
        "value": value,
        "type": field_type,
        "page": page,
        "block_id": block_id,
        "value_state": state,
        "evidence_text": extra.pop("evidence_text", f"{name} {value}"),
        "validation": extra.pop("validation", "passed"),
        "field_id": field_id or name,
    }
    item.update(extra)
    return item


def _payload(original, *, status="needs_review", fields=None, pages=None, page_count=1, trigger="unreadable"):
    sha = sha256_hex(original)
    pages = pages if pages is not None else [{
        "page": 1, "width": 1653, "height": 2339,
        "blocks": [
            _block("p1-b001", "前月ご請求額 100"),
            _block("p1-b002", "当月ご入金額 40"),
            _block("p1-b003", "当月お買上額 10"),
            _block("p1-b004", "当月ご請求額 70"),
        ],
    }]
    fields = fields if fields is not None else [
        _field("previous_invoice_amount", "100", block_id="p1-b001"),
        _field("payment_amount", "40", block_id="p1-b002"),
        _field("current_purchase_amount", "10", block_id="p1-b003"),
        _field("current_invoice_amount", "70", block_id="p1-b004"),
        _field("note", "ok", state="value", field_type="string", block_id="p1-b001"),
    ]
    return {
        "schema_version": "ocr-fallback/v1",
        "status": status,
        "trigger": trigger,
        "source": {
            "context_file_id": "cf-1",
            "filename": "invoice.pdf",
            "sha256": sha,
            "page_count": page_count,
        },
        "engine": _engine(),
        "pages": pages,
        "fields": fields,
        "checks": [],
        "review": {"required": status == "needs_review", "decision": None, "reviewer": None, "reviewed_at": None},
    }


def _write_run_artifacts(root, payload, original):
    root.mkdir(parents=True, exist_ok=True)
    write_artifact(root, "ocr_result.json", json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
    write_artifact(root, "ocr_result.md", b"# ocr\n")
    write_artifact(root, "validation.json", json.dumps({"checks": payload.get("checks") or []}, ensure_ascii=False).encode("utf-8"))
    write_artifact(root, "page-1.png", b"png-bytes")
    write_artifact(root, "page-1-layout.png", b"layout-bytes")
    generate_manifest(root)


def _persist_payload(store, run_id, payload):
    for page in payload.get("pages") or []:
        store.save_page(run_id, int(page.get("page") or 1), width=page.get("width"), height=page.get("height"), status="done")
        for block in page.get("blocks") or []:
            store.save_block(
                run_id, int(page.get("page") or 1), str(block.get("block_id")),
                label=str(block.get("label") or "text"), bbox=block.get("bbox"),
                text=str(block.get("text") or ""), confidence=block.get("confidence"),
            )
    for index, item in enumerate(payload.get("fields") or [], 1):
        store.save_field(
            run_id, str(item.get("field_id") or f"f{index:03d}"),
            name=str(item.get("name") or ""),
            value_text=str(item.get("value") or ""),
            value_type=str(item.get("type") or "string"),
            page_no=item.get("page"),
            block_id=str(item.get("block_id") or ""),
            validation_status=str(item.get("validation") or item.get("value_state") or ""),
        )


def seed_run(
    tmp_path,
    *,
    project_id="p1",
    file_id="cf-1",
    status="needs_review",
    original=None,
    fields=None,
    pages=None,
    page_count=1,
    error_code="",
    run_id=None,
):
    original = original or b"%PDF-ocr-4a-seed"
    sha = sha256_hex(original)
    store = OcrStore(tmp_path / "ocr.db")
    rid = run_id or "run-" + sha[:12]
    artifact_root = tmp_path / "artifacts" / rid
    payload = _payload(original, status=status if status in {"passed", "needs_review", "failed", "skipped"} else "needs_review",
                       fields=fields, pages=pages, page_count=page_count)
    payload["run_id"] = rid
    _write_run_artifacts(artifact_root, payload, original)
    run, _ = store.create_run(
        project_id=project_id, context_file_id=file_id, source_sha256=sha,
        status=status, run_id=rid, artifact_root=str(artifact_root),
        error_code=error_code, trigger="unreadable", engine=_engine(),
    )
    store.update_run(rid, status=status, error_code=error_code, finished=True, artifact_root=str(artifact_root))
    _persist_payload(store, rid, payload)
    return store, dict(store.get_run(rid)), original, artifact_root, payload


@pytest.fixture(autouse=True)
def reset_ocr_runtime(tmp_path):
    hold = None
    OCR_RUNTIME.update({
        "client_factory": None,
        "renderer": None,
        "feature_enabled": True,
        "artifact_base": tmp_path / "ocr_jobs",
        "experience_store_factory": None,
        "job_hold": hold,
        "page_count_fn": None,
    })
    yield
    OCR_RUNTIME.update({
        "client_factory": None,
        "renderer": None,
        "feature_enabled": None,
        "artifact_base": None,
        "experience_store_factory": None,
        "job_hold": None,
        "page_count_fn": None,
    })


def _fake_renderer(_data, page):
    return f"PNG-PAGE-{page}".encode("utf-8")


def _client():
    def handler(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={
            "page": 1, "width": 100, "height": 100,
            "blocks": [_block("p1-b001", "ok", [1, 2, 3, 4], "text")],
        })
    return OcrClient(transport=httpx.MockTransport(handler))


def make_pdf_bytes(pages=1):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=72)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _project_with_pdf(tmp_path, original=None, project_name="ocr-p"):
    original = original if original is not None else make_pdf_bytes(1)
    memory = ShortTermMemory(tmp_path / "conversations.db")
    project = memory.create_project(project_name)
    item = memory.add_context_file(
        project["id"], "invoice.pdf", "unreadable", len(original), original,
        "application/pdf", "pdf", "unreadable", sha256_hex(original),
    )
    return memory, project, item, original


# --- 1. OCR要求 ---

@pytest.mark.asyncio
async def test_ocr_request_requires_idempotency_key_and_returns_202(tmp_path, monkeypatch):
    import app.web as web
    memory, project, item, original = _project_with_pdf(tmp_path)
    monkeypatch.setattr(web, "memory", memory)
    hold = threading.Event()
    OCR_RUNTIME["job_hold"] = hold
    OCR_RUNTIME["feature_enabled"] = True
    OCR_RUNTIME["renderer"] = _fake_renderer
    OCR_RUNTIME["client_factory"] = _client

    with pytest.raises(HTTPException) as missing:
        await web.request_context_file_ocr(
            project["id"], item["id"], web.OcrRequestPayload(),
        )
    assert missing.value.status_code == 400
    assert missing.value.detail["error_code"] == "idempotency_required"

    started = time.perf_counter()
    response = await web.request_context_file_ocr(
        project["id"], item["id"], web.OcrRequestPayload(idempotency_key="k-1"),
    )
    elapsed = time.perf_counter() - started
    assert isinstance(response, JSONResponse)
    assert response.status_code == 202
    body = json.loads(response.body)
    assert body["run_id"]
    assert body["ocr_status"] in {"queued", "running"}
    assert elapsed < 1.0
    store = OcrStore(memory.path)
    saved = store.get_run(body["run_id"])
    assert saved["status"] in {"queued", "running"}
    hold.set()


@pytest.mark.asyncio
async def test_duplicate_idempotency_returns_existing_run(tmp_path, monkeypatch):
    import app.web as web
    memory, project, item, original = _project_with_pdf(tmp_path)
    monkeypatch.setattr(web, "memory", memory)
    hold = threading.Event()
    OCR_RUNTIME.update({"job_hold": hold, "feature_enabled": True, "renderer": _fake_renderer, "client_factory": _client})
    first = await web.request_context_file_ocr(
        project["id"], item["id"], web.OcrRequestPayload(idempotency_key="same-key"),
    )
    second = await web.request_context_file_ocr(
        project["id"], item["id"], web.OcrRequestPayload(idempotency_key="same-key"),
    )
    a = json.loads(first.body)
    b = json.loads(second.body)
    assert a["run_id"] == b["run_id"]
    assert b["duplicate"] is True
    store = OcrStore(memory.path)
    with store.connect() as db:
        count = db.execute("SELECT COUNT(*) AS n FROM ocr_runs").fetchone()["n"]
    assert count == 1
    hold.set()


@pytest.mark.asyncio
async def test_feature_flag_disabled_rejects_ocr_start_but_get_is_allowed(tmp_path, monkeypatch):
    import app.web as web
    memory, project, item, original = _project_with_pdf(tmp_path)
    monkeypatch.setattr(web, "memory", memory)
    store, run, original, root, payload = seed_run(tmp_path, project_id=project["id"], file_id=item["id"])
    monkeypatch.setattr(web, "_ocr_store", lambda: store)
    OCR_RUNTIME["feature_enabled"] = False
    with pytest.raises(HTTPException) as err:
        await web.request_context_file_ocr(
            project["id"], item["id"], web.OcrRequestPayload(idempotency_key="nope"),
        )
    assert err.value.status_code == 409
    assert err.value.detail["error_code"] == "feature_disabled"
    view = await web.get_ocr_run(project["id"], run["run_id"])
    assert view["run_id"] == run["run_id"]
    assert view["ocr_status"] == "needs_review"


# --- 2. GET 状態/成果物/根拠 ---

@pytest.mark.asyncio
async def test_get_status_artifacts_evidence_and_boundaries(tmp_path, monkeypatch):
    import app.web as web
    memory, project, item, original = _project_with_pdf(tmp_path)
    other = memory.create_project("other")
    monkeypatch.setattr(web, "memory", memory)
    store, run, original, root, payload = seed_run(
        tmp_path, project_id=project["id"], file_id=item["id"],
    )
    monkeypatch.setattr(web, "_ocr_store", lambda: store)

    view = await web.get_ocr_run(project["id"], run["run_id"])
    assert view["ocr_status"] == "needs_review"
    assert view["adoption_status"] == "unapproved"
    assert view["rag_registered"] is False
    assert "trigger" in view
    assert "error_code" in view

    artifacts = await web.get_ocr_artifacts(project["id"], run["run_id"])
    names = {row["name"] for row in artifacts["files"]}
    assert "ocr_result.json" in names
    assert all(row.get("sha256") and row.get("size") for row in artifacts["files"])

    evidence = await web.get_ocr_evidence(project["id"], run["run_id"], "current_invoice_amount")
    assert evidence["page"] == 1
    assert evidence["block_id"] == "p1-b004"
    assert evidence["bbox"] == [10, 20, 30, 40]
    assert evidence["evidence_text"]

    with pytest.raises(HTTPException) as cross:
        await web.get_ocr_run(other["id"], run["run_id"])
    assert cross.value.status_code == 404

    with pytest.raises(HTTPException) as missing:
        await web.get_ocr_evidence(project["id"], run["run_id"], "no-such-field")
    assert missing.value.status_code == 404

    manifest_path = root / "manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["files"].append({"name": "../secret.txt", "sha256": "0" * 64, "size": 1})
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(HTTPException) as traversal:
        await web.get_ocr_artifacts(project["id"], run["run_id"])
    assert traversal.value.status_code in {400, 409}


def test_list_artifacts_rejects_absolute_and_dotdot(tmp_path):
    store, run, original, root, payload = seed_run(tmp_path)
    from app.ocr_review import OcrApiError
    manifest_path = root / "manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["files"] = [{"name": "/tmp/ocr_result.json", "sha256": "0" * 64, "size": 1}]
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(OcrApiError) as err:
        list_ocr_artifacts(run, artifact_root=root)
    assert err.value.status_code in {400, 409}


# --- 3. レビュー ---

def test_review_approve_correct_reject_and_signature(tmp_path):
    store, run, original, root, payload = seed_run(tmp_path)
    approved = submit_ocr_review(
        store, project_id="p1", run_id=run["run_id"], decision="approve",
        reviewer="alice", reason="原本の請求額を確認した", original_bytes=original,
    )
    assert approved["decision"] == "approved"
    assert approved["signature"]
    review = store.get_review(run["run_id"])
    assert review["signature"] == approved["signature"]
    assert review["reviewer"] == "alice"
    assert adoption_status_of(store.get_run(run["run_id"]), review) == "approved"

    store2, run2, original2, root2, payload2 = seed_run(tmp_path / "c", run_id="run-correct")
    original_value = store2.list_fields(run2["run_id"])
    invoice = next(item for item in original_value if item["name"] == "current_invoice_amount")
    corrected = submit_ocr_review(
        store2, project_id="p1", run_id=run2["run_id"], decision="correct_and_approve",
        reviewer="bob", reason="請求額の桁を確認", original_bytes=original2,
        corrected_values={"current_invoice_amount": "70"},
        correction_reason="印字どおり70円",
    )
    assert corrected["decision"] == "correct_and_approve"
    still = store2.list_fields(run2["run_id"])
    after = next(item for item in still if item["name"] == "current_invoice_amount")
    assert after["value_text"] == invoice["value_text"]
    saved_review = store2.get_review(run2["run_id"])
    corr = json.loads(saved_review["corrected_values_json"])
    assert corr["current_invoice_amount"] == "70"
    assert saved_review["evidence"] == "印字どおり70円"

    store3, run3, original3, *_ = seed_run(tmp_path / "r", run_id="run-reject")
    rejected = submit_ocr_review(
        store3, project_id="p1", run_id=run3["run_id"], decision="reject",
        reviewer="carol", reason="帳票が対象外", original_bytes=original3,
    )
    assert rejected["adoption_status"] == "rejected"
    assert is_ocr_adoptable(store3.get_run(run3["run_id"]), store3.get_review(run3["run_id"])) is False

    from app.ocr_review import OcrApiError
    with pytest.raises(OcrApiError) as missing_reviewer:
        submit_ocr_review(
            store, project_id="p1", run_id=run["run_id"], decision="approve",
            reviewer="", reason="理由だけ", original_bytes=original,
        )
    assert missing_reviewer.value.status_code == 422


# --- 4. 承認不可 ---

def test_failed_hash_tamper_schema_missing_page_cannot_approve_or_publish(tmp_path):
    from app.ocr_review import OcrApiError
    store, run, original, root, payload = seed_run(tmp_path, status="failed", error_code="OCR_TIMEOUT")
    with pytest.raises(OcrApiError) as failed:
        submit_ocr_review(
            store, project_id="p1", run_id=run["run_id"], decision="approve",
            reviewer="alice", reason="確認した", original_bytes=original,
        )
    assert failed.value.status_code == 409

    store_h, run_h, original_h, *_ = seed_run(tmp_path / "h", run_id="run-hash")
    with pytest.raises(OcrApiError) as mismatch:
        submit_ocr_review(
            store_h, project_id="p1", run_id=run_h["run_id"], decision="approve",
            reviewer="alice", reason="確認した", original_bytes=b"%PDF-replaced",
        )
    assert mismatch.value.error_code == OcrErrorCode.OCR_SOURCE_HASH_MISMATCH.value

    store_t, run_t, original_t, root_t, _ = seed_run(tmp_path / "t", run_id="run-tamper")
    (root_t / "ocr_result.json").write_bytes((root_t / "ocr_result.json").read_bytes() + b"x")
    with pytest.raises(OcrApiError) as tamper:
        submit_ocr_review(
            store_t, project_id="p1", run_id=run_t["run_id"], decision="approve",
            reviewer="alice", reason="確認した", original_bytes=original_t,
        )
    assert tamper.value.error_code == OcrErrorCode.OCR_ARTIFACT_TAMPERED.value

    store_s, run_s, original_s, root_s, payload_s = seed_run(tmp_path / "s", run_id="run-schema")
    bad = dict(payload_s)
    bad.pop("schema_version")
    write_artifact(root_s, "ocr_result.json", json.dumps(bad).encode("utf-8"))
    generate_manifest(root_s)
    with pytest.raises(OcrApiError) as schema:
        submit_ocr_review(
            store_s, project_id="p1", run_id=run_s["run_id"], decision="approve",
            reviewer="alice", reason="確認した", original_bytes=original_s,
        )
    assert schema.value.error_code == OcrErrorCode.OCR_SCHEMA_INVALID.value

    pages = [{"page": 1, "width": 10, "height": 10, "blocks": [_block()]}]
    store_p, run_p, original_p, *_ = seed_run(
        tmp_path / "p", run_id="run-page", pages=pages, page_count=2,
    )
    with pytest.raises(OcrApiError) as missing_page:
        submit_ocr_review(
            store_p, project_id="p1", run_id=run_p["run_id"], decision="approve",
            reviewer="alice", reason="確認した", original_bytes=original_p,
        )
    assert missing_page.value.error_code == OcrErrorCode.OCR_PAGE_RENDER_FAILED.value

    store_ok, run_ok, original_ok, root_ok, _ = seed_run(tmp_path / "ok", run_id="run-ok")
    submit_ocr_review(
        store_ok, project_id="p1", run_id=run_ok["run_id"], decision="approve",
        reviewer="alice", reason="確認した", original_bytes=original_ok,
    )
    (root_ok / "ocr_result.md").write_bytes(b"tampered-after-approve")
    with pytest.raises(OcrApiError) as publish_tamper:
        publish_ocr_run(
            store_ok, project_id="p1", run_id=run_ok["run_id"], original_bytes=original_ok,
        )
    assert publish_tamper.value.error_code == OcrErrorCode.OCR_ARTIFACT_TAMPERED.value


# --- 5. 訂正で検算回避不可 ---

def test_correction_cannot_bypass_totals_formula_or_vehicle(tmp_path):
    from app.ocr_review import OcrApiError
    store, run, original, *_ = seed_run(tmp_path)
    with pytest.raises(OcrApiError) as total:
        submit_ocr_review(
            store, project_id="p1", run_id=run["run_id"], decision="correct_and_approve",
            reviewer="alice", reason="合計を直す", original_bytes=original,
            corrected_values={"current_invoice_amount": "71"},
            correction_reason="請求額を71にした",
        )
    assert total.value.error_code == OcrErrorCode.OCR_TOTAL_MISMATCH.value

    fields = [
        _field("previous_invoice_amount", "100", block_id="p1-b001"),
        _field("payment_amount", "40", block_id="p1-b002"),
        _field("current_purchase_amount", "10", block_id="p1-b003"),
        _field("current_invoice_amount", "70", block_id="p1-b004"),
        _field("vehicle_number", "1234", field_type="string", block_id="p1-b001", used_for_vehicle_cost=True),
    ]
    store_v, run_v, original_v, *_ = seed_run(tmp_path / "v", fields=fields, run_id="run-veh")
    with pytest.raises(OcrApiError) as vehicle:
        submit_ocr_review(
            store_v, project_id="p1", run_id=run_v["run_id"], decision="correct_and_approve",
            reviewer="alice", reason="車番不明", original_bytes=original_v,
            corrected_values={"vehicle_number": {"value": "", "value_state": "blank"}},
            correction_reason="車番が読めない",
        )
    assert vehicle.value.error_code == OcrErrorCode.OCR_FIELD_AMBIGUOUS.value


# --- 6. 公開 ---

def test_publish_only_approved_and_gate_respects_review(tmp_path):
    from app.ocr_review import OcrApiError
    store, run, original, *_ = seed_run(tmp_path)
    with pytest.raises(OcrApiError) as unapproved:
        publish_ocr_run(store, project_id="p1", run_id=run["run_id"], original_bytes=original)
    assert unapproved.value.status_code == 409

    submit_ocr_review(
        store, project_id="p1", run_id=run["run_id"], decision="approve",
        reviewer="alice", reason="確認した", original_bytes=original,
    )
    published = publish_ocr_run(store, project_id="p1", run_id=run["run_id"], original_bytes=original)
    assert published["adoption_status"] == "published"
    assert store.get_run(run["run_id"])["published_at"]

    store_r, run_r, original_r, *_ = seed_run(tmp_path / "rej", run_id="run-rej", status="needs_review")
    submit_ocr_review(
        store_r, project_id="p1", run_id=run_r["run_id"], decision="reject",
        reviewer="alice", reason="対象外", original_bytes=original_r,
    )
    with pytest.raises(OcrApiError) as rejected:
        publish_ocr_run(store_r, project_id="p1", run_id=run_r["run_id"], original_bytes=original_r)
    assert rejected.value.status_code == 409

    store_f, run_f, original_f, *_ = seed_run(tmp_path / "fail", run_id="run-fail", status="failed", error_code="OCR_TIMEOUT")
    with pytest.raises(OcrApiError):
        publish_ocr_run(store_f, project_id="p1", run_id=run_f["run_id"], original_bytes=original_f)

    sources = [{
        "id": "cf-1", "filename": "fuel.pdf", "quality": "unreadable",
        "requires_ocr": True, "original_available": True,
    }]
    gate_ok = evaluate_ocr_resolution_gate(sources, project_id="p1", store=store, feature_enabled=True)
    assert gate_ok.blocked is False
    assert run["run_id"] in gate_ok.allowed_run_ids
    gate_rej = evaluate_ocr_resolution_gate(sources, project_id="p1", store=store_r, feature_enabled=True)
    assert gate_rej.blocked is True
    gate_un = evaluate_ocr_resolution_gate(
        sources, project_id="p1", store=OcrStore(tmp_path / "empty.db"), feature_enabled=True,
    )
    store_u, run_u, *_ = seed_run(tmp_path / "u", run_id="run-unapp")
    gate_unapp = evaluate_ocr_resolution_gate(sources, project_id="p1", store=store_u, feature_enabled=True)
    assert gate_unapp.blocked is True


# --- 7. RAG ---

def test_rag_requires_approval_and_explicit_confirm(tmp_path):
    from app.ocr_review import OcrApiError
    store, run, original, *_ = seed_run(tmp_path)
    exp = ExperienceStore(tmp_path / "experience.sqlite3")
    with pytest.raises(OcrApiError) as before:
        register_ocr_rag(
            store, project_id="p1", run_id=run["run_id"], original_bytes=original,
            confirm_rag=True, reviewer="alice", reason="再利用する", experience_store=exp,
        )
    assert before.value.status_code == 409
    assert exp.list("p1") == []

    submit_ocr_review(
        store, project_id="p1", run_id=run["run_id"], decision="approve",
        reviewer="alice", reason="確認した", original_bytes=original,
    )
    with pytest.raises(OcrApiError) as default:
        register_ocr_rag(
            store, project_id="p1", run_id=run["run_id"], original_bytes=original,
            confirm_rag=False, reviewer="alice", reason="再利用する", experience_store=exp,
        )
    assert default.value.error_code == "rag_not_confirmed"
    assert exp.list("p1") == []

    registered = register_ocr_rag(
        store, project_id="p1", run_id=run["run_id"], original_bytes=original,
        confirm_rag=True, reviewer="alice", reason="帳票型と検算手順を残す", experience_store=exp,
    )
    assert registered["rag_registered"] is True
    rows = exp.list("p1")
    assert len(rows) == 1
    assert "48229" not in rows[0]["content"]
    assert "70" not in json.dumps(json.loads(rows[0]["content"]).get("field_names", []))
    evidence = json.loads(rows[0]["evidence"])
    assert evidence["source_sha256"]
    assert evidence["manifest_hash"]
    assert evidence["review_signature"]

    store_r, run_r, original_r, *_ = seed_run(tmp_path / "rr", run_id="run-rag-rej")
    submit_ocr_review(
        store_r, project_id="p1", run_id=run_r["run_id"], decision="reject",
        reviewer="alice", reason="対象外", original_bytes=original_r,
    )
    with pytest.raises(OcrApiError):
        register_ocr_rag(
            store_r, project_id="p1", run_id=run_r["run_id"], original_bytes=original_r,
            confirm_rag=True, reviewer="alice", reason="登録", experience_store=exp,
        )

    store_f, run_f, original_f, *_ = seed_run(tmp_path / "rf", run_id="run-rag-fail", status="failed", error_code="OCR_TIMEOUT")
    with pytest.raises(OcrApiError):
        register_ocr_rag(
            store_f, project_id="p1", run_id=run_f["run_id"], original_bytes=original_f,
            confirm_rag=True, reviewer="alice", reason="登録", experience_store=exp,
        )

    store_t, run_t, original_t, root_t, _ = seed_run(tmp_path / "rt", run_id="run-rag-tamper")
    submit_ocr_review(
        store_t, project_id="p1", run_id=run_t["run_id"], decision="approve",
        reviewer="alice", reason="確認した", original_bytes=original_t,
    )
    (root_t / "ocr_result.json").write_bytes((root_t / "ocr_result.json").read_bytes() + b"x")
    with pytest.raises(OcrApiError) as tampered:
        register_ocr_rag(
            store_t, project_id="p1", run_id=run_t["run_id"], original_bytes=original_t,
            confirm_rag=True, reviewer="alice", reason="登録", experience_store=exp,
        )
    assert tampered.value.error_code == OcrErrorCode.OCR_ARTIFACT_TAMPERED.value

    revoked = revoke_ocr_rag(
        store, project_id="p1", run_id=run["run_id"], reviewer="alice", reason="原本差替",
        experience_store=exp,
    )
    assert revoked["rag_revoked"] is True
    assert rag_registered_of(store.get_run(run["run_id"])) is False
    assert exp.list("p1", True) == []


def test_experience_memory_register_approved_ocr_entry(tmp_path):
    from app.experience_memory import ExperienceMemory, register_approved_ocr
    memory = ExperienceMemory(tmp_path / "experience_memory", {})
    rid = register_approved_ocr(
        memory, "p1",
        json.dumps({"kind": "ocr_approved_procedure", "field_names": ["current_invoice_amount"]}),
        {"ocr_run_id": "r1", "source_sha256": "a" * 64, "manifest_hash": "b" * 64, "review_signature": "c" * 64},
        "alice", "証跡を確認した",
    )
    assert rid
    assert memory.store.list("p1", True)


# --- 8. 状態分離 ---

def test_status_fields_are_separate(tmp_path):
    store, run, original, *_ = seed_run(tmp_path)
    view = run_status_view(store, project_id="p1", run_id=run["run_id"])
    assert view["ocr_status"] == "needs_review"
    assert view["adoption_status"] == "unapproved"
    assert view["rag_registered"] is False
    submit_ocr_review(
        store, project_id="p1", run_id=run["run_id"], decision="approve",
        reviewer="alice", reason="確認した", original_bytes=original,
    )
    view2 = run_status_view(store, project_id="p1", run_id=run["run_id"])
    assert view2["ocr_status"] == "needs_review"
    assert view2["adoption_status"] == "approved"
    assert view2["rag_registered"] is False
    publish_ocr_run(store, project_id="p1", run_id=run["run_id"], original_bytes=original)
    view3 = run_status_view(store, project_id="p1", run_id=run["run_id"])
    assert view3["ocr_status"] == "needs_review"
    assert view3["adoption_status"] == "published"
    assert view3["rag_registered"] is False


def test_correct_and_approve_is_adoptable_like_approved():
    assert is_ocr_adoptable({"status": "needs_review"}, {"decision": "correct_and_approve"}) is True
    assert is_ocr_adoptable({"status": "needs_review"}, {"decision": "rejected"}) is False
    assert is_ocr_adoptable({"status": "failed"}, {"decision": "approved"}) is True


def test_ocr_rag_lesson_omits_raw_amounts():
    lesson = json.loads(ocr_rag_lesson(
        {"trigger": "garbled", "source_sha256": "a" * 64, "engine": json.dumps(_engine())},
        {"decision": "approved", "signature": "sig"},
        [{"check_id": "VAL-05", "status": "passed"}],
        ["current_invoice_amount"],
    ))
    blob = json.dumps(lesson)
    assert "48229" not in blob
    assert lesson["field_names"] == ["current_invoice_amount"]
    assert lesson["kind"] == "ocr_approved_procedure"


# --- ページ数は原本から求める ---

@pytest.mark.asyncio
async def test_ocr_request_uses_actual_pdf_page_count_not_default_one(tmp_path, monkeypatch):
    import app.web as web
    from app import ocr_review as review_mod
    original = make_pdf_bytes(3)
    memory, project, item, _ = _project_with_pdf(tmp_path, original=original)
    monkeypatch.setattr(web, "memory", memory)
    hold = threading.Event()
    OCR_RUNTIME.update({"job_hold": hold, "feature_enabled": True, "renderer": _fake_renderer, "client_factory": _client})
    captured = {}
    real_enqueue = review_mod.enqueue_ocr_run

    def wrapping(*args, **kwargs):
        captured["page_count"] = kwargs.get("page_count")
        captured["pages_quality"] = list(kwargs.get("pages_quality") or [])
        return real_enqueue(*args, **kwargs)

    monkeypatch.setattr(review_mod, "enqueue_ocr_run", wrapping)
    response = await web.request_context_file_ocr(
        project["id"], item["id"], web.OcrRequestPayload(idempotency_key="pages-3"),
    )
    assert isinstance(response, JSONResponse)
    assert response.status_code == 202
    assert captured["page_count"] == 3
    pages = [row["page"] for row in captured["pages_quality"]]
    assert pages == [1, 2, 3]
    assert all(row["quality"] == "unreadable" for row in captured["pages_quality"])
    hold.set()


@pytest.mark.asyncio
async def test_claimed_page_count_mismatch_is_rejected(tmp_path, monkeypatch):
    import app.web as web
    original = make_pdf_bytes(3)
    memory, project, item, _ = _project_with_pdf(tmp_path, original=original)
    monkeypatch.setattr(web, "memory", memory)
    OCR_RUNTIME["feature_enabled"] = True
    with pytest.raises(HTTPException) as err:
        await web.request_context_file_ocr(
            project["id"], item["id"],
            web.OcrRequestPayload(idempotency_key="mismatch", page_count=1),
        )
    assert err.value.status_code == 409
    assert err.value.detail["error_code"] == OcrErrorCode.OCR_SCHEMA_INVALID.value


@pytest.mark.asyncio
async def test_unreadable_original_does_not_fallback_to_one_page(tmp_path, monkeypatch):
    import app.web as web
    memory, project, item, _ = _project_with_pdf(tmp_path, original=b"not-a-pdf")
    monkeypatch.setattr(web, "memory", memory)
    OCR_RUNTIME["feature_enabled"] = True
    with pytest.raises(HTTPException) as err:
        await web.request_context_file_ocr(
            project["id"], item["id"], web.OcrRequestPayload(idempotency_key="bad-pdf"),
        )
    assert err.value.status_code == 422
    assert err.value.detail["error_code"] == OcrErrorCode.OCR_PAGE_RENDER_FAILED.value


@pytest.mark.asyncio
async def test_more_than_500_pages_requires_range_selection(tmp_path, monkeypatch):
    import app.web as web
    memory, project, item, _ = _project_with_pdf(tmp_path, original=make_pdf_bytes(1))
    monkeypatch.setattr(web, "memory", memory)
    OCR_RUNTIME["feature_enabled"] = True
    OCR_RUNTIME["page_count_fn"] = lambda _data: 501
    with pytest.raises(HTTPException) as err:
        await web.request_context_file_ocr(
            project["id"], item["id"], web.OcrRequestPayload(idempotency_key="too-many"),
        )
    assert err.value.status_code == 409
    assert err.value.detail["error_code"] == OcrErrorCode.OCR_SCHEMA_INVALID.value
    assert "500" in err.value.detail["message"]
