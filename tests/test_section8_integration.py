"""§8 統合試験: P0-1〜P0-6 と §7 を組み合わせた一連シナリオ。架空データのみ。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.completion_gate import accept, evaluate
from app.goal_completion_flag import enable
from app.goal_contract import activate
from app.goal_review import ReviewStore, execution_gate, plan_snapshot, review_plan
from app.memory.short_term import ShortTermMemory
from app.next_action_controller import compute
from app.plan_coverage import build as build_coverage
from app.plan_feedback import issues_for
from app.plan_recovery import apply_recovery, preview_recovery
from app.project_manager import ProjectOrchestrator
from app.restart_convergence import reconcile_after_restart
from app.structured_planning import (
    compile_plan,
    compile_task,
    inspect_plan_structure,
)
from app.workflow_readiness import build_readiness
from app.workspace_files import WorkspaceSandbox


FILLER = "架空の検証可能な具体的内容を記載する。" * 40
ROOT = Path(__file__).resolve().parents[1]

GOAL = "架空のテスト販売案件の準備資料を整える"
SUCCESS = (
    "1. 対象市場と顧客課題を定義する\n"
    "2. 販売計画の骨子を作成する\n"
    "3. 実際の営業活動について、実行済み、承認待ち、未着手、失敗、次回行動を明確に報告する"
)
CONSTRAINTS = "社内規程に準拠し、外部漏洩を防止する。"
CRITERIA = [
    "対象市場と顧客課題を定義する",
    "販売計画の骨子を作成する",
    "実際の営業活動について、実行済み、承認待ち、未着手、失敗、次回行動を明確に報告する",
]
PUBLIC_SUMMARY = (
    "架空のテスト販売案件について、目標・達成条件・工程・成果物の対応を公開用に整理した説明です。"
    "秘密情報や個人情報は含みません。"
)


def _write_policy(tmp_path, **fields):
    body = {"required": True}
    body.update(fields)
    (tmp_path / "goal_review_policy.json").write_text(
        json.dumps(body, ensure_ascii=False), encoding="utf-8",
    )


def _pass_review(provider):
    return {
        "id": provider, "ok": True,
        "review": json.dumps({"verdict": "pass", "issues": []}),
    }


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
    from app.structured_planning import contract_of
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


def _complete_final(manager, pid, task, criteria_ids):
    from app.structured_planning import contract_of
    contract = contract_of(task) or {}
    path = contract["outputs"][0]["path"]
    project = manager.memory.get_project(pid)
    target = manager.workspace.resolve_file(project.get("workspace_path", ""), pid, path)[2]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_final_report(criteria_ids), encoding="utf-8")
    manager.memory.update_task(task["id"], "completed", result="最終検証済み")


def _complete_local_artifacts(manager, pid):
    from app.structured_planning import contract_of
    mission = manager.memory.get_mission(pid)
    criteria_ids = [
        cid for task in mission["tasks"]
        for cid in ((contract_of(task) or {}).get("criterion_ids") or [])
    ]
    for task in mission["tasks"]:
        if (contract_of(task) or {}).get("final_verification"):
            _complete_final(manager, pid, task, criteria_ids)
            continue
        _complete_outputs(manager, pid, task)
    return manager.memory.get_mission(pid)


def _by_id(gate):
    return {row["criterion_id"]: row for row in gate["criteria"]}


def _create_duplicated_sales_project(tmp_path):
    """架空のテスト販売案件。Ver.N に重複タスクと旧レビューを残す。"""
    memory = ShortTermMemory(tmp_path / "conversations.db")
    project = memory.create_project("テスト販売案件", workspace_path="projects/test-sales")
    pid = project["id"]
    memory.save_mission(pid, GOAL, SUCCESS, CONSTRAINTS, True, ["a", "b"])
    ws = WorkspaceSandbox(tmp_path / "workspace")
    manager = ProjectOrchestrator(
        memory, None, lambda p: ("", []), None,
        lambda: [{"id": "a", "configured": True}, {"id": "b", "configured": True}],
        workspace=ws,
    )
    enable(manager.memory.path, pid)

    with manager.memory._connect() as db:
        db.execute(
            "UPDATE project_missions SET plan_version=10, status='planning' WHERE project_id=?",
            (pid,),
        )

    t1 = compile_task(1, CRITERIA[0], {
        "title": "市場定義資料", "scope": "架空市場の整理",
        "headings": ["目的", "実施内容"], "depends_on": [],
    }, [])
    t2 = compile_task(2, CRITERIA[1], {
        "title": "計画骨子資料", "scope": "架空計画の整理",
        "headings": ["目的", "実施内容"], "depends_on": ["SC01"],
    }, [])
    t3 = compile_task(3, CRITERIA[2], {
        "title": "営業活動報告", "scope": "実施状態の整理",
        "headings": ["目的", "実施内容"], "depends_on": ["SC01", "SC02"],
    }, [])
    compiled = compile_plan(CRITERIA, [t1, t2, t3], goal=GOAL)
    tasks = list(compiled["tasks"])

    duplicate_task = dict(t1)
    duplicate_task["task_key"] = "SC01_DUP"
    duplicate_task["title"] = "市場定義資料 (旧重複)"
    tasks.insert(2, duplicate_task)

    legacy_eval_task = {
        "task_key": "TASK_EVAL",
        "title": "計画草案の評価と改善提案",
        "description": "旧バージョンの評価タスク",
        "acceptance_criteria": json.dumps({"schema": "local-cowork-plan/v1", "criterion_ids": []}),
        "mode": "local",
        "depends_on": ["SC01"],
    }
    tasks.insert(4, legacy_eval_task)

    with manager.memory._connect() as db:
        db.execute("DELETE FROM project_tasks WHERE project_id=?", (pid,))
        for pos, t in enumerate(tasks, 1):
            db.execute(
                """INSERT INTO project_tasks(
                    id, project_id, position, title, description, acceptance_criteria,
                    mode, status, task_key, depends_on, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, '2026-09-30T00:00:00Z', '2026-09-30T00:00:00Z')""",
                (
                    f"task_{pos}_{t['task_key']}", pid, pos, t["title"],
                    t.get("description", ""), t.get("acceptance_criteria", ""),
                    t.get("mode", "local"), t.get("task_key", f"task_{pos}"),
                    json.dumps(t.get("depends_on", [])),
                ),
            )

    manager.memory.set_plan_reviews(pid, [
        {"id": "legacy_provider", "ok": True, "review": "Ver.N以前の旧指摘内容です。"},
    ])
    return manager, pid


@pytest.mark.asyncio
async def test_section8_end_to_end_scenario(tmp_path):
    """計画書§8 統合試験 1〜8 を1本のシナリオとして通す。"""
    manager, pid = _create_duplicated_sales_project(tmp_path)
    _write_policy(tmp_path, min_success_count=1)

    # 1. 重複タスクを含む旧計画(Ver.N)を読み込む
    mission_n = manager.memory.get_mission(pid)
    assert mission_n["plan_version"] == 10
    old_keys = [t["task_key"] for t in mission_n["tasks"]]
    assert "SC01_DUP" in old_keys
    assert "TASK_EVAL" in old_keys
    assert old_keys.count("SC01") + old_keys.count("SC01_DUP") >= 2
    _, sig_n = plan_snapshot(manager, pid)
    assert issues_for(manager, pid, sig_n) == []
    assert mission_n["plan_reviews"][0]["id"] == "legacy_provider"

    # 2. legacy reviewを保持したまま Ver.N+1 候補をプレビュー生成する(§7)
    preview = preview_recovery(manager, pid)
    assert preview["saved"] is False
    assert preview["current_version"] == 10
    assert preview["candidate_version"] == 11
    after_preview = manager.memory.get_mission(pid)
    assert after_preview["plan_version"] == 10
    assert [t["task_key"] for t in after_preview["tasks"]] == old_keys
    assert after_preview["plan_reviews"][0]["id"] == "legacy_provider"
    assert issues_for(manager, pid, sig_n) == []

    # 3. 重複タスク0件、達成条件の被覆PASS
    candidate_keys = [t["task_key"] for t in preview["candidate_tasks"]]
    assert "SC01_DUP" not in candidate_keys
    assert "TASK_EVAL" not in candidate_keys
    assert len(candidate_keys) == len(set(candidate_keys))
    assert candidate_keys == ["SC00", "SC01", "SC02", "SC03", "final_verification"]
    structure = inspect_plan_structure(
        {"tasks": preview["candidate_tasks"]}, {"SC01", "SC02", "SC03"},
    )
    assert structure["passed"] is True
    assert preview["coverage"]["passed"] is True
    dummy_mission = {"tasks": preview["candidate_tasks"], "plan_version": 11}
    from app.goal_contract import from_mission
    coverage = build_coverage(dummy_mission, from_mission(after_preview, manager, pid))
    assert coverage["passed"] is True

    applied = apply_recovery(
        manager, pid, reviewer="scenario_reviewer",
        note="重複を解消しVer.N+1へ安全移行", expected_current_version=10,
    )
    assert applied["status"] == "applied"
    assert applied["new_version"] == 11
    mission_n1 = manager.memory.get_mission(pid)
    assert [t["task_key"] for t in mission_n1["tasks"]] == [
        "SC00", "SC01", "SC02", "SC03", "final_verification",
    ]
    assert mission_n1["plan_reviews"][0]["id"] == "legacy_provider"
    _, sig_n1 = plan_snapshot(manager, pid)
    assert issues_for(manager, pid, sig_n1) == []
    activate(manager, pid)

    # 4. 外部レビューの部分失敗(429/401相当)が指摘へ混入しない(P0-6)
    async def runner(_text, providers):
        out = []
        for provider in providers:
            if provider == "a":
                out.append({
                    "id": "a", "ok": False,
                    "error": "HTTP 429: RESOURCE_EXHAUSTED rate limit",
                    "outcome": "connection_error", "status_code": 429,
                    "error_category": "connection_error",
                })
            else:
                out.append(_pass_review(provider))
        return out

    manager.plan_review_runner = runner
    result = await review_plan(manager, pid, sig_n1, PUBLIC_SUMMARY, True)
    issues = issues_for(manager, pid, sig_n1)
    blob = json.dumps(issues, ensure_ascii=False)
    assert "429" not in blob
    assert "RESOURCE_EXHAUSTED" not in blob
    assert "401" not in blob
    assert all("HTTP" not in (item.get("text") or "") for item in issues)
    assert result["connection_errors"]
    assert any(err["provider"] == "a" and err["status_code"] == 429 for err in result["connection_errors"])
    assert result["status"] == "passed"

    # 5. 人間承認前に実行を開始できない
    assert mission_n1["status"] == "planning" or manager.memory.get_mission(pid)["status"] == "planning"
    ready_before = build_readiness(manager, pid)
    assert ready_before["allowed_actions"]["start"]["allowed"] is False
    with pytest.raises(ValueError, match="承認済みまたは一時停止中"):
        await manager.start(pid)
    approved = manager.approve(pid)
    assert approved["status"] == "ready"
    assert execution_gate(manager, pid)["blocked"] is False
    ready_after = build_readiness(manager, pid)
    assert ready_after["allowed_actions"]["start"]["allowed"] is True

    # 6. ローカルタスク完了後、外部行動が必要なcriterionは外部証拠待ち(P0-4)
    _complete_local_artifacts(manager, pid)
    pending = manager.memory.create_action(pid, "manual", "架空の見込み客A", "初回案内の下書き")
    gate_waiting = evaluate(manager, pid)
    by_waiting = _by_id(gate_waiting)
    assert by_waiting["SC01"]["status"] == "PASS"
    assert by_waiting["SC02"]["status"] == "PASS"
    assert by_waiting["SC03"]["status"] in {"FAIL", "BLOCKED"}
    assert by_waiting["SC03"]["status"] != "PASS"
    sc03_code = by_waiting["SC03"].get("reason_code") or ""
    sc03_msg = by_waiting["SC03"].get("message") or ""
    assert "EXTERNAL" in sc03_code or "待ち" in sc03_msg or "証拠" in sc03_msg
    assert gate_waiting["achieved"] is False

    waiting_action = compute(manager, pid)
    waiting_ready = build_readiness(manager, pid)
    waiting_next = waiting_ready["next_action"]["id"]

    # 8. 途中で状態を再取得しなおしても同じ次アクションに収束する(P0-5)
    reconcile_after_restart(manager.memory)
    again_ready = build_readiness(manager, pid)
    again_action = compute(manager, pid)
    assert again_ready["next_action"]["id"] == waiting_next
    assert again_action["action_id"] == waiting_action["action_id"]
    assert again_ready["plan_signature"] == waiting_ready["plan_signature"]

    # 7. 証拠と最終人間確認の両方が揃った後だけ achieved
    manager.memory.update_action(pid, pending["id"], "approved")
    manager.memory.update_action(
        pid, pending["id"], "executed",
        evidence="2026-03-01 担当:tester 架空面談記録 TEST-1 実施結果: 未受注",
    )
    gate_ready = evaluate(manager, pid)
    by_ready = _by_id(gate_ready)
    assert by_ready["SC01"]["status"] == "PASS"
    assert by_ready["SC02"]["status"] == "PASS"
    assert by_ready["SC03"]["status"] == "PASS"
    assert gate_ready["achieved"] is False
    assert gate_ready["reason_code"] == "HUMAN_ACCEPTANCE_MISSING"
    with pytest.raises(ValueError, match="accepted_by is required"):
        accept(manager, pid, accepted_by="")
    accepted = accept(manager, pid, accepted_by="scenario_tester")
    assert accepted["achieved"] is True
    assert accepted["human_accepted"] is True
    assert all(row["status"] == "PASS" for row in accepted["criteria"])

    final_ready = build_readiness(manager, pid)
    final_action = compute(manager, pid)
    reconcile_after_restart(manager.memory)
    final_again = build_readiness(manager, pid)
    assert final_again["next_action"]["id"] == final_ready["next_action"]["id"]
    assert compute(manager, pid)["action_id"] == final_action["action_id"]
    assert evaluate(manager, pid)["achieved"] is True


def test_healthcheck_script_exists_and_checks_expected_layout():
    """scripts/healthcheck.ps1 の静的確認。実行はしない。"""
    path = ROOT / "scripts" / "healthcheck.ps1"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "Ollama API" in text
    assert "Integrated Front" in text
    assert "8099" in text
    assert "3000" in text
    assert "8000" in text
    assert "8010" in text
    assert "docker compose ps" in text
    for service in ("web", "open-webui", "cptr", "google-publisher-browser"):
        assert service in text
    assert "OLLAMA_MODEL" in text
    assert "WORKSPACE_PATH" in text
    assert "Localhost port" in text
    assert "Health check: PASS" in text
