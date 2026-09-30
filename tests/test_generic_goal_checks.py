"""P0-4 generic criterion evaluator. Fictional data only."""
from __future__ import annotations

import pytest

from app.completion_gate import accept, evaluate
from app.goal_contract import activate
from app.memory.short_term import ShortTermMemory
from app.project_manager import ProjectOrchestrator
from app.structured_planning import compile_plan, compile_task, contract_of
from app.workspace_files import WorkspaceSandbox


FILLER = "架空の検証可能な具体的内容を記載する。" * 40


def _manager(tmp_path, goal, success):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("汎用案件テスト", workspace_path="projects/generic")
    pid = project["id"]
    memory.save_mission(pid, goal, success, "", False, [])
    workspace = WorkspaceSandbox(tmp_path / "workspace")
    manager = ProjectOrchestrator(
        memory, None, lambda p: ("", []), None, lambda: [], workspace=workspace,
    )
    return manager, pid


def _write_markdown(manager, pid, path, headings):
    project = manager.memory.get_project(pid)
    target = manager.workspace.resolve_file(project.get("workspace_path", ""), pid, path)[2]
    target.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"## {heading}\n{FILLER}" for heading in headings)
    target.write_text(body, encoding="utf-8")
    return target


def _write_csv(manager, pid, path, columns, rows):
    project = manager.memory.get_project(pid)
    target = manager.workspace.resolve_file(project.get("workspace_path", ""), pid, path)[2]
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [",".join(columns)]
    for row in rows:
        lines.append(",".join(row))
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def _complete_outputs(manager, pid, task):
    contract = contract_of(task) or {}
    for output in contract.get("outputs") or []:
        path = output["path"]
        if path.endswith(".csv"):
            _write_csv(
                manager, pid, path,
                output["required_columns"],
                [["T-1", "架空要件", "未実施", "未入力テンプレート", "承認待ち", ""]],
            )
        else:
            _write_markdown(manager, pid, path, output["required_headings"])
    manager.memory.update_task(task["id"], "completed", result="成果物作成済み")


def _final_report(criteria_ids, *, verdicts=None):
    verdicts = verdicts or {cid: "PASS" for cid in criteria_ids}
    sections = ["# 最終検証・達成条件別の判定", "", "## 達成条件別判定"]
    for cid in criteria_ids:
        sections.append(f"### {cid} — {verdicts[cid]}")
        sections.append("")
        sections.append(f"- {cid}: {verdicts[cid]}")
        sections.append("- 根拠パス: result/")
        sections.append(FILLER)
    sections.extend([
        "",
        "## 成果物検証",
        FILLER,
        "",
        "## 未達条件と承認待ち",
        FILLER,
        "",
    ])
    return "\n".join(sections)


def _complete_final(manager, pid, task, criteria_ids, *, verdicts=None):
    contract = contract_of(task) or {}
    path = contract["outputs"][0]["path"]
    project = manager.memory.get_project(pid)
    target = manager.workspace.resolve_file(project.get("workspace_path", ""), pid, path)[2]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_final_report(criteria_ids, verdicts=verdicts), encoding="utf-8")
    manager.memory.update_task(task["id"], "completed", result="最終検証済み")


def _by_id(gate):
    return {row["criterion_id"]: row for row in gate["criteria"]}


def setup_two_artifact_criteria(tmp_path):
    goal = "架空の準備資料を整える"
    success = "1. 対象市場と顧客課題を定義する\n2. 販売計画の骨子を作成する"
    manager, pid = _manager(tmp_path, goal, success)
    tasks = [
        compile_task(1, "対象市場と顧客課題を定義する", {
            "title": "市場定義資料", "scope": "架空市場の整理",
            "headings": ["目的", "実施内容"], "depends_on": [],
        }, []),
        compile_task(2, "販売計画の骨子を作成する", {
            "title": "計画骨子資料", "scope": "架空計画の整理",
            "headings": ["目的", "実施内容"], "depends_on": ["SC01"],
        }, []),
    ]
    plan = compile_plan(
        ["対象市場と顧客課題を定義する", "販売計画の骨子を作成する"],
        tasks, goal=goal,
    )
    manager.memory.replace_plan(pid, plan["summary"], plan["tasks"])
    activate(manager, pid)
    return manager, pid


def setup_sales_like(tmp_path):
    goal = "本システムを販売実行する"
    success = (
        "1. 対象市場と顧客課題を定義する\n"
        "2. 実際の営業活動について、実行済み、承認待ち、未着手、失敗、次回行動を明確に報告する"
    )
    manager, pid = _manager(tmp_path, goal, success)
    tasks = [
        compile_task(1, "対象市場と顧客課題を定義する", {
            "title": "市場定義資料", "scope": "架空市場の整理",
            "headings": ["目的", "実施内容"], "depends_on": [],
        }, []),
        compile_task(2, "実際の営業活動について、実行済み、承認待ち、未着手、失敗、次回行動を明確に報告する", {
            "title": "営業活動報告", "scope": "実施状態の整理",
            "headings": ["目的", "実施内容"], "depends_on": ["SC01"],
        }, []),
    ]
    plan = compile_plan(
        [
            "対象市場と顧客課題を定義する",
            "実際の営業活動について、実行済み、承認待ち、未着手、失敗、次回行動を明確に報告する",
        ],
        tasks, goal=goal,
    )
    manager.memory.replace_plan(pid, plan["summary"], plan["tasks"])
    activate(manager, pid)
    return manager, pid


def complete_all_artifacts(manager, pid, *, include_final=True, skip_keys=()):
    mission = manager.memory.get_mission(pid)
    criteria_ids = [
        cid for task in mission["tasks"]
        for cid in ((contract_of(task) or {}).get("criterion_ids") or [])
    ]
    for task in mission["tasks"]:
        key = task["task_key"]
        if key in skip_keys:
            continue
        if (contract_of(task) or {}).get("final_verification"):
            if include_final:
                _complete_final(manager, pid, task, criteria_ids)
            continue
        _complete_outputs(manager, pid, task)
    return manager.memory.get_mission(pid)


def test_generic_all_artifacts_pass(tmp_path):
    manager, pid = setup_two_artifact_criteria(tmp_path)
    complete_all_artifacts(manager, pid)
    gate = evaluate(manager, pid)
    by_id = _by_id(gate)
    assert by_id["SC01"]["status"] == "PASS"
    assert by_id["SC02"]["status"] == "PASS"
    assert gate["achieved"] is False
    assert gate["reason_code"] == "HUMAN_ACCEPTANCE_MISSING"


def test_missing_artifact_fails_only_that_criterion(tmp_path):
    manager, pid = setup_two_artifact_criteria(tmp_path)
    complete_all_artifacts(manager, pid, skip_keys={"SC02"})
    gate = evaluate(manager, pid)
    by_id = _by_id(gate)
    assert by_id["SC01"]["status"] == "PASS"
    assert by_id["SC02"]["status"] in {"FAIL", "BLOCKED"}
    assert "SC01" not in gate["failed_criteria"]
    assert "SC02" in gate["failed_criteria"]
    assert by_id["SC01"].get("reason_code") != by_id["SC02"].get("reason_code") or by_id["SC02"]["status"] != "PASS"


def test_sc10_materials_without_execution_evidence_are_not_pass(tmp_path):
    manager, pid = setup_sales_like(tmp_path)
    complete_all_artifacts(manager, pid)
    gate = evaluate(manager, pid)
    by_id = _by_id(gate)
    assert by_id["SC01"]["status"] == "PASS"
    assert by_id["SC02"]["status"] != "PASS"
    assert by_id["SC02"]["status"] in {"FAIL", "BLOCKED"}
    assert "EXTERNAL" in (by_id["SC02"].get("reason_code") or "") or "TRIAL" in (by_id["SC02"].get("reason_code") or "")
    assert gate["achieved"] is False


def test_achieved_requires_all_pass_human_and_hash(tmp_path):
    manager, pid = setup_two_artifact_criteria(tmp_path)
    complete_all_artifacts(manager, pid)
    before = evaluate(manager, pid)
    assert before["achieved"] is False
    with pytest.raises(ValueError, match="accepted_by is required"):
        accept(manager, pid, accepted_by="")
    result = accept(manager, pid, accepted_by="tester")
    assert result["achieved"] is True
    assert result["human_accepted"] is True
    assert all(row["status"] == "PASS" for row in result["criteria"])
    assert result["artifact_hash"]


def test_hash_change_invalidates_existing_acceptance(tmp_path):
    manager, pid = setup_two_artifact_criteria(tmp_path)
    complete_all_artifacts(manager, pid)
    accepted = accept(manager, pid, accepted_by="tester")
    assert accepted["achieved"] is True
    mission = manager.memory.get_mission(pid)
    sc01 = next(task for task in mission["tasks"] if task["task_key"] == "SC01")
    path = contract_of(sc01)["outputs"][0]["path"]
    project = manager.memory.get_project(pid)
    target = manager.workspace.resolve_file(project.get("workspace_path", ""), pid, path)[2]
    target.write_text(target.read_text(encoding="utf-8") + "\n追記した架空の変更。\n", encoding="utf-8")
    drifted = evaluate(manager, pid)
    assert drifted["achieved"] is False
    assert drifted["human_acceptance"]["valid"] is False
    assert "HASH_DRIFT" in drifted["human_acceptance"]["reason"]


def test_human_confirmation_rejected_before_all_pass(tmp_path):
    manager, pid = setup_two_artifact_criteria(tmp_path)
    complete_all_artifacts(manager, pid, skip_keys={"SC02"})
    with pytest.raises(ValueError, match="全達成条件がPASS"):
        accept(manager, pid, accepted_by="tester")
    gate = evaluate(manager, pid)
    assert gate["achieved"] is False
    assert _by_id(gate)["SC01"]["status"] == "PASS"


def test_sales_executed_evidence_can_pass_sc10(tmp_path):
    manager, pid = setup_sales_like(tmp_path)
    complete_all_artifacts(manager, pid)
    action = manager.memory.create_action(pid, "manual", "架空の見込み客A", "初回案内を実施")
    manager.memory.update_action(pid, action["id"], "approved")
    manager.memory.update_action(
        pid, action["id"], "executed",
        evidence="2026-03-01 架空面談記録 TEST-1 実施結果: 未受注 学習内容: 仮説を更新",
    )
    gate = evaluate(manager, pid)
    by_id = _by_id(gate)
    assert by_id["SC01"]["status"] == "PASS"
    assert by_id["SC02"]["status"] == "PASS"
    assert gate["achieved"] is False
    result = accept(manager, pid, accepted_by="tester")
    assert result["achieved"] is True
