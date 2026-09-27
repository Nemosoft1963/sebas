"""OCR-1 Phase 1: スキーマ・証跡・クライアント・レジストリ・compose。実OCR/実GPUは使わない。"""
from pathlib import Path

import httpx
import pytest
import yaml

from app.capability_registry import (
    OCR_CAPABILITY_IDS,
    OCR_FEATURE_FLAG_ENV,
    CapabilityRegistry,
    DEFINITIONS,
    PDF_TEXT_CONSTRAINT,
    ocr_compose_present,
    ocr_feature_enabled,
    ocr_runtime_validated,
    resolve_capability_id,
)
from app.context_files import ContextExtraction, extract_context_file
from app.ocr_artifacts import (
    generate_manifest,
    make_execution_key,
    render_pdf_page,
    resolve_artifact_path,
    sha256_hex,
    verify_manifest,
    verify_source_hash,
    write_artifact,
)
from app.ocr_client import DEFAULT_BASE_URL, OcrClient, OcrClientConfigError, assert_local_ocr_url
from app.ocr_schema import (
    FOLLOW_UP_KIND,
    OCR_ERROR_CODES,
    FollowUpKind,
    OcrErrorCode,
    classify_field_state,
    field_as_zero_amount,
    follow_up_for,
    follow_up_kind,
    make_ocr_field,
    validate_ocr_result,
)

SPEC_VLM_DIGEST = "sha256:5713fd30ab76094b7b6a20d95fd8e26fa9dc452bcc90ccb16f1fb056bd2a0f4d"
SPEC_DOC_PARSER_DIGEST = "sha256:ad0b1f056a76967f9191cd06398e8babb21b49a4673a28c3de5fd31f481884db"
ROOT = Path(__file__).resolve().parents[1]


def valid_ocr_payload(**overrides):
    payload = {
        "schema_version": "ocr-fallback/v1",
        "status": "needs_review",
        "trigger": "garbled",
        "source": {
            "context_file_id": "cf-1",
            "filename": "invoice.pdf",
            "sha256": "a" * 64,
            "page_count": 1,
        },
        "engine": {
            "pipeline": "PaddleOCR-VL-1.6",
            "vlm_model": "PaddleOCR-VL-1.6-0.9B",
            "layout_model": "PP-DocLayoutV3",
            "image_digest": SPEC_VLM_DIGEST,
            "parameters_hash": "b" * 64,
        },
        "pages": [{
            "page": 1,
            "width": 1653,
            "height": 2339,
            "blocks": [{
                "block_id": "p1-b001",
                "label": "table",
                "bbox": [126, 1118, 1590, 2241],
                "text": "当月ご請求額 0",
                "confidence": 0.91,
            }],
        }],
        "fields": [{
            "name": "current_invoice_amount",
            "value": "0",
            "type": "decimal",
            "page": 1,
            "block_id": "p1-b023",
            "value_state": "zero",
            "evidence_text": "当月ご請求額 0",
            "validation": "passed",
        }],
        "checks": [],
        "review": {
            "required": True,
            "decision": None,
            "reviewer": None,
            "reviewed_at": None,
        },
    }
    payload.update(overrides)
    return payload


def test_valid_ocr_result_passes_schema():
    assert validate_ocr_result(valid_ocr_payload()) == []


@pytest.mark.parametrize("status", ["passed", "needs_review", "failed", "skipped"])
def test_allowed_status_values(status):
    assert validate_ocr_result(valid_ocr_payload(status=status)) == []


@pytest.mark.parametrize("trigger", [
    "garbled", "unreadable", "low_text_density", "table_required", "manual",
])
def test_allowed_trigger_values(trigger):
    assert validate_ocr_result(valid_ocr_payload(trigger=trigger)) == []


@pytest.mark.parametrize("field,value", [("status", "ok"), ("trigger", "auto")])
def test_status_and_trigger_out_of_range_are_schema_invalid(field, value):
    errors = validate_ocr_result(valid_ocr_payload(**{field: value}))
    assert errors
    assert all(item.code == OcrErrorCode.OCR_SCHEMA_INVALID.value for item in errors)


def test_invalid_bbox_is_schema_invalid():
    payload = valid_ocr_payload()
    payload["pages"][0]["blocks"][0]["bbox"] = [1, 2, 3]
    assert validate_ocr_result(payload)[0].code == "OCR_SCHEMA_INVALID"
    payload["pages"][0]["blocks"][0]["bbox"] = [1, 2, 3, "x"]
    assert any(item.code == "OCR_SCHEMA_INVALID" for item in validate_ocr_result(payload))


@pytest.mark.parametrize("confidence", [-0.01, 1.01, "high"])
def test_confidence_out_of_range_is_schema_invalid(confidence):
    payload = valid_ocr_payload()
    payload["pages"][0]["blocks"][0]["confidence"] = confidence
    errors = validate_ocr_result(payload)
    assert errors and all(item.code == "OCR_SCHEMA_INVALID" for item in errors)


def test_field_requires_page_and_block_id():
    payload = valid_ocr_payload()
    payload["fields"][0].pop("page")
    payload["fields"][0].pop("block_id")
    paths = {item.path for item in validate_ocr_result(payload)}
    assert "fields[0].page" in paths
    assert "fields[0].block_id" in paths


def test_missing_required_keys_are_schema_invalid():
    errors = validate_ocr_result({"status": "passed"})
    assert any(item.path == "schema_version" for item in errors)
    assert all(item.code == "OCR_SCHEMA_INVALID" for item in errors)


def test_blank_zero_unreadable_are_distinct_and_unreadable_is_not_zero():
    blank = make_ocr_field("fuel", "", page=1, block_id="p1-b001")
    zero = make_ocr_field("fuel", "0", page=1, block_id="p1-b001")
    unreadable = make_ocr_field("fuel", None, page=1, block_id="p1-b001", unreadable=True)
    unreadable_from_zero = make_ocr_field("fuel", "0", page=1, block_id="p1-b001", unreadable=True)
    assert blank.value_state == "blank"
    assert zero.value_state == "zero"
    assert unreadable.value_state == "unreadable"
    assert classify_field_state("0") == "zero"
    assert field_as_zero_amount(zero) is True
    assert field_as_zero_amount(blank) is False
    assert field_as_zero_amount(unreadable) is False
    assert field_as_zero_amount(unreadable_from_zero) is False
    assert unreadable_from_zero.value != "0"
    payload = valid_ocr_payload(fields=[{
        "name": "fuel",
        "value": "0",
        "type": "decimal",
        "page": 1,
        "block_id": "p1-b001",
        "value_state": "unreadable",
    }])
    assert any(item.code == "OCR_SCHEMA_INVALID" for item in validate_ocr_result(payload))


def test_error_codes_and_follow_up_table():
    assert len(OCR_ERROR_CODES) == 12
    assert follow_up_kind("OCR_NOT_REQUIRED") is FollowUpKind.SKIP
    assert follow_up_kind(OcrErrorCode.OCR_SOURCE_HASH_MISMATCH) is FollowUpKind.FORCE_STOP
    assert follow_up_kind(OcrErrorCode.OCR_TIMEOUT) is FollowUpKind.NEEDS_REVIEW
    assert follow_up_kind(OcrErrorCode.OCR_SCHEMA_INVALID) is FollowUpKind.REJECT_RESULT
    assert follow_up_kind(OcrErrorCode.OCR_REVIEW_REQUIRED) is FollowUpKind.FORBID_PUBLISH
    for code in OCR_ERROR_CODES:
        if code == "OCR_NOT_REQUIRED":
            continue
        row = follow_up_for(code)
        assert row["kind"] == FOLLOW_UP_KIND[code].value
        assert row["label"]


def test_source_hash_match_and_mismatch():
    data = b"%PDF-1.4 fake original"
    digest = sha256_hex(data)
    assert verify_source_hash(data, digest) == []
    issues = verify_source_hash(data, "0" * 64)
    assert issues[0].code == "OCR_SOURCE_HASH_MISMATCH"
    assert follow_up_kind(issues[0].code) is FollowUpKind.FORCE_STOP


def test_manifest_generate_verify_and_tamper(tmp_path):
    write_artifact(tmp_path, "ocr_result.json", b'{"ok":true}')
    write_artifact(tmp_path, "ocr_result.md", b"# ocr\n")
    write_artifact(tmp_path, "page-1.png", b"png-bytes")
    write_artifact(tmp_path, "page-1-layout.png", b"layout-bytes")
    write_artifact(tmp_path, "validation.json", b'{"checks":[]}')
    generate_manifest(tmp_path)
    assert verify_manifest(tmp_path) == []
    target = tmp_path / "ocr_result.json"
    target.write_bytes(target.read_bytes() + b"x")
    issues = verify_manifest(tmp_path)
    assert issues[0].code == "OCR_ARTIFACT_TAMPERED"


@pytest.mark.parametrize("relative", [
    "../secret.json",
    "..\\secret.json",
    "/tmp/ocr_result.json",
    "C:/Users/ocr_result.json",
    "subdir/ocr_result.json",
])
def test_artifact_path_traversal_rejected(tmp_path, relative):
    with pytest.raises(ValueError):
        resolve_artifact_path(tmp_path, relative)


def test_execution_key_stable_and_changes_with_inputs():
    key = make_execution_key("A" * 64, "PaddleOCR-VL-1.6", "p" * 64)
    same = make_execution_key("a" * 64, "PaddleOCR-VL-1.6", "sha256:" + "p" * 64)
    assert key == same
    assert key != make_execution_key("b" * 64, "PaddleOCR-VL-1.6", "p" * 64)
    assert key != make_execution_key("a" * 64, "PaddleOCR-VL-1.6", "q" * 64)


def test_default_renderer_returns_page_render_failed():
    outcome = render_pdf_page(b"%PDF", 1)
    assert outcome.ok is False
    assert outcome.error_code == "OCR_PAGE_RENDER_FAILED"
    fake = render_pdf_page(b"%PDF", 1, renderer=lambda data, page: b"PNG")
    assert fake.ok is True


def test_client_health_200_and_non_200():
    def handler(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404)

    with OcrClient(transport=httpx.MockTransport(handler)) as client:
        assert client.health().ok is True

    def bad(request):
        return httpx.Response(503)

    with OcrClient(transport=httpx.MockTransport(bad)) as client:
        down = client.health()
    assert down.ok is False
    assert down.error_code == "OCR_SERVICE_UNAVAILABLE"


def test_client_connection_refused_is_unavailable():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    with OcrClient(transport=httpx.MockTransport(handler)) as client:
        result = client.health()
        recognize = client.recognize_page(b"png")
    assert result.error_code == "OCR_SERVICE_UNAVAILABLE"
    assert recognize.error_code == "OCR_SERVICE_UNAVAILABLE"
    assert recognize.retried is True


def test_client_timeout_is_ocr_timeout():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with OcrClient(transport=httpx.MockTransport(handler)) as client:
        health = client.health()
        page = client.recognize_page(b"png")
    assert health.error_code == "OCR_TIMEOUT"
    assert page.error_code == "OCR_TIMEOUT"


def test_client_retries_transient_once_only():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"text": "ok"})

    with OcrClient(transport=httpx.MockTransport(handler)) as client:
        result = client.recognize_page(b"png")
    assert result.ok is True and result.retried is True and calls["n"] == 2

    calls["n"] = 0

    def always_fail(request):
        calls["n"] += 1
        return httpx.Response(503)

    with OcrClient(transport=httpx.MockTransport(always_fail)) as client:
        failed = client.recognize_page(b"png")
    assert failed.ok is False
    assert failed.retried is True
    assert calls["n"] == 2


def test_client_does_not_retry_poor_recognition():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={"text": "", "quality": "bad"})

    with OcrClient(transport=httpx.MockTransport(handler)) as client:
        result = client.recognize_page(b"png")
    assert result.ok is True
    assert result.retried is False
    assert calls["n"] == 1


def test_client_rejects_non_loopback_hosts():
    with pytest.raises(OcrClientConfigError):
        OcrClient("http://8.8.8.8:18118")
    with pytest.raises(OcrClientConfigError):
        assert_local_ocr_url("http://example.com:18118")
    assert assert_local_ocr_url(DEFAULT_BASE_URL) == DEFAULT_BASE_URL


def test_ocr_capabilities_registered_configured_not_validated(monkeypatch):
    monkeypatch.delenv(OCR_FEATURE_FLAG_ENV, raising=False)
    assert ocr_feature_enabled() is False
    checks = CapabilityRegistry().check(list(OCR_CAPABILITY_IDS), [])
    assert [item["capability_id"] for item in checks] == list(OCR_CAPABILITY_IDS)
    for item in checks:
        assert item["configured"] is True
        assert item["validated"] is False
        assert item["state"] == "unavailable"
        assert item["reason_code"] == "feature_disabled"
    assert ocr_runtime_validated() is False
    monkeypatch.setenv(OCR_FEATURE_FLAG_ENV, "true")
    for item in CapabilityRegistry().check(list(OCR_CAPABILITY_IDS), []):
        assert item["state"] == "configured"
        assert item["validated"] is False


def test_ocr_image_presence_does_not_validate():
    assert ocr_compose_present() is True
    assert ocr_runtime_validated() is False
    assert CapabilityRegistry().check(["source.ocr_pdf_fallback"], [])[0]["validated"] is False


def test_extract_table_alias_and_pdf_text_note_unchanged():
    assert resolve_capability_id("source.extract_table") == "source.extract_pdf_table"
    checks = CapabilityRegistry().check(
        ["source.extract_table", "source.extract_pdf_text", "source.extract_pdf_table"],
        [],
    )
    assert checks[0]["state"] == "unavailable"
    assert checks[0]["reason_code"] == "unsupported_operation"
    assert checks[1]["constraints"] == PDF_TEXT_CONSTRAINT
    assert "OCR・一般的な表復元は保証しない" in DEFINITIONS["source.extract_pdf_text"][2]
    assert checks[2]["validated"] is False


def test_context_extraction_optional_fields_default_and_existing_call():
    extracted = extract_context_file("note.txt", b"hello")
    assert extracted.content == "hello"
    assert extracted.quality is None
    assert extracted.page_count is None
    assert extracted.requires_ocr is None
    assert extracted.ocr_reason_codes is None
    assert extracted.extraction_run_id is None
    legacy = ContextExtraction("body", "text", "text/plain", "note")
    assert legacy.quality is None
    assert legacy.page_count is None
    assert legacy.requires_ocr is None
    assert legacy.ocr_reason_codes is None
    assert legacy.extraction_run_id is None


def test_ocr_compose_definition_is_localhost_only_with_spec_digests():
    path = ROOT / "docker-compose.ocr.yml"
    text = path.read_text(encoding="utf-8")
    assert SPEC_VLM_DIGEST in text
    assert SPEC_DOC_PARSER_DIGEST in text
    data = yaml.safe_load(text)
    mounts = []
    for name in ("paddleocr-vlm", "paddleocr-doc-parser"):
        service = data["services"][name]
        assert "ocr" in (service.get("profiles") or [])
        assert service["network_mode"] == "service:web"
        assert "ports" not in service
        mounts.extend(str(volume) for volume in service.get("volumes") or [])
        devices = (
            ((service.get("deploy") or {}).get("resources") or {})
            .get("reservations") or {}
        ).get("devices") or []
        assert devices, name
    joined = "\n".join(mounts).lower()
    assert "docker.sock" not in joined
    assert "c:/" not in joined
    assert "/users" not in joined
