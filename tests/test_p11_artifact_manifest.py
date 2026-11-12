"""P1-1 成果物の正本化(manifest)の受入試験。疑似PJ・実関数・外部通信なし。

ガードを外すと失敗する、挙動を直接見る試験にする。極性を逆に書かない。
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from app import artifact_manifest as am
from app.memory.short_term import ShortTermMemory
from app.workspace_files import WorkspaceSandbox


def _manager(tmp_path: Path, monkeypatch=None):
    from app.project_manager import ProjectOrchestrator

    mem_dir = tmp_path / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    memory = ShortTermMemory(mem_dir / "conversations.db")
    pid = memory.create_project("架空P11", "")["id"]
    manager = ProjectOrchestrator(
        memory, None, lambda p: ("", []), None, lambda: [],
        workspace=WorkspaceSandbox(tmp_path / "workspace"),
    )
    return memory, manager, pid


def test_build_verify_roundtrip_and_mismatch():
    body = "## 概要\n\n原本と結果を確認した文書。\n".encode("utf-8")
    manifest = am.build_manifest(
        project_id="p1", plan_signature="sig", task_id="t1",
        path="result/a.md", origin="execution", content_bytes=body,
        source_hashes=["abc"], validation_state="valid",
    )
    assert manifest["artifact_sha256"] == am.sha256_hex(body)
    assert "原本と結果" not in json.dumps(manifest, ensure_ascii=False)
    assert manifest["source_hashes"] == ["abc"]
    ok = am.verify_artifact(manifest, body)
    assert ok["status"] == "valid"
    tampered = body + "改".encode("utf-8")
    bad = am.verify_artifact(manifest, tampered)
    assert bad["status"] == "invalid"
    assert bad["reason"] == "sha_mismatch"
    assert bad["expected_sha256"] != bad["actual_sha256"]
    missing = am.verify_artifact(None, body)
    assert missing["status"] == "unverified"
    missing2 = am.verify_artifact({}, body)
    assert missing2["status"] == "unverified"


def test_counts_for_auto_pass_only_execution_valid():
    good = am.build_manifest(
        project_id="p", plan_signature="s", task_id="t", path="result/a.md",
        origin="execution", content_bytes=b"a", validation_state="valid")
    assert am.counts_for_auto_pass(good) is True
    for origin, state in [
        ("manual_preview", "valid"), ("unknown", "valid"),
        ("execution", "invalid"), ("execution", "not_tested"),
        ("execution", "unverified"),
    ]:
        manifest = am.build_manifest(
            project_id="p", plan_signature="s", task_id="t", path="result/a.md",
            origin=origin, content_bytes=b"a", validation_state=state)
        assert am.counts_for_auto_pass(manifest) is False, (origin, state)
    assert am.counts_for_auto_pass(None) is False
    assert am.counts_for_auto_pass({}) is False
    # draft 判定は安全側: manifest 無し・manual_preview・unknown は真。
    assert am.is_draft(None) is True
    assert am.is_draft({}) is True
    assert am.is_draft(am.build_manifest(
        project_id="p", plan_signature="", task_id="", path="result/a.md",
        origin="manual_preview", content_bytes=b"a")) is True
    assert am.is_draft(am.build_manifest(
        project_id="p", plan_signature="", task_id="", path="result/a.md",
        origin="unknown", content_bytes=b"a")) is True
    assert am.is_draft(good) is False


def test_execution_save_records_execution_manifest_without_body(tmp_path):
    memory, manager, pid = _manager(tmp_path)
    project = memory.get_project(pid)
    audit = manager.workspace.apply_operations(
        project["workspace_path"], pid,
        [{"action": "write_text", "path": "result/report.md",
          "content": "## 概要\n\n実行工程の本文。\n"}],
    )
    manager._record_execution_manifests(pid, project, "task-1", audit)
    stored = manager.workspace.read_artifact_manifest(
        project["workspace_path"], pid, "result/report.md")
    assert stored is not None
    assert stored["origin"] == "execution"
    assert stored["task_id"] == "task-1"
    assert stored["artifact_sha256"] == am.sha256_hex(
        "## 概要\n\n実行工程の本文。\n".encode("utf-8"))
    assert "実行工程の本文" not in json.dumps(stored, ensure_ascii=False)


def test_manual_operations_and_upload_record_manual_preview(tmp_path):
    import app.web as web

    memory, manager, pid = _manager(tmp_path)
    project = memory.get_project(pid)
    web.memory = memory
    web.workspace = manager.workspace
    web.orchestrator = manager

    audit = manager.workspace.apply_operations(
        project["workspace_path"], pid,
        [{"action": "write_text", "path": "result/manual.md",
          "content": "## 概要\n\n手動ドラフト。\n"}],
    )
    web._record_manual_preview_manifests(pid, project, audit)
    first = manager.workspace.read_artifact_manifest(
        project["workspace_path"], pid, "result/manual.md")
    assert first is not None and first["origin"] == "manual_preview"

    # 実行工程が手動ファイルを上書きしたら origin が execution に変わる。
    audit2 = manager.workspace.apply_operations(
        project["workspace_path"], pid,
        [{"action": "write_text", "path": "result/manual.md",
          "content": "## 概要\n\n実行工程の上書き。\n"}],
    )
    manager._record_execution_manifests(pid, project, "task-9", audit2)
    second = manager.workspace.read_artifact_manifest(
        project["workspace_path"], pid, "result/manual.md")
    assert second is not None and second["origin"] == "execution"

    # 逆に手動操作が実行工程の成果物を上書きしたら manual_preview に変わる。
    audit3 = manager.workspace.apply_operations(
        project["workspace_path"], pid,
        [{"action": "write_text", "path": "result/manual.md",
          "content": "## 概要\n\n手動の再上書き。\n"}],
    )
    web._record_manual_preview_manifests(pid, project, audit3)
    third = manager.workspace.read_artifact_manifest(
        project["workspace_path"], pid, "result/manual.md")
    assert third is not None and third["origin"] == "manual_preview"
    assert third["artifact_sha256"] == am.sha256_hex(
        "## 概要\n\n手動の再上書き。\n".encode("utf-8"))

    # アップロード経路も manual_preview になる。
    saved = manager.workspace.save_upload(
        project["workspace_path"], pid, "result/uploaded.md",
        "## 概要\n\nアップロード。\n".encode("utf-8"))
    web._record_manual_preview_manifests(
        pid, project, [{**saved, "action": "upload"}])
    uploaded = manager.workspace.read_artifact_manifest(
        project["workspace_path"], pid, "result/uploaded.md")
    assert uploaded is not None and uploaded["origin"] == "manual_preview"


def test_zip_contains_manifest_and_draft_readme(tmp_path):
    import app.web as web
    from fastapi.testclient import TestClient

    memory, manager, pid = _manager(tmp_path)
    memory.save_mission(pid, "架空目標", "result/a.md を作る", "", False, [])
    project = memory.get_project(pid)
    web.memory = memory
    web.workspace = manager.workspace
    web.orchestrator = manager

    audit = manager.workspace.apply_operations(
        project["workspace_path"], pid,
        [{"action": "write_text", "path": "result/a.md",
          "content": "## 概要\n\n実行の正本。\n"},
         {"action": "write_text", "path": "result/b.md",
          "content": "## 概要\n\n手動ドラフト。\n"}],
    )
    manager._record_execution_manifests(pid, project, "task-a", [audit[0]])
    web._record_manual_preview_manifests(pid, project, [audit[1]])

    client = TestClient(web.app)
    response = client.get(f"/api/projects/{pid}/mission/artifacts/download")
    assert response.status_code == 200, response.text
    bundle = zipfile.ZipFile(io.BytesIO(response.content))
    names = set(bundle.namelist())
    assert "result/a.md" in names and "result/b.md" in names
    assert "MANIFEST.json" in names
    # 既存ファイルの出力は壊さない。
    assert bundle.read("result/a.md").decode("utf-8") == "## 概要\n\n実行の正本。\n"
    assert bundle.read("result/b.md").decode("utf-8") == "## 概要\n\n手動ドラフト。\n"
    manifest_doc = json.loads(bundle.read("MANIFEST.json").decode("utf-8"))
    assert manifest_doc["files"]["result/a.md"]["draft"] is False
    assert manifest_doc["files"]["result/a.md"]["origin"] == "execution"
    assert manifest_doc["files"]["result/b.md"]["draft"] is True
    assert manifest_doc["files"]["result/b.md"]["origin"] == "manual_preview"
    assert "実行の正本" not in bundle.read("MANIFEST.json").decode("utf-8")
    # draft があるので README が付く。
    assert "README_試験用ドラフト.txt" in names
    readme = bundle.read("README_試験用ドラフト.txt").decode("utf-8")
    assert "試験用ドラフト" in readme and "result/b.md" in readme


def test_zip_changed_execution_file_is_marked_invalid_draft(tmp_path):
    import app.web as web
    from fastapi.testclient import TestClient

    memory, manager, pid = _manager(tmp_path)
    memory.save_mission(pid, "架空目標", "result/a.md を作る", "", False, [])
    project = memory.get_project(pid)
    web.memory = memory
    web.workspace = manager.workspace
    web.orchestrator = manager
    audit = manager.workspace.apply_operations(
        project["workspace_path"], pid,
        [{"action": "write_text", "path": "result/a.md", "content": "正本"}],
    )
    manager._record_execution_manifests(pid, project, "task-a", audit)
    manager.workspace.apply_operations(
        project["workspace_path"], pid,
        [{"action": "write_text", "path": "result/a.md", "content": "変更後"}],
    )
    response = TestClient(web.app).get(f"/api/projects/{pid}/mission/artifacts/download")
    assert response.status_code == 200
    bundle = zipfile.ZipFile(io.BytesIO(response.content))
    info = json.loads(bundle.read("MANIFEST.json"))["files"]["result/a.md"]
    assert info["verified"] == "invalid"
    assert info["validation_state"] == "invalid"
    assert info["draft"] is True
    assert "README_試験用ドラフト.txt" in bundle.namelist()


def test_semantic_checks_not_tested_heading_fail_deterministic():
    # 理解確認などは not_tested で合格に数えない。
    result = am.semantic_checks("何かの文書", "受講者の理解確認を実施する")
    assert result["status"] == "not_tested"
    assert any(item["result"] == "not_tested" for item in result["checks"])
    # 見出し欠落は fail。
    failed = am.semantic_checks(
        "## 概要\n\n本文。\n", "見出し「詳細手順」を含める")
    assert failed["status"] == "fail"
    assert any(item["result"] == "fail" for item in failed["checks"])
    # 決定的: 同一入力は同一出力。
    first = am.semantic_checks("## 概要\n\n必須語句を含む。\n",
                               "見出し「概要」を含め必須語句「必須語句」を含む")
    second = am.semantic_checks("## 概要\n\n必須語句を含む。\n",
                                "見出し「概要」を含め必須語句「必須語句」を含む")
    assert first == second
    assert first["status"] == "pass"


def test_semantic_checks_mixed_not_tested_precedence():
    criterion = '理解確認ができること。見出し「概要」'

    mixed_pass = am.semantic_checks("## 概要\n\n本文。\n", criterion)
    assert mixed_pass["status"] == "not_tested"
    assert mixed_pass["status"] != "pass"
    assert "理解確認" in mixed_pass["reason"]

    mixed_fail = am.semantic_checks("本文のみ。\n", criterion)
    assert mixed_fail["status"] == "fail"

    testable_only = am.semantic_checks(
        "## 概要\n\n本文。\n", '見出し「概要」')
    assert testable_only["status"] == "pass"


def test_manifest_record_failure_does_not_fail_save(tmp_path):
    memory, manager, pid = _manager(tmp_path)
    project = memory.get_project(pid)
    audit = manager.workspace.apply_operations(
        project["workspace_path"], pid,
        [{"action": "write_text", "path": "result/ok.md",
          "content": "## 概要\n\n本文。\n"}],
    )
    assert audit and audit[0]["status"] == "written"

    import unittest.mock as mock

    with mock.patch.object(
        manager.workspace, "record_artifact_manifest",
        side_effect=OSError("injected manifest failure"),
    ):
        # 失敗しても例外にせず、本体の保存は成功のまま。
        manager._record_execution_manifests(pid, project, "task-x", audit)
    kinds = [event["kind"] for event in
             memory.get_mission(pid, event_limit=500)["events"]]
    assert "artifact_manifest_failed" in kinds


def test_static_guard_no_swallow_no_print_no_network():
    import re

    src = Path("app/artifact_manifest.py").read_text(encoding="utf-8")
    assert "except Exception: pass" not in src
    assert "except Exception:pass" not in src.replace(" ", "")
    assert not re.search(r"(?m)^\s*except\s*:", src)
    assert "print(" not in src
    for token in ("urllib", "requests", "httpx", "socket", "LLM", "ollama"):
        assert token not in src, token
    for token in ("contextlib.suppress", "or True", "or 1"):
        assert token not in src, token

    import sys

    _test_root = Path(__file__).resolve().parents[1]
    if str(_test_root) not in sys.path:
        sys.path.insert(0, str(_test_root))
    import scripts.fail_open_lint as lint

    root = Path("tests/fail_open_baseline.json").resolve()
    if not root.exists():
        root = Path(__file__).resolve().parents[1] / "tests" / "fail_open_baseline.json"
    current = lint.lint_project(root.parents[1])
    base = lint.load_baseline(Path("tests/fail_open_baseline.json"))
    new_items, _stale = lint.check_against_baseline(current, base["by_id"])
    assert new_items == [], new_items[:3]


def test_workspace_list_enriches_origin_without_js_change(tmp_path):
    import app.web as web
    from fastapi.testclient import TestClient

    memory, manager, pid = _manager(tmp_path)
    project = memory.get_project(pid)
    web.memory = memory
    web.workspace = manager.workspace
    web.orchestrator = manager
    audit = manager.workspace.apply_operations(
        project["workspace_path"], pid,
        [{"action": "write_text", "path": "result/c.md",
          "content": "## 概要\n\n本文。\n"}],
    )
    web._record_manual_preview_manifests(pid, project, audit)
    client = TestClient(web.app)
    response = client.get(f"/api/projects/{pid}/workspace")
    assert response.status_code == 200, response.text
    entries = {item["path"]: item for item in response.json()["entries"]}
    assert entries["result/c.md"]["origin"] == "manual_preview"
    assert entries["result/c.md"]["draft"] is True
    assert entries["result/c.md"]["validation_state"] == "unverified"


def test_workspace_list_manifest_read_failure_is_draft(tmp_path, monkeypatch):
    import app.web as web
    from fastapi.testclient import TestClient

    memory, manager, pid = _manager(tmp_path)
    project = memory.get_project(pid)
    web.memory = memory
    web.workspace = manager.workspace
    web.orchestrator = manager
    manager.workspace.apply_operations(
        project["workspace_path"], pid,
        [{"action": "write_text", "path": "result/a.md", "content": "本文"}],
    )
    def fail_read(*args, **kwargs):
        raise OSError("manifest unavailable")
    monkeypatch.setattr(manager.workspace, "read_artifact_manifest", fail_read)
    response = TestClient(web.app).get(f"/api/projects/{pid}/workspace")
    assert response.status_code == 200
    entries = {item["path"]: item for item in response.json()["entries"]}
    assert entries["result/a.md"]["origin"] == "unknown"
    assert entries["result/a.md"]["validation_state"] == "unverified"
    assert entries["result/a.md"]["draft"] is True
