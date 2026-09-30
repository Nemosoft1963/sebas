"""Safe recovery and migration for existing projects with legacy/duplicated plans."""
from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from app.experience_store import canonical, fingerprint
from app.goal_completion_store import GoalCompletionStore
from app.goal_contract import preview as get_goal_contract, from_mission
from app.goal_review import ReviewStore, plan_snapshot
from app.plan_coverage import build as build_coverage, save as save_coverage
from app.structured_planning import (
    contract_of,
    extract_criteria,
    compile_plan,
    compile_task,
    validate_rebuild_generic_candidate,
)
from app.vehicle_workflow import applicable as vehicle_applicable


_REVIEW_TASK_RE = re.compile(r"計画.*(?:評価|レビュー)|評価.*改善|草案.*評価", re.I)
_REVIEW_HEADING_MARKERS = {
    "目標との整合性", "タスクの不足・重複", "依存関係", "並列化可能性",
    "実行可能性", "リスク評価", "検証設計", "完了判定基準",
}


def _is_legacy_review_task(task: dict, contract: dict) -> bool:
    """Identify review prose that must never become an execution task."""
    if _REVIEW_TASK_RE.search(str(task.get("title") or "")):
        return True
    headings = {
        str(heading).strip().lstrip("0123456789. ")
        for output in contract.get("outputs", [])
        for heading in output.get("required_headings", [])
    }
    return len(headings & _REVIEW_HEADING_MARKERS) >= 3


def _get_workspace_file_info(manager, project_id: str, path: str) -> dict[str, Any] | None:
    """Check if an artifact exists in the project workspace and return metadata."""
    if not hasattr(manager, "workspace") or not manager.workspace:
        return None
    project = manager.memory.get_project(project_id)
    workspace_path = (project or {}).get("workspace_path", "")
    try:
        _, _, file_path = manager.workspace.resolve_file(
            workspace_path, project_id, path, must_exist=True
        )
        if file_path.is_file():
            data = file_path.read_bytes()
            return {
                "path": path,
                "exists": True,
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
    except (FileNotFoundError, ValueError, OSError):
        pass
    return None


def _find_reusable_artifacts(manager, project_id: str, candidate_tasks: list[dict], existing_tasks: list[dict]) -> list[dict]:
    """Identify existing artifact files that can be reused in the candidate plan."""
    reusable = []
    seen_paths = set()

    # Collect all outputs from existing and candidate tasks
    all_target_paths = set()
    for task in candidate_tasks:
        contract = contract_of(task) or {}
        for output in contract.get("outputs", []):
            if output.get("path"):
                all_target_paths.add(output["path"])

    for task in existing_tasks:
        contract = contract_of(task) or {}
        for output in contract.get("outputs", []):
            if output.get("path"):
                all_target_paths.add(output["path"])

    for path in sorted(all_target_paths):
        if path in seen_paths:
            continue
        info = _get_workspace_file_info(manager, project_id, path)
        if info:
            seen_paths.add(path)
            reusable.append({
                "path": path,
                "size_bytes": info["size_bytes"],
                "sha256": info["sha256"],
                "status": "available",
            })

    return reusable


def _find_invalidated_approvals(manager, project_id: str, old_signature: str, new_signature: str) -> list[dict]:
    """List approvals, reviews, and acceptances invalidated by plan migration."""
    invalidated = []
    store = ReviewStore(manager.memory.path)

    # Check ReviewStore plan reviews & send approvals
    plan_review = store.get(project_id, "plan", old_signature)
    if plan_review:
        invalidated.append({
            "type": "plan_review",
            "signature": old_signature,
            "status": plan_review.get("status"),
            "reason": "計画署名の変更により旧版のレビュー結果は無効化（監査履歴として保存）",
        })

    send_approval = store.get(project_id, "send_approval", old_signature)
    if send_approval:
        invalidated.append({
            "type": "send_approval",
            "signature": old_signature,
            "approved_at": send_approval.get("created_at") or send_approval.get("timestamp"),
            "reason": "計画署名の変更により旧版の外部送信承認は無効化",
        })

    # Check GoalCompletionStore human acceptances
    gc_store = GoalCompletionStore(manager.memory.path)
    with gc_store.connect() as db:
        db.row_factory = __import__("sqlite3").Row
        rows = db.execute(
            "SELECT * FROM human_acceptances WHERE project_id=? AND revoked_at IS NULL",
            (project_id,),
        ).fetchall()
        for row in rows:
            item = dict(row)
            if item.get("plan_signature") != new_signature:
                invalidated.append({
                    "type": "human_acceptance",
                    "acceptance_id": item.get("id"),
                    "plan_signature": item.get("plan_signature"),
                    "accepted_by": item.get("accepted_by"),
                    "accepted_at": item.get("accepted_at"),
                    "reason": "旧計画署名または旧成果物ハッシュに対する合格承認のため新版では再受入が必要",
                })

    return invalidated


def _compute_task_diff(old_tasks: list[dict], new_tasks: list[dict]) -> dict[str, list]:
    """Compute added, removed, modified, and unchanged tasks between old and new plans."""
    old_by_key: dict[str, list[dict]] = {}
    for task in old_tasks:
        key = task.get("task_key") or ""
        old_by_key.setdefault(key, []).append(task)

    new_by_key: dict[str, dict] = {}
    for task in new_tasks:
        key = task.get("task_key") or ""
        new_by_key[key] = task

    added = []
    removed = []
    modified = []
    unchanged = []

    # Check old tasks vs new tasks
    processed_new_keys = set()
    for key, task_list in old_by_key.items():
        if key not in new_by_key:
            for t in task_list:
                removed.append({
                    "task_key": key,
                    "title": t.get("title"),
                    "reason": "新計画で不要または重複のため削除",
                })
        else:
            # Key exists in new plan
            new_task = new_by_key[key]
            processed_new_keys.add(key)
            primary_old = task_list[0]
            # Any extra duplicate tasks in old plan are removed
            if len(task_list) > 1:
                for extra in task_list[1:]:
                    removed.append({
                        "task_key": key,
                        "title": extra.get("title"),
                        "reason": "重複タスクの解消",
                    })

            # Compare primary_old and new_task
            old_contract = contract_of(primary_old) or {}
            new_contract = contract_of(new_task) or {}
            is_same = (
                primary_old.get("title") == new_task.get("title")
                and primary_old.get("description") == new_task.get("description")
                and primary_old.get("mode") == new_task.get("mode")
                and primary_old.get("depends_on") == new_task.get("depends_on")
                and old_contract == new_contract
            )
            if is_same:
                unchanged.append({
                    "task_key": key,
                    "title": new_task.get("title"),
                })
            else:
                modified.append({
                    "task_key": key,
                    "title": new_task.get("title"),
                    "before": {
                        "title": primary_old.get("title"),
                        "description": primary_old.get("description"),
                        "depends_on": primary_old.get("depends_on"),
                    },
                    "after": {
                        "title": new_task.get("title"),
                        "description": new_task.get("description"),
                        "depends_on": new_task.get("depends_on"),
                    },
                })

    for key, new_task in new_by_key.items():
        if key not in processed_new_keys:
            added.append({
                "task_key": key,
                "title": new_task.get("title"),
                "description": new_task.get("description"),
                "depends_on": new_task.get("depends_on"),
            })

    return {
        "added": added,
        "removed": removed,
        "modified": modified,
        "unchanged": unchanged,
    }


def preview_recovery(manager, project_id: str) -> dict[str, Any]:
    """
    Generate a preview of the candidate recovered plan (Ver.N+1) without modifying the database.
    Read-only operation.
    """
    mission = manager.memory.get_mission(project_id)
    if not mission or not mission.get("goal"):
        raise ValueError("プロジェクト目標が設定されていません")

    is_vehicle = vehicle_applicable(mission)
    goal_contract = get_goal_contract(manager, project_id)
    if not goal_contract:
        goal_contract = from_mission(mission, manager, project_id)

    # Extract clean criteria from GoalContract or mission
    if goal_contract.get("criteria"):
        criteria = [c["statement"] for c in goal_contract["criteria"] if c.get("statement")]
    else:
        criteria = extract_criteria(mission.get("goal", ""), mission.get("success_criteria", ""))
    if not criteria:
        criteria = [mission.get("goal") or "プロジェクト目標の達成"]

    # Gather source IDs
    files = manager.memory.list_context_files(project_id)
    source_ids = [f["id"] for f in files if f.get("source") != "memo"]

    # Existing tasks
    old_tasks = mission.get("tasks", [])
    old_by_key = {}
    for t in old_tasks:
        k = t.get("task_key")
        if k and k not in old_by_key:
            old_by_key[k] = t

    # Build clean candidate tasks using P0-1~P0-6 compiled planner structure
    candidate_tasks_uncompiled = []
    for index, criterion in enumerate(criteria, 1):
        key = f"SC{index:02d}"
        old_task = old_by_key.get(key)
        if old_task:
            old_contract = contract_of(old_task) or {}
            legacy_review = _is_legacy_review_task(old_task, old_contract)
            title = (
                str(criterion).strip()[:120]
                if legacy_review
                else (old_task.get("title") or f"達成条件 SC{index:02d} の実行")
            )
            scope = f"達成条件 {key}: {criterion} を満たす成果物を作成・検証する。"
            headings = []
            if not legacy_review:
                for out in old_contract.get("outputs", []):
                    if out.get("required_headings"):
                        headings = [h for h in out["required_headings"] if h not in {"根拠と未確認事項", "実施状態と次の行動"}]
                        break
            if not headings or len(headings) < 2:
                headings = ["現状と前提確認", "具体的な実施内容", "成果と検証結果"]
            proposal = {
                "title": title,
                "scope": scope[:1500],
                "headings": headings[:6],
                "depends_on": [d for d in old_task.get("depends_on", []) if d != key and d.startswith("SC")],
            }
        else:
            scope = f"達成条件 {key}: {criterion} を満たす成果物を作成・検証する。"
            headings = ["現状と前提確認", "具体的設計・作成手順"]
            proposal = {
                "title": f"達成条件 SC{index:02d} の実行設計",
                "scope": scope[:1500],
                "headings": headings,
                "depends_on": [f"SC{i:02d}" for i in range(1, index)] if index > 1 else [],
            }
        task = compile_task(index, criterion, proposal, source_ids)
        candidate_tasks_uncompiled.append(task)

    compiled = compile_plan(criteria, candidate_tasks_uncompiled, goal=mission.get("goal", ""))
    new_tasks = compiled["tasks"]
    new_summary = (
        (mission.get("plan_summary") or "").strip()
        + "\n\n## 安全回復 (Ver.移行候補)\n重複タスクを排除し、P0-1〜P0-6基準で再構成した実行計画。"
    ).strip()

    # Validate the candidate plan
    if not is_vehicle:
        validate_rebuild_generic_candidate(
            {"tasks": new_tasks, "summary": new_summary},
            goal_contract=goal_contract,
            mission=mission,
        )

    # Compute coverage
    current_version = int(mission.get("plan_version") or 0)
    candidate_version = current_version + 1
    dummy_candidate_mission = {
        "tasks": new_tasks,
        "plan_version": candidate_version,
    }
    coverage = build_coverage(dummy_candidate_mission, goal_contract)

    # Compute task diff
    task_diff = _compute_task_diff(old_tasks, new_tasks)

    # Compute reusable artifacts
    reusable_artifacts = _find_reusable_artifacts(manager, project_id, new_tasks, old_tasks)

    # Compute invalidated approvals
    old_snapshot, old_signature = plan_snapshot(manager, project_id)
    dummy_snapshot = deepcopy(old_snapshot)
    dummy_snapshot["tasks"] = new_tasks
    new_signature = fingerprint(canonical(dummy_snapshot))
    invalidated_approvals = _find_invalidated_approvals(manager, project_id, old_signature, new_signature)

    return {
        "project_id": project_id,
        "current_version": current_version,
        "candidate_version": candidate_version,
        "candidate_summary": new_summary,
        "candidate_tasks": new_tasks,
        "coverage": coverage,
        "task_diff": task_diff,
        "reusable_artifacts": reusable_artifacts,
        "invalidated_approvals": invalidated_approvals,
        "saved": False,
    }


def apply_recovery(
    manager,
    project_id: str,
    reviewer: str,
    note: str = "",
    expected_current_version: int | None = None,
) -> dict[str, Any]:
    """
    Apply the recovered candidate plan as Ver.N+1 only when explicitly approved by a human.
    """
    actor = str(reviewer or "").strip()
    if not actor:
        raise ValueError("人間による明示的な承認者名(reviewer)が必要です")

    mission = manager.memory.get_mission(project_id)
    if not mission:
        raise ValueError("プロジェクトが見つかりません")

    current_version = int(mission.get("plan_version") or 0)
    if expected_current_version is not None and expected_current_version != current_version:
        raise ValueError(f"計画版が変更されています (現行: Ver.{current_version}, 指定: Ver.{expected_current_version})")

    # Generate preview
    preview = preview_recovery(manager, project_id)
    candidate_tasks = preview["candidate_tasks"]
    candidate_summary = preview["candidate_summary"]
    coverage = preview["coverage"]

    # Save as new version via replace_plan (increments plan_version, preserves old version history)
    updated_mission = manager.memory.replace_plan(
        project_id,
        candidate_summary,
        candidate_tasks,
        expected_version=current_version,
    )

    # Save coverage ledger
    save_coverage(manager, project_id, coverage)

    # Record event
    manager.memory.add_event(
        project_id,
        "recovery_plan_applied",
        f"安全回復処理により計画を Ver.{updated_mission['plan_version']} として更新しました (承認者: {actor})",
        detail=json.dumps({
            "from_version": current_version,
            "to_version": updated_mission["plan_version"],
            "reviewer": actor,
            "note": note,
            "task_diff": preview["task_diff"],
        }, ensure_ascii=False)[:12000],
    )

    return {
        "status": "applied",
        "project_id": project_id,
        "previous_version": current_version,
        "new_version": updated_mission["plan_version"],
        "plan_summary": updated_mission["plan_summary"],
        "task_count": len(updated_mission["tasks"]),
        "coverage_passed": coverage.get("passed", False),
        "applied_by": actor,
    }
