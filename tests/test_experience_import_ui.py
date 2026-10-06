"""Tests for experience import UI static assets and endpoints."""
from pathlib import Path
from fastapi.testclient import TestClient
import app.web as web_module

ROOT = Path(__file__).resolve().parents[1]
JS_FILE = ROOT / "app" / "static" / "experience_import.js"
INDEX_FILE = ROOT / "app" / "static" / "index.html"


def test_experience_import_js_structure_and_confirm_rag():
    """(a) confirm_rag を送るコードが存在し、チェックボックス要素の状態を参照していること。"""
    assert JS_FILE.is_file(), "experience_import.js does not exist"
    code = JS_FILE.read_text(encoding="utf-8")

    # confirm_rag キーが送信オブジェクトに含まれていること
    assert "confirm_rag" in code

    # チェックボックス要素 id 'experienceRagConfirm' を参照して confirmed 値を取得していること
    assert "experienceRagConfirm" in code
    assert ".checked" in code

    # 送信ボディに confirm_rag が渡されていること
    assert "confirm_rag: confirmed" in code or "confirm_rag:confirmed" in code or "confirm_rag: !!el(" in code or "confirm_rag:!!el(" in code


def test_experience_import_early_return_when_not_confirmed_or_no_actor():
    """(b) チェックボックス未チェック・回答者名未入力の状態でボタンを押しても fetch が呼ばれない (early return)。"""
    code = JS_FILE.read_text(encoding="utf-8")

    # submitImport 関数または送信処理において actor や confirmed の未入力・未チェックを検査して return していること
    assert "if(!actor||!confirmed)" in code or "if (!actor || !confirmed)" in code or "if(!actor || !confirmed)" in code
    # 送信ボタンが disabled になるロジックも存在すること
    assert "el('experienceImportBtn').disabled" in code or 'el("experienceImportBtn").disabled' in code


def test_experience_import_fetch_endpoint_and_method():
    """(c) /api/projects/{project_id}/experience/import-success-cases への POST fetch 呼び出しが存在すること。"""
    code = JS_FILE.read_text(encoding="utf-8")

    assert "/experience/import-success-cases" in code
    assert "method:'POST'" in code or 'method:"POST"' in code or "method: 'POST'" in code


def test_experience_import_web_route():
    """(d) app/web.py に experience_import.js を配信するルートが追加されている。"""
    client = TestClient(web_module.app)
    resp = client.get("/static/experience_import.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers.get("content-type", "")
    assert "no-store" in resp.headers.get("cache-control", "")
    assert "経験RAG取り込み(成功事例)" in resp.text


def test_experience_import_index_html_integration():
    """(e) app/static/index.html にスクリプトタグとパネルのコンテナが追加されている。"""
    assert INDEX_FILE.is_file()
    html_text = INDEX_FILE.read_text(encoding="utf-8")

    assert '<script src="/static/experience_import.js"' in html_text or "<script src='/static/experience_import.js'" in html_text
    assert 'id="experienceImportPanel"' in html_text or "id='experienceImportPanel'" in html_text


def test_experience_import_approval_input_version_field():
    """承認操作に input_version 入力欄があり、安全DOMのみで送信される。"""
    code = JS_FILE.read_text(encoding="utf-8")
    assert "experienceInputVersion" in code
    assert "この事例を適用する案件入力版を指定してください。収集エージェントは入力版を保証しません" in code
    assert "input_version" in code
    assert JS_FILE.read_text(encoding="utf-8").count("innerHTML") == 0
