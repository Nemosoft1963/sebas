"""S5: 削除状態DBが読めないときの fail-closed (疑似PJのみ・実関数)。

一時ディレクトリの疑似PJ (架空データ) のみ。本番DB・実案件に触れない。
外部通信なし。PYTHONHASHSEED に依存しない (順序固定・件数で判定しない)。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.memory.short_term import ShortTermMemory
from app.project_delete import (
    delete_db_path,
    delete_state_status,
    get_delete_state,
    is_deleted,
    is_deleted_or_unknown,
    list_deleted,
)
from app.project_lifecycle_registry import audit_registry_completeness


@pytest.fixture()
def env(tmp_path):
    data_dir = tmp_path / "架空s5-data"
    mem_dir = data_dir / "memory"
    mem_dir.mkdir(parents=True)
    mem_path = mem_dir / "conversations.db"
    memory = ShortTermMemory(mem_path)
    project = memory.create_project("架空疑似PJ", "架空コンテキスト")
    pid = project["id"]
    other = memory.create_project("架空別PJ", "架空別コンテキスト")
    oid = other["id"]
    memory.save_mission(pid, "架空目標", "架空達成条件", "架空制約", False, [], 2)
    memory.save_mission(oid, "架空別目標", "架空別条件", "", False, [], 2)
    ws_root = tmp_path / "架空s5-ws"
    ws_root.mkdir()
    backup_root = tmp_path / "架空s5-bk"
    backup_root.mkdir()
    yield {"mem": mem_path, "memory": memory, "pid": pid, "oid": oid,
           "ws_root": ws_root, "backup_root": backup_root}


def _delete(env, pid):
    from app.project_delete import build_delete_request_preview, request_delete
    prev = build_delete_request_preview(env["mem"], pid, env["ws_root"], {})
    assert prev["can_delete"] is True, prev.get("blocked_reasons")
    name = ((prev.get("project") or {}).get("name")
            or env["memory"].get_project(pid)["name"])
    out = request_delete(env["mem"], pid, preview_token=prev["preview_token"],
                         project_name=name, actor="架空担当",
                         workspace_root=env["ws_root"],
                         backup_root=env["backup_root"], executions={})
    assert out["ok"] is True, out
    return out


def _purge(env, pid):
    from app.project_delete import build_purge_preview, purge_deleted
    import sqlite3 as _sql
    db = _sql.connect(str(delete_db_path(env["mem"])))
    try:
        with db:
            db.execute("UPDATE project_delete_states SET purgeable_at=? WHERE project_id=?",
                       ("2000-01-01T00:00:00+00:00", pid))
    finally:
        db.close()
    pv = build_purge_preview(env["mem"], pid, env["ws_root"])
    assert pv["can_purge"] is True, pv.get("blocked_reasons")
    done = purge_deleted(env["mem"], pid, purge_token=pv["purge_token"],
                         project_name=pv["project_name"], actor="架空担当",
                         workspace_root=env["ws_root"],
                         backup_root=env["backup_root"])
    assert done["ok"] is True, done
    return done


def _break_db(env):
    delete_db_path(env["mem"]).write_bytes(b"S5-CORRUPT-not-sqlite")


def _drop_table(env):
    db = sqlite3.connect(str(delete_db_path(env["mem"])))
    try:
        with db:
            db.execute("DROP TABLE project_delete_states")
    finally:
        db.close()


def test_status_matrix(env):
    mem = env["mem"]
    pid = env["pid"]
    # サイドカーDB無し -> active。
    assert delete_state_status(mem, pid) == "active"
    assert is_deleted_or_unknown(mem, pid) is False
    # 正常で行無し -> active。
    from app.project_delete import _connect
    db = _connect(mem)
    db.close()
    assert delete_state_status(mem, pid) == "active"
    # 論理削除 -> deleted。
    _delete(env, pid)
    assert delete_state_status(mem, pid) == "deleted"
    assert is_deleted_or_unknown(mem, pid) is True
    # 完全削除後 -> purged。
    _purge(env, pid)
    assert delete_state_status(mem, pid) == "purged"
    assert is_deleted_or_unknown(mem, pid) is True
    # 既存の行あり状態値はそのまま返す (restored)。
    from app.project_delete import _set_state
    _set_state(mem, pid, "restored", actor="架空担当",
               backup_id="架空退避", manifest_sha256="0" * 64)
    assert delete_state_status(mem, pid) == "restored"
    assert is_deleted_or_unknown(mem, pid) is False
    # 既存の正常系は不変。
    assert list_deleted(mem)
    assert audit_registry_completeness()["ok"] is True


def test_status_unknown_cases(env, monkeypatch):
    mem = env["mem"]
    pid = env["pid"]
    _delete(env, pid)
    assert delete_state_status(mem, pid) == "deleted"
    # DBが破損 (ゴミのバイト列) -> unknown。is_deleted は従来どおり False。
    _break_db(env)
    assert delete_state_status(mem, pid) == "unknown"
    assert is_deleted_or_unknown(mem, pid) is True
    assert get_delete_state(mem, pid) is None
    assert is_deleted(mem, pid) is False


def test_status_unknown_missing_table(env):
    _delete(env, env["pid"])
    assert delete_state_status(env["mem"], env["pid"]) == "deleted"
    _drop_table(env)
    assert delete_state_status(env["mem"], env["pid"]) == "unknown"
    assert is_deleted_or_unknown(env["mem"], env["pid"]) is True
    assert is_deleted(env["mem"], env["pid"]) is False


def test_status_unknown_sqlite_error(env, monkeypatch):
    _delete(env, env["pid"])
    assert delete_state_status(env["mem"], env["pid"]) == "deleted"
    monkeypatch.setattr(sqlite3, "connect",
                        lambda *a, **k: (_ for _ in ()).throw(sqlite3.Error("架空接続失敗")))
    assert delete_state_status(env["mem"], env["pid"]) == "unknown"
    assert is_deleted_or_unknown(env["mem"], env["pid"]) is True
    # is_deleted の従来の挙動 (unknown でも False) は変えていない。
    assert is_deleted(env["mem"], env["pid"]) is False


def test_status_unknown_permission_error(env, monkeypatch):
    _delete(env, env["pid"])
    assert delete_state_status(env["mem"], env["pid"]) == "deleted"
    monkeypatch.setattr(Path, "exists", lambda self: True)
    monkeypatch.setattr(sqlite3, "connect",
                        lambda *a, **k: (_ for _ in ()).throw(PermissionError("架空権限失敗")))
    assert delete_state_status(env["mem"], env["pid"]) == "unknown"
    assert is_deleted_or_unknown(env["mem"], env["pid"]) is True


def _api_client(monkeypatch, tmp_path, mem_path, pid):
    from fastapi.testclient import TestClient
    import app.web as web
    import types
    memory = ShortTermMemory(mem_path)
    data_dir = mem_path.parent.parent
    ws_root = tmp_path / "架空s5-api-ws"
    ws_root.mkdir(exist_ok=True)

    async def _noop_pause(_pid, cancelled=False):
        return {"project_id": _pid, "status": "paused"}

    manager = types.SimpleNamespace(memory=memory, pause=_noop_pause)
    monkeypatch.setattr(web, "memory", memory)
    monkeypatch.setattr(web, "orchestrator", manager)
    monkeypatch.setattr(web, "DATA_DIR", data_dir)
    monkeypatch.setattr(web, "WORKSPACE_ROOT", ws_root)
    return TestClient(web.app, raise_server_exceptions=False)


def test_require_project_rejects_when_unreadable(monkeypatch, tmp_path, env):
    pid = env["pid"]
    oid = env["oid"]
    _delete(env, pid)
    assert delete_state_status(env["mem"], pid) == "deleted"
    _break_db(env)
    assert delete_state_status(env["mem"], pid) == "unknown"
    client = _api_client(monkeypatch, tmp_path, env["mem"], pid)
    denied = client.get(f"/api/projects/{pid}/mission")
    assert denied.status_code == 410
    assert "状態を確定できません" in denied.json().get("detail", "")
    # 共有サイドカーDBが壊れたときは unknown になるのは全PJだが、
    # active かつ DBが正常な環境では何も変わらない (別途 test_require_project_ok_when_active)。
    assert delete_state_status(env["mem"], oid) == "unknown"


def test_require_project_ok_when_active(monkeypatch, tmp_path, env):
    client = _api_client(monkeypatch, tmp_path, env["mem"], env["pid"])
    assert delete_state_status(env["mem"], env["pid"]) == "active"
    assert client.get(f"/api/projects/{env['pid']}/mission").status_code == 200
    assert client.get(f"/api/projects/{env['oid']}/mission").status_code == 200


def test_auto_resume_excluded_when_unreadable(env):
    from app.safe_auto_resume import resume
    pid = env["pid"]
    _delete(env, pid)
    _break_db(env)
    mgr = SimpleNamespace(memory=env["memory"])
    out = asyncio.run(resume(mgr, pid))
    assert out["status"] == "excluded"
    assert out["executed"] == []
    assert "状態を確定できません" in out["reason"]


def test_triz_excluded_when_unreadable(env):
    from app.automatic_triz import on_failure
    pid = env["pid"]
    _delete(env, pid)
    _break_db(env)
    mgr = SimpleNamespace(memory=env["memory"])
    out = asyncio.run(on_failure(mgr, pid, {"id": "t", "title": "架空"}, ValueError("x")))
    assert out["status"] == "excluded"
    assert "状態を確定できません" in out["reason"]


def test_goal_review_queue_skips_when_unreadable(env):
    from app.goal_review import ReviewStore
    from app.goal_review_queue import tick
    pid = env["pid"]
    oid = env["oid"]
    other = SimpleNamespace(memory=env["memory"])
    _delete(env, pid)
    _break_db(env)
    rs = ReviewStore(env["mem"])
    rs.put(pid, "plan_queue", "架空sig-s5", {"status": "waiting_budget", "next_at": 0,
                                            "providers": [], "public_summary": "x" * 30})
    rs.put(oid, "plan_queue", "架空sig-s5b", {"status": "waiting_budget", "next_at": 0,
                                             "providers": [], "public_summary": "x" * 30})
    asyncio.run(tick(other))
    assert rs.get(pid, "plan_queue", "架空sig-s5")["status"] == "waiting_budget"


def test_plan_review_loop_blocked_when_unreadable(env):
    from app.plan_review_loop import preview_run, remap_preview, remap_run, start_run
    pid = env["pid"]
    _delete(env, pid)
    _break_db(env)
    mgr = SimpleNamespace(memory=env["memory"])
    with pytest.raises(ValueError, match="状態を確定できません"):
        preview_run(mgr, pid, ["chatgpt"])
    with pytest.raises(ValueError, match="状態を確定できません"):
        start_run(mgr, pid, "架空担当", ["chatgpt"], "x" * 30)
    with pytest.raises(ValueError, match="状態を確定できません"):
        asyncio.run(remap_run(mgr, pid, "架空run", actor="架空担当"))
    with pytest.raises(ValueError, match="状態を確定できません"):
        remap_preview(mgr, pid, "架空run")


def test_experience_and_case_reference_excluded_when_unreadable(env):
    from app.experience_memory import configured_memory
    cfg = env["mem"].parent / "experience_memory.json"
    cfg.write_text(json.dumps({"embedding_model": "架空embed",
                               "projects": {env["pid"]: "shadow",
                                            env["oid"]: "shadow"}}, ensure_ascii=False),
                   encoding="utf-8")
    assert configured_memory(env["mem"], env["oid"]) is not None
    _delete(env, env["pid"])
    _break_db(env)
    # 共有サイドカーDBが壊れたときは unknown になるのは全PJだが、
    # active かつ DBが正常な環境では何も変わらない (他PJ単独の正常系は別途確認)。
    assert configured_memory(env["mem"], env["pid"]) is None
    assert configured_memory(env["mem"], env["oid"]) is None
    assert delete_state_status(env["mem"], env["oid"]) == "unknown"
    from app.plan_case_reference import current_view
    view = current_view(env["mem"], env["pid"], 0)
    assert view["status"] == "unavailable"
    assert "状態を確定できません" in json.dumps(view, ensure_ascii=False)


def test_autopilot_and_funnel_blocked_when_unreadable(env):
    import app.autopilot as auto
    from app.execution_funnel import portfolio_funnel
    pid = env["pid"]
    oid = env["oid"]
    manager = SimpleNamespace(memory=env["memory"])
    auto.set_enabled(manager, oid, True, actor="架空担当", reason="架空理由")
    _delete(env, pid)
    _break_db(env)
    # 本番側補正と同等以上: 読めなければ拒否/集計に含めない。
    with pytest.raises(ValueError, match="状態を確定できません"):
        auto.set_enabled(manager, pid, True, actor="架空担当", reason="架空理由")
    with pytest.raises(ValueError, match="状態を確定できません"):
        auto.autopilot_tick(manager, pid, actor="架空担当")
    with pytest.raises(ValueError, match="状態を確定できません"):
        auto.run_named_action(manager, pid, "refresh_coverage", actor="架空担当")
    rows = portfolio_funnel(manager)["rows"]
    assert all(r["project_id"] != pid for r in rows)
    # 共有サイドカーDBが壊れたときは unknown の全PJが集計から外れる。
    # active かつ DBが正常な環境では何も変わらない (別途正常系テストで確認)。
    assert all(r["project_id"] != oid for r in rows)
    assert auto.is_enabled(manager, oid) is True


def test_list_projects_keeps_unknown_with_flag(env):
    pid = env["pid"]
    oid = env["oid"]
    _delete(env, pid)
    _break_db(env)
    items = env["memory"].list_projects()
    by_id = {it["id"]: it for it in items}
    # 一覧には残るがフラグが付く (全PJが消えない)。
    assert pid in by_id
    assert by_id[pid].get("delete_state_unknown") is True
    assert oid in by_id
    assert by_id[oid].get("delete_state_unknown") is True
    # 既存の一覧のキーを削除・変更しない。
    for key in ("id", "name", "turn_count", "context_file_count"):
        assert key in by_id[pid]


def test_list_projects_active_unchanged(env):
    items = env["memory"].list_projects()
    by_id = {it["id"]: it for it in items}
    assert env["pid"] in by_id and env["oid"] in by_id
    assert "delete_state_unknown" not in by_id[env["pid"]]
    assert "delete_state_unknown" not in by_id[env["oid"]]


def test_other_project_unaffected_when_db_healthy(env, monkeypatch, tmp_path):
    """DBが正常 (active) な環境では他PJを含め何も変わらない。"""
    from app.experience_memory import configured_memory
    from app.safe_auto_resume import resume
    from app.automatic_triz import on_failure
    cfg = env["mem"].parent / "experience_memory.json"
    cfg.write_text(json.dumps({"embedding_model": "架空embed",
                               "projects": {env["pid"]: "shadow",
                                            env["oid"]: "shadow"}}, ensure_ascii=False),
                   encoding="utf-8")
    assert delete_state_status(env["mem"], env["pid"]) == "active"
    assert delete_state_status(env["mem"], env["oid"]) == "active"
    client = _api_client(monkeypatch, tmp_path, env["mem"], env["pid"])
    assert client.get(f"/api/projects/{env['pid']}/mission").status_code == 200
    assert client.get(f"/api/projects/{env['oid']}/mission").status_code == 200
    assert configured_memory(env["mem"], env["pid"]) is not None
    assert configured_memory(env["mem"], env["oid"]) is not None
    mgr = SimpleNamespace(memory=env["memory"])
    out = asyncio.run(resume(mgr, env["oid"]))
    assert out["status"] in ("disabled", "blocked", "completed", "partial")
    items = env["memory"].list_projects()
    assert {it["id"] for it in items} >= {env["pid"], env["oid"]}
    assert all("delete_state_unknown" not in it for it in items)


def test_no_secret_in_logs_or_responses(env, caplog, monkeypatch, tmp_path):
    import logging
    secret = "架空S5秘密-xyz987"
    env["memory"].add_event(env["pid"], "架空kind", secret, None, detail=secret)
    _delete(env, env["pid"])
    _break_db(env)
    with caplog.at_level(logging.WARNING):
        assert delete_state_status(env["mem"], env["pid"]) == "unknown"
    assert secret not in caplog.text
    client = _api_client(monkeypatch, tmp_path, env["mem"], env["pid"])
    body = client.get(f"/api/projects/{env['pid']}/mission")
    assert body.status_code == 410
    assert secret not in body.text


def test_audit_registry_completeness():
    assert audit_registry_completeness()["ok"] is True


def test_delete_state_path_error_is_unknown(monkeypatch, env):
    import app.project_delete as deletion
    def fail(*args):
        raise OSError("unavailable")
    monkeypatch.setattr(deletion, "delete_db_path", fail)
    assert deletion.delete_state_status(env["mem"], env["pid"]) == "unknown"
    assert deletion.is_deleted_or_unknown(env["mem"], env["pid"]) is True


def test_unrecognized_persisted_state_is_unknown(env):
    from app.project_delete import _set_state
    _set_state(env["mem"], env["pid"], "restoring")
    assert delete_state_status(env["mem"], env["pid"]) == "unknown"
    assert is_deleted_or_unknown(env["mem"], env["pid"]) is True
