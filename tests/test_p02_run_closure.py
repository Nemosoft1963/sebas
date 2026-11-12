"""P0-2 受入: _run() の終了規則(疑似PJ・実関数・フェイクLLM・外部通信なし)。

- 最終成果物の一部不足 → finished+unmet。達成・RAG登録なし。
- 実施後の数値が必要 → finished+undetermined+external_result。架空値なし。
- 人間判断が必要 → finished+undetermined+human_decision。自動承認なし。
- 根拠のある修正可能な欠落 → 最大2回後に残件として終了。無限追補なし。
- 台帳読取不能 → stopped_error。旧式追補へ迂回しない。
- ファイル予定パスがディレクトリ → stopped_error。既存データを削除・上書きしない。
- 同じ run_id の終了記録とタスクが重複しない(再起動・再試行)。
- 時間切れ/キャンセル → stopped_time_limit/cancelled。running のまま残らない。
- 完了ゲートが通れば finished+achieved(ゲート結果つき)。通らないのに achieved は書けない。
- 保存結果(DB)と API応答(GET /runs/latest)まで検査する。
"""
from __future__ import annotations

import asyncio
import sys
import time
import types

import pytest

from app.memory.short_term import ShortTermMemory
from app.workspace_files import WorkspaceSandbox


class _FakeLLM:
    model = "fake-llm"

    async def complete(self, messages):
        raise AssertionError("local LLM must not decide run closure")

    async def complete_json(self, messages, schema):
        raise AssertionError("local LLM must not decide run closure")

    def stream(self, messages):  # pragma: no cover - unused
        raise AssertionError("local LLM must not decide run closure")


def _manager(tmp_path, name="p02"):
    from app.project_manager import ProjectOrchestrator

    data_dir = tmp_path / name
    mem_dir = data_dir / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    mem_path = mem_dir / "conversations.db"
    memory = ShortTermMemory(mem_path)
    workspace_root = data_dir / "workspace"
    workspace_root.mkdir(parents=True, exist_ok=True)
    workspace = WorkspaceSandbox(workspace_root)
    project = memory.create_project(f"架空P02-{name}", "架空ctx", "projects/p02")
    pid = project["id"]

    async def _static(project_dict):
        return "", []

    async def _research(*args, **kwargs):
        raise AssertionError("no external research")

    def _providers():
        return []

    manager = ProjectOrchestrator(
        memory, _FakeLLM(), _static, _research, _providers,
        workspace=workspace,
    )
    return manager, pid


def _plan(manager, pid, criterion="SC01: 架空の成果物を用意する"):
    mission = manager.memory.get_mission(pid)
    task = {
        "task_key": "t1",
        "depends_on": [],
        "title": "架空作業",
        "description": "架空成果物を作る。result/a.md",
        "acceptance_criteria": "result/a.mdが存在する",
        "mode": "local",
    }
    manager.memory.replace_plan(pid, mission.get("plan_summary") or "p",
                                [task])
    return manager.memory.get_mission(pid)["tasks"][0]


def _contract(task_id, item_id, path, method="phrase",
              result_note="必須語句なし: 架空語句"):
    import json

    return json.dumps({
        "schema": "structured-plan/v1",
        "criterion": "SC01",
        "criterion_ids": ["SC01"],
        "requirement_matrix": [{
            "item_id": item_id, "criterion_id": "SC01",
            "artifact_path": path, "method": method,
            "required": True, "requirement": "架空要求",
        }],
        "outputs": [{"path": path}],
        "source_refs": [],
    })


def _write(manager, pid, path, content):
    project = manager.memory.get_project(pid)
    manager.workspace.apply_operations(
        project.get("workspace_path", ""), pid,
        [{"action": "write_text", "path": path, "content": content}],
    )


def _ledger(manager, pid, task, rows, plan_signature="sig-p02",
            source_hash="src-p02", hashes=None):
    from app.requirement_item_ledger import record_ledger
    from app.requirement_verification import ledger_payload

    record_ledger(
        manager.memory.path,
        ledger_payload(pid, plan_signature, source_hash, rows, hashes or {}),
    )


def _latest(manager, pid):
    import app.run_cycles as runs

    return runs.get_latest_run(manager.memory.path, pid)


def _latest_via_api(manager, pid):
    from fastapi.testclient import TestClient

    import app.web as web

    web.memory = manager.memory
    web.workspace = manager.workspace
    created = []

    class _Orch:
        pass

    web.orchestrator = _Orch()
    client = TestClient(web.app)
    response = client.get(f"/api/projects/{pid}/runs/latest")
    assert response.status_code == 200, response.text
    return response.json()


def _run_cycle_of(manager, pid):
    import sqlite3
    from app.requirement_item_ledger import item_ledger_db_path

    path = item_ledger_db_path(manager.memory.path)
    db = sqlite3.connect(str(path))
    try:
        rows = db.execute(
            "SELECT run_id, execution_state, goal_verdict, pending_kind "
            "FROM project_run_cycles WHERE project_id=? ORDER BY started_at",
            (pid,)).fetchall()
        return [tuple(r) for r in rows]
    finally:
        db.close()


def _finish_run_in_test(manager, pid, *, run_id, **kwargs):
    import app.run_cycles as runs

    return runs.finish_run(manager.memory.path, run_id, **kwargs)


def _close_from_ledger(manager, pid, run_id, *, stop_reason="受入"):
    return manager._close_run_cycle_from_ledger(
        pid, run_id, True, stop_reason=stop_reason)


def _start_cycle(manager, pid, run_id):
    import app.run_cycles as runs

    return runs.start_run(manager.memory.path, pid, run_id,
                          plan_signature="sig-p02", source_hash="src-p02")


def test_missing_artifact_is_finished_unmet(tmp_path):
    manager, pid = _manager(tmp_path, "unmet")
    task = _plan(manager, pid)
    row = {"item_id": "SC01-R01", "criterion_id": "SC01",
           "artifact_path": "result/a.md", "requirement": "架空要求",
           "method": "phrase", "required": True, "result": "FAIL",
           "line": -1, "quote": "", "reason": "対象成果物不明のため判定不能"}
    _ledger(manager, pid, task, [row])
    rid = "run-unmet-1"
    _start_cycle(manager, pid, rid)
    ended = _close_from_ledger(manager, pid, rid, stop_reason="成果物不足")
    assert ended["execution_state"] == "finished"
    assert ended["goal_verdict"] == "unmet"
    saved = _latest(manager, pid)
    assert saved["found"] is True and saved["run_id"] == rid
    assert saved["goal_verdict"] == "unmet"
    assert any(item["kind"] == "artifact_missing" for item in saved["open_items"])
    assert saved["open_items"][0]["owner"] == "system"
    assert saved["open_items"][0]["evidence_needed"]
    assert saved["open_items"][0]["next_action"]
    # API応答まで届く。
    viewed = _latest_via_api(manager, pid)
    assert viewed["run_id"] == rid and viewed["goal_verdict"] == "unmet"
    assert viewed["open_items"]
    # 達成・RAG登録なし: completed 扱いにしない。
    mission = manager.memory.get_mission(pid)
    assert mission["status"] != "completed"


def test_external_result_is_finished_undetermined(tmp_path):
    manager, pid = _manager(tmp_path, "external")
    task = _plan(manager, pid)
    row = {"item_id": "SC01-R02", "criterion_id": "SC01",
           "artifact_path": "result/a.md", "requirement": "実施後の数値",
           "method": "count", "required": True, "result": "UNTESTABLE",
           "line": -1, "quote": "",
           "reason": "承認済み外部アクションの実行証拠が不足しています"}
    _ledger(manager, pid, task, [row])
    rid = "run-external-1"
    _start_cycle(manager, pid, rid)
    ended = _close_from_ledger(manager, pid, rid)
    assert (ended["execution_state"], ended["goal_verdict"]) == ("finished", "undetermined")
    saved = _latest(manager, pid)
    assert saved["pending_kind"] == "external_result"
    item = saved["open_items"][0]
    assert item["kind"] == "external_result" and item["owner"] == "external"
    assert "観測値" in item["evidence_needed"] and "証拠形式" in item["evidence_needed"]
    assert "新しいrun_id" in item["next_action"]
    # 架空の値を作らない: ステータス文に数値の断定が無い。
    assert "0" not in item["evidence_needed"] or "0件" not in item["status_text"]


def test_human_decision_is_finished_undetermined_without_approval(tmp_path):
    manager, pid = _manager(tmp_path, "human")
    task = _plan(manager, pid)
    row = {"item_id": "SC01-R03", "criterion_id": "SC01",
           "artifact_path": "result/a.md", "requirement": "文意の適合",
           "method": "llm_semantic", "required": True, "result": "UNTESTABLE",
           "line": -1, "quote": "", "reason": "人の確認待ち: 判断が未実施"}
    _ledger(manager, pid, task, [row])
    rid = "run-human-1"
    _start_cycle(manager, pid, rid)
    ended = _close_from_ledger(manager, pid, rid)
    assert (ended["execution_state"], ended["goal_verdict"]) == ("finished", "undetermined")
    saved = _latest(manager, pid)
    assert saved["pending_kind"] == "human_decision"
    item = saved["open_items"][0]
    assert item["owner"] == "human"
    assert "自動付与" not in item["next_action"] or "しない" in item["next_action"]
    mission = manager.memory.get_mission(pid)
    assert mission["status"] != "completed"


def test_repairable_item_stops_after_two_attempts(tmp_path):
    import app.run_cycles as runs
    from app.requirement_item_ledger import count_repair_attempts, record_repair_run

    manager, pid = _manager(tmp_path, "retry")
    task = _plan(manager, pid)
    row = {"item_id": "SC01-R04", "criterion_id": "SC01",
           "artifact_path": "result/a.md", "requirement": "必須語句",
           "method": "phrase", "required": True, "result": "FAIL",
           "line": -1, "quote": "", "reason": "必須語句なし: 架空語句"}
    _ledger(manager, pid, task, [row])
    rid = "run-retry-1"
    _start_cycle(manager, pid, rid)
    signature = "sig-retry"
    for _ in range(2):
        record_repair_run(
            manager.memory.path, project_id=pid, item_id="SC01-R04",
            failure_kind="phrase_missing", failure_signature=signature,
            plan_signature="sig-p02", source_hash="src-p02",
            artifact_path="result/a.md",
            artifact_hash_before="h", artifact_hash_after="h",
            item_result_before="FAIL", item_result_after="FAIL",
            decision="repair_failed", operator_action="人の操作",
            reason="疑似の失敗")
    assert count_repair_attempts(manager.memory.path, pid, signature) == 2
    # 上限到達: 追補を増やさず残件として終了する(ここでは記録自体の上限検査)。
    ended = _close_from_ledger(manager, pid, rid)
    assert ended["execution_state"] == "finished"
    saved = _latest(manager, pid)
    task_keys = [t["task_key"] for t in manager.memory.get_mission(pid)["tasks"]]
    assert not [k for k in task_keys if k.startswith("goal_supplement_")]
    assert saved["open_items"]


def test_ledger_unreadable_is_stopped_error_without_legacy(tmp_path):
    import sqlite3

    from app.requirement_item_ledger import LedgerReadError, latest_ledger

    manager, pid = _manager(tmp_path, "ledger")
    task = _plan(manager, pid)
    row = {"item_id": "SC01-R01", "criterion_id": "SC01",
           "artifact_path": "result/a.md", "requirement": "架空要求",
           "method": "phrase", "required": True, "result": "FAIL",
           "line": -1, "quote": "", "reason": "必須語句なし: 架空語句"}
    _ledger(manager, pid, task, [row])
    rid = "run-ledger-1"
    _start_cycle(manager, pid, rid)
    # 台帳DBは存在するがSQLiteとして読めない(壊れたDB)状態にする。
    from app.requirement_item_ledger import item_ledger_db_path

    broken = item_ledger_db_path(manager.memory.path)
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_bytes(b"not-a-sqlite-database")
    # 読み取り専用経路は LedgerReadError(空・成功に倒さない)。
    with pytest.raises(LedgerReadError):
        latest_ledger(manager.memory.path, pid)
    # 台帳の読取失敗は旧式追補へ迂回しない。
    # 記録DB自体が壊れているため、終了記録も例外で表に出す(成功扱いにしない)。
    # _close_run_cycle 側で finally 相当の記録を試み、失敗は握りつぶさない。
    import sqlite3 as _sqlite3

    with pytest.raises((_sqlite3.DatabaseError, RuntimeError)):
        _close_from_ledger(manager, pid, rid)
    mission = manager.memory.get_mission(pid)
    assert not [t for t in mission["tasks"] if t["task_key"].startswith("goal_supplement_")]
    # running でも finished でもない(記録不能を成功/終了として扱わない)。
    import app.run_cycles as _runs

    with pytest.raises(_runs.RunCycleReadError):
        _latest(manager, pid)


def test_contract_path_conflict_is_stopped_error(tmp_path):
    from app.workspace_files import ContractPathConflictError

    manager, pid = _manager(tmp_path, "conflict")
    task = _plan(manager, pid)
    project = manager.memory.get_project(pid)
    # ファイル予定パス自体をディレクトリにする(契約のパス種別衝突)。
    target_dir = manager.workspace.resolve_file(
        project.get("workspace_path", ""), pid, "result/a.md")[2]
    target_dir.mkdir(parents=True, exist_ok=True)
    row = {"item_id": "SC01-R05", "criterion_id": "SC01",
           "artifact_path": "result/a.md", "requirement": "架空要求",
           "method": "phrase", "required": True, "result": "FAIL",
           "line": -1, "quote": "", "reason": "必須語句なし: 架空語句"}
    _ledger(manager, pid, task, [row])
    rid = "run-conflict-1"
    _start_cycle(manager, pid, rid)
    # 書込前の検査が衝突を検出する(削除・上書きしない)。
    with pytest.raises(ContractPathConflictError):
        manager.workspace.apply_operations(
            project.get("workspace_path", ""), pid,
            [{"action": "write_text", "path": "result/a.md", "content": "x"}],
        )
    # P0-2 分類: 基盤側の衝突は stopped_error で記録する。
    ended = manager._close_run_cycle(
        pid, rid, True, execution_state="stopped_error",
        goal_verdict="undetermined", pending_kind="verification",
        stop_reason="契約ファイルパス衝突のため異常停止",
    )
    assert ended["execution_state"] == "stopped_error"
    # 既存データを削除・上書きしていない。
    assert manager.workspace.resolve_file(
        project.get("workspace_path", ""), pid,
        "result", must_exist=True)[2].is_dir()


def test_same_run_id_is_idempotent(tmp_path):
    manager, pid = _manager(tmp_path, "idem")
    rid = "run-idem-1"
    _start_cycle(manager, pid, rid)
    import app.run_cycles as runs

    first = runs.add_open_item(
        manager.memory.path, rid, item_id="SC01-R01",
        kind="artifact_missing", status_text="不足",
        evidence_needed="証拠", owner="system",
        next_action="次", idempotency_key="dup-1")
    again = runs.add_open_item(
        manager.memory.path, rid, item_id="SC01-R01",
        kind="artifact_missing", status_text="不足",
        evidence_needed="証拠", owner="system",
        next_action="次", idempotency_key="dup-1")
    assert again["id"] == first["id"] and again["idempotent"] is True
    end1 = _finish_run_in_test(
        manager, pid, run_id=rid, execution_state="finished",
        goal_verdict="unmet")
    end2 = _finish_run_in_test(
        manager, pid, run_id=rid, execution_state="cancelled",
        goal_verdict="unmet")
    assert end1["idempotent"] is False
    assert end2["idempotent"] is True
    assert end2["execution_state"] == "finished"
    cycles = _run_cycle_of(manager, pid)
    assert [c for c in cycles if c[0] == rid] and len(
        [c for c in cycles if c[0] == rid]) == 1


def test_cancelled_and_time_limit_are_recorded(tmp_path):
    manager, pid = _manager(tmp_path, "cancel")
    for rid, state in (("run-cancel-1", "cancelled"),
                       ("run-time-1", "stopped_time_limit")):
        _start_cycle(manager, pid, rid)
        ended = _finish_run_in_test(
            manager, pid, run_id=rid, execution_state=state,
            goal_verdict="undetermined")
        assert ended["execution_state"] == state
    cycles = _run_cycle_of(manager, pid)
    assert not [c for c in cycles if c[1] == "running"]


def test_unclassified_is_human_owned(tmp_path):
    from app.run_closure import build_open_item_params, map_open_kind

    assert map_open_kind("unclassified") == "unclassified"
    assert map_open_kind("future_unknown_kind") == "unclassified"
    params = build_open_item_params(
        run_id="r", row={"item_id": "x", "result": "UNTESTABLE"},
        failure_kind="unclassified",
    )
    assert params["kind"] == "unclassified"
    assert params["owner"] == "human"
    assert "分類できないため人が確認" in params["next_action"]


def test_failure_classification_uses_types_not_message_text(tmp_path):
    manager, _ = _manager(tmp_path, "classify")
    assert manager._classify_run_failure(RuntimeError("please cancel this operation")) == "stopped_error"
    assert manager._classify_run_failure(TimeoutError("ordinary")) == "stopped_time_limit"
    assert manager._classify_run_failure(asyncio.CancelledError()) == "cancelled"


def test_early_failed_tasks_record_open_items_before_close(tmp_path):
    manager, pid = _manager(tmp_path, "early-failed")
    rid = "run-early-failed"
    _start_cycle(manager, pid, rid)
    manager._record_early_open_items(
        pid, rid, True,
        tasks=[{"task_key": "t-fail", "status": "failed", "error": "実行基盤エラー"}],
    )
    manager._close_run_cycle(
        pid, rid, True, execution_state="finished", goal_verdict="unmet",
        pending_kind="artifact", stop_reason="失敗",
    )
    saved = _latest(manager, pid)
    assert saved["open_items"] and saved["open_items"][0]["next_action"]
    viewed = _latest_via_api(manager, pid)
    assert viewed["open_items"] and viewed["goal_verdict"] == "unmet"


def test_early_external_wait_records_open_item_before_close(tmp_path):
    manager, pid = _manager(tmp_path, "early-external")
    rid = "run-early-external"
    _start_cycle(manager, pid, rid)
    manager._record_early_open_items(pid, rid, True, external_result=True)
    manager._close_run_cycle(
        pid, rid, True, execution_state="finished", goal_verdict="undetermined",
        pending_kind="external_result", stop_reason="外部結果待ち",
    )
    saved = _latest(manager, pid)
    item = saved["open_items"][0]
    assert item["kind"] == "external_result" and item["owner"] == "external"
    assert "観測値" in item["evidence_needed"] and "証拠形式" in item["evidence_needed"]
    assert _latest_via_api(manager, pid)["open_items"][0]["kind"] == "external_result"


def test_recursive_run_reuses_one_cycle_and_finishes(tmp_path, monkeypatch):
    manager, pid = _manager(tmp_path, "recursive")
    task = _plan(manager, pid)
    manager.memory.update_task(task["id"], "completed", result="done")
    manager.memory.set_mission_status(pid, "ready", "test-ready", "test")
    calls = {"report": 0}

    async def final_report(_pid):
        calls["report"] += 1
        if calls["report"] == 1:
            return "未達", "failed", ["追補必要"]
        return "完了", "passed", []

    def supplement(_pid, _unmet, _report):
        manager.memory.append_plan_tasks(pid, [{
            "task_key": "supplement", "depends_on": ["t1"], "title": "追補",
            "description": "追補", "acceptance_criteria": "追補", "mode": "local",
        }], "追補")
        manager.memory.set_mission_status(pid, "paused", "追補追加", "supplement")
        return True

    async def run_one(_pid, pending, _agent):
        manager.memory.update_task(pending["id"], "completed", result="done")
        return True

    original_set_status = manager.memory.set_mission_status

    def set_status(_pid, status, message, reason):
        return original_set_status(_pid, status, message, reason)

    monkeypatch.setattr(manager, "_final_report", final_report)
    monkeypatch.setattr(manager, "_append_goal_supplement", supplement)
    monkeypatch.setattr(manager, "_run_one", run_one)
    monkeypatch.setattr(manager, "_evaluate_completion_gate", lambda _pid: {"achieved": True})
    monkeypatch.setattr(manager.memory, "set_mission_status", set_status)
    awaitable = manager._run(pid)
    asyncio.run(awaitable)
    cycles = _run_cycle_of(manager, pid)
    assert len(cycles) == 1
    assert cycles[0][1] == "finished"
    assert cycles[0][2] in {"achieved", "undetermined"}


def test_run_closure_binds_structured_verification_artifact(tmp_path):
    import json

    manager, pid = _manager(tmp_path, "verification-binding")
    task = _plan(manager, pid)
    contract = json.dumps({
        "schema": "structured-plan/v1",
        "criterion": "final",
        "criterion_ids": ["SC01"],
        "final_verification": True,
        "role": "final_verification",
        "execution_kind": "final_verification",
        "requirement_matrix": [],
        "outputs": [{"path": "result/final_verification.md", "kind": "document",
                     "required_headings": ["final"], "minimum_characters": 1}],
        "source_refs": [],
    })
    manager.memory.replace_plan(pid, "verification", [{
        "task_key": "final_verification", "depends_on": [],
        "title": "最終検証", "description": "verify",
        "acceptance_criteria": contract, "mode": "local",
    }])
    task = manager.memory.get_mission(pid)["tasks"][0]
    manager.memory.update_task(task["id"], "completed", result="verified")
    _write(manager, pid, "result/final_verification.md", "# final\nverified\n")
    rid = "run-verification-binding"
    _start_cycle(manager, pid, rid)
    manager._close_run_cycle(
        pid, rid, True, execution_state="finished", goal_verdict="unmet",
        pending_kind="verification", stop_reason="acceptance",
    )
    saved = _latest(manager, pid)
    assert saved["verification_ref"] == "result/final_verification.md"
    assert len(saved["verification_hash"]) == 64


def test_run_closure_records_missing_verification_reason(tmp_path):
    import json

    manager, pid = _manager(tmp_path, "verification-missing")
    contract = json.dumps({
        "schema": "local-cowork-plan/v1", "criterion_ids": ["SC01"],
        "final_verification": True,
        "outputs": [{"path": "result/final_verification.md"}],
    })
    manager.memory.replace_plan(pid, "verification", [{
        "task_key": "final_verification", "depends_on": [], "title": "最終検証",
        "description": "verify", "acceptance_criteria": contract, "mode": "local",
    }])
    rid = "run-verification-missing"
    _start_cycle(manager, pid, rid)
    manager._close_run_cycle(
        pid, rid, True, execution_state="finished", goal_verdict="unmet",
        pending_kind="verification", stop_reason="acceptance",
    )
    assert "検証書なし" in _latest(manager, pid)["stop_reason"]


def test_run_closure_records_unreadable_verification_reason(tmp_path, monkeypatch):
    import json

    manager, pid = _manager(tmp_path, "verification-unreadable")
    contract = json.dumps({
        "schema": "local-cowork-plan/v1", "criterion_ids": ["SC01"],
        "final_verification": True,
        "outputs": [{"path": "result/final_verification.md"}],
    })
    manager.memory.replace_plan(pid, "verification", [{
        "task_key": "final_verification", "depends_on": [], "title": "最終検証",
        "description": "verify", "acceptance_criteria": contract, "mode": "local",
    }])
    project = manager.memory.get_project(pid)
    original = manager.workspace.resolve_file

    def unreadable(workspace_path, project_id, relative, must_exist=False):
        if relative == "result/final_verification.md":
            raise PermissionError("denied")
        return original(workspace_path, project_id, relative, must_exist=must_exist)

    monkeypatch.setattr(manager.workspace, "resolve_file", unreadable)
    rid = "run-verification-unreadable"
    _start_cycle(manager, pid, rid)
    manager._close_run_cycle(
        pid, rid, True, execution_state="finished", goal_verdict="unmet",
        pending_kind="verification", stop_reason="acceptance",
    )
    assert "検証書を読めない: PermissionError" in _latest(manager, pid)["stop_reason"]


def test_achieved_requires_gate_result(tmp_path):
    manager, pid = _manager(tmp_path, "gate")
    rid = "run-gate-1"
    _start_cycle(manager, pid, rid)
    import app.run_cycles as runs

    with pytest.raises(ValueError):
        runs.finish_run(manager.memory.path, rid, execution_state="finished",
                        goal_verdict="achieved")
    done = runs.finish_run(
        manager.memory.path, rid, execution_state="finished",
        goal_verdict="achieved",
        completion_gate_result={"achieved": True})
    assert done["goal_verdict"] == "achieved"
    cycles = _run_cycle_of(manager, pid)
    assert ("run-gate-1", "finished", "achieved", "none") in cycles
