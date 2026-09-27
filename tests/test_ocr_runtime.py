"""OCR Phase 5-A: 実サービスアダプター、描画、起動配線、compose。"""
from __future__ import annotations

import io
import json
import sys
import types
from pathlib import Path

import httpx
import pytest
import yaml
from pypdf import PdfWriter

from app.ocr_artifacts import render_pdf_page, sha256_hex
from app.ocr_client import OcrClientConfigError
from app.ocr_fallback import normalize_recognition, run_ocr_fallback
from app.ocr_invoice_adapter import is_ocr_adoptable
from app.ocr_review import OCR_RUNTIME
from app.ocr_runtime import PaddleXLayoutClient, install_default_runtime, render_page_png
from app.ocr_schema import OcrErrorCode
from app.ocr_store import OcrStore

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "paddlex_layout_parsing_sample.json"
BASE_COMPOSE_HASH = "664f570d709ae94516fb49a08cc48208450ea30d"


def _fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _client(handler):
    return PaddleXLayoutClient(transport=httpx.MockTransport(handler))


def _pdf(pages=1):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=72)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def test_oa01_health_success_unavailable_and_timeout():
    with _client(lambda request: httpx.Response(200, json={"status": "ok"})) as client:
        assert client.health().ok is True
    with _client(lambda request: httpx.Response(503)) as client:
        down = client.health()
    assert down.error_code == OcrErrorCode.OCR_SERVICE_UNAVAILABLE.value

    def connection(request):
        raise httpx.ConnectError("refused", request=request)

    with _client(connection) as client:
        assert client.health().error_code == OcrErrorCode.OCR_SERVICE_UNAVAILABLE.value

    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    with _client(timeout) as client:
        assert client.health().error_code == OcrErrorCode.OCR_TIMEOUT.value


def test_oa02_fixture_maps_blocks_labels_html_bbox_and_confidence():
    captured = {}

    def handler(request):
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=_fixture())

    with _client(handler) as client:
        outcome = client.recognize_page(b"\x89PNG", page=7)
    assert outcome.ok is True
    assert captured["fileType"] == 1 and captured["visualize"] is False
    assert set(captured) == {"file", "fileType", "visualize"}
    data = outcome.data
    assert data["width"] == 1191.0 and data["height"] == 1684.0
    assert len(data["blocks"]) == 9
    assert [item["block_id"] for item in data["blocks"]] == [f"p7-b{i:03d}" for i in range(1, 10)]
    assert data["blocks"][0]["label"] == "title"
    assert data["blocks"][1]["label"] == "text"
    assert data["blocks"][2]["label"] == "table"
    assert data["blocks"][2]["text"].startswith("<table>")
    assert all(isinstance(item["text"], str) for item in data["blocks"])
    assert all(len(item["bbox"]) == 4 and all(isinstance(x, float) for x in item["bbox"]) for item in data["blocks"])
    assert all("confidence" in item for item in data["blocks"])
    assert data["blocks"][0]["confidence"] == pytest.approx(0.7203338742256165)
    mapped = normalize_recognition(data, page_no=7)
    assert len(mapped["blocks"]) == 9


def test_oa02_unmatched_detection_confidence_is_zero():
    payload = _fixture()
    payload["result"]["layoutParsingResults"][0]["prunedResult"]["layout_det_res"]["boxes"] = []
    with _client(lambda request: httpx.Response(200, json=payload)) as client:
        result = client.recognize_page(b"png")
    assert result.ok and all(block["confidence"] == 0.0 for block in result.data["blocks"])


@pytest.mark.parametrize("payload", [
    {"errorCode": 9, "errorMsg": "failed"},
    {"errorCode": 0, "result": {}},
    {"errorCode": 0, "result": {"layoutParsingResults": [{"prunedResult": {"width": 1, "height": 1}}]}},
])
def test_oa03_invalid_service_responses_fail(payload):
    with _client(lambda request: httpx.Response(200, json=payload)) as client:
        outcome = client.recognize_page(b"png")
    assert outcome.ok is False
    assert outcome.error_code == OcrErrorCode.OCR_LAYOUT_FAILED.value


def test_oa03_non_json_fails_and_5xx_retries_once():
    with _client(lambda request: httpx.Response(200, text="not-json")) as client:
        assert client.recognize_page(b"png").error_code == OcrErrorCode.OCR_LAYOUT_FAILED.value
    calls = {"n": 0}

    def unavailable(request):
        calls["n"] += 1
        return httpx.Response(503)

    with _client(unavailable) as client:
        result = client.recognize_page(b"png")
    assert not result.ok and result.retried is True and calls["n"] == 2
    assert result.error_code == OcrErrorCode.OCR_SERVICE_UNAVAILABLE.value


def test_oa03_zero_blocks_is_success():
    payload = _fixture()
    pruned = payload["result"]["layoutParsingResults"][0]["prunedResult"]
    pruned["parsing_res_list"] = []
    with _client(lambda request: httpx.Response(200, json=payload)) as client:
        result = client.recognize_page(b"png")
    assert result.ok is True and result.data["blocks"] == []


@pytest.mark.parametrize("url", [
    "http://example.com", "http://192.168.0.5:8080", "http://user:pass@localhost:8080",
])
def test_oa04_non_loopback_or_credentials_rejected(url):
    with pytest.raises(OcrClientConfigError):
        PaddleXLayoutClient(url)


def test_oa05_install_runtime_disabled_enabled_preserves_overrides_and_rejects_url(monkeypatch):
    previous = dict(OCR_RUNTIME)
    try:
        before = dict(OCR_RUNTIME)
        result = install_default_runtime({})
        assert result["reason"] == "feature_disabled" and OCR_RUNTIME == before

        OCR_RUNTIME["client_factory"] = None
        OCR_RUNTIME["renderer"] = None
        installed = install_default_runtime({"LOCALSAPORTER_OCR_ENABLED": "1"})
        assert installed["client_factory"] and installed["renderer"]
        client = OCR_RUNTIME["client_factory"]()
        assert isinstance(client, PaddleXLayoutClient)
        client.close()
        custom_client = lambda: object()
        custom_renderer = lambda data, page: b"custom"
        OCR_RUNTIME["client_factory"] = custom_client
        OCR_RUNTIME["renderer"] = custom_renderer
        install_default_runtime({"LOCALSAPORTER_OCR_ENABLED": "true"})
        assert OCR_RUNTIME["client_factory"] is custom_client
        assert OCR_RUNTIME["renderer"] is custom_renderer

        OCR_RUNTIME["client_factory"] = None
        OCR_RUNTIME["renderer"] = None
        rejected = install_default_runtime({
            "LOCALSAPORTER_OCR_ENABLED": "1", "LOCALSAPORTER_OCR_URL": "http://example.com",
        })
        assert rejected["reason"] == "invalid_local_url"
        assert OCR_RUNTIME["client_factory"] is None and OCR_RUNTIME["renderer"] is None
    finally:
        OCR_RUNTIME.clear()
        OCR_RUNTIME.update(previous)


def test_oa05_lifespan_contains_protected_installation():
    source = (ROOT / "app" / "web.py").read_text(encoding="utf-8")
    lifespan = source[source.index("async def lifespan"):source.index("app = FastAPI")]
    assert "install_default_runtime()" in lifespan
    assert "except Exception as exc" in lifespan
    assert "OCR runtime installation failed" in lifespan


def test_oa06_renderer_import_range_invalid_and_wrapper_failure(monkeypatch):
    monkeypatch.setitem(sys.modules, "pypdfium2", None)
    with pytest.raises(ImportError):
        render_page_png(b"%PDF", 1)
    wrapped = render_pdf_page(b"%PDF", 1, render_page_png)
    assert not wrapped.ok and wrapped.error_code == OcrErrorCode.OCR_PAGE_RENDER_FAILED.value

    class BrokenDocument:
        def __init__(self, data):
            raise ValueError("invalid PDF")

    monkeypatch.setitem(sys.modules, "pypdfium2", types.SimpleNamespace(PdfDocument=BrokenDocument))
    with pytest.raises(ValueError):
        render_page_png(b"bad", 1)

    class EmptyDocument:
        def __init__(self, data): pass
        def __len__(self): return 1
        def close(self): pass

    monkeypatch.setitem(sys.modules, "pypdfium2", types.SimpleNamespace(PdfDocument=EmptyDocument))
    with pytest.raises(IndexError):
        render_page_png(b"pdf", 2)


def test_oa06_renderer_missing_pil_raises(monkeypatch):
    monkeypatch.setitem(sys.modules, "PIL", None)
    monkeypatch.setitem(sys.modules, "PIL.Image", None)
    with pytest.raises((ImportError, ModuleNotFoundError, RuntimeError, Exception)):
        render_page_png(_pdf(1), 1)


def test_oa06_fake_pdfium_returns_png(monkeypatch):
    signature = b"\x89PNG\r\n\x1a\n"

    class Image:
        def save(self, stream, format):
            assert format == "PNG"
            stream.write(signature + b"fake")

    class Bitmap:
        def to_pil(self): return Image()

    class Page:
        def render(self, scale):
            assert scale == 2.0
            return Bitmap()
        def close(self): pass

    class Document:
        def __init__(self, data): assert data == b"pdf"
        def __len__(self): return 1
        def __getitem__(self, index): assert index == 0; return Page()
        def close(self): pass

    monkeypatch.setitem(sys.modules, "pypdfium2", types.SimpleNamespace(PdfDocument=Document))
    assert render_page_png(b"pdf", 1).startswith(signature)


def test_oa07_fake_integration_persists_blocks_and_is_not_adoptable(tmp_path):
    fixture = _fixture()
    for block in fixture["result"]["layoutParsingResults"][0]["prunedResult"]["parsing_res_list"]:
        block["block_content"] = f"block-{block['block_id']}"
    fixture["result"]["layoutParsingResults"][0]["prunedResult"]["parsing_res_list"][0]["block_content"] = "field"

    def handler(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(200, json=fixture)

    original = _pdf(2)
    store = OcrStore(tmp_path / "ocr.db")
    with _client(handler) as client:
        result = run_ocr_fallback(
            original_bytes=original,
            expected_sha256=sha256_hex(original),
            project_id="p1",
            context_file_id="cf1",
            filename="sample.pdf",
            page_count=2,
            pages_quality=[
                {"page": 1, "quality": "unreadable", "text": "", "char_count": 0},
                {"page": 2, "quality": "unreadable", "text": "", "char_count": 0},
            ],
            store=store,
            client=client,
            renderer=lambda data, page: b"\x89PNG\r\n\x1a\n" + str(page).encode(),
            feature_enabled=True,
        )
    assert result["status"] in {"passed", "needs_review"}
    assert len(store.list_blocks(result["run_id"])) == 18
    run = store.get_run(result["run_id"])
    review = store.get_review(result["run_id"])
    assert review is None
    assert run["adoption_status"] == "unapproved"
    if result["status"] == "passed":
        # passed は既存仕様で採用可能。フィールド抽出が無い今回の単位では採用されるデータが無い
        assert is_ocr_adoptable(run, None) is True
    else:
        assert is_ocr_adoptable(run, None) is False
        assert result.get("downstream_allowed") is False


def test_oa08_compose_structure_and_base_compose_unchanged():
    ocr = yaml.safe_load((ROOT / "docker-compose.ocr.yml").read_text(encoding="utf-8"))
    services = ocr["services"]
    for name in ("paddleocr-vlm", "paddleocr-doc-parser"):
        service = services[name]
        assert service["network_mode"] == "service:web"
        assert "ports" not in service
        assert service["profiles"] == ["ocr"]
        assert service["restart"] == "no"
    vlm_command = " ".join(services["paddleocr-vlm"]["command"])
    assert "--host 127.0.0.1" in vlm_command
    assert "--port 8081" in vlm_command
    assert "--backend vllm" in vlm_command
    assert "${" not in vlm_command
    parser_command = services["paddleocr-doc-parser"]["command"]
    assert parser_command[:2] == ["paddlex", "--serve"]
    assert parser_command == [
        "paddlex", "--serve", "--pipeline", "/home/paddleocr/pipeline_config_vllm.yaml",
        "--host", "127.0.0.1", "--port", "8080",
    ]
    assert services["paddleocr-doc-parser"]["volumes"] == [
        "./docker/ocr/pipeline_config_vllm.yaml:/home/paddleocr/pipeline_config_vllm.yaml:ro"
    ]
    assert services["web"]["environment"]["LOCALSAPORTER_OCR_ENABLED"] == "1"
    assert services["web"]["environment"]["LOCALSAPORTER_OCR_URL"] == "http://127.0.0.1:8080"

    import hashlib
    base = (ROOT / "docker-compose.yml").read_bytes()
    assert hashlib.sha1(base).hexdigest() == BASE_COMPOSE_HASH
