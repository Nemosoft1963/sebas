"""Tests for safe plan recovery and migration of existing projects (§7)."""
import json
import pytest
from fastapi.testclient import TestClient

from app.goal_completion_store import GoalCompletionStore
from app.goal_review import ReviewStore, plan_snapshot
from app.plan_recovery import preview_recovery, apply_recovery
from app.plan_feedback import issues_for
from app.structured_planning import compile_plan, compile_task, contract_of
from test_goal_review import setup


def _create_duplicated_v22_project(tmp_path):
    """Create a fictional project at Ver.22 with duplicated/legacy task structure."""
    manager, pid, _ = setup(tmp_path)
    goal = "業務ナレッジ共有システムと営業支援ツールの導入検証計画"
    success = (
        "1. 業務ナレッジ共有システムの構成案を作成する\n"
        "2. 営業支援ツールの連携仕様と導入工程を策定する\n"
        "3. 全体検証計画と費用対効果の評価基準を整理する"
    )
    constraints = "社内規程に準拠し、外部漏洩を防止する。"
    manager.memory.save_mission(pid, goal, success, constraints, False, [])

    # Simulate Ver.22 by setting plan_version = 22
    with manager.memory._connect() as db:
        db.execute(
            "UPDATE project_missions SET plan_version=22, status='planning' WHERE project_id=?",
            (pid,),
        )

    # Base tasks for SC01, SC02, SC03
    t1 = compile_task(1, "業務ナレッジ共有システムの構成案を作成する", {
        "title": "ナレッジ共有システム構成案",
        "scope": "業務ナレッジ共有システムの構成案を作成する",
        "headings": ["システム構成", "利用手順"],
        "depends_on": [],
    }, [])
    t2 = compile_task(2, "営業支援ツールの連携仕様と導入工程を策定する", {
        "title": "営業支援ツール連携仕様",
        "scope": "営業支援ツールの連携仕様と導入工程を策定する",
        "headings": ["連携仕様", "導入工程"],
        "depends_on": ["SC01"],
    }, [])
    t3 = compile_task(3, "全体検証計画と費用対効果の評価基準を整理する", {
        "title": "全体検証計画と評価基準",
        "scope": "全体検証計画と費用対効果の評価基準を整理する",
        "headings": ["検証計画", "評価基準"],
        "depends_on": ["SC01", "SC02"],
    }, [])

    compiled = compile_plan(
        [
            "業務ナレッジ共有システムの構成案を作成する",
            "営業支援ツールの連携仕様と導入工程を策定する",
            "全体検証計画と費用対効果の評価基準を整理する",
        ],
        [t1, t2, t3],
        goal=goal,
    )

    # Intentionally insert duplicate tasks to simulate legacy messy state
    tasks = list(compiled["tasks"])
    duplicate_task = dict(t1)
    duplicate_task["task_key"] = "SC01_DUP"
    duplicate_task["title"] = "ナレッジ共有システム構成案 (旧重複)"
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

    # Save to memory without incrementing version past 22
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

    return manager, pid


@pytest.mark.asyncio
async def test_recovery_preview_removes_duplicates_and_is_readonly(tmp_path):
    """1. 架空の重複タスクを含むVer.22でプレビューAPIを呼ぶと、重複が解消されたVer.23候補が生成され、保存されない(read-only)"""
    manager, pid = _create_duplicated_v22_project(tmp_path)

    # Mission state before preview
    m_before = manager.memory.get_mission(pid)
    assert m_before["plan_version"] == 22
    old_task_keys = [t["task_key"] for t in m_before["tasks"]]
    assert "SC01_DUP" in old_task_keys
    assert "TASK_EVAL" in old_task_keys
    assert len(m_before["tasks"]) == 7  # SC00, SC01, SC01_DUP, SC02, TASK_EVAL, SC03, final_verification

    # Execute preview
    preview = preview_recovery(manager, pid)

    # Assert preview result
    assert preview["saved"] is False
    assert preview["current_version"] == 22
    assert preview["candidate_version"] == 23
    assert preview["project_id"] == pid

    candidate_keys = [t["task_key"] for t in preview["candidate_tasks"]]
    assert "SC01_DUP" not in candidate_keys
    assert "TASK_EVAL" not in candidate_keys
    assert candidate_keys == ["SC00", "SC01", "SC02", "SC03", "final_verification"]

    # Assert read-only: mission in DB is completely unchanged
    m_after = manager.memory.get_mission(pid)
    assert m_after["plan_version"] == 22
    assert [t["task_key"] for t in m_after["tasks"]] == old_task_keys
    assert len(m_after["tasks"]) == 7


@pytest.mark.asyncio
async def test_recovery_preview_includes_diff_coverage_artifacts_invalidated(tmp_path):
    """2. プレビュー結果に被覆表・タスク差分・再利用成果物・無効化される承認の一覧が含まれること"""
    manager, pid = _create_duplicated_v22_project(tmp_path)

    # Create an existing artifact in workspace
    project = manager.memory.get_project(pid)
    _, _, sc01_path = manager.workspace.resolve_file(
        project.get("workspace_path", ""), pid, "result/sc01.md", must_exist=False
    )
    sc01_path.parent.mkdir(parents=True, exist_ok=True)
    sc01_path.write_text("# ナレッジ共有システム構成案\n\n## システム構成\n詳細...\n\n## 利用手順\n手順...", encoding="utf-8")

    # Record a legacy plan review in ReviewStore
    _, old_sig = plan_snapshot(manager, pid)
    review_store = ReviewStore(manager.memory.path)
    review_store.put(pid, "plan", old_sig, {
        "status": "passed",
        "reviews": [{"provider": "mock_reviewer", "status": "pass", "issues": []}],
    })

    # Record a human acceptance in GoalCompletionStore
    gc_store = GoalCompletionStore(manager.memory.path)
    gc_store.record_acceptance(
        pid, "hash_gc_old", old_sig, "in_hash", "src_hash", "art_hash", "human_tester", "旧版受入"
    )

    preview = preview_recovery(manager, pid)

    # 1. Coverage table
    cov = preview["coverage"]
    assert cov is not None
    assert cov["schema"] == "sebas-plan-coverage/v1"
    assert len(cov["rows"]) == 3  # SC01, SC02, SC03
    assert cov["passed"] is True

    # 2. Task diff
    diff = preview["task_diff"]
    removed_keys = [r["task_key"] for r in diff["removed"]]
    assert "SC01_DUP" in removed_keys
    assert "TASK_EVAL" in removed_keys
    assert len(diff["unchanged"]) >= 1

    # 3. Reusable artifacts
    artifacts = preview["reusable_artifacts"]
    artifact_paths = [a["path"] for a in artifacts]
    assert "result/sc01.md" in artifact_paths
    sc01_entry = next(a for a in artifacts if a["path"] == "result/sc01.md")
    assert sc01_entry["status"] == "available"
    assert sc01_entry["sha256"] != ""

    # 4. Invalidated approvals
    invalidated = preview["invalidated_approvals"]
    inv_types = [inv["type"] for inv in invalidated]
    assert "plan_review" in inv_types
    assert "human_acceptance" in inv_types


@pytest.mark.asyncio
async def test_recovery_apply_saves_only_on_explicit_approval_and_protects_history(tmp_path):
    """3. 人間の明示的な承認操作をした場合だけ新バージョンとして保存され、元のバージョンや履歴が保護される"""
    manager, pid = _create_duplicated_v22_project(tmp_path)

    # 承認者なしでの apply は拒否されること
    with pytest.raises(ValueError, match="承認者名"):
        apply_recovery(manager, pid, reviewer="")

    # バージョン不一致の指定での apply は拒否されること
    with pytest.raises(ValueError, match="計画版が変更"):
        apply_recovery(manager, pid, reviewer="chief_reviewer", expected_current_version=99)

    # プレビューを呼んでも保存されていないこと
    preview = preview_recovery(manager, pid)
    assert manager.memory.get_mission(pid)["plan_version"] == 22

    # 明示的承認による適用
    result = apply_recovery(
        manager, pid, reviewer="chief_reviewer", note="重複タスクを解消しVer.23として安全移行",
        expected_current_version=22,
    )

    assert result["status"] == "applied"
    assert result["previous_version"] == 22
    assert result["new_version"] == 23
    assert result["applied_by"] == "chief_reviewer"

    # DBの確認
    m_v23 = manager.memory.get_mission(pid)
    assert m_v23["plan_version"] == 23
    candidate_keys = [t["task_key"] for t in m_v23["tasks"]]
    assert "SC01_DUP" not in candidate_keys
    assert "TASK_EVAL" not in candidate_keys
    assert candidate_keys == ["SC00", "SC01", "SC02", "SC03", "final_verification"]

    # イベント履歴に記録され、全イベント履歴が残っていること
    events = m_v23["events"]
    applied_events = [e for e in events if e["kind"] == "recovery_plan_applied"]
    assert len(applied_events) == 1
    assert "chief_reviewer" in applied_events[0]["message"]

    # GoalCompletionStore に coverage が保存されていること
    saved_cov = GoalCompletionStore(manager.memory.path).get_coverage(pid, 23)
    assert saved_cov is not None
    assert saved_cov["passed"] is True


@pytest.mark.asyncio
async def test_recovery_preserves_prior_artifacts_instructions_actions_and_history(tmp_path):
    """4. 目標、達成条件、制約、追加指示、事前成果物、外部アクション証拠、全イベント履歴が削除・上書きされないこと"""
    manager, pid = _create_duplicated_v22_project(tmp_path)

    # 追加指示の登録
    manager.memory.add_mission_instruction(pid, "追加指示: 最新のセキュリティ指針を必ず反映すること")

    # 成果物の作成
    project = manager.memory.get_project(pid)
    _, _, prior_artifact = manager.workspace.resolve_file(
        project.get("workspace_path", ""), pid, "result/sc01.md", must_exist=False
    )
    prior_artifact.parent.mkdir(parents=True, exist_ok=True)
    prior_content = "# ナレッジ共有システム構成案\n\nVer.20時点で完成した成果物"
    prior_artifact.write_text(prior_content, encoding="utf-8")

    # 外部アクションと証拠の登録
    act = manager.memory.create_action(pid, "email", "client@example.com", "提案メール本文")
    manager.memory.update_action(pid, act["id"], "approved")
    manager.memory.update_action(pid, act["id"], "executed", evidence="SMTP message-id: <test1234@local>")

    # イベント数の記録
    events_before = len(manager.memory.get_mission(pid)["events"])

    # 移行を適用
    apply_recovery(manager, pid, reviewer="auditor_user", note="整合性維持移行")

    # 検証:
    m_after = manager.memory.get_mission(pid)
    # 1. 目標、達成条件、制約、追加指示の保持
    assert "業務ナレッジ共有システム" in m_after["goal"]
    assert "営業支援ツールの連携仕様" in m_after["success_criteria"]
    assert "セキュリティ指針" in m_after["constraints_text"]

    # 2. 事前成果物の保持
    assert prior_artifact.exists()
    assert prior_artifact.read_text(encoding="utf-8") == prior_content

    # 3. 外部アクション証拠の保持
    actions_after = manager.memory.list_actions(pid)
    assert len(actions_after) == 1
    assert actions_after[0]["status"] == "executed"
    assert "SMTP message-id" in actions_after[0]["evidence"]

    # 4. 全イベント履歴の保持（減っていないこと）
    events_after = len(m_after["events"])
    assert events_after >= events_before + 1


@pytest.mark.asyncio
async def test_recovery_does_not_carry_legacy_reviews_as_current_issues(tmp_path):
    """5. 旧レビュー(mission.plan_reviews)が現行の指摘(issues_for)として自動復活しないこと"""
    manager, pid = _create_duplicated_v22_project(tmp_path)
    manager.memory.set_plan_reviews(pid, [
        {"id": "legacy_provider", "ok": True, "review": "Ver.22以前の旧指摘内容です。"},
    ])

    # 移行前
    _, v22_sig = plan_snapshot(manager, pid)
    assert issues_for(manager, pid, v22_sig) == []

    # 移行後
    apply_recovery(manager, pid, reviewer="safety_lead")
    _, v23_sig = plan_snapshot(manager, pid)
    assert issues_for(manager, pid, v23_sig) == []

    # 監査履歴としての mission.plan_reviews は保持されていること
    m_v23 = manager.memory.get_mission(pid)
    assert len(m_v23["plan_reviews"]) == 1
    assert m_v23["plan_reviews"][0]["id"] == "legacy_provider"


@pytest.mark.asyncio
async def test_recovery_preview_and_apply_web_api(tmp_path):
    """6. Web API経由のプレビュー・適用エンドポイントのテスト"""
    import app.web as web_module
    manager, pid = _create_duplicated_v22_project(tmp_path)
    web_module.memory = manager.memory
    web_module.orchestrator = manager
    web_module.workspace = manager.workspace

    client = TestClient(web_module.app)

    # 1. Preview API (GET)
    resp = client.get(f"/api/projects/{pid}/plan/recovery/preview")
    assert resp.status_code == 200
    data = resp.json()
    assert data["saved"] is False
    assert data["current_version"] == 22
    assert data["candidate_version"] == 23
    assert "SC01_DUP" not in [t["task_key"] for t in data["candidate_tasks"]]
    assert "task_diff" in data
    assert "coverage" in data

    # 2. Preview API (POST)
    resp_post = client.post(f"/api/projects/{pid}/plan/recovery/preview")
    assert resp_post.status_code == 200
    assert resp_post.json()["candidate_version"] == 23

    # 3. Apply API without reviewer -> 422
    resp_err = client.post(f"/api/projects/{pid}/plan/recovery/apply", json={"reviewer": ""})
    assert resp_err.status_code == 422

    # 4. Apply API with reviewer -> 200
    resp_apply = client.post(
        f"/api/projects/{pid}/plan/recovery/apply",
        json={"reviewer": "system_admin", "note": "移行承認", "expected_version": 22},
    )
    assert resp_apply.status_code == 200
    applied_data = resp_apply.json()
    assert applied_data["status"] == "applied"
    assert applied_data["new_version"] == 23

    # 5. Mission check
    mission_resp = client.get(f"/api/projects/{pid}/mission")
    assert mission_resp.status_code == 200
    assert mission_resp.json()["plan_version"] == 23

@pytest.mark.asyncio
async def test_recovery_replaces_review_task_content_with_goal_execution(tmp_path):
    """Criterion tasks polluted by review prose become goal execution tasks."""
    manager, pid = _create_duplicated_v22_project(tmp_path)
    mission = manager.memory.get_mission(pid)
    sc01 = next(task for task in mission["tasks"] if task["task_key"] == "SC01")
    contract = contract_of(sc01)
    contract["outputs"][0]["required_headings"] = [
        "1. 目標との整合性", "2. タスクの不足・重複", "3. 依存関係",
        "4. 並列化可能性", "5. 実行可能性", "6. リスク評価",
    ]
    with manager.memory._connect() as db:
        db.execute(
            "UPDATE project_tasks SET title=?, acceptance_criteria=? WHERE project_id=? AND task_key='SC01'",
            ("計画草案の評価と改善提案", json.dumps(contract, ensure_ascii=False), pid),
        )

    preview = preview_recovery(manager, pid)
    recovered = next(task for task in preview["candidate_tasks"] if task["task_key"] == "SC01")
    assert recovered["title"] == "業務ナレッジ共有システムの構成案を作成する"
    assert "評価と改善提案" not in recovered["title"]
    headings = contract_of(recovered)["outputs"][0]["required_headings"]
    assert "1. 目標との整合性" not in headings
    assert "具体的な実施内容" in headings
