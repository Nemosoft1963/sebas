"""OCR-4B: UI向け最小APIと静的検査。実OCR/GPU/実PDF/ブラウザは使わない。"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.ocr_schema import OcrErrorCode
from test_ocr_phase4a import _project_with_pdf, seed_run

ROOT = Path(__file__).resolve().parents[1]
JS_PATH = ROOT / "app" / "static" / "ocr_review.js"
HTML_PATH = ROOT / "app" / "static" / "index.html"
DANGEROUS_JS = (
    "innerHTML",
    "outerHTML",
    "insertAdjacentHTML",
    "document.write",
    "eval(",
    "new Function",
)


def _detail(exc: HTTPException) -> dict:
    detail = exc.detail
    return detail if isinstance(detail, dict) else {"message": str(detail)}


@pytest.mark.asyncio
async def test_list_ocr_runs_newest_first_and_other_project_404(tmp_path, monkeypatch):
    import app.web as web
    memory, project, item, original = _project_with_pdf(tmp_path)
    other = memory.create_project("other")
    monkeypatch.setattr(web, "memory", memory)
    store, run_old, *_ = seed_run(
        tmp_path, project_id=project["id"], file_id=item["id"], run_id="run-old",
    )
    store, run_new, *_ = seed_run(
        tmp_path, project_id=project["id"], file_id=item["id"], run_id="run-new",
        status="passed",
    )
    monkeypatch.setattr(web, "_ocr_store", lambda: store)

    body = await web.list_context_file_ocr_runs(project["id"], item["id"])
    assert body["file_id"] == item["id"]
    ids = [row["run_id"] for row in body["runs"]]
    assert set(ids) == {"run-old", "run-new"}
    assert ids[0] == "run-new"
    assert body["runs"][0]["ocr_status"] == "passed"
    assert body["runs"][0]["adoption_status"] == "unapproved"
    assert "trigger" in body["runs"][0]
    assert body["runs"][1]["ocr_status"] == "needs_review"

    with pytest.raises(HTTPException) as cross:
        await web.list_context_file_ocr_runs(other["id"], item["id"])
    assert cross.value.status_code == 404


@pytest.mark.asyncio
async def test_list_ocr_runs_missing_file_is_404(tmp_path, monkeypatch):
    import app.web as web
    memory, project, item, original = _project_with_pdf(tmp_path)
    monkeypatch.setattr(web, "memory", memory)
    store, *_ = seed_run(tmp_path, project_id=project["id"], file_id=item["id"])
    monkeypatch.setattr(web, "_ocr_store", lambda: store)
    with pytest.raises(HTTPException) as missing:
        await web.list_context_file_ocr_runs(project["id"], "no-such-file")
    assert missing.value.status_code == 404


@pytest.mark.asyncio
async def test_artifact_download_png_md_headers_and_security(tmp_path, monkeypatch):
    import app.web as web
    memory, project, item, original = _project_with_pdf(tmp_path)
    other = memory.create_project("other")
    monkeypatch.setattr(web, "memory", memory)
    store, run, original, root, payload = seed_run(
        tmp_path, project_id=project["id"], file_id=item["id"],
    )
    monkeypatch.setattr(web, "_ocr_store", lambda: store)

    png = await web.download_ocr_artifact(project["id"], run["run_id"], "page-1.png")
    assert png.media_type == "image/png"
    assert png.body == b"png-bytes"
    assert png.headers.get("x-content-type-options") == "nosniff"

    md = await web.download_ocr_artifact(project["id"], run["run_id"], "ocr_result.md")
    assert md.media_type.startswith("text/markdown")
    assert md.body == b"# ocr\n"
    assert md.headers.get("x-content-type-options") == "nosniff"

    with pytest.raises(HTTPException) as cross:
        await web.download_ocr_artifact(other["id"], run["run_id"], "page-1.png")
    assert cross.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [
    "../secret.png",
    "..\\secret.png",
    "/tmp/ocr_result.md",
    "C:/Users/ocr_result.md",
    "%2e%2e/secret.png",
    "%2e%2e%2fsecret.md",
    "..%2fsecret.json",
    "subdir/page-1.png",
])
async def test_artifact_download_rejects_path_traversal(tmp_path, monkeypatch, name):
    import app.web as web
    memory, project, item, original = _project_with_pdf(tmp_path)
    monkeypatch.setattr(web, "memory", memory)
    store, run, *_ = seed_run(tmp_path, project_id=project["id"], file_id=item["id"])
    monkeypatch.setattr(web, "_ocr_store", lambda: store)
    with pytest.raises(HTTPException) as err:
        await web.download_ocr_artifact(project["id"], run["run_id"], name)
    assert err.value.status_code in {400, 404}


@pytest.mark.asyncio
async def test_artifact_download_missing_manifest_entry_is_404(tmp_path, monkeypatch):
    import app.web as web
    memory, project, item, original = _project_with_pdf(tmp_path)
    monkeypatch.setattr(web, "memory", memory)
    store, run, original, root, payload = seed_run(
        tmp_path, project_id=project["id"], file_id=item["id"],
    )
    monkeypatch.setattr(web, "_ocr_store", lambda: store)
    (root / "page-9.png").write_bytes(b"not-listed")
    with pytest.raises(HTTPException) as missing:
        await web.download_ocr_artifact(project["id"], run["run_id"], "page-9.png")
    assert missing.value.status_code == 404


@pytest.mark.asyncio
async def test_artifact_download_tampered_is_rejected(tmp_path, monkeypatch):
    import app.web as web
    memory, project, item, original = _project_with_pdf(tmp_path)
    monkeypatch.setattr(web, "memory", memory)
    store, run, original, root, payload = seed_run(
        tmp_path, project_id=project["id"], file_id=item["id"],
    )
    monkeypatch.setattr(web, "_ocr_store", lambda: store)
    target = root / "page-1.png"
    target.write_bytes(target.read_bytes() + b"x")
    with pytest.raises(HTTPException) as tampered:
        await web.download_ocr_artifact(project["id"], run["run_id"], "page-1.png")
    assert tampered.value.status_code == 409
    assert _detail(tampered.value)["error_code"] == OcrErrorCode.OCR_ARTIFACT_TAMPERED.value


def test_ocr_review_js_exists_and_is_referenced():
    assert JS_PATH.is_file()
    html = HTML_PATH.read_text(encoding="utf-8")
    assert "/static/ocr_review.js" in html
    assert "/static/ocr_review.css" in html
    assert 'id="ocrReviewRoot"' in html
    assert "/static/workflow_readiness.js" in html
    assert "/static/artifact_shelf.js" in html
    assert "/static/goal_review.js" in html
    assert "/static/project_mission.js" in html


def test_ocr_review_js_does_not_use_unsafe_html_sinks():
    source = JS_PATH.read_text(encoding="utf-8")
    for token in DANGEROUS_JS:
        assert token not in source, token
    assert "textContent" in source
    assert "createElement" in source
    assert "setAttribute" in source


def test_ocr_review_js_keeps_success_and_adoption_and_value_states_separate():
    source = JS_PATH.read_text(encoding="utf-8")
    assert "OCR成功" in source
    assert "業務計算への採用済み" in source
    assert re.search(r"passed\s*:\s*'OCR成功'", source)
    assert "passed" in source and "採用済み" in source
    assert not re.search(r"passed\s*:\s*'[^']*採用済み", source)
    assert "value:'値あり'" in source or "value: '値あり'" in source or "value:'値あり'" in source.replace(" ", "")
    compact = re.sub(r"\s+", "", source)
    assert "value:'値あり'" in compact
    assert "zero:'0（印字どおりのゼロ）'" in compact
    assert "blank:'空欄'" in compact
    assert "unreadable:'読取不能'" in compact
    assert compact.count("unreadable:'読取不能'") >= 1
    assert "ocrRagConfirm" in source
    assert re.search(r"ragBox\.checked\s*=\s*true", source) is None
    assert 'ragBox.checked=true' not in compact
    assert "この承認結果を再利用知識へ登録" in source
    assert "OCR機能は無効です" in source
    assert "OCRで再読取" in source


def test_ocr_review_js_does_not_display_unreadable_as_zero():
    source = JS_PATH.read_text(encoding="utf-8")
    assert "（読取不能）" in source
    assert "（空欄）" in source
    assert "state==='unreadable'" in source.replace(" ", "") or "state==='unreadable'" in re.sub(r"\s+", "", source)
    compact = re.sub(r"\s+", "", source)
    assert "state==='unreadable'" in compact
    assert "valueEl.textContent='（読取不能）'" in compact
    assert "state==='blank'" in compact
    assert "valueEl.textContent='（空欄）'" in compact


def test_ocr_review_ui_scroll_validation_and_action_results():
    source = JS_PATH.read_text(encoding="utf-8")
    css_source = (ROOT / "app" / "static" / "ocr_review.css").read_text(encoding="utf-8")
    compact = re.sub(r"\s+", "", source)

    # 1. openRun smooth scroll with safety check
    assert "scrollIntoView" in source
    assert "block:'start'" in compact or 'block:"start"' in compact
    assert "behavior:'smooth'" in compact or 'behavior:"smooth"' in compact
    assert "typeofscreenEl.scrollIntoView==='function'" in compact or "screenEl.scrollIntoView" in compact

    # 2. Validation for reviewer & reason before API call on approve, correctApprove, reject
    assert "レビュー者名と理由を入力してください" in source
    assert "el('ocrActionError').textContent='レビュー者名と理由を入力してください'" in compact
    assert "el('ocrApprove').onclick" in source
    assert "el('ocrCorrectApprove').onclick" in source
    assert "el('ocrReject').onclick" in source

    # 3. Action result element & status messages
    assert "ocrActionResult" in source
    assert "ocr-action-result" in source
    assert "role" in source and "status" in source
    assert "actionResult.setAttribute('role','status')" in compact or 'actionResult.setAttribute("role","status")' in compact
    assert "承認しました" in source
    assert "却下しました" in source

    # 4. CSS styling
    assert ".ocr-action-result" in css_source
    assert ".ocr-action-error" in css_source

    # 5. scrollIntoView is only in openRun, not in loadFiles
    load_files_match = re.search(r"function loadFiles\s*\(\)\s*\{(.*?)\n\s*\}", source, re.DOTALL)
    if load_files_match:
        assert "scrollIntoView" not in load_files_match.group(1)
