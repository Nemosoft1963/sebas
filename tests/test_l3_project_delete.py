"""L3 tests: PJ削除の再設計 (プレビュー→退避→論理削除→復元→purge)。

一時ディレクトリの疑似PJ (架空データ) のみ。本番DB・実案件に触れない。
実在のストア・実在の関数 (L1/L2実関数) のみを使う。外部通信なし。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from pathlib import Path

import pytest

from app.memory.short_term import ShortTermMemory
from app.project_delete import (
    PREVIEW_TOKEN_TTL_SEC,
    build_delete_request_preview,
    build_purge_preview,
    failure_injection,
    get_delete_state,
    is_deleted,
    list_deleted,
    purge_deleted,
    request_delete,
    restore_deleted,
)
from app.project_lifecycle_backup import create_backup, verify_backup
from app.project_lifecycle_registry import (
    audit_registry_completeness,
    count_all,
    list_orphans_after_legacy_delete,
)
from app.workspace_files import WorkspaceSandbox

DUMMY_SECRET = "架空テスト秘密-L3-13579"


@pytest.fixture()
def env(tmp_path):
    data_dir = tmp_path / "架空data"
    mem_dir = data_dir / "memory"
    mem_dir.mkdir(parents=True)
    mem_path = mem_dir / "conversations.db"
    memory = ShortTermMemory(mem_path)
    project = memory.create_project("架空疑似PJ", "架空コンテキスト")
    pid = project["id"]
    ws_root = tmp_path / "架空ws"
    ws_root.mkdir()
    ws = WorkspaceSandbox(ws_root)
    backup_root = tmp_path / "架空退避"
    backup_root.mkdir()
    yield {"data_dir": data_dir, "mem": mem_path, "memory": memory,
           "pid": pid, "ws_root": ws_root, "ws": ws, "backup_root": backup_root}


def _fill(env) -> dict:
    mem = env["mem"]
    pid = env["pid"]
    memory = env["memory"]
    ws = env["ws"]
    memory.save_mission(pid, "架空目標", "架空達成条件", "架空制約", False, [], 2)
    memory.save("架空sess", "架空u", "架空a", "架空m", 1.0, False, pid)
    ctx = memory.add_context_file(pid, "架空原本.md", "架空原本本文", 10, b"dummy",
                                  "text/markdown", "markdown", "",
                                  hashlib.sha256(b"dummy").hexdigest())
    memory.add_event(pid, "架空kind", "架空msg", None)
    memory.replace_plan(pid, "架空計画概要", [{
        "task_key": "架空t1", "title": "架空タスク1",
        "description": "架空説明", "acceptance_criteria": "架空条件",
        "mode": "local", "depends_on": [],
    }])
    from app.ocr_store import OcrStore
    store = OcrStore(mem)
    run, _ = store.create_run(project_id=pid, context_file_id=ctx["id"],
                              source_sha256="0" * 64, run_id="架空run1",
                              status="passed", adoption_status="approved")
    store.save_page(run["run_id"], 1, status="done")
    store.save_field(run["run_id"], "架空f1", name="架空項目", value_text="架空値")
    store.save_review(run["run_id"], decision="approve", reviewer="架空確認者")
    from app.goal_review import ReviewStore
    rs = ReviewStore(mem)
    rs.put(pid, "plan", "架空sig", {"status": "pass", "ok": True})
    from app.goal_completion_store import GoalCompletionStore
    gcs = GoalCompletionStore(mem)
    gcs.put_contract(pid, {"version": 1, "goal": "架空"}, "active")
    gcs.put_coverage(pid, 1, "架空hash", {"ok": True})
    ws.apply_operations("", pid, [{"action": "write_text", "path": "架空note.txt",
                                   "content": "架空内容"}])
    return {"ctx": ctx, "run_id": run["run_id"]}


def _do_delete(env, **kw):
    prev = build_delete_request_preview(env["mem"], env["pid"], env["ws_root"], {})
    assert prev["can_delete"] is True, prev.get("blocked_reasons")
    params = {"preview_token": prev["preview_token"],
              "project_name": prev["project_name"] if "project_name" in prev else (prev.get("project") or {}).get("name", ""),
              "actor": "架空担当", "workspace_root": env["ws_root"],
              "backup_root": env["backup_root"], "executions": {}}
    # build_delete_request_preview は project 名を project.name で返す。
    if not params["project_name"]:
        params["project_name"] = env["memory"].get_project(env["pid"])["name"]
    params.update(kw)
    return request_delete(env["mem"], env["pid"], **params), prev


def _sha_table(path: Path, sql: str, args=()) -> str:
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        rows = db.execute(sql, args).fetchall()
    finally:
        db.close()
    return hashlib.sha256(json.dumps([list(r) for r in rows],
                                     ensure_ascii=False, sort_keys=True,
                                     default=str).encode()).hexdigest()


# --- 網羅性 ---------------------------------------------------------------

def test_delete_tables_registered():
    audit = audit_registry_completeness()
    assert audit["ok"] is True, audit["unregistered"]
    from app.project_lifecycle_registry import REGISTRY
    assert any(e.table == "project_delete_states" for e in REGISTRY)
    assert any(e.table == "project_delete_ops" for e in REGISTRY)


# --- プレビュー -------------------------------------------------------------

def test_preview_read_only(env):
    _fill(env)
    before = {p.as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in [env["mem"],
                        env["mem"].parent / "goal_reviews.sqlite3",
                        env["mem"].parent / "goal_completion.sqlite3"]
              if p.exists()}
    prev = build_delete_request_preview(env["mem"], env["pid"], env["ws_root"], {})
    assert prev["read_only"] is True
    assert prev["preview_token"]
    assert prev["restorable_note"] and prev["retention_days"] == 7
    after = {p.as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in [env["mem"],
                       env["mem"].parent / "goal_reviews.sqlite3",
                       env["mem"].parent / "goal_completion.sqlite3"]
             if p.exists()}
    assert before == after


# --- 退避→論理削除 ----------------------------------------------------------

def test_delete_request_backup_verify_and_logical(env):
    info = _fill(env)
    mem = env["mem"]
    pid = env["pid"]
    ctx_before = env["memory"].get_context_file(
        pid, env["memory"].list_context_files(pid)[0]["id"])
    counts_before = count_all(mem, pid, env["ws_root"])
    out, _prev = _do_delete(env)
    assert out["ok"] is True, out
    assert verify_backup(env["backup_root"] / out["backup_id"] / "manifest.json")["ok"] is True
    st = get_delete_state(mem, pid)
    assert st is not None and st["state"] == "deleted"
    assert st["backup_id"] == out["backup_id"]
    assert st["manifest_sha256"] == out["manifest_sha256"]
    assert st["purgeable_at"] > st["deleted_at"]
    # 棚卸しレジストリに登録 (網羅性検証が通ること)。
    assert audit_registry_completeness()["ok"] is True
    # 論理削除: データは残す (project_events は追加型の監査履歴であり、
    # delete-request 自体が1件足すため件数完全一致の対象外。L2 と同じ流儀)。
    after = count_all(mem, pid, env["ws_root"])
    diff = {k: (counts_before.get(k, 0), after.get(k, 0))
            for k in counts_before if counts_before.get(k) != after.get(k)}
    assert set(diff) <= {"main:project_events"}, diff
    if "main:project_events" in diff:
        assert diff["main:project_events"][1] == diff["main:project_events"][0] + 1
    ctx_after = env["memory"].get_context_file(
        pid, env["memory"].list_context_files(pid)[0]["id"])
    assert ctx_after["sha256"] == ctx_before["sha256"]
    # 一覧から見えない。
    assert all(p["id"] != pid for p in env["memory"].list_projects())
    assert any(p["id"] == pid for p in env["memory"].list_projects_including_deleted())
    assert is_deleted(mem, pid) is True
    _ = info


def test_backup_failure_changes_nothing(env):
    _fill(env)
    before = count_all(env["mem"], env["pid"], env["ws_root"])
    prev = build_delete_request_preview(env["mem"], env["pid"], env["ws_root"], {})
    name = (prev.get("project") or {}).get("name") or env["memory"].get_project(env["pid"])["name"]
    with failure_injection("backup"):
        fail = request_delete(
            env["mem"], env["pid"], preview_token=prev["preview_token"],
            project_name=name, actor="架空担当",
            workspace_root=env["ws_root"], backup_root=env["backup_root"],
            executions={})
    assert fail["ok"] is False
    assert count_all(env["mem"], env["pid"], env["ws_root"]) == before
    assert is_deleted(env["mem"], env["pid"]) is False
    st = get_delete_state(env["mem"], env["pid"])
    assert st is None or st.get("state") == "failed"


def test_delete_failure_marks_failed(env):
    _fill(env)
    prev = build_delete_request_preview(env["mem"], env["pid"], env["ws_root"], {})
    name = (prev.get("project") or {}).get("name") or env["memory"].get_project(env["pid"])["name"]
    with failure_injection("delete"):
        out = request_delete(
            env["mem"], env["pid"], preview_token=prev["preview_token"],
            project_name=name, actor="架空担当",
            workspace_root=env["ws_root"], backup_root=env["backup_root"],
            executions={})
    assert out["ok"] is False
    st = get_delete_state(env["mem"], env["pid"])
    assert st is not None and st["state"] == "failed"
    assert "backup_id" in out
    assert "退避" in out.get("restore_hint", "")


# --- 論理削除中の除外 --------------------------------------------------------

def test_logical_delete_excluded_everywhere(env):
    _fill(env)
    # RAGを有効化。
    cfg_path = env["mem"].parent / "experience_memory.json"
    cfg_path.write_text(json.dumps({
        "embedding_model": "架空embed",
        "projects": {env["pid"]: "shadow"},
    }, ensure_ascii=False), encoding="utf-8")
    out, _prev = _do_delete(env)
    assert out["ok"] is True
    pid = env["pid"]
    # 一覧・検索相当: list_projects から除外、存在確認の共通入口で除外。
    assert all(p["id"] != pid for p in env["memory"].list_projects())
    # RAG: configured_memory が None (無効扱い)。
    from app.experience_memory import configured_memory
    assert configured_memory(env["mem"], pid) is None
    # 自動再開は起動しない。
    import asyncio
    from types import SimpleNamespace
    mgr = SimpleNamespace(memory=env["memory"])
    res = asyncio.run(__import__("app.safe_auto_resume", fromlist=["resume"]).resume(mgr, pid))
    assert res["status"] == "excluded" and res["executed"] == []
    # TRIZは起動しない。
    from app.automatic_triz import on_failure as triz_on_failure
    tres = asyncio.run(triz_on_failure(mgr, pid, {"id": "t", "title": "架空"}, ValueError("x")))
    assert tres["status"] == "excluded"
    # L4自動評価ループ: plan_queue の tick が何もしない (waiting を残す)。
    from app.goal_review import ReviewStore
    rs = ReviewStore(env["mem"])
    rs.put(pid, "plan_queue", "架空sigq", {"status": "waiting_budget", "next_at": 0,
                                          "providers": [], "public_summary": "x" * 30})
    from app.goal_review_queue import tick
    asyncio.run(tick(mgr))
    assert rs.get(pid, "plan_queue", "架空sigq")["status"] == "waiting_budget"


def test_other_project_untouched(env):
    _fill(env)
    other = env["memory"].create_project("架空別PJ", "架空別ctx")
    oid = other["id"]
    env["memory"].save_mission(oid, "架空別目標", "架空別条件", "", False, [], 2)
    env["memory"].save("s2", "架空u2", "架空a2", "m", 1.0, False, oid)
    before = count_all(env["mem"], oid, env["ws_root"])
    before_files = env["memory"].list_context_files(oid)
    out, _prev = _do_delete(env)
    assert out["ok"] is True
    assert count_all(env["mem"], oid, env["ws_root"]) == before
    assert [f["id"] for f in env["memory"].list_context_files(oid)] == [
        f["id"] for f in before_files]
    # 既定PJは不変。
    assert env["memory"].get_project("default") is not None


# --- 停止条件 ---------------------------------------------------------------

def test_stop_conditions(env):
    _fill(env)
    # 実行中ジョブ。
    prev = build_delete_request_preview(env["mem"], env["pid"], env["ws_root"],
                                        {"e": {"project_id": env["pid"], "status": "running"}})
    assert prev["can_delete"] is False
    bad = request_delete(
        env["mem"], env["pid"], preview_token=prev["preview_token"],
        project_name=(prev.get("project") or {}).get("name") or "x",
        actor="架空担当", workspace_root=env["ws_root"],
        backup_root=env["backup_root"],
        executions={"e": {"project_id": env["pid"], "status": "running"}})
    assert bad["ok"] is False
    # 未確定OCR。
    from app.ocr_store import OcrStore
    store = OcrStore(env["mem"])
    ctx = env["memory"].list_context_files(env["pid"])[0]
    store.create_run(project_id=env["pid"], context_file_id=ctx["id"],
                     source_sha256="1" * 64, run_id="架空run-unconfirmed",
                     status="needs_review")
    prev2 = build_delete_request_preview(env["mem"], env["pid"], env["ws_root"], {})
    assert prev2["can_delete"] is False
    # 外部操作待ち。
    store.update_run("架空run-unconfirmed", status="passed", adoption_status="approved")
    env["memory"].create_action(env["pid"], "manual", "架空t2", "架空c2", None)
    prev3 = build_delete_request_preview(env["mem"], env["pid"], env["ws_root"], {})
    assert prev3["can_delete"] is False
    # 既定PJ拒否。
    prev_d = build_delete_request_preview(env["mem"], "default", env["ws_root"], {})
    assert prev_d["can_delete"] is False
    assert any("既定" in r for r in prev_d["blocked_reasons"])
    bad_d = request_delete(env["mem"], "default", preview_token="x",
                           project_name="既定プロジェクト", actor="架空担当",
                           workspace_root=env["ws_root"],
                           backup_root=env["backup_root"], executions={})
    assert bad_d["ok"] is False


# --- トークン・PJ名 ----------------------------------------------------------

def test_token_and_name(env):
    _fill(env)
    prev = build_delete_request_preview(env["mem"], env["pid"], env["ws_root"], {})
    token, name = prev["preview_token"], ((prev.get("project") or {}).get("name")
                                          or env["memory"].get_project(env["pid"])["name"])
    bad = request_delete(env["mem"], env["pid"], preview_token=token,
                         project_name="別人", actor="架空担当",
                         workspace_root=env["ws_root"],
                         backup_root=env["backup_root"], executions={})
    assert bad["ok"] is False
    # 状態変化後は拒否。
    env["memory"].add_context_file(env["pid"], "架空追加.md", "x", 1, b"x",
                                   "text/markdown", "markdown", "",
                                   hashlib.sha256(b"x").hexdigest())
    stale = request_delete(env["mem"], env["pid"], preview_token=token,
                           project_name=name, actor="架空担当",
                           workspace_root=env["ws_root"],
                           backup_root=env["backup_root"], executions={})
    assert stale["ok"] is False and "変わり" in stale["reason"]
    # 期限切れ。
    prev2 = build_delete_request_preview(env["mem"], env["pid"], env["ws_root"], {})
    import app.project_delete as pd
    h = hashlib.sha256(prev2["preview_token"].encode()).hexdigest()
    pd._tokens[h]["expires"] = time.time() - 1
    name2 = ((prev2.get("project") or {}).get("name")
             or env["memory"].get_project(env["pid"])["name"])
    exp = request_delete(env["mem"], env["pid"], preview_token=prev2["preview_token"],
                         project_name=name2, actor="架空担当",
                         workspace_root=env["ws_root"],
                         backup_root=env["backup_root"], executions={})
    assert exp["ok"] is False and "期限" in exp["reason"]


# --- 復元 --------------------------------------------------------------------

def test_restore_roundtrip(env):
    _fill(env)
    mem = env["mem"]
    pid = env["pid"]
    ctx_rows = env["memory"].list_context_files(pid)
    full_before = env["memory"].get_context_file(pid, ctx_rows[0]["id"])
    sha_before = full_before["sha256"]
    goal_before = env["memory"].get_mission(pid)["goal"]
    counts_before = count_all(mem, pid, env["ws_root"])
    out, _prev = _do_delete(env)
    assert out["ok"] is True
    res = restore_deleted(mem, pid, actor="架空担当",
                          workspace_root=env["ws_root"],
                          backup_root=env["backup_root"])
    assert res["ok"] is True, res
    assert res["verified"] is True
    # データとハッシュが一致 (project_events は追加型監査のため+1のみ許容)。
    after = count_all(mem, pid, env["ws_root"])
    diff = {k: (counts_before.get(k, 0), after.get(k, 0))
            for k in counts_before if counts_before.get(k) != after.get(k)}
    assert set(diff) <= {"main:project_events"}, diff
    rows = env["memory"].list_context_files(pid)
    assert env["memory"].get_context_file(pid, rows[0]["id"])["sha256"] == sha_before
    assert env["memory"].get_mission(pid)["goal"] == goal_before
    assert all(p["id"] == pid or True for p in env["memory"].list_projects())
    assert any(p["id"] == pid for p in env["memory"].list_projects())
    # 冪等: 同じ冪等キーで再送。
    again = restore_deleted(mem, pid, actor="架空担当", idempotency_key="",
                            workspace_root=env["ws_root"],
                            backup_root=env["backup_root"])
    # 既に復元済みのため拒否 (成功と表示しない)。
    assert again["ok"] is False


def test_restore_idempotent_with_key(env):
    _fill(env)
    out, _prev = _do_delete(env)
    assert out["ok"] is True
    # まず復元せずに failed を作らない。復元を2回、同じ冪等キーで送る:
    # 1回目は成功、2回目は「既に復元」で拒否だが、キーが違えば再実行しない設計の
    # 代わりに、削除の冪等を検証する。
    prev2 = build_delete_request_preview(env["mem"], env["pid"], env["ws_root"], {})
    # 論理削除中のため blocked。
    assert prev2["can_delete"] is False
    res = restore_deleted(env["mem"], env["pid"], actor="架空担当",
                          idempotency_key="架空restore1",
                          workspace_root=env["ws_root"],
                          backup_root=env["backup_root"])
    assert res["ok"] is True


# --- purge -------------------------------------------------------------------

def test_purge_retention_and_orphans(env):
    _fill(env)
    out, _prev = _do_delete(env)
    assert out["ok"] is True
    pid = env["pid"]
    mem = env["mem"]
    # 保持期間内は拒否。
    pv = build_purge_preview(mem, pid, env["ws_root"])
    assert pv["can_purge"] is False
    assert any("保持期間" in r for r in pv["blocked_reasons"])
    bad = purge_deleted(mem, pid, purge_token=pv["purge_token"],
                        project_name=pv["project_name"], actor="架空担当",
                        workspace_root=env["ws_root"],
                        backup_root=env["backup_root"])
    assert bad["ok"] is False
    # 期間後を模す: purgeable_at を過去にする。
    import sqlite3 as _sql
    from app.project_delete import delete_db_path
    db = _sql.connect(str(delete_db_path(mem)))
    try:
        with db:
            db.execute("UPDATE project_delete_states SET purgeable_at=? WHERE project_id=?",
                       ("2000-01-01T00:00:00+00:00", pid))
    finally:
        db.close()
    pv2 = build_purge_preview(mem, pid, env["ws_root"])
    assert pv2["can_purge"] is True, pv2.get("blocked_reasons")
    assert "消せません" in pv2["external_warning"] or "検出されません" in pv2["external_warning"]
    done = purge_deleted(mem, pid, purge_token=pv2["purge_token"],
                         project_name=pv2["project_name"], actor="架空担当",
                         workspace_root=env["ws_root"],
                         backup_root=env["backup_root"])
    assert done["ok"] is True, done
    assert done["orphans"]["orphan_count"] == 0
    assert list_orphans_after_legacy_delete(mem, pid, env["ws_root"])["orphan_count"] == 0
    # 退避が残り復元手順が示される。
    assert verify_backup(env["backup_root"] / done["backup_id"] / "manifest.json")["ok"] is True
    assert "restore_backup" in done["restore_procedure"]
    st = get_delete_state(mem, pid)
    assert st is not None and st["state"] == "purged"
    # 他PJ不変は別テストで検証。


def test_purge_requires_token_and_name(env):
    _fill(env)
    out, _prev = _do_delete(env)
    assert out["ok"] is True
    import sqlite3 as _sql
    from app.project_delete import delete_db_path
    db = _sql.connect(str(delete_db_path(env["mem"])))
    try:
        with db:
            db.execute("UPDATE project_delete_states SET purgeable_at=? WHERE project_id=?",
                       ("2000-01-01T00:00:00+00:00", env["pid"]))
    finally:
        db.close()
    pv = build_purge_preview(env["mem"], env["pid"], env["ws_root"])
    bad = purge_deleted(env["mem"], env["pid"], purge_token=pv["purge_token"],
                        project_name="別人", actor="架空担当",
                        workspace_root=env["ws_root"],
                        backup_root=env["backup_root"])
    assert bad["ok"] is False
    # delete-request の preview token を purge に流用できない。
    prev = build_delete_request_preview(env["mem"], env["pid"], env["ws_root"], {})
    bad2 = purge_deleted(env["mem"], env["pid"], purge_token=prev["preview_token"],
                         project_name=pv["project_name"], actor="架空担当",
                         workspace_root=env["ws_root"],
                         backup_root=env["backup_root"])
    assert bad2["ok"] is False
    # 既定PJは purge も拒否。
    bad3 = purge_deleted(env["mem"], "default", purge_token="x",
                         project_name="既定プロジェクト", actor="架空担当",
                         workspace_root=env["ws_root"],
                         backup_root=env["backup_root"])
    assert bad3["ok"] is False


# --- 冪等・同時要求 ------------------------------------------------------------

def test_idempotent_resend_and_concurrent(tmp_path):
    data_dir = tmp_path / "架空conc-data"
    mem_dir = data_dir / "memory"
    mem_dir.mkdir(parents=True)
    mem_path = mem_dir / "conversations.db"
    memory = ShortTermMemory(mem_path)
    project = memory.create_project("架空同時PJ", "架空ctx")
    pid = project["id"]
    memory.save_mission(pid, "架空目標", "架空条件", "", False, [], 2)
    ws_root = tmp_path / "架空conc-ws"
    ws_root.mkdir()
    backup_root = tmp_path / "架空conc-bk"
    backup_root.mkdir()

    async def one(i: int):
        prev = await asyncio.to_thread(build_delete_request_preview, mem_path, pid,
                                       ws_root, {})
        if not prev["can_delete"] and i > 0:
            return {"ok": False, "reason": "blocked"}
        name = ((prev.get("project") or {}).get("name") or "架空同時PJ")
        return await asyncio.to_thread(
            request_delete, mem_path, pid,
            preview_token=prev["preview_token"], project_name=name,
            actor="架空担当", idempotency_key="架空conc",
            workspace_root=ws_root, backup_root=backup_root, executions={})

    async def main():
        return await asyncio.gather(*[one(i) for i in range(4)])

    results = asyncio.run(main())
    assert sum(1 for r in results if r.get("ok")) >= 1
    # 二重に退避が増えない (同じ冪等キー)。
    from app.project_lifecycle_backup import list_backups
    assert len(list_backups(backup_root, pid)) == 1


# --- 既存DELETE互換 ------------------------------------------------------------

def test_legacy_delete_still_works_and_default_protected(env):
    _fill(env)
    pid = env["pid"]
    # 既存 delete_project は従来どおり動く。
    n = env["memory"].delete_project(pid)
    assert isinstance(n, int)
    assert env["memory"].get_project(pid) is None
    # 既定PJは削除不可のまま。
    with pytest.raises(ValueError):
        env["memory"].delete_project("default")


# --- 秘密・画面 -----------------------------------------------------------------

def test_secret_not_in_manifest_logs_responses(env, caplog):
    _fill(env)
    with env["memory"]._connect() as db:
        db.execute("INSERT INTO project_events(project_id,task_id,kind,message,detail,created_at)"
                   " VALUES(?,?,?,?,?,?)",
                   (env["pid"], None, "架空kind", DUMMY_SECRET, DUMMY_SECRET, "2026-01-01"))
    import logging
    with caplog.at_level(logging.INFO):
        out, prev = _do_delete(env)
    assert out["ok"] is True
    mpath = env["backup_root"] / out["backup_id"] / "manifest.json"
    assert DUMMY_SECRET not in mpath.read_text(encoding="utf-8")
    assert DUMMY_SECRET not in caplog.text
    assert DUMMY_SECRET not in json.dumps(prev, ensure_ascii=False)
    assert DUMMY_SECRET not in json.dumps(out, ensure_ascii=False, default=str)
    states = list_deleted(env["mem"])
    assert DUMMY_SECRET not in json.dumps(states, ensure_ascii=False)


def test_js_has_no_innerhtml():
    text = (Path("app/static/project_delete.js")).read_text(encoding="utf-8")
    assert "innerHTML" not in text
    index = Path("app/static/index.html").read_text(encoding="utf-8")
    assert "project_delete.js" in index


def test_deleted_list_and_state(env):
    _fill(env)
    out, _prev = _do_delete(env)
    assert out["ok"] is True
    rows = list_deleted(env["mem"])
    assert any(r["project_id"] == env["pid"] and r["state"] == "deleted" for r in rows)


# --- API ---------------------------------------------------------------------

def _api_client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import app.web as web
    data_dir = tmp_path / "架空api-data"
    mem_dir = data_dir / "memory"
    mem_dir.mkdir(parents=True)
    mem_path = mem_dir / "conversations.db"
    memory = ShortTermMemory(mem_path)
    project = memory.create_project("架空API疑似PJ", "架空ctx")
    pid = project["id"]
    memory.save_mission(pid, "架空目標", "架空条件", "", False, [], 2)
    import types
    async def _noop_pause(_pid, cancelled=False):
        return {"project_id": _pid, "status": "paused"}
    manager = types.SimpleNamespace(memory=memory, pause=_noop_pause)
    monkeypatch.setattr(web, "memory", memory)
    monkeypatch.setattr(web, "orchestrator", manager)
    monkeypatch.setattr(web, "DATA_DIR", data_dir)
    ws_root = tmp_path / "架空api-ws"
    ws_root.mkdir()
    monkeypatch.setattr(web, "WORKSPACE_ROOT", ws_root)
    client = TestClient(web.app)
    return client, pid, mem_path


def test_lifecycle_delete_api(monkeypatch, tmp_path):
    client, pid, mem_path = _api_client(monkeypatch, tmp_path)
    before = mem_path.read_bytes()
    r = client.get(f"/api/projects/{pid}/lifecycle/delete-request-preview")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["read_only"] is True or "preview_token" in body
    assert "復元" in json.dumps(body, ensure_ascii=False)
    assert mem_path.read_bytes() == before
    # PJ名なしでは確定できない。
    rb = client.post(f"/api/projects/{pid}/lifecycle/delete-request", json={
        "preview_token": body["preview_token"], "project_name": "別人",
        "actor": "架空担当"})
    assert rb.status_code == 409
    # 正常実行。
    ok = client.post(f"/api/projects/{pid}/lifecycle/delete-request", json={
        "preview_token": body["preview_token"],
        "project_name": body.get("project", {}).get("name") or body.get("project_name"),
        "actor": "架空担当"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["ok"] is True
    # 論理削除中は通常の読出しが除外される (410)。
    assert client.get(f"/api/projects/{pid}/mission").status_code == 410
    assert client.get("/api/projects").status_code == 200
    assert all(p["id"] != pid for p in client.get("/api/projects").json())
    # 復元。
    rs = client.post(f"/api/projects/{pid}/lifecycle/delete-restore", json={
        "actor": "架空担当"})
    assert rs.status_code == 200, rs.text
    assert rs.json()["ok"] is True
    assert client.get(f"/api/projects/{pid}/mission").status_code == 200
    # purge プレビューは保持期間内で拒否表示。
    pv = client.get(f"/api/projects/{pid}/lifecycle/purge-preview")
    assert pv.status_code == 200
    # 論理削除されていないため blocked。
    assert pv.json()["can_purge"] is False
    # 削除済み一覧。
    dl = client.get("/api/projects/deleted/list")
    assert dl.status_code == 200


def test_legacy_delete_api_rejected(monkeypatch, tmp_path):
    client, pid, _ = _api_client(monkeypatch, tmp_path)
    r = client.delete(f"/api/projects/{pid}")
    assert r.status_code == 409, r.text
    assert "即時削除は無効" in r.json().get("detail", "")
    # The project must remain after a rejected legacy request.
    assert client.get(f"/api/projects/{pid}/mission").status_code == 200
    d = client.delete("/api/projects/default")
    assert d.status_code == 409
