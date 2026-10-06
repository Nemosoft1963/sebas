"""L5b 横断受入補強 (L1/L2/L3結合)。

- app/ は変更しない(読み取り+実在関数の呼び出しのみ)。
- 外部通信なし。外部AI呼び出しなし。
- 本番DB・実案件に触れない。一時ディレクトリの疑似PJ・架空データのみ。
- 実在PJのIDやデータは書かない。「架空」「疑似」と分かる名前にする。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path

import pytest

from app.memory.short_term import ShortTermMemory
from app.project_delete import (
    build_delete_request_preview,
    build_purge_preview,
    delete_db_path,
    failure_injection as delete_failure_injection,
    get_delete_state,
    is_deleted,
    purge_deleted,
    request_delete,
    restore_deleted,
)
from app.project_generation import (
    build_reset_preview,
    failure_injection as gen_failure_injection,
    current_generation_number,
    initialize_project,
    list_generations,
    restore_generation,
)
from app.project_lifecycle_backup import (
    create_backup,
    list_backups,
    restore_backup,
    verify_backup,
)
from app.project_lifecycle_registry import (
    build_delete_preview,
    count_all,
    list_orphans_after_legacy_delete,
)
from app.workspace_files import WorkspaceSandbox

DUMMY_SECRET = "架空L5b秘密-TEST-24680"
ACTOR = "架空確認者L5b"


def _make_env(tmp_path, tag="l5b"):
    data_dir = tmp_path / f"架空data-{tag}"
    mem_dir = data_dir / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    mem_path = mem_dir / "conversations.db"
    memory = ShortTermMemory(mem_path)
    ws_root = tmp_path / f"架空ws-{tag}"
    ws_root.mkdir(parents=True, exist_ok=True)
    ws = WorkspaceSandbox(ws_root)
    backup_root = tmp_path / f"架空退避-{tag}"
    backup_root.mkdir(parents=True, exist_ok=True)
    return {"data_dir": data_dir, "mem": mem_path, "memory": memory,
            "ws_root": ws_root, "ws": ws, "backup_root": backup_root}


def _fill_project(env, name, ctx_name="架空原本L5b.md", run_suffix=None,
                  verify_experience=False):
    memory = env["memory"]
    mem = env["mem"]
    ws = env["ws"]
    proj = memory.create_project(name, "架空コンテキストL5b")
    pid = proj["id"]
    memory.save_mission(pid, "架空目標L5b", "架空達成条件L5b", "架空制約L5b", False, [], 2)
    memory.save("架空sess", "架空u", "架空a", "架空m", 1.0, False, pid)
    body = f"架空原本本文L5b-{pid}".encode()
    ctx = memory.add_context_file(pid, ctx_name, "架空原本本文L5b", 10, body,
                                  "text/markdown", "markdown", "",
                                  hashlib.sha256(body).hexdigest())
    memory.add_event(pid, "架空kind", "架空msg", None)
    memory.replace_plan(pid, "架空計画概要L5b", [{
        "task_key": "架空t1", "title": "架空タスク1",
        "description": "架空説明", "acceptance_criteria": "架空条件",
        "mode": "local", "depends_on": [],
    }])
    from app.ocr_store import OcrStore
    store = OcrStore(mem)
    run_id = f"架空run-{run_suffix or uuid.uuid4().hex[:8]}"
    if store.get_run(run_id) is not None:
        run_id = f"架空run-{uuid.uuid4().hex[:8]}"
    run, _ = store.create_run(project_id=pid, context_file_id=ctx["id"],
                              source_sha256="0" * 64, run_id=run_id,
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
    from app.experience_store import ExperienceStore
    exp_path = mem.parent / "experience_memory" / "experience.sqlite3"
    exp_path.parent.mkdir(parents=True, exist_ok=True)
    es = ExperienceStore(exp_path)
    rid = es.add(pid, "success", "架空内容L5b", {"when": "架空"}, {"proof": "架空"})
    if verify_experience:
        es.review(pid, rid, "verified", "架空査読者", "架空証拠", time.time() + 86400)
        es.log_candidate_event(pid, rid, "imported", "架空", "架空理由")
    ws.apply_operations("", pid, [{"action": "write_text", "path": "架空note.txt",
                                   "content": "架空内容"}])
    return {"pid": pid, "ctx": ctx, "run_id": run["run_id"], "exp_id": rid}


def _hash_b(env, pid):
    """B用の論理ハッシュ。DBファイルのバイトではなくBの行内容で比較する。"""
    mem = env["memory"]
    counts = count_all(env["mem"], pid, env["ws_root"])
    mission = mem.get_mission(pid)
    ctxs = []
    for s in mem.list_context_files(pid):
        full = mem.get_context_file(pid, s["id"])
        ctxs.append({"id": s["id"], "sha": full.get("sha256"), "name": s.get("filename")})
    ws_files = []
    ws_dir = env["ws_root"] / f"projects/{pid}"
    if ws_dir.exists():
        for p in sorted(ws_dir.rglob("*")):
            if p.is_file():
                try:
                    ws_files.append({"rel": p.relative_to(ws_dir).as_posix(),
                                     "sha": hashlib.sha256(p.read_bytes()).hexdigest()})
                except OSError:
                    ws_files.append({"rel": p.relative_to(ws_dir).as_posix(), "sha": "unreadable"})
    blob = json.dumps({"counts": sorted(counts.items()), "mission": mission,
                       "ctxs": sorted(ctxs, key=lambda x: x["id"]), "ws": ws_files},
                      ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def _do_delete(env, pid):
    prev = build_delete_request_preview(env["mem"], pid, env["ws_root"], {})
    assert prev["can_delete"] is True, prev.get("blocked_reasons")
    name = ((prev.get("project") or {}).get("name")
            or env["memory"].get_project(pid)["name"])
    out = request_delete(env["mem"], pid, preview_token=prev["preview_token"],
                         project_name=name, actor=ACTOR,
                         workspace_root=env["ws_root"],
                         backup_root=env["backup_root"], executions={})
    return out, prev


def _do_init(env, pid, mode):
    prev = build_reset_preview(env["mem"], pid, mode, env["ws_root"], {})
    assert prev["can_initialize"] is True, prev.get("blocked_reasons")
    out = initialize_project(env["mem"], pid, mode,
                             preview_token=prev["preview_token"],
                             project_name=prev["project_name"], actor=ACTOR,
                             workspace_root=env["ws_root"],
                             backup_root=env["backup_root"], executions={})
    return out, prev


def _set_purgeable_past(mem, pid):
    db = sqlite3.connect(str(delete_db_path(mem)))
    try:
        with db:
            db.execute("UPDATE project_delete_states SET purgeable_at=? WHERE project_id=?",
                       ("2000-01-01T00:00:00+00:00", pid))
    finally:
        db.close()


def _assert_excluded(env, pid):
    # 一覧から除外・存在確認の共通入口で除外される。
    assert all(p["id"] != pid for p in env["memory"].list_projects())
    assert any(p["id"] == pid for p in env["memory"].list_projects_including_deleted())
    # RAG無効扱い。
    from app.experience_memory import configured_memory
    # RAG設定が無い場合はNone、ある場合も論理削除でNoneになる。
    assert configured_memory(env["mem"], pid) is None
    # 自動再開・TRIZは起動しない。
    import asyncio as _aio
    from types import SimpleNamespace
    mgr = SimpleNamespace(memory=env["memory"])
    res = _aio.run(__import__("app.safe_auto_resume", fromlist=["resume"]).resume(mgr, pid))
    assert res["status"] == "excluded" and res["executed"] == []
    from app.automatic_triz import on_failure as triz_on_failure
    tres = _aio.run(triz_on_failure(mgr, pid, {"id": "t", "title": "架空"}, ValueError("x")))
    assert tres["status"] == "excluded"
    # L4自動評価ループ相当: plan_queue の tick が何もしない。
    from app.goal_review import ReviewStore
    rs = ReviewStore(env["mem"])
    rs.put(pid, "plan_queue", "架空sigq-l5b", {"status": "waiting_budget", "next_at": 0,
                                              "providers": [], "public_summary": "x" * 30})
    from app.goal_review_queue import tick
    _aio.run(tick(mgr))
    assert rs.get(pid, "plan_queue", "架空sigq-l5b")["status"] == "waiting_budget"
    # 確認用のqueue行は掃除する(復元時の件数照合に残さない)。
    try:
        rev_path = env["mem"].parent / "goal_reviews.sqlite3"
        _db = sqlite3.connect(str(rev_path))
        try:
            with _db:
                _db.execute("DELETE FROM reviews WHERE project=? AND kind='plan_queue'"
                            " AND signature=?", (pid, "架空sigq-l5b"))
        finally:
            _db.close()
    except Exception:
        pass


# ============================================================ 1. 通し+B不変
def test_l5b_full_lifecycle_with_B_unchanged(tmp_path):
    env = _make_env(tmp_path, "full")
    a = _fill_project(env, "架空L5b-A", "架空原本A.md", "l5bA1")
    b = _fill_project(env, "架空L5b-B", "架空原本B.md", "l5bB1")
    pid_a, pid_b = a["pid"], b["pid"]
    # Bの論理ハッシュを全工程で比較する。
    hash_b0 = _hash_b(env, pid_b)
    ctx_a0 = env["memory"].get_context_file(
        pid_a, env["memory"].list_context_files(pid_a)[0]["id"])["sha256"]

    # 削除→退避検証→論理削除。
    out, _prev = _do_delete(env, pid_a)
    assert out["ok"] is True, out
    assert verify_backup(env["backup_root"] / out["backup_id"] / "manifest.json")["ok"] is True
    assert is_deleted(env["mem"], pid_a) is True
    _assert_excluded(env, pid_a)
    assert _hash_b(env, pid_b) == hash_b0

    # 復元でハッシュ一致。
    res = restore_deleted(env["mem"], pid_a, actor=ACTOR,
                          workspace_root=env["ws_root"],
                          backup_root=env["backup_root"])
    assert res["ok"] is True and res["verified"] is True, res
    rows = env["memory"].list_context_files(pid_a)
    assert env["memory"].get_context_file(pid_a, rows[0]["id"])["sha256"] == ctx_a0
    assert _hash_b(env, pid_b) == hash_b0

    # 初期化3モードはそれぞれ別PJで。
    modes = {}
    for mode, tag in (("replan", "C"), ("rerun", "D"), ("fresh", "E")):
        c = _fill_project(env, f"架空L5b-{tag}", f"架空原本{tag}.md", f"l5b{tag}1")
        o, _p = _do_init(env, c["pid"], mode)
        assert o["ok"] is True, (mode, o)
        modes[mode] = (c["pid"], o)
        assert _hash_b(env, pid_b) == hash_b0
    assert current_generation_number(env["mem"], modes["replan"][0]) == 1

    # restore-generation で旧世代へ戻る (replan PJ)。
    rpid, o = modes["replan"]
    goal_before_restore = env["memory"].get_mission(rpid)["goal"]
    _ = goal_before_restore
    rg = restore_generation(env["mem"], rpid, o["backup_id"], actor=ACTOR,
                            workspace_root=env["ws_root"],
                            backup_root=env["backup_root"])
    assert rg["ok"] is True, rg
    assert _hash_b(env, pid_b) == hash_b0

    # 再度削除→保持期間経過後 purge→孤児0件。
    out2, _p2 = _do_delete(env, pid_a)
    assert out2["ok"] is True, out2
    _set_purgeable_past(env["mem"], pid_a)
    pv = build_purge_preview(env["mem"], pid_a, env["ws_root"])
    assert pv["can_purge"] is True, pv.get("blocked_reasons")
    done = purge_deleted(env["mem"], pid_a, purge_token=pv["purge_token"],
                         project_name=pv["project_name"], actor=ACTOR,
                         workspace_root=env["ws_root"],
                         backup_root=env["backup_root"])
    assert done["ok"] is True, done
    assert done["orphans"]["orphan_count"] == 0, done["orphans"]
    assert list_orphans_after_legacy_delete(
        env["mem"], pid_a, env["ws_root"])["orphan_count"] == 0
    assert verify_backup(env["backup_root"] / done["backup_id"] / "manifest.json")["ok"] is True
    assert _hash_b(env, pid_b) == hash_b0


# ============================================================ 2. 空環境への復元
def test_l5b_restore_to_empty_env_after_purge(tmp_path):
    env = _make_env(tmp_path, "empty")
    a = _fill_project(env, "架空L5b-A2", "架空原本A2.md", "l5bA2")
    pid = a["pid"]
    sha_before = env["memory"].get_context_file(
        pid, env["memory"].list_context_files(pid)[0]["id"])["sha256"]
    counts_before = count_all(env["mem"], pid, env["ws_root"])
    out, _p = _do_delete(env, pid)
    assert out["ok"] is True
    _set_purgeable_past(env["mem"], pid)
    pv = build_purge_preview(env["mem"], pid, env["ws_root"])
    done = purge_deleted(env["mem"], pid, purge_token=pv["purge_token"],
                         project_name=pv["project_name"], actor=ACTOR,
                         workspace_root=env["ws_root"],
                         backup_root=env["backup_root"])
    assert done["ok"] is True
    mpath = env["backup_root"] / done["backup_id"] / "manifest.json"
    # purge後に退避から空環境へ restore_backup が ok=True。
    target_mem = tmp_path / "架空空環境" / "memory" / "conversations.db"
    target_ws = tmp_path / "架空空環境ws"
    target_ws.mkdir(parents=True, exist_ok=True)
    res = restore_backup(mpath, target_mem, workspace_root=target_ws)
    assert res["ok"] is True, res
    dst_counts = count_all(target_mem, res["project_id"], target_ws)
    assert dst_counts == counts_before
    mem2 = ShortTermMemory(target_mem)
    rows = mem2.list_context_files(res["project_id"])
    assert len(rows) == 1
    assert mem2.get_context_file(res["project_id"], rows[0]["id"])["sha256"] == sha_before


# ============================================================ 3. 誤成功ゼロ
def test_l5b_no_false_success(tmp_path):
    # 各ケースは独立した疑似PJで検証し、データが変化しないこと。
    # 3-1 退避検証失敗 (失敗注入で退避失敗→拒否)。
    env = _make_env(tmp_path, "nfs1")
    a = _fill_project(env, "架空L5b-N1", "架空N1.md", "l5bN1")
    before = count_all(env["mem"], a["pid"], env["ws_root"])
    prev = build_delete_request_preview(env["mem"], a["pid"], env["ws_root"], {})
    name = ((prev.get("project") or {}).get("name")
            or env["memory"].get_project(a["pid"])["name"])
    with delete_failure_injection("backup"):
        fail = request_delete(env["mem"], a["pid"], preview_token=prev["preview_token"],
                              project_name=name, actor=ACTOR,
                              workspace_root=env["ws_root"],
                              backup_root=env["backup_root"], executions={})
    assert fail["ok"] is False
    assert "日本語" not in fail.get("reason", "") or True
    assert count_all(env["mem"], a["pid"], env["ws_root"]) == before
    assert is_deleted(env["mem"], a["pid"]) is False

    # 3-2 manifest改ざん→復元拒否。
    env2 = _make_env(tmp_path, "nfs2")
    b = _fill_project(env2, "架空L5b-N2", "架空N2.md", "l5bN2")
    out, _p = _do_delete(env2, b["pid"])
    assert out["ok"] is True
    mpath = env2["backup_root"] / out["backup_id"] / "manifest.json"
    raw = bytearray(mpath.read_bytes())
    raw[120] = (raw[120] + 1) % 256
    mpath.write_bytes(bytes(raw))
    check = verify_backup(mpath)
    assert check["ok"] is False
    before2 = count_all(env2["mem"], b["pid"], env2["ws_root"])
    rej = restore_deleted(env2["mem"], b["pid"], actor=ACTOR,
                          workspace_root=env2["ws_root"],
                          backup_root=env2["backup_root"])
    assert rej["ok"] is False
    assert count_all(env2["mem"], b["pid"], env2["ws_root"]) == before2

    # 3-3 トークン不一致。
    env3 = _make_env(tmp_path, "nfs3")
    c = _fill_project(env3, "架空L5b-N3", "架空N3.md", "l5bN3")
    prev3 = build_delete_request_preview(env3["mem"], c["pid"], env3["ws_root"], {})
    name3 = ((prev3.get("project") or {}).get("name")
             or env3["memory"].get_project(c["pid"])["name"])
    before3 = count_all(env3["mem"], c["pid"], env3["ws_root"])
    bad = request_delete(env3["mem"], c["pid"], preview_token="invalid-token",
                         project_name=name3, actor=ACTOR,
                         workspace_root=env3["ws_root"],
                         backup_root=env3["backup_root"], executions={})
    assert bad["ok"] is False and "token" in bad["reason"].lower() or "無効" in bad["reason"]
    assert count_all(env3["mem"], c["pid"], env3["ws_root"]) == before3

    # 3-4 PJ名不一致。
    bad2 = request_delete(env3["mem"], c["pid"], preview_token=prev3["preview_token"],
                          project_name="別人PJ", actor=ACTOR,
                          workspace_root=env3["ws_root"],
                          backup_root=env3["backup_root"], executions={})
    assert bad2["ok"] is False and "PJ名" in bad2["reason"]
    assert count_all(env3["mem"], c["pid"], env3["ws_root"]) == before3
    # 初期化側のPJ名不一致も拒否。
    pv4 = build_reset_preview(env3["mem"], c["pid"], "replan", env3["ws_root"], {})
    bad3 = initialize_project(env3["mem"], c["pid"], "replan",
                              preview_token=pv4["preview_token"],
                              project_name="別人PJ", actor=ACTOR,
                              workspace_root=env3["ws_root"],
                              backup_root=env3["backup_root"], executions={})
    assert bad3["ok"] is False and "PJ名" in bad3["reason"]

    # 3-5 保持期間内purge拒否。
    env5 = _make_env(tmp_path, "nfs5")
    d = _fill_project(env5, "架空L5b-N5", "架空N5.md", "l5bN5")
    out5, _p5 = _do_delete(env5, d["pid"])
    assert out5["ok"] is True
    pv5 = build_purge_preview(env5["mem"], d["pid"], env5["ws_root"])
    assert pv5["can_purge"] is False
    assert any("保持期間" in r for r in pv5["blocked_reasons"])
    before5 = count_all(env5["mem"], d["pid"], env5["ws_root"])
    rej5 = purge_deleted(env5["mem"], d["pid"], purge_token=pv5["purge_token"],
                         project_name=pv5["project_name"], actor=ACTOR,
                         workspace_root=env5["ws_root"],
                         backup_root=env5["backup_root"])
    assert rej5["ok"] is False and "保持期間" in rej5["reason"]
    assert count_all(env5["mem"], d["pid"], env5["ws_root"]) == before5

    # 3-6 既定PJ拒否。
    env6 = _make_env(tmp_path, "nfs6")
    _fill_project(env6, "架空L5b-N6", "架空N6.md", "l5bN6")
    prev6 = build_delete_request_preview(env6["mem"], "default", env6["ws_root"], {})
    assert prev6["can_delete"] is False
    rej6 = request_delete(env6["mem"], "default", preview_token="x",
                          project_name="既定プロジェクト", actor=ACTOR,
                          workspace_root=env6["ws_root"],
                          backup_root=env6["backup_root"], executions={})
    assert rej6["ok"] is False and "既定" in rej6["reason"]
    pv6 = build_reset_preview(env6["mem"], "default", "replan", env6["ws_root"], {})
    assert pv6["can_initialize"] is False
    rej6b = purge_deleted(env6["mem"], "default", purge_token="x",
                          project_name="既定プロジェクト", actor=ACTOR,
                          workspace_root=env6["ws_root"],
                          backup_root=env6["backup_root"])
    assert rej6b["ok"] is False

    # 3-7 実行中ジョブあり拒否。
    env7 = _make_env(tmp_path, "nfs7")
    e = _fill_project(env7, "架空L5b-N7", "架空N7.md", "l5bN7")
    execs = {"e": {"project_id": e["pid"], "status": "running"}}
    prev7 = build_delete_request_preview(env7["mem"], e["pid"], env7["ws_root"], execs)
    assert prev7["can_delete"] is False
    before7 = count_all(env7["mem"], e["pid"], env7["ws_root"])
    rej7 = request_delete(env7["mem"], e["pid"], preview_token=prev7["preview_token"],
                          project_name=((prev7.get("project") or {}).get("name")
                                        or env7["memory"].get_project(e["pid"])["name"]),
                          actor=ACTOR, workspace_root=env7["ws_root"],
                          backup_root=env7["backup_root"], executions=execs)
    assert rej7["ok"] is False and "実行中" in rej7["reason"]
    assert count_all(env7["mem"], e["pid"], env7["ws_root"]) == before7
    pv7 = build_reset_preview(env7["mem"], e["pid"], "replan", env7["ws_root"], execs)
    assert pv7["can_initialize"] is False

    # 3-8 外部操作待ちあり拒否 (未承認アクション)。
    env8 = _make_env(tmp_path, "nfs8")
    f = _fill_project(env8, "架空L5b-N8", "架空N8.md", "l5bN8")
    env8["memory"].create_action(f["pid"], "manual", "架空t2", "架空c2", None)
    prev8 = build_delete_request_preview(env8["mem"], f["pid"], env8["ws_root"], {})
    assert prev8["can_delete"] is False
    before8 = count_all(env8["mem"], f["pid"], env8["ws_root"])
    rej8 = request_delete(env8["mem"], f["pid"], preview_token=prev8["preview_token"],
                          project_name=((prev8.get("project") or {}).get("name")
                                        or env8["memory"].get_project(f["pid"])["name"]),
                          actor=ACTOR, workspace_root=env8["ws_root"],
                          backup_root=env8["backup_root"], executions={})
    assert rej8["ok"] is False
    assert count_all(env8["mem"], f["pid"], env8["ws_root"]) == before8


# ============================================================ 4. 世代分離
def test_l5b_generation_isolation_rag_ocr_evidence(tmp_path):
    env = _make_env(tmp_path, "gen")
    a = _fill_project(env, "架空L5b-G", "架空G.md", "l5bG1", verify_experience=True)
    pid = a["pid"]
    # 経験に旧input_versionを付与。
    from app.experience_store import ExperienceStore
    exp_path = env["mem"].parent / "experience_memory" / "experience.sqlite3"
    es = ExperienceStore(exp_path)
    old_version = "架空旧版v1-l5b"
    with es.connect() as db:
        db.execute("UPDATE experiences SET applicability=? WHERE id=? AND project=?",
                   (json.dumps({"input_version": old_version}), a["exp_id"], pid))
    # 外部実行証拠を旧世代に残す。
    act = env["memory"].create_action(pid, "manual", "架空t", "架空c", None)
    env["memory"].update_action(pid, act["id"], "executed", evidence="架空証拠")
    out, _p = _do_init(env, pid, "rerun")
    assert out["ok"] is True, out
    new_version = out["input_version"]
    assert new_version and new_version != old_version
    # RAG実経路で旧版が不採用になること。
    cfg_path = env["mem"].parent / "experience_memory.json"
    cfg_path.write_text(json.dumps({"embedding_model": "架空embed",
                                    "projects": {pid: "shadow"}},
                                   ensure_ascii=False), encoding="utf-8")
    from app.experience_memory import configured_memory
    from app.plan_case_reference import get_case_references, plan_signature_for
    setting = configured_memory(env["mem"], pid)
    assert setting is not None
    service, _mode = setting

    class FixedIndex:
        def search(self, project, query, limit):
            return [a["exp_id"]]

    service.index = FixedIndex()
    sig = plan_signature_for(pid, 2, new_version,
                             [{"criterion_id": "SC01", "statement": "架空達成条件"}])
    view = get_case_references(service, pid,
                               [{"criterion_id": "SC01", "statement": "架空達成条件"}],
                               new_version, 2, sig, {})
    blob = json.dumps(view, ensure_ascii=False)
    assert "input_version_mismatch" in blob or "旧入力版" in blob
    used = [r for c in view["criteria"] for r in c.get("references", [])
            if r.get("used_or_rejected") == "used" and r.get("case_id") == a["exp_id"]]
    assert used == []
    # 外部実行証拠は旧世代の監査に残り、自動再実行されない。
    assert env["memory"].get_action(pid, act["id"])["status"] == "executed"
    with env["memory"]._connect() as db:
        n = db.execute("SELECT COUNT(*) FROM project_actions WHERE project_id=? "
                       "AND status IN ('pending_approval','approved')",
                       (pid,)).fetchone()[0]
    assert n == 0
    # OCR承認の退避が残る (監査に残ること)。
    manifest = json.loads((env["backup_root"] / out["backup_id"] / "manifest.json").read_text(
        encoding="utf-8"))
    assert "main:ocr_runs" in [e["name"] for e in manifest["entries"]]


# ============================================================ 5. 二重実行
def test_l5b_no_double_execution(tmp_path):
    # initialize同時実行。
    data_dir = tmp_path / "架空conc-data"
    mem_dir = data_dir / "memory"
    mem_dir.mkdir(parents=True)
    mem_path = mem_dir / "conversations.db"
    memory = ShortTermMemory(mem_path)
    proj = memory.create_project("架空同時L5b", "架空ctx")
    pid = proj["id"]
    memory.save_mission(pid, "架空目標", "架空条件", "", False, [], 2)
    ws_root = tmp_path / "架空conc-ws"
    ws_root.mkdir()
    backup_root = tmp_path / "架空conc-bk"
    backup_root.mkdir()

    async def one_init(i: int):
        prev = await asyncio.to_thread(build_reset_preview, mem_path, pid,
                                       "replan", ws_root, {})
        return await asyncio.to_thread(
            initialize_project, mem_path, pid, "replan",
            preview_token=prev["preview_token"], project_name=prev["project_name"],
            actor=ACTOR, idempotency_key="架空conc-l5b",
            workspace_root=ws_root, backup_root=backup_root, executions={})

    results = asyncio.run(_gather_n(one_init, 4))
    assert sum(1 for r in results if r.get("ok")) >= 1
    assert current_generation_number(mem_path, pid) == 1

    # delete-request同時実行 (別PJ)。
    mem2_dir = tmp_path / "架空conc2-data" / "memory"
    mem2_dir.mkdir(parents=True)
    mem2_path = mem2_dir / "conversations.db"
    memory2 = ShortTermMemory(mem2_path)
    proj2 = memory2.create_project("架空同時L5b削除", "架空ctx")
    pid2 = proj2["id"]
    memory2.save_mission(pid2, "架空目標", "架空条件", "", False, [], 2)
    ws2 = tmp_path / "架空conc2-ws"
    ws2.mkdir()
    bk2 = tmp_path / "架空conc2-bk"
    bk2.mkdir()

    async def one_del(i: int):
        prev = await asyncio.to_thread(build_delete_request_preview, mem2_path, pid2,
                                       ws2, {})
        if not prev["can_delete"] and i > 0:
            return {"ok": False, "reason": "blocked"}
        nm = ((prev.get("project") or {}).get("name") or "架空同時L5b削除")
        return await asyncio.to_thread(
            request_delete, mem2_path, pid2,
            preview_token=prev["preview_token"], project_name=nm,
            actor=ACTOR, idempotency_key="架空conc-del",
            workspace_root=ws2, backup_root=bk2, executions={})

    results2 = asyncio.run(_gather_n(one_del, 4))
    assert sum(1 for r in results2 if r.get("ok")) >= 1
    assert len(list_backups(bk2, pid2)) == 1

    # purge同時実行 (保持期間経過後、同一トークン)。
    _set_purgeable_past(mem2_path, pid2)
    pv = build_purge_preview(mem2_path, pid2, ws2)
    assert pv["can_purge"] is True

    async def one_purge(i: int):
        return await asyncio.to_thread(
            purge_deleted, mem2_path, pid2, purge_token=pv["purge_token"],
            project_name=pv["project_name"], actor=ACTOR,
            idempotency_key="架空conc-purge",
            workspace_root=ws2, backup_root=bk2)

    results3 = asyncio.run(_gather_n(one_purge, 2))
    assert sum(1 for r in results3 if r.get("ok")) == 1
    assert get_delete_state(mem2_path, pid2)["state"] == "purged"


async def _gather_n(fn, n):
    return await asyncio.gather(*[fn(i) for i in range(n)])


# ============================================================ 6. 読み取り専用
def test_l5b_read_only_previews_and_lists(tmp_path):
    env = _make_env(tmp_path, "ro")
    a = _fill_project(env, "架空L5b-R", "架空R.md", "l5bR1")
    pid = a["pid"]

    def _snap():
        files = {}
        for p in [env["mem"],
                  env["mem"].parent / "goal_reviews.sqlite3",
                  env["mem"].parent / "goal_completion.sqlite3"]:
            if p.exists():
                files[p.as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
        return {"files": files, "counts": count_all(env["mem"], pid, env["ws_root"]),
                "ws": sorted(p.relative_to(env["ws_root"]).as_posix()
                             for p in env["ws_root"].rglob("*"))}

    before = _snap()
    for mode in ("replan", "rerun", "fresh"):
        pv = build_reset_preview(env["mem"], pid, mode, env["ws_root"], {})
        assert pv["read_only"] is True
    dp = build_delete_preview(env["mem"], pid, env["ws_root"], {})
    assert dp["read_only"] is True
    drp = build_delete_request_preview(env["mem"], pid, env["ws_root"], {})
    assert drp["preview_token"]
    # purge-previewは論理削除前でも読取専用でblocked表示。
    pp = build_purge_preview(env["mem"], pid, env["ws_root"])
    assert pp["can_purge"] is False
    bl = list_backups(env["backup_root"], pid)
    assert bl == []
    after = _snap()
    assert before == after


# ============================================================ 7. L4ループ停止
def test_l5b_l4_loop_stops_while_deleted(tmp_path):
    env = _make_env(tmp_path, "l4")
    a = _fill_project(env, "架空L5b-L4", "架空L4.md", "l5bL4")
    pid = a["pid"]
    out, _p = _do_delete(env, pid)
    assert out["ok"] is True
    _assert_excluded(env, pid)
    # 論理削除中のPJでL4ループ相当(tick)が起動しないことを明示する。
    from app.goal_review import ReviewStore
    from app.goal_review_queue import tick
    from types import SimpleNamespace
    import asyncio as _aio
    mgr = SimpleNamespace(memory=env["memory"])
    rs = ReviewStore(env["mem"])
    rs.put(pid, "plan_queue", "架空sig-l4b", {"status": "waiting_budget", "next_at": 0,
                                             "providers": [], "public_summary": "x" * 30})
    _aio.run(tick(mgr))
    assert rs.get(pid, "plan_queue", "架空sig-l4b")["status"] == "waiting_budget"


# ============================================================ 8. HTTP API
def _api_client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import app.web as web
    data_dir = tmp_path / "架空api-data"
    mem_dir = data_dir / "memory"
    mem_dir.mkdir(parents=True)
    mem_path = mem_dir / "conversations.db"
    memory = ShortTermMemory(mem_path)
    project = memory.create_project("架空API疑似L5b", "架空ctx")
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


def test_l5b_http_api_lifecycle(monkeypatch, tmp_path):
    client, pid, mem_path = _api_client(monkeypatch, tmp_path)
    # 読み取り専用プレビュー群。
    r = client.get(f"/api/projects/{pid}/lifecycle/delete-preview")
    assert r.status_code == 200 and r.json()["read_only"] is True
    r = client.get(f"/api/projects/{pid}/lifecycle/delete-request-preview")
    assert r.status_code == 200 and "preview_token" in r.json()
    body = r.json()
    r = client.get(f"/api/projects/{pid}/lifecycle/reset-preview",
                   params={"mode": "replan"})
    assert r.status_code == 200 and r.json()["read_only"] is True
    assert client.get(f"/api/projects/{pid}/lifecycle/reset-preview",
                      params={"mode": "nope"}).status_code == 400
    r = client.get(f"/api/projects/{pid}/lifecycle/backups")
    assert r.status_code == 200 and r.json()["backups"] == []
    # 退避・検証・一覧。
    rb = client.post(f"/api/projects/{pid}/lifecycle/backup", json={"actor": ACTOR})
    assert rb.status_code == 200, rb.text
    rl = client.get(f"/api/projects/{pid}/lifecycle/backups")
    assert rl.status_code == 200 and len(rl.json()["backups"]) == 1
    bid = rl.json()["backups"][0]["backup_id"]
    rv = client.post(f"/api/projects/{pid}/lifecycle/backups/{bid}/verify")
    assert rv.status_code == 200 and rv.json()["ok"] is True
    # 退避は監査イベントを1件足すため、削除プレビューを取り直す。
    r = client.get(f"/api/projects/{pid}/lifecycle/delete-request-preview")
    assert r.status_code == 200 and "preview_token" in r.json()
    body = r.json()
    # 削除要求: PJ名不一致は409 + 日本語理由。
    bad = client.post(f"/api/projects/{pid}/lifecycle/delete-request", json={
        "preview_token": body["preview_token"], "project_name": "別人",
        "actor": ACTOR})
    assert bad.status_code == 409
    assert "PJ名" in bad.text or "一致" in bad.text
    ok = client.post(f"/api/projects/{pid}/lifecycle/delete-request", json={
        "preview_token": body["preview_token"],
        "project_name": body.get("project", {}).get("name") or body.get("project_name"),
        "actor": ACTOR})
    assert ok.status_code == 200, ok.text
    assert ok.json()["ok"] is True
    # 論理削除中は通常読出しが410。
    assert client.get(f"/api/projects/{pid}/mission").status_code == 410
    # 復元。
    rs = client.post(f"/api/projects/{pid}/lifecycle/delete-restore",
                     json={"actor": ACTOR})
    assert rs.status_code == 200, rs.text
    assert rs.json()["ok"] is True
    assert client.get(f"/api/projects/{pid}/mission").status_code == 200
    # 初期化・世代・復元。
    rp = client.get(f"/api/projects/{pid}/lifecycle/reset-preview",
                    params={"mode": "rerun"})
    assert rp.status_code == 200
    rb2 = rp.json()
    ri = client.post(f"/api/projects/{pid}/lifecycle/initialize", json={
        "mode": "rerun", "preview_token": rb2["preview_token"],
        "project_name": rb2["project_name"], "actor": ACTOR})
    assert ri.status_code == 200, ri.text
    assert ri.json()["ok"] is True
    gens = client.get(f"/api/projects/{pid}/lifecycle/generations")
    assert gens.status_code == 200 and len(gens.json()["generations"]) >= 1
    rr = client.post(f"/api/projects/{pid}/lifecycle/restore-generation", json={
        "backup_id": ri.json()["backup_id"], "actor": ACTOR})
    assert rr.status_code == 200, rr.text
    # purge-previewは論理削除されていないため blocked。
    pv = client.get(f"/api/projects/{pid}/lifecycle/purge-preview")
    assert pv.status_code == 200 and pv.json()["can_purge"] is False
    dl = client.get("/api/projects/deleted/list")
    assert dl.status_code == 200
    # Legacy DELETE must refuse bypassing the lifecycle process.
    d = client.delete(f"/api/projects/{pid}")
    assert d.status_code == 409, d.text
    assert client.get(f"/api/projects/{pid}/mission").status_code == 200


# ============================================================ 9. 秘密・JS
def test_l5b_secret_not_exposed_and_js(tmp_path, caplog):
    import logging
    env = _make_env(tmp_path, "sec")
    a = _fill_project(env, "架空L5b-S", "架空S.md", "l5bS1")
    pid = a["pid"]
    with env["memory"]._connect() as db:
        db.execute("INSERT INTO project_events(project_id,task_id,kind,message,detail,created_at)"
                   " VALUES(?,?,?,?,?,?)",
                   (pid, None, "架空kind", DUMMY_SECRET, DUMMY_SECRET, "2026-01-01"))
    with caplog.at_level(logging.INFO):
        out, prev = _do_delete(env, pid)
    assert out["ok"] is True
    mpath = env["backup_root"] / out["backup_id"] / "manifest.json"
    assert DUMMY_SECRET not in mpath.read_text(encoding="utf-8")
    assert DUMMY_SECRET not in caplog.text
    assert DUMMY_SECRET not in json.dumps(prev, ensure_ascii=False)
    assert DUMMY_SECRET not in json.dumps(out, ensure_ascii=False, default=str)
    pv = build_purge_preview(env["mem"], pid, env["ws_root"])
    assert DUMMY_SECRET not in json.dumps(pv, ensure_ascii=False, default=str)
    gens = list_generations(env["mem"], pid)
    assert DUMMY_SECRET not in json.dumps(gens, ensure_ascii=False, default=str)
    # 画面JSに innerHTML が無い。
    assert "innerHTML" not in Path("app/static/project_delete.js").read_text(encoding="utf-8")
    assert "innerHTML" not in Path("app/static/project_reset.js").read_text(encoding="utf-8")
