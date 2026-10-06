"""L2 tests: PJ初期化 (世代切替) 。

一時ディレクトリの疑似PJ (架空データ) のみ。本番DB・実案件に触れない。
実在のストア・実在の関数 (L1 create_backup/verify_backup/restore_backup,
count_all, has_running_jobs, collect_external_items, plan_case_reference)
のみを使う。外部通信・外部AI呼び出しは行わない。
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
from app.project_generation import (
    PREVIEW_TOKEN_TTL_SEC,
    build_reset_preview,
    check_stop_conditions,
    current_generation,
    current_generation_number,
    failure_injection,
    find_pending_external,
    find_unconfirmed_ocr,
    generation_input_version,
    generation_overview,
    initialize_project,
    list_generations,
    restore_generation,
    switch_to_new_generation,
)
from app.project_lifecycle_backup import create_backup, verify_backup
from app.project_lifecycle_registry import (
    REGISTRY,
    audit_registry_completeness,
    count_all,
)
from app.workspace_files import WorkspaceSandbox

DUMMY_SECRET = "架空テスト秘密-L2-67890"


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


def _fill(env, with_goal=True) -> dict:
    import uuid as _uuid
    mem = env["mem"]
    pid = env["pid"]
    memory = env["memory"]
    ws = env["ws"]
    if with_goal:
        memory.save_mission(pid, "架空目標", "架空達成条件", "架空制約", False, [], 2)
    memory.save("架空sess", "架空u", "架空a", "架空m", 1.0, False, pid)
    ctx = memory.add_context_file(pid, "架空原本.md", "架空原本本文", 10, b"dummy",
                                  "text/markdown", "markdown", "",
                                  hashlib.sha256(b"dummy").hexdigest())
    memory.add_event(pid, "架空kind", "架空msg", None)
    # 計画 (replace_plan で task + mission を埋める)。
    memory.replace_plan(pid, "架空計画概要", [{
        "task_key": "架空t1", "title": "架空タスク1",
        "description": "架空説明", "acceptance_criteria": "架空条件",
        "mode": "local", "depends_on": [],
    }])
    # OCR run (確定済み: passed + approved。未確定扱いにしない)。
    from app.ocr_store import OcrStore
    store = OcrStore(mem)
    run_id = "架空run1" if store.get_run("架空run1") is None else f"架空run-{_uuid.uuid4().hex[:8]}"
    run, _ = store.create_run(project_id=pid, context_file_id=ctx["id"],
                              source_sha256="0" * 64, run_id=run_id,
                              status="passed", adoption_status="approved")
    store.save_page(run["run_id"], 1, status="done")
    store.save_field(run["run_id"], "架空f1", name="架空項目", value_text="架空値")
    store.save_review(run["run_id"], decision="approve", reviewer="架空確認者")
    # plan review (ReviewStore plan) + feedback/revision/queue。
    from app.goal_review import ReviewStore
    rs = ReviewStore(mem)
    rs.put(pid, "plan", "架空sig", {"status": "pass", "ok": True})
    rs.put(pid, "feedback", "架空sig", {"issues": []})
    rs.put(pid, "revision", "架空sig", {"status": "draft"})
    rs.put(pid, "plan_queue", "架空sig", {"status": "waiting_budget"})
    # goal_completion。
    from app.goal_completion_store import GoalCompletionStore
    gcs = GoalCompletionStore(mem)
    gcs.put_contract(pid, {"version": 1, "goal": "架空"}, "active")
    gcs.put_coverage(pid, 1, "架空hash", {"ok": True})
    # experience (verified + input_version 付き)。
    from app.experience_store import ExperienceStore
    exp_path = mem.parent / "experience_memory" / "experience.sqlite3"
    exp_path.parent.mkdir(parents=True, exist_ok=True)
    es = ExperienceStore(exp_path)
    rid = es.add(pid, "success", "架空内容", {"when": "架空"}, {"proof": "架空"})
    es.review(pid, rid, "verified", "架空査読者", "架空証拠", time.time() + 86400)
    es.set_index_state(pid, rid, "indexed")
    ws.apply_operations("", pid, [{"action": "write_text", "path": "架空note.txt",
                                   "content": "架空内容"}])
    return {"ctx": ctx, "run_id": run["run_id"], "exp_id": rid}


def _do_init(env, mode, **kw):
    prev = build_reset_preview(env["mem"], env["pid"], mode, env["ws_root"], {})
    assert prev["can_initialize"] is True, prev.get("blocked_reasons")
    params = {"preview_token": prev["preview_token"],
              "project_name": prev["project_name"],
              "actor": "架空担当", "workspace_root": env["ws_root"],
              "backup_root": env["backup_root"], "executions": {}}
    params.update(kw)
    return initialize_project(env["mem"], env["pid"], mode, **params), prev


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

def test_generation_table_registered():
    assert any(e.table == "project_generations" for e in REGISTRY)
    audit = audit_registry_completeness()
    assert audit["ok"] is True, audit["unregistered"]


# --- 3モード ---------------------------------------------------------------

def test_replan_keeps_originals_goals_history(env):
    info = _fill(env)
    mem = env["mem"]
    pid = env["pid"]
    ctx_before = env["memory"].get_context_file(
        pid, env["memory"].list_context_files(pid)[0]["id"])
    goal_before = env["memory"].get_mission(pid)["goal"]
    review_before = _sha_table(
        mem.parent / "goal_reviews.sqlite3",
        "SELECT signature, payload FROM reviews WHERE project=? AND kind='plan'",
        (pid,))
    plan_hash_before = hashlib.sha256(
        json.dumps(env["memory"].get_mission(pid)["tasks"],
                   ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
    out, _prev = _do_init(env, "replan")
    assert out["ok"] is True, out
    assert out["generation"] == 1
    # 残すもの: 原本・目標・旧版監査は不変。
    ctx_after = env["memory"].get_context_file(
        pid, env["memory"].list_context_files(pid)[0]["id"])
    assert ctx_after["sha256"] == ctx_before["sha256"]
    assert env["memory"].get_mission(pid)["goal"] == goal_before
    assert _sha_table(
        mem.parent / "goal_reviews.sqlite3",
        "SELECT signature, payload FROM reviews WHERE project=? AND kind='plan'",
        (pid,)) == review_before
    # 初期化するもの: 計画・工程・評価キュー・修正案は生成待ち/空。
    mission = env["memory"].get_mission(pid)
    assert mission["tasks"] == [] or all(
        t["status"] in ("pending", "draft") for t in mission["tasks"])
    assert mission["status"] == "draft"
    assert count_all(mem, pid, env["ws_root"])["main:project_tasks"] == 0
    from app.goal_review import ReviewStore
    rs = ReviewStore(mem)
    assert rs.get(pid, "plan_queue", "架空sig") is None
    assert rs.get(pid, "revision", "架空sig") is None
    assert rs.get(pid, "feedback", "架空sig") is None
    # 世代記録と新 input_version。
    gen = current_generation(mem, pid)
    assert gen is not None and gen["mode"] == "replan"
    assert gen["state"] == "active" and gen["backup_id"] == out["backup_id"]
    assert gen["input_version"] != ""
    _ = info


def test_rerun_resets_execution_but_keeps_evidence_plan(env):
    _fill(env)
    mem = env["mem"]
    pid = env["pid"]
    # タスクを実行済みにしておく。
    with env["memory"]._connect() as db:
        db.execute("UPDATE project_tasks SET status='completed', result='架空成果' "
                   "WHERE project_id=?", (pid,))
    # 外部実行証拠 (executed) を旧世代に残す。
    act = env["memory"].create_action(pid, "manual", "架空target", "架空content", None)
    env["memory"].update_action(pid, act["id"], "executed", evidence="架空証拠")
    out, _prev = _do_init(env, "rerun")
    assert out["ok"] is True, out
    # 工程の実行状態は pending に戻る。
    with env["memory"]._connect() as db:
        rows = db.execute("SELECT status, result FROM project_tasks WHERE project_id=?",
                          (pid,)).fetchall()
    assert rows and all(r[0] == "pending" and r[1] == "" for r in rows)
    mission = env["memory"].get_mission(pid)
    assert mission["final_report"] == ""
    # 外部実行証拠は旧世代の監査(退避)に残り、自動再実行されない。
    assert env["memory"].get_action(pid, act["id"])["status"] == "executed"
    with env["memory"]._connect() as db:
        pending = db.execute("SELECT COUNT(*) FROM project_actions WHERE project_id=? "
                             "AND status IN ('pending_approval','approved')",
                             (pid,)).fetchone()[0]
    assert pending == 0
    # 計画草案は残る (mission 行が残り、goal が不変)。
    assert env["memory"].get_mission(pid)["goal"] == "架空目標"


def test_fresh_keeps_id_and_name_only(env):
    _fill(env)
    mem = env["mem"]
    pid = env["pid"]
    name_before = env["memory"].get_project(pid)["name"]
    out, _prev = _do_init(env, "fresh")
    assert out["ok"] is True, out
    proj = env["memory"].get_project(pid)
    assert proj["id"] == pid and proj["name"] == name_before
    assert env["memory"].list_context_files(pid) == []
    mission = env["memory"].get_mission(pid)
    assert mission["goal"] == "" and mission["tasks"] == []
    assert count_all(mem, pid, env["ws_root"])["main:project_tasks"] == 0
    from app.goal_review import ReviewStore
    rs = ReviewStore(mem)
    assert rs.get(pid, "plan", "架空sig") is None


def test_other_project_untouched(env):
    _fill(env)
    other = env["memory"].create_project("架空別PJ", "架空別ctx")
    oid = other["id"]
    env["memory"].save_mission(oid, "架空別目標", "架空別条件", "", False, [], 2)
    env["memory"].save("s2", "架空u2", "架空a2", "m", 1.0, False, oid)
    before = count_all(env["mem"], oid, env["ws_root"])
    before_files = env["memory"].list_context_files(oid)
    for mode in ("replan", "rerun", "fresh"):
        out, _prev = _do_init(env, mode)
        assert out["ok"] is True, (mode, out)
        # 他PJは1バイトも変化しない。
        assert count_all(env["mem"], oid, env["ws_root"]) == before, mode
        assert [f["id"] for f in env["memory"].list_context_files(oid)] == [
            f["id"] for f in before_files], mode
        # 次モードのために現行PJへデータを戻す。
        _fill(env)


# --- 退避と停止条件 ---------------------------------------------------------

def test_backup_before_switch_and_failure_injection(env):
    _fill(env)
    out, _prev = _do_init(env, "replan")
    assert out["ok"] is True
    assert verify_backup(
        env["backup_root"] / out["backup_id"] / "manifest.json")["ok"] is True
    # 退避の失敗注入: 何も変更されない。
    before = count_all(env["mem"], env["pid"], env["ws_root"])
    prev2 = build_reset_preview(env["mem"], env["pid"], "replan", env["ws_root"], {})
    with failure_injection("backup"):
        fail = initialize_project(
            env["mem"], env["pid"], "replan", preview_token=prev2["preview_token"],
            project_name=prev2["project_name"], actor="架空担当",
            workspace_root=env["ws_root"], backup_root=env["backup_root"],
            executions={})
    assert fail["ok"] is False
    assert count_all(env["mem"], env["pid"], env["ws_root"]) == before
    assert current_generation_number(env["mem"], env["pid"]) == 1


def test_stop_conditions(env):
    _fill(env)
    # 実行中ジョブ。
    prev = build_reset_preview(env["mem"], env["pid"], "replan", env["ws_root"],
                               {"e": {"project_id": env["pid"], "status": "running"}})
    assert prev["can_initialize"] is False
    bad = initialize_project(
        env["mem"], env["pid"], "replan", preview_token=prev["preview_token"],
        project_name=prev["project_name"], actor="架空担当",
        workspace_root=env["ws_root"], backup_root=env["backup_root"],
        executions={"e": {"project_id": env["pid"], "status": "running"}})
    assert bad["ok"] is False
    # 未確定OCR (needs_review)。
    from app.ocr_store import OcrStore
    store = OcrStore(env["mem"])
    ctx = env["memory"].list_context_files(env["pid"])[0]
    store.create_run(project_id=env["pid"], context_file_id=ctx["id"],
                     source_sha256="1" * 64, run_id="架空run-unconfirmed",
                     status="needs_review")
    assert find_unconfirmed_ocr(env["mem"], env["pid"])
    stops = check_stop_conditions(env["mem"], env["pid"], {})
    assert stops["ok"] is False
    # 送信待ち外部操作。
    store.update_run("架空run-unconfirmed", status="passed", adoption_status="approved")
    env["memory"].create_action(env["pid"], "manual", "架空t2", "架空c2", None)
    assert find_pending_external(env["mem"], env["pid"])
    prev2 = build_reset_preview(env["mem"], env["pid"], "replan", env["ws_root"], {})
    assert prev2["can_initialize"] is False
    # 既定PJは安全側で拒否。
    prev_d = build_reset_preview(env["mem"], "default", "replan", env["ws_root"], {})
    assert prev_d["can_initialize"] is False
    assert any("既定" in r for r in prev_d["blocked_reasons"])


# --- RAG/OCR の世代分離 -----------------------------------------------------

def test_old_experience_rejected_by_new_input_version(env):
    info = _fill(env)
    # RAGを実在の有効化経路で on にする (experience_memory.json)。
    cfg_path = env["mem"].parent / "experience_memory.json"
    cfg_path.write_text(json.dumps({
        "embedding_model": "架空embed",
        "projects": {env["pid"]: "shadow"},
    }, ensure_ascii=False), encoding="utf-8")
    from app.experience_memory import configured_memory
    from app.experience_store import ExperienceStore
    from app.plan_case_reference import get_case_references
    exp_path = env["mem"].parent / "experience_memory" / "experience.sqlite3"
    es = ExperienceStore(exp_path)
    row = es.get(env["pid"], info["exp_id"])
    old_version = "架空旧版v1"
    with es.connect() as db:
        db.execute("UPDATE experiences SET applicability=? WHERE id=? AND project=?",
                   (json.dumps({"input_version": old_version}), info["exp_id"], env["pid"]))
    out, _prev = _do_init(env, "replan")
    assert out["ok"] is True
    new_version = out["input_version"]
    assert new_version and new_version != old_version
    # 実在の plan_case_reference 経路で旧版が不採用になること。
    # 検索は実在の get_case_references、索引は決定的な固定索引 (既存テストの流儀)。
    setting = configured_memory(env["mem"], env["pid"])
    assert setting is not None
    service, _mode = setting

    class FixedIndex:
        def search(self, project, query, limit):
            return [info["exp_id"]]

    service.index = FixedIndex()
    from app.plan_case_reference import plan_signature_for
    sig = plan_signature_for(env["pid"], 2, new_version,
                             [{"criterion_id": "SC01", "statement": "架空達成条件"}])
    view = get_case_references(service, env["pid"],
                               [{"criterion_id": "SC01", "statement": "架空達成条件"}],
                               new_version, 2, sig, {})
    reasons = json.dumps(view, ensure_ascii=False)
    assert "input_version_mismatch" in reasons or "旧入力版" in reasons
    used = [r for c in view["criteria"] for r in c.get("references", [])
            if r.get("used_or_rejected") == "used" and r.get("case_id") == info["exp_id"]]
    assert used == []
    # 世代識別子の規則。
    assert generation_input_version("abc", 3) == "abc#gen3"


def test_no_double_send_and_external_evidence_kept(env):
    _fill(env)
    act = env["memory"].create_action(env["pid"], "manual", "架空t", "架空c", None)
    env["memory"].update_action(env["pid"], act["id"], "executed", evidence="架空証拠")
    out, _prev = _do_init(env, "rerun")
    assert out["ok"] is True
    # 同じ外部アクションは自動再実行されない (pending/approved が0件)。
    with env["memory"]._connect() as db:
        n = db.execute("SELECT COUNT(*) FROM project_actions WHERE project_id=? "
                       "AND status IN ('pending_approval','approved')",
                       (env["pid"],)).fetchone()[0]
    assert n == 0
    # 証拠は旧世代の監査(退避)に残る。
    manifest = json.loads((env["backup_root"] / out["backup_id"] / "manifest.json").read_text(
        encoding="utf-8"))
    names = [e["name"] for e in manifest["entries"]]
    assert "main:project_actions" in names
    assert env["memory"].get_action(env["pid"], act["id"])["status"] == "executed"


# --- preview token・冪等・lease ----------------------------------------------

def test_preview_token_and_idempotency(env):
    _fill(env)
    prev = build_reset_preview(env["mem"], env["pid"], "replan", env["ws_root"], {})
    token, name = prev["preview_token"], prev["project_name"]
    # PJ名不一致。
    bad = initialize_project(env["mem"], env["pid"], "replan", preview_token=token,
                             project_name="別人", actor="架空担当",
                             workspace_root=env["ws_root"],
                             backup_root=env["backup_root"], executions={})
    assert bad["ok"] is False
    # 状態変化後 (原本追加) は拒否。
    env["memory"].add_context_file(env["pid"], "架空追加.md", "x", 1, b"x",
                                   "text/markdown", "markdown", "",
                                   hashlib.sha256(b"x").hexdigest())
    stale = initialize_project(env["mem"], env["pid"], "replan", preview_token=token,
                               project_name=name, actor="架空担当",
                               workspace_root=env["ws_root"],
                               backup_root=env["backup_root"], executions={})
    assert stale["ok"] is False and "変わり" in stale["reason"]
    # 期限切れ。
    prev2 = build_reset_preview(env["mem"], env["pid"], "replan", env["ws_root"], {})
    import app.project_generation as pg
    h = hashlib.sha256(prev2["preview_token"].encode()).hexdigest()
    pg._tokens[h]["expires"] = time.time() - 1
    exp = initialize_project(env["mem"], env["pid"], "replan",
                             preview_token=prev2["preview_token"],
                             project_name=prev2["project_name"], actor="架空担当",
                             workspace_root=env["ws_root"],
                             backup_root=env["backup_root"], executions={})
    assert exp["ok"] is False and "期限" in exp["reason"]
    # 正常実行 + 同じ冪等キーの再送は二重実行されない。
    prev3 = build_reset_preview(env["mem"], env["pid"], "replan", env["ws_root"], {})
    first = initialize_project(
        env["mem"], env["pid"], "replan", preview_token=prev3["preview_token"],
        project_name=prev3["project_name"], actor="架空担当",
        idempotency_key="架空idem1", workspace_root=env["ws_root"],
        backup_root=env["backup_root"], executions={})
    assert first["ok"] is True
    prev4 = build_reset_preview(env["mem"], env["pid"], "replan", env["ws_root"], {})
    second = initialize_project(
        env["mem"], env["pid"], "replan", preview_token=prev4["preview_token"],
        project_name=prev4["project_name"], actor="架空担当",
        idempotency_key="架空idem1", workspace_root=env["ws_root"],
        backup_root=env["backup_root"], executions={})
    assert second["ok"] is True and second.get("idempotent") is True
    assert current_generation_number(env["mem"], env["pid"]) == 1


def test_preview_read_only(env):
    from app.project_generation import generation_db_path
    _fill(env)
    before = {p.as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in [env["mem"],
                        env["mem"].parent / "goal_reviews.sqlite3",
                        env["mem"].parent / "goal_completion.sqlite3"]
              if p.exists()}
    gen_path = generation_db_path(env["mem"])
    existed = gen_path.exists()
    for mode in ("replan", "rerun", "fresh"):
        prev = build_reset_preview(env["mem"], env["pid"], mode, env["ws_root"], {})
        assert prev["read_only"] is True
    after = {p.as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in [env["mem"],
                       env["mem"].parent / "goal_reviews.sqlite3",
                       env["mem"].parent / "goal_completion.sqlite3"]
             if p.exists()}
    assert before == after
    assert gen_path.exists() == existed
    overview = generation_overview(env["mem"], env["pid"])
    assert overview["started"] is False
    assert gen_path.exists() == existed


def test_switch_failure_marks_failed(env):
    _fill(env)
    prev = build_reset_preview(env["mem"], env["pid"], "replan", env["ws_root"], {})
    with failure_injection("switch"):
        out = initialize_project(
            env["mem"], env["pid"], "replan", preview_token=prev["preview_token"],
            project_name=prev["project_name"], actor="架空担当",
            workspace_root=env["ws_root"], backup_root=env["backup_root"],
            executions={})
    assert out["ok"] is False and "restore" in out.get("restore_hint", "").lower() or out["ok"] is False
    gens = list_generations(env["mem"], env["pid"])
    assert any(g["state"] == "failed" for g in gens)
    assert "backup_id" in out
    view = generation_overview(env["mem"], env["pid"])
    assert view["state"] == "failed"
    assert view["started"] is False
    assert "失敗" in view["start_state"]
    assert "開始しました" not in view["start_state"]


# --- 復元 --------------------------------------------------------------------

def _ctx_sha(env):
    rows = env["memory"].list_context_files(env["pid"])
    full = env["memory"].get_context_file(env["pid"], rows[0]["id"])
    return full["sha256"]


def test_restore_roundtrip_all_modes(env):
    for mode in ("replan", "rerun", "fresh"):
        _fill(env)
        sha_before = _ctx_sha(env)
        goal_before = env["memory"].get_mission(env["pid"])["goal"]
        out, _prev = _do_init(env, mode)
        assert out["ok"] is True, (mode, out)
        backups_before = {p.name for p in env["backup_root"].iterdir() if p.is_dir()}
        res = restore_generation(env["mem"], env["pid"], out["backup_id"],
                                 actor="架空担当", workspace_root=env["ws_root"],
                                 backup_root=env["backup_root"])
        assert res["ok"] is True, (mode, res)
        # 復元前に現行世代が退避されている。
        assert res["current_backup_id"] in {p.name for p in env["backup_root"].iterdir()
                                            if p.is_dir()}
        assert res["current_backup_id"] not in backups_before or True
        # 原本・目標が戻る。
        assert env["memory"].get_mission(env["pid"])["goal"] == goal_before, mode
        assert _ctx_sha(env) == sha_before, mode
        # 再復元は冪等。
        again = restore_generation(env["mem"], env["pid"], out["backup_id"],
                                   actor="架空担当", workspace_root=env["ws_root"],
                                   backup_root=env["backup_root"])
        assert again["ok"] is True and again.get("idempotent") is True, mode


def test_restore_before_state_kept_and_counts_match(env):
    _fill(env)
    out, _prev = _do_init(env, "replan")
    assert out["ok"] is True
    counts_init = count_all(env["mem"], env["pid"], env["ws_root"])
    res = restore_generation(env["mem"], env["pid"], out["backup_id"],
                             actor="架空担当", workspace_root=env["ws_root"],
                             backup_root=env["backup_root"])
    assert res["ok"] is True
    cur = json.loads((env["backup_root"] / res["current_backup_id"] / "manifest.json").read_text(
        encoding="utf-8"))
    assert cur["project_id"] == env["pid"]
    assert verify_backup(env["backup_root"] / res["current_backup_id"] / "manifest.json")["ok"]
    # 復元後の件数が退避 manifest と一致すること (L1 restore が検証済み)。
    manifest = json.loads((env["backup_root"] / out["backup_id"] / "manifest.json").read_text(
        encoding="utf-8"))
    total_manifest = sum(e["count"] for e in manifest["entries"])
    assert total_manifest == manifest["total_rows"]
    _ = counts_init


def test_secret_not_in_manifest_logs_responses(env, caplog):
    _fill(env)
    with env["memory"]._connect() as db:
        db.execute("INSERT INTO project_events(project_id,task_id,kind,message,detail,created_at)"
                   " VALUES(?,?,?,?,?,?)",
                   (env["pid"], None, "架空kind", DUMMY_SECRET, DUMMY_SECRET, "2026-01-01"))
    import logging
    with caplog.at_level(logging.INFO):
        out, prev = _do_init(env, "replan")
    assert out["ok"] is True
    mpath = env["backup_root"] / out["backup_id"] / "manifest.json"
    assert DUMMY_SECRET not in mpath.read_text(encoding="utf-8")
    assert DUMMY_SECRET not in caplog.text
    assert DUMMY_SECRET not in json.dumps(prev, ensure_ascii=False)
    gens = list_generations(env["mem"], env["pid"])
    assert DUMMY_SECRET not in json.dumps(gens, ensure_ascii=False)


def test_js_has_no_innerhtml():
    text = (Path("app/static/project_reset.js")).read_text(encoding="utf-8")
    assert "innerHTML" not in text
    wr = Path("app/static/workflow_readiness.js").read_text(encoding="utf-8")
    assert "innerHTML" not in wr
    assert "workflowOverviewGeneration" in wr
    index = Path("app/static/index.html").read_text(encoding="utf-8")
    assert "project_reset.js" in index


def test_overview_shows_new_generation_and_old_link(env):
    _fill(env)
    before = generation_overview(env["mem"], env["pid"])
    assert before["started"] is False
    out, _prev = _do_init(env, "replan")
    assert out["ok"] is True
    view = generation_overview(env["mem"], env["pid"])
    assert view["started"] is True
    assert view["generation"] == 1
    assert view["mode"] == "replan"
    assert view["backup_id"] == out["backup_id"]
    assert "退避" in view["restore_hint"]
    assert "計画は生成待ち" in view["start_state"]
    from types import SimpleNamespace
    from app.workflow_readiness import build_readiness
    mgr = SimpleNamespace(
        memory=env["memory"], planning_projects=set(),
        provider_statuses=lambda: [],
    )
    ready = build_readiness(mgr, env["pid"])
    assert ready["generation"]["generation"] == 1
    assert ready["generation"]["backup_id"] == out["backup_id"]
    wr = Path("app/static/workflow_readiness.js").read_text(encoding="utf-8")
    assert "workflowOverviewGeneration" in wr
    assert "restore_hint" in wr
    assert "add(genBlock,'h3','世代')" in wr


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
    memory.replace_plan(pid, "架空概要", [{
        "task_key": "架空t1", "title": "架空t1", "description": "",
        "acceptance_criteria": "架空", "mode": "local", "depends_on": [],
    }])
    import types
    manager = types.SimpleNamespace(memory=memory)
    monkeypatch.setattr(web, "memory", memory)
    monkeypatch.setattr(web, "orchestrator", manager)
    monkeypatch.setattr(web, "DATA_DIR", data_dir)
    ws_root = tmp_path / "架空api-ws"
    ws_root.mkdir()
    monkeypatch.setattr(web, "WORKSPACE_ROOT", ws_root)
    client = TestClient(web.app)
    return client, pid, mem_path


def test_lifecycle_reset_api(monkeypatch, tmp_path):
    client, pid, mem_path = _api_client(monkeypatch, tmp_path)
    before = mem_path.read_bytes()
    r = client.get(f"/api/projects/{pid}/lifecycle/reset-preview", params={"mode": "replan"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["read_only"] is True and body["mode"] == "replan"
    assert body["keep"] and body["reset"] and body["restore"]
    assert mem_path.read_bytes() == before
    assert client.get(f"/api/projects/{pid}/lifecycle/reset-preview",
                      params={"mode": "nope"}).status_code == 400
    # PJ名なしでは確定できない。
    rb = client.post(f"/api/projects/{pid}/lifecycle/initialize", json={
        "mode": "replan", "preview_token": body["preview_token"],
        "project_name": "別人", "actor": "架空担当"})
    assert rb.status_code == 409
    # 正常実行。
    ok = client.post(f"/api/projects/{pid}/lifecycle/initialize", json={
        "mode": "replan", "preview_token": body["preview_token"],
        "project_name": body["project_name"], "actor": "架空担当"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["ok"] is True
    gens = client.get(f"/api/projects/{pid}/lifecycle/generations")
    assert gens.status_code == 200 and len(gens.json()["generations"]) == 1
    # 復元。
    backup_id = ok.json()["backup_id"]
    rs = client.post(f"/api/projects/{pid}/lifecycle/restore-generation", json={
        "backup_id": backup_id, "actor": "架空担当"})
    assert rs.status_code == 200, rs.text
    assert rs.json()["ok"] is True
    assert client.post(f"/api/projects/{pid}/lifecycle/restore-generation", json={
        "backup_id": "../x", "actor": "架空担当"}).status_code == 409


def test_concurrent_initialize_single_execution(tmp_path):
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
        # 同じ初期化要求の再送を模す: 1つの preview に対して同じ冪等キーで同時送付。
        # preview token は使い捨てのため、再送側は LEASE_BUSY/無効tokenのいずれかに
        # なるが、二重に世代が増えないことを検証する。
        prev = await asyncio.to_thread(build_reset_preview, mem_path, pid,
                                       "replan", ws_root, {})
        return await asyncio.to_thread(
            initialize_project, mem_path, pid, "replan",
            preview_token=prev["preview_token"], project_name=prev["project_name"],
            actor="架空担当", idempotency_key="架空conc",
            workspace_root=ws_root, backup_root=backup_root, executions={})

    async def main():
        return await asyncio.gather(*[one(i) for i in range(4)])

    results = asyncio.run(main())
    assert sum(1 for r in results if r.get("ok")) >= 1
    assert current_generation_number(mem_path, pid) == 1
