"""P4 lease + post-execution independent re-evaluation (pseudo case only).

Real SafeAutoResume / mission-task structure / ReviewStore / completion_gate /
resolution_coordinator are used. External AI is stubbed by existing style.
Existing tests are never modified.
"""
import asyncio
import hashlib
import json
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.goal_contract import activate
from app.memory.short_term import ShortTermMemory
from app.structured_planning import SCHEMA


FILLER = "架空の検証可能な具体的内容を記載する。" * 40


class Manager(SimpleNamespace):
    pass


def _manager(tmp_path):
    mem = ShortTermMemory(tmp_path / "memory" / "conversations.db")
    pid = mem.create_project("p4pseudo")["id"]
    # 単一達成条件だけにする(成功文全体の追加条件化を避ける)。
    mem.save_mission(
        pid,
        "疑似文書目標\n1. 架空市場の定義資料を作成する",
        "疑似文書目標\n1. 架空市場の定義資料を作成する",
        "",
        True,
        [],
    )
    from app.workspace_files import WorkspaceSandbox

    ws = WorkspaceSandbox(tmp_path / "workspace")
    mgr = Manager(memory=mem, workspace=ws, planning_projects=set())
    return mgr, mem, pid


def _sc01_task():
    # 自動再開の人間待ち語に触れない最小契約。被覆は SC01 に結び付く。
    return {
        "id": "t-sc01", "task_key": "SC01", "depends_on": [],
        "title": "疑似文書作成", "description": "架空市場の定義資料を作成",
        "acceptance_criteria": json.dumps({
            "schema": SCHEMA, "criterion_ids": ["SC01"],
            "criterion": "架空市場の定義資料を作成する",
            "outputs": [{"path": "result/sc01.md",
                         "required_headings": ["目的", "実施内容"],
                         "minimum_characters": 200}],
        }, ensure_ascii=False),
        "mode": "local", "status": "pending",
    }


def _final_task(deps):
    return {
        "id": "t-fv", "task_key": "final_verification", "depends_on": list(deps),
        "title": "最終確認", "description": "達成条件の確認",
        "acceptance_criteria": json.dumps({
            "schema": SCHEMA, "final_verification": True,
            "outputs": [{"path": "result/final_verification.md",
                         "required_headings": ["達成条件別判定", "成果物検証",
                                               "未達条件と承認待ち"],
                         "minimum_characters": 200}],
        }, ensure_ascii=False),
        "mode": "local", "status": "pending",
    }


def _external_task():
    # action_requirements を持つため人間待ちになる外部工程。
    return {
        "id": "t-sc02", "task_key": "SC02", "depends_on": [],
        "title": "顧客へ送信", "description": "顧客への送信",
        "acceptance_criteria": json.dumps({
            "schema": SCHEMA, "criterion_ids": ["SC02"],
            "criterion": "架空顧客へ送信する",
            "outputs": [{"path": "result/sc02.md",
                         "required_headings": ["目的", "実施内容"],
                         "minimum_characters": 200}],
            "action_requirements": [{"kind": "approved_external_action",
                                     "minimum_executed": 1,
                                     "evidence_required": True}],
        }, ensure_ascii=False),
        "mode": "local", "status": "pending",
    }


def _plan(mgr, mem, pid, extra_tasks=None):
    tasks = [_sc01_task(), _final_task(["SC01"])]
    for extra in extra_tasks or []:
        tasks.append(extra)
    mem.replace_plan(pid, "p4 pseudo plan", tasks)
    mem.set_mission_status(pid, "ready")
    return mem.get_mission(pid)


def _approve(mgr, pid):
    from app.goal_review import ReviewStore, plan_snapshot

    _snapshot, signature = plan_snapshot(mgr, pid)
    ReviewStore(mgr.memory.path).put(pid, "plan", signature, {"status": "passed"})
    mem = mgr.memory
    mem.set_mission_status(pid, "ready")
    return signature


def _enable(mgr, pid):
    import app.safe_auto_resume as auto

    return auto.set_enabled(mgr, pid, True)


def _write_doc(mgr, pid, path, text):
    project = mgr.memory.get_project(pid)
    target = mgr.workspace.resolve_file(project.get("workspace_path", ""), pid, path)[2]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


@pytest.fixture(autouse=True)
def feature_flag(monkeypatch):
    monkeypatch.setenv("LOCALSAPORTER_AUTO_RESUME_AVAILABLE", "1")


def _gate_environment(mgr, mem, pid, monkeypatch):
    """resume の前提(承認・ゲート)を疑似案件に寄せる。安全条件自体は変えない。"""
    import app.safe_auto_resume as auto

    mission = _plan(mgr, mem, pid)
    activate(mgr, pid)
    signature = _approve(mgr, pid)
    mem.set_mission_status(pid, "ready")
    monkeypatch.setattr(auto, "_approved", lambda *a, **k: True)
    monkeypatch.setattr(auto, "stage_gate",
                        lambda *a, **k: {"blocked": False, "reason": "",
                                         "verified": True, "unresolved_count": 0,
                                         "plan_approval_blocked": False,
                                         "plan_signature": signature})
    return mission, signature


def _executor_writing(mgr, pid, calls):
    def _run(task, key):
        calls.append(str(task.get("task_key")))
        text = "# 疑似文書\n\n## 目的\n" + FILLER + "\n\n## 実施内容\n" + FILLER + "\n"
        target = _write_doc(mgr, pid, "result/sc01.md", text)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        return {"artifact_hash": digest,
                "evidence": [{"path": "result/sc01.md", "sha256": digest,
                              "size": target.stat().st_size}]}
    return _run


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_p4_lease_concurrent_runs_execute_once(tmp_path, monkeypatch):
    import app.safe_auto_resume as auto

    mgr, mem, pid = _manager(tmp_path)
    _gate_environment(mgr, mem, pid, monkeypatch)
    _enable(mgr, pid)
    calls = []

    async def _main():
        return await asyncio.gather(
            auto.resume(mgr, pid, _executor_writing(mgr, pid, calls)),
            auto.resume(mgr, pid, _executor_writing(mgr, pid, calls)),
        )

    first, second = _run(_main())
    assert calls.count("SC01") == 1
    assert sorted(first["executed"] + second["executed"]) == ["SC01"]
    rows = [r for r in auto.records(mgr, pid) if r["task_key"] == "SC01"]
    assert len(rows) == 1 and rows[0]["status"] == "completed"


def test_p4_lease_valid_claim_is_not_stolen(tmp_path, monkeypatch):
    import app.safe_auto_resume as auto

    mgr, mem, pid = _manager(tmp_path)
    mission, signature = _gate_environment(mgr, mem, pid, monkeypatch)
    task = next(t for t in mem.get_mission(pid)["tasks"] if t["task_key"] == "SC01")
    ih = auto._input_hash(signature, task, {})
    idem = auto._canonical_hash({"project": pid, "task": "SC01", "input": ih})
    first = auto._claim(mgr, pid, "SC01", ih, idem, 2)
    assert first is not None
    assert auto._claim(mgr, pid, "SC01", ih, idem, 2) is None
    rows = [r for r in auto.records(mgr, pid) if r["task_key"] == "SC01"]
    assert rows and rows[0]["status"] == "running"


def test_p4_lease_expired_with_valid_artifact_is_adopted(tmp_path, monkeypatch):
    import app.safe_auto_resume as auto

    mgr, mem, pid = _manager(tmp_path)
    mission, signature = _gate_environment(mgr, mem, pid, monkeypatch)
    task = next(t for t in mem.get_mission(pid)["tasks"] if t["task_key"] == "SC01")
    ih = auto._input_hash(signature, task, {})
    idem = auto._canonical_hash({"project": pid, "task": "SC01", "input": ih})
    claimed = auto._claim(mgr, pid, "SC01", ih, idem, 2)
    assert claimed is not None
    # クラッシュ後に成果物だけ残った状態を再現: running のまま証拠を付与し期限切れにする。
    target = _write_doc(mgr, pid, "result/sc01.md",
                        "# 疑似文書\n\n## 目的\n" + FILLER + "\n\n## 実施内容\n" + FILLER + "\n")
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    proof = json.dumps([{"path": "result/sc01.md", "sha256": digest,
                         "size": target.stat().st_size}], ensure_ascii=False)
    db_path = auto._db_path(mgr)
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE runs SET artifact_hash=?, evidence=?, lease_expires=? "
                   "WHERE project_id=? AND task_key=? AND idempotency_key=?",
                   (digest, proof, time.time() - 1, pid, "SC01", idem))
    # 有効化ゲートは環境どおり(承認済み前提のモック)。
    _enable(mgr, pid)
    calls = []
    result = _run(auto.resume(mgr, pid, _executor_writing(mgr, pid, calls)))
    assert calls == []
    assert result["executed"] == ["SC01"]
    rows = [r for r in auto.records(mgr, pid) if r["task_key"] == "SC01"]
    assert rows[0]["status"] == "completed" and rows[0]["artifact_hash"] == digest
    assert int(rows[0]["expired_count"]) >= 1
    assert rows[0]["last_expired_reason"] == auto.LEASE_EXPIRED_REASON


def test_p4_lease_expired_hash_mismatch_fails_closed(tmp_path, monkeypatch):
    import app.safe_auto_resume as auto

    mgr, mem, pid = _manager(tmp_path)
    mission, signature = _gate_environment(mgr, mem, pid, monkeypatch)
    task = next(t for t in mem.get_mission(pid)["tasks"] if t["task_key"] == "SC01")
    ih = auto._input_hash(signature, task, {})
    idem = auto._canonical_hash({"project": pid, "task": "SC01", "input": ih})
    assert auto._claim(mgr, pid, "SC01", ih, idem, 2) is not None
    target = _write_doc(mgr, pid, "result/sc01.md", "# 旧成果物\n" + FILLER)
    good = hashlib.sha256(target.read_bytes()).hexdigest()
    proof = json.dumps([{"path": "result/sc01.md", "sha256": good,
                         "size": target.stat().st_size}], ensure_ascii=False)
    target.write_text("# 改ざん後\n" + FILLER, encoding="utf-8")
    db_path = auto._db_path(mgr)
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE runs SET artifact_hash=?, evidence=?, lease_expires=? "
                   "WHERE project_id=? AND task_key=? AND idempotency_key=?",
                   (good, proof, time.time() - 1, pid, "SC01", idem))
    _enable(mgr, pid)
    calls = []
    result = _run(auto.resume(mgr, pid, _executor_writing(mgr, pid, calls)))
    assert calls == []
    assert result["executed"] == []
    assert result["failed"] == ["SC01"]
    rows = [r for r in auto.records(mgr, pid) if r["task_key"] == "SC01"]
    assert rows[0]["status"] == "failed"


def test_p4_legacy_db_without_lease_columns_still_works(tmp_path, monkeypatch):
    import app.safe_auto_resume as auto

    mgr, mem, pid = _manager(tmp_path)
    _gate_environment(mgr, mem, pid, monkeypatch)
    # 既存DB(リース列なし)を再現: 旧スキーマのまま完了行を1件置く。
    db_path = auto._db_path(mgr)
    if db_path.exists():
        db_path.unlink()
    with sqlite3.connect(db_path) as db:
        db.execute("""CREATE TABLE settings(
            project_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0,
            updated REAL NOT NULL)""")
        db.execute("""CREATE TABLE runs(
            project_id TEXT NOT NULL, task_key TEXT NOT NULL, input_hash TEXT NOT NULL,
            artifact_hash TEXT NOT NULL DEFAULT '', run_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL, status TEXT NOT NULL,
            evidence TEXT NOT NULL DEFAULT '',
            retry_count INTEGER NOT NULL DEFAULT 0, retry_limit INTEGER NOT NULL,
            failure_kind TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
            updated REAL NOT NULL,
            PRIMARY KEY(project_id, task_key, idempotency_key))""")
        db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (pid, "OLD", "old-input", "old-artifact", "old-run", "old-idem",
                    "completed", "[]", 0, 2, "", "", time.time()))
    rows = [r for r in auto.records(mgr, pid) if r["task_key"] == "OLD"]
    assert len(rows) == 1 and rows[0]["status"] == "completed"
    mission = mem.get_mission(pid)
    task = next(t for t in mission["tasks"] if t["task_key"] == "SC01")
    from app.goal_review import plan_snapshot as snapshot

    _snapshot, signature = snapshot(mgr, pid)
    ih = auto._input_hash(signature, task, {})
    idem = auto._canonical_hash({"project": pid, "task": "SC01", "input": ih})
    assert auto._claim(mgr, pid, "SC01", ih, idem, 2) is not None
    with sqlite3.connect(db_path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(runs)").fetchall()}
    assert {"lease_expires", "owner_id", "expired_count",
            "last_expired_reason"} <= columns
    old = [r for r in auto.records(mgr, pid) if r["task_key"] == "OLD"]
    assert old and old[0]["status"] == "completed" and old[0]["artifact_hash"] == "old-artifact"


def _resolution_setup(mgr, mem, pid):
    import app.resolution_coordinator as coord

    # completion_gate の未達条件から残件を作る(実在の detect)。
    detected = coord.detect(mgr, pid, "human-p4")
    assert detected["resolutions"]
    return coord, detected


def test_p4_post_execution_independent_reevaluation_updates_resolution(tmp_path, monkeypatch):
    import app.safe_auto_resume as auto

    mgr, mem, pid = _manager(tmp_path)
    _gate_environment(mgr, mem, pid, monkeypatch)
    coord, detected = _resolution_setup(mgr, mem, pid)
    target = next(x for x in detected["resolutions"] if x["task_key"] == "SC01")
    before = coord.get(mgr, pid, target["id"])
    assert before["reevaluation"] == {}
    _enable(mgr, pid)
    calls = []
    result = _run(auto.resume(mgr, pid, _executor_writing(mgr, pid, calls)))
    assert result["executed"] == ["SC01"] and calls == ["SC01"]
    after = coord.get(mgr, pid, target["id"])
    assert after["reevaluation"]
    assert after["reevaluation"]["p4_reevaluation"]["run_id"]
    # 自己申告だけでは resolved にならない: advance("resolved") は独立 gate に従う。
    assert after["state"] != "resolved"
    with pytest.raises(ValueError):
        coord.advance(mgr, pid, target["id"], "resolved", "human-p4", "", {})


def test_p4_reevaluation_failure_does_not_resolve_and_keeps_completion(tmp_path, monkeypatch):
    import app.safe_auto_resume as auto

    mgr, mem, pid = _manager(tmp_path)
    _gate_environment(mgr, mem, pid, monkeypatch)
    coord, detected = _resolution_setup(mgr, mem, pid)
    target = next(x for x in detected["resolutions"] if x["task_key"] == "SC01")
    _enable(mgr, pid)
    calls = []
    with patch("app.completion_gate.evaluate", side_effect=RuntimeError("gate down")):
        result = _run(auto.resume(mgr, pid, _executor_writing(mgr, pid, calls)))
    assert result["executed"] == ["SC01"]
    rows = [r for r in auto.records(mgr, pid) if r["task_key"] == "SC01"]
    assert rows[0]["status"] == "completed"
    after = coord.get(mgr, pid, target["id"])
    assert after["state"] != "resolved"
    assert "gate_evaluate" in after["reevaluation"]["p4_reevaluation"]["error"]


def test_p4_external_unapproved_human_wait_never_executes_nor_reevaluates(tmp_path, monkeypatch):
    import app.safe_auto_resume as auto

    mgr, mem, pid = _manager(tmp_path)
    _gate_environment(mgr, mem, pid, monkeypatch)
    # 外部工程を追加しても計画・承認を作り直す(疑似案件内)。
    mission = mem.get_mission(pid)
    tasks = [_sc01_task(), _external_task(), _final_task(["SC01", "SC02"])]
    mem.replace_plan(pid, "p4 mixed plan", tasks)
    mem.set_mission_status(pid, "ready")
    _approve(mgr, pid)
    import app.resolution_coordinator as coord

    coord.detect(mgr, pid, "human-p4")
    _enable(mgr, pid)
    calls = []
    result = _run(auto.resume(mgr, pid, _executor_writing(mgr, pid, calls)))
    assert "SC02" not in result["executed"]
    assert "SC02" in result["waiting_human"]
    views = coord.list_resolutions(mgr, pid)
    sc02 = [v for v in views if v["task_key"] == "SC02" or v["criterion_id"] == "SC02"]
    for view in sc02:
        assert view["reevaluation"].get("p4_reevaluation", {}).get("run_id", "") == ""
        assert view["state"] != "resolved"


def test_p4_no_side_effects_and_default_disabled(tmp_path, monkeypatch):
    import app.safe_auto_resume as auto

    mgr, mem, pid = _manager(tmp_path)
    _gate_environment(mgr, mem, pid, monkeypatch)
    coord, detected = _resolution_setup(mgr, mem, pid)
    target = next(x for x in detected["resolutions"] if x["task_key"] == "SC01")
    assert auto.enabled(mgr, pid) is False
    from app.goal_completion_store import GoalCompletionStore

    def _snapshots():
        return {
            "actions": list(mem.list_actions(pid)),
            "accept": GoalCompletionStore(mgr.memory.path).latest_unrevoked_acceptance(pid),
            "evals": GoalCompletionStore(mgr.memory.path).latest_evaluation(pid),
            "enabled": auto.enabled(mgr, pid),
        }

    _enable(mgr, pid)
    before = _snapshots()
    calls = []
    result = _run(auto.resume(mgr, pid, _executor_writing(mgr, pid, calls)))
    after = _snapshots()
    assert result["executed"] == ["SC01"]
    # 読み取り評価(persist=False)のため評価は保存されない。承認・外部・RAGは不変。
    assert after == before
    assert auto.enabled(mgr, "other-project") is False
    assert coord.get(mgr, pid, target["id"])["state"] != "resolved"
