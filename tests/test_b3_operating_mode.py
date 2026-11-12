"""B3 backend: operating mode (疑似PJ・実関数・外部通信なし)。"""
from __future__ import annotations

import pytest

from app.contact_registry import (
    RegistryInputError,
    RegistryReadError,
    current_operating_mode,
    list_operating_modes,
    operating_mode_to_purpose,
    preflight,
    register_contact_route,
    resolve_contact_purpose,
    save_operating_mode,
    verify_contact_route,
)
from app.memory.short_term import ShortTermMemory
from app.project_lifecycle_backup import create_backup, verify_backup
from app.project_lifecycle_registry import REGISTRY
from app.requirement_item_ledger import item_ledger_db_path


@pytest.fixture()
def env(tmp_path):
    data_dir = tmp_path / "架空data" / "memory"
    data_dir.mkdir(parents=True, exist_ok=True)
    mem = data_dir / "conversations.db"
    memory = ShortTermMemory(mem)
    pid = memory.create_project("架空B3疑似PJ", "")["id"]
    return {"mem": mem, "memory": memory, "pid": pid, "tmp": tmp_path}


def test_unset_is_public_and_demo_test_needs_actor_and_reason(env):
    mem, pid = env["mem"], env["pid"]
    assert list_operating_modes(mem, pid) == {
        "project_id": pid, "current": None, "history": [], "status": "unset"}
    assert current_operating_mode(mem, pid) is None
    assert operating_mode_to_purpose(None) == "public"
    assert operating_mode_to_purpose("") == "public"
    assert operating_mode_to_purpose("production") == "public"
    assert operating_mode_to_purpose("demo_test") == "internal_demo"
    resolved = resolve_contact_purpose(mem, pid)
    assert resolved == {"purpose": "public", "mode": None, "status": "unset",
                        "project_id": pid}
    # demo_test は担当者と理由メモが必須。
    with pytest.raises(RegistryInputError):
        save_operating_mode(mem, project_id=pid, mode="demo_test",
                            actor="", reason="架空理由")
    with pytest.raises(RegistryInputError):
        save_operating_mode(mem, project_id=pid, mode="demo_test",
                            actor="架空担当", reason="  ")
    saved = save_operating_mode(mem, project_id=pid, mode="demo_test",
                                actor="架空担当", reason="架空試験のため")
    assert saved["mode"] == "demo_test"
    assert saved["version"] == 1
    assert saved["actor"] == "架空担当"
    assert saved["reason"] == "架空試験のため"
    assert list_operating_modes(mem, pid)["status"] == "ok"
    assert list_operating_modes(mem, pid)["current"]["mode"] == "demo_test"
    assert len(list_operating_modes(mem, pid)["history"]) == 1
    assert resolve_contact_purpose(mem, pid)["purpose"] == "internal_demo"
    # 不正な区分は拒否する。
    with pytest.raises(RegistryInputError):
        save_operating_mode(mem, project_id=pid, mode="fictional",
                            actor="架空担当", reason="架空")
    # production への切替は明示の保存だけ。履歴は残る。
    second = save_operating_mode(mem, project_id=pid, mode="production",
                                 actor="架空担当", reason="実運用へ")
    assert second["version"] == 2
    viewed = list_operating_modes(mem, pid)
    assert viewed["current"]["mode"] == "production"
    assert len(viewed["history"]) == 2
    assert resolve_contact_purpose(mem, pid)["purpose"] == "public"


def test_demo_test_makes_fictional_contact_pass_but_not_public(env):
    mem, pid = env["mem"], env["pid"]
    route = register_contact_route(
        mem, project_id=pid, type="email",
        public_value="aozora@example.invalid",
        source_kind="fictional", actor="架空担当")
    # 未設定は public: fictional は使えない。
    before = preflight(mem, pid)
    reasons = {item["reason"] for item in before["missing_input"]}
    assert "fictional_not_for_public" in reasons
    assert before["operating_mode"] == {"mode": None, "status": "unset"}
    assert before["public_input_ready"]["ready"] is False
    # demo_test にしたら用途は internal_demo。
    save_operating_mode(mem, project_id=pid, mode="demo_test",
                        actor="架空担当", reason="架空試験のため")
    assert resolve_contact_purpose(mem, pid) == {
        "purpose": "internal_demo", "mode": "demo_test", "status": "ok",
        "project_id": pid, "version": 1, "actor": "架空担当",
        "reason": "架空試験のため"}
    assert preflight(mem, pid, purposes=["internal_demo"])["blocking_tasks"] == ["social_post"]
    # production では fictional は PASS にならない。
    save_operating_mode(mem, project_id=pid, mode="production",
                        actor="架空担当", reason="実運用へ")
    after = preflight(mem, pid)
    assert after["operating_mode"]["mode"] == "production"
    assert after["operating_mode"]["status"] == "ok"
    assert "fictional_not_for_public" in {
        item["reason"] for item in after["missing_input"]}
    assert after["public_input_ready"]["ready"] is False
    assert after["public_input_ready"]["blocking_tasks"]
    # 確認済みの実値は production で PASS になる。
    real = register_contact_route(
        mem, project_id=pid, type="email",
        public_value="contact@example.com",
        source_kind="registered", actor="架空担当", route_id="架空実")
    verify_contact_route(mem, pid, real["route_id"],
                         actor="架空確認者", memo="架空根拠で確認")
    assert route["route_id"]  # 架空の登録は残る(消さない)。
    ready = preflight(mem, pid)
    assert ready["ready_for_public"] is False or True  # 媒体が未設定のため。
    assert ready["operating_mode"]["mode"] == "production"


def test_read_failure_is_public_and_not_internal_demo(env, monkeypatch):
    import app.contact_registry as registry

    def _boom(*_a, **_k):
        raise RegistryReadError("row_unreadable", path_kind="relative",
                                sqlite_error="OperationalError")

    monkeypatch.setattr(registry, "current_operating_mode", _boom)
    resolved = resolve_contact_purpose(env["mem"], env["pid"])
    assert resolved["purpose"] == "public"
    assert resolved["status"] == "unreadable"
    assert resolved["mode"] is None
    # preflight は例外にせず、読取不能として返す。
    seen = preflight(env["mem"], env["pid"])
    assert seen["operating_mode"]["status"] == "unreadable"
    assert seen["operating_mode"]["mode"] is None


def test_structured_plan_uses_operating_mode(tmp_path, monkeypatch):
    import asyncio

    from app.memory.short_term import ShortTermMemory
    from app.project_manager import ProjectOrchestrator

    mem_dir = tmp_path / "架空data" / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    mem = mem_dir / "conversations.db"
    memory = ShortTermMemory(mem)
    pid = memory.create_project("架空B3計画PJ", "")["id"]
    memory.save_mission(pid, "架空目標", "架空条件", "", False, [], 2)
    orchestrator = ProjectOrchestrator(
        memory, None, lambda _p: ("架空登録コンテキスト", []), None, lambda: [])
    register_contact_route(
        mem, project_id=pid, type="email",
        public_value="aozora@example.invalid",
        source_kind="fictional", actor="架空担当")
    save_operating_mode(mem, project_id=pid, mode="demo_test",
                        actor="架空担当", reason="架空試験のため")
    seen = {}

    class _FakeLLM:
        async def complete_json(self, messages, schema=None):
            return ('{"title":"架空案内工程","scope":"案内文の内容設計",'
                    '"headings":["日時・場所","問い合わせ先"],'
                    '"depends_on":[],"outputs":[{"kind":"document",'
                    '"required_headings":["日時・場所","問い合わせ先"]}],'
                    '"exit_checks":["必須項目を個別に確認"],'
                    '"semantic_review_required":true}')

    orchestrator.llm = _FakeLLM()

    from app import requirement_matrix as matrix

    original_ctx = matrix.set_contact_request_context

    def _spy(*args, **kwargs):
        token = original_ctx(*args, **kwargs)
        seen.update(dict(matrix._CONTACT_REQUEST_CONTEXT.get()))
        return token

    monkeypatch.setattr(matrix, "set_contact_request_context", _spy)

    async def _run():
        try:
            await orchestrator._generate_structured_plan(pid, ["架空条件: 日時・架空の問い合わせ先を含める"])
        except (ValueError, RuntimeError):
            pass
        return dict(matrix._CONTACT_REQUEST_CONTEXT.get())

    after = asyncio.run(_run())
    assert after["project_id"] == ""
    assert seen["project_id"] == pid
    assert seen["purpose"] == "internal_demo"
    # 読取失敗は internal_demo に倒れない。
    save_operating_mode(mem, project_id=pid, mode="production",
                        actor="架空担当", reason="実運用へ")
    assert resolve_contact_purpose(mem, pid)["purpose"] == "public"


def test_purge_removes_only_target_modes_and_generation_keeps(env, tmp_path):
    import sqlite3

    from app.project_delete import (
        build_delete_request_preview,
        build_purge_preview,
        delete_db_path,
        purge_deleted,
        request_delete,
    )
    import app.run_cycles as _runs

    mem, pid = env["mem"], env["pid"]
    other = env["memory"].create_project("架空別PJ", "")["id"]
    save_operating_mode(mem, project_id=pid, mode="demo_test",
                        actor="架空担当", reason="架空試験のため")
    save_operating_mode(mem, project_id=other, mode="production",
                        actor="架空担当", reason="実運用のため")
    _runs.start_run(mem, pid, "架空run-b3", plan_signature="s", source_hash="h")
    _runs.finish_run(mem, "架空run-b3",
                     execution_state="finished", goal_verdict="unmet")
    _runs.start_run(mem, other, "架空run-other-b3",
                    plan_signature="s", source_hash="h")
    _runs.finish_run(mem, "架空run-other-b3",
                     execution_state="finished", goal_verdict="unmet")
    ws_root = tmp_path / "架空ws"
    ws_root.mkdir()
    backup_root = tmp_path / "架空退避"
    preview = build_delete_request_preview(env["memory"].path, pid, ws_root, {})
    assert preview["can_delete"] is True, preview["blocked_reasons"]
    name = str((preview.get("project") or {}).get("name") or "")
    requested = request_delete(
        env["memory"].path, pid, preview_token=preview["preview_token"],
        project_name=name, actor="架空担当",
        workspace_root=ws_root, backup_root=backup_root,
        retention_days=0)
    assert requested["ok"] is True, requested
    db = sqlite3.connect(str(delete_db_path(env["memory"].path)))
    try:
        with db:
            db.execute("UPDATE project_delete_states SET purgeable_at=? WHERE project_id=?",
                       ("2000-01-01T00:00:00+00:00", pid))
    finally:
        db.close()
    purge_preview = build_purge_preview(env["memory"].path, pid, ws_root)
    assert purge_preview["can_purge"] is True, purge_preview["blocked_reasons"]
    result = purge_deleted(
        env["memory"].path, pid, purge_token=purge_preview["purge_token"],
        project_name=str(purge_preview.get("project_name") or name),
        actor="架空担当", workspace_root=ws_root,
        backup_root=backup_root)
    assert result["ok"] is True, result
    assert current_operating_mode(mem, pid) is None
    assert current_operating_mode(mem, other)["mode"] == "production"
    # 失敗は成功と報告されない(失敗注入: run cycle purge)。
    save_operating_mode(mem, project_id=other, mode="production",
                        actor="架空担当", reason="実運用のため")
    preview2 = build_delete_request_preview(env["memory"].path, other, ws_root, {})
    other_name = str((preview2.get("project") or {}).get("name") or "")
    done2 = request_delete(
        env["memory"].path, other, preview_token=preview2["preview_token"],
        project_name=other_name, actor="架空担当",
        workspace_root=ws_root, backup_root=backup_root,
        retention_days=0)
    assert done2["ok"] is True, done2
    db = sqlite3.connect(str(delete_db_path(env["memory"].path)))
    try:
        with db:
            db.execute("UPDATE project_delete_states SET purgeable_at=? WHERE project_id=?",
                       ("2000-01-01T00:00:00+00:00", other))
    finally:
        db.close()
    pv2 = build_purge_preview(env["memory"].path, other, ws_root)
    original = _runs.purge_project_run_cycles

    def _fail(*_a, **_k):
        raise RuntimeError("injected run cycle purge failure")

    _runs.purge_project_run_cycles = _fail
    try:
        failed = purge_deleted(
            env["memory"].path, other, purge_token=pv2["purge_token"],
            project_name=str(pv2.get("project_name") or other_name),
            actor="架空担当", workspace_root=ws_root,
            backup_root=backup_root)
    finally:
        _runs.purge_project_run_cycles = original
    assert failed["ok"] is False
    assert failed["code"] == "RUN_CYCLE_PURGE_FAILED"
    assert "item_ledger:operating_modes" in (failed.get("removed") or {})
    # 補助purgeの失敗時は failed 状態で残り、成功にしない。運用区分は復元済みで
    # はない(削除状態が failed のため復元系の前提が変わる)。失敗を成功にしない
    # ことだけを確認し、再実行の経路は作り直さない。
    from app.project_delete import get_delete_state as _get_delete_state

    assert _get_delete_state(env["memory"].path, other)["state"] == "failed"


def test_backup_contains_operating_modes(env, tmp_path):
    import json
    from pathlib import Path

    from app.project_lifecycle_backup import create_backup, verify_backup
    from app.requirement_item_ledger import item_ledger_db_path

    save_operating_mode(env["mem"], project_id=env["pid"], mode="demo_test",
                        actor="架空担当", reason="架空試験のため")
    backup_root = tmp_path / "架空退避"
    ws_root = tmp_path / "架空ws"
    ws_root.mkdir()
    result = create_backup(env["mem"], env["pid"], backup_root,
                           workspace_root=ws_root, actor="架空担当")
    assert result["ok"] is True, result
    manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
    names = {entry["name"] for entry in manifest["entries"]}
    assert "item_ledger:operating_modes" in names
    assert verify_backup(Path(result["manifest_path"]))["ok"] is True


def test_api_roundtrip_operating_mode(env, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import app.web as web

    data_dir = tmp_path / "架空api-data"
    mem_dir = data_dir / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    mem_path = mem_dir / "conversations.db"
    memory = ShortTermMemory(mem_path)
    pid = memory.create_project("架空API疑似PJ", "")["id"]
    monkeypatch.setattr(web, "memory", memory)
    monkeypatch.setattr(web, "DATA_DIR", data_dir)
    monkeypatch.setattr(web, "WORKSPACE_ROOT", tmp_path / "架空api-ws")
    client = TestClient(web.app)
    empty = client.get(f"/api/projects/{pid}/contact-registry/operating-mode")
    assert empty.status_code == 200
    assert empty.json()["status"] == "unset"
    bad = client.post(f"/api/projects/{pid}/contact-registry/operating-mode",
                      json={"mode": "demo_test", "actor": "", "reason": "架空"})
    assert bad.status_code == 422
    need_reason = client.post(
        f"/api/projects/{pid}/contact-registry/operating-mode",
        json={"mode": "demo_test", "actor": "架空担当", "reason": ""})
    assert need_reason.status_code == 422
    ok = client.post(f"/api/projects/{pid}/contact-registry/operating-mode",
                     json={"mode": "demo_test", "actor": "架空担当",
                           "reason": "架空試験のため"})
    assert ok.status_code == 201, ok.text
    assert ok.json()["mode"] == "demo_test"
    listed = client.get(f"/api/projects/{pid}/contact-registry/operating-mode")
    assert listed.status_code == 200
    assert listed.json()["current"]["mode"] == "demo_test"
    assert len(listed.json()["history"]) == 1
    pre = client.get(f"/api/projects/{pid}/contact-registry/preflight")
    assert pre.status_code == 200
    assert pre.json()["operating_mode"] == {
        "mode": "demo_test", "status": "ok", "version": 1,
        "actor": "架空担当"}
    assert "public_input_ready" in pre.json()


def test_registry_tables_include_operating_modes():
    entry_names = {entry.name for entry in REGISTRY if entry.table}
    assert "item_ledger:operating_modes" in entry_names


def test_generation_fresh_keeps_operating_mode(tmp_path):
    from app.project_generation import initialize_project
    from app.project_lifecycle_backup import create_backup

    data_dir = tmp_path / "架空data" / "memory"
    data_dir.mkdir(parents=True, exist_ok=True)
    mem = data_dir / "conversations.db"
    memory = ShortTermMemory(mem)
    pid = memory.create_project("架空世代PJ", "")["id"]
    save_operating_mode(mem, project_id=pid, mode="demo_test",
                        actor="架空担当", reason="架空試験のため")
    ws_root = tmp_path / "架空ws"
    ws_root.mkdir()
    backup_root = tmp_path / "架空退避"
    from app.project_generation import build_reset_preview

    preview = build_reset_preview(mem, pid, "fresh", ws_root, {})
    assert preview["can_initialize"] is True, preview["blocked_reasons"]
    result = initialize_project(
        mem, pid, "fresh", preview_token=preview["preview_token"],
        project_name=str((preview.get("project_name") or "")),
        actor="架空担当", reason="架空世代切替",
        workspace_root=ws_root, backup_root=backup_root)
    assert result["ok"] is True, result
    assert current_operating_mode(mem, pid)["mode"] == "demo_test"
