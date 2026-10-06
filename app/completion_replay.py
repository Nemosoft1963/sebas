"""Conservative, read-only preview of the current completion path."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from app.goal_review import plan_snapshot
from app.safe_auto_resume import enabled as auto_enabled, records as auto_records

STAGES = (
    "plan_repair", "external_validation", "plan_approval", "local_execution",
    "local_evidence", "external_operation", "final_decision",
)


def _stage(stage_id: str, ready: bool, reason: str, human: bool, **extra) -> dict:
    return {"id": stage_id, "reachable": bool(ready), "human_required": human,
            "stop_reason": "" if ready else reason, **extra}


def _unavailable(project_id: str, signature: str, reason: str) -> dict:
    return {
        "project_id": project_id, "plan_signature": signature,
        "read_only": True, "crossed_approval_boundary": False,
        "stages": [_stage(name, False, reason, name in {
            "plan_repair", "plan_approval", "external_operation", "final_decision"})
                   for name in STAGES],
        "completion_gate": {"achieved": False, "reason_code": "EVIDENCE_UNAVAILABLE"},
        "pending_summary": {}, "would_achieve": False, "repair_blocking": True,
        "notice": reason,
    }


def _existing_stores(manager) -> bool:
    source = Path(manager.memory.path)
    parent = source.parent
    return (parent / "goal_reviews.sqlite3").is_file() and (parent / "goal_completion.sqlite3").is_file()


def _verified_local_runs(manager, project_id: str, mission: dict) -> list[dict]:
    from app.recovery_record import _verify_run_artifact

    keys = {str(t.get("task_key") or t.get("id") or "") for t in mission.get("tasks", [])}
    verified = []
    for run in auto_records(manager, project_id):
        if (run.get("status") != "completed" or run.get("task_key") not in keys
                or not run.get("artifact_hash") or not run.get("evidence")):
            continue
        try:
            _verify_run_artifact(manager, project_id, run)
        except (OSError, ValueError, KeyError, AttributeError):
            continue
        verified.append({"run_id": run.get("run_id"), "task_key": run.get("task_key")})
    return verified


def _external_criteria(manager, project_id: str, mission: dict, gate: dict) -> tuple[list[str], bool]:
    from app.goal_contract import get_active
    from app.generic_goal_checks import _needs_external

    contract = get_active(manager, project_id) or {}
    tasks = {str(t.get("task_key") or t.get("id") or ""): t for t in mission.get("tasks", [])}
    required = []
    for criterion in contract.get("criteria") or []:
        related = [tasks[k] for k in criterion.get("exec_task_keys") or [] if k in tasks]
        if _needs_external(criterion, related):
            required.append(str(criterion.get("criterion_id") or ""))
    status = {str(item.get("criterion_id") or ""): item.get("status") for item in gate.get("criteria") or []}
    return required, bool(required) and all(status.get(cid) == "PASS" for cid in required)


def preview(manager, project_id: str) -> dict:
    """Inspect existing evidence. Never approve, execute, register RAG, or create sidecar DBs."""
    try:
        mission = manager.memory.get_mission(project_id)
        _snapshot, signature = plan_snapshot(manager, project_id)
    except Exception as exc:
        return _unavailable(project_id, "", "計画の確認に失敗しました: " + type(exc).__name__)
    if not _existing_stores(manager):
        return _unavailable(project_id, signature, "検証台帳が未作成です。読取時に新規作成しません")

    from app.completion_gate import evaluate
    from app.goal_review import ReviewStore
    from app.local_patch_preview import preview as patch_preview
    from app.pending_ledger import build as pending_build

    try:
        plan = ReviewStore(manager.memory.path).get(project_id, "plan", signature) or {}
        patches = patch_preview(manager, project_id)
        ledger = pending_build(manager, project_id)
        gate = evaluate(manager, project_id, persist=False)
        summary = ledger.get("summary") or {}
        repair_blocking = bool(summary.get("repair_blocking"))
        repair_reason = str(summary.get("repair_reason") or "修復ループ未完了です")
        plan_status = str(summary.get("plan_status") or "unknown")
        no_issues = summary.get("issue_count") == 0 and summary.get("unresolved_count") == 0
        verified = plan.get("status") == "passed" and no_issues
        approved = verified and plan_status == "approved" and not repair_blocking
        safe_patch = any(x.get("classification") == "safe_plan_patch" for x in patches.get("candidates") or [])
        server_auto = os.environ.get("LOCALSAPORTER_AUTO_RESUME_AVAILABLE") == "1"
        project_auto = auto_enabled(manager, project_id)
        local_ready = approved and server_auto and project_auto and mission.get("status") in {"ready", "paused"}
        local_runs = _verified_local_runs(manager, project_id, mission)
        external_ids, external_pass = _external_criteria(manager, project_id, mission, gate)
        final_pass = bool(gate.get("achieved")) and approved
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, RuntimeError) as exc:
        return _unavailable(project_id, signature, "証拠の確認に失敗しました: " + type(exc).__name__)

    stages = [
        _stage("plan_repair", safe_patch, "安全な局所修復候補はありません。未解決指摘は人間が確認します", True,
               execution_start_allowed=False),
        _stage("external_validation", verified, "同一署名の外部検証または指摘解消が未完了です", False),
        _stage("plan_approval", approved, repair_reason if repair_blocking else "現行計画の人間承認が必要です", True),
        _stage("local_execution", local_ready, repair_reason if repair_blocking else "P2のサーバー・案件設定と計画承認が必要です", False,
               execution_guaranteed=False),
        _stage("local_evidence", bool(local_runs), "現行工程に結び付く実成果物の検証済みP2記録がありません", False,
               verified_runs=local_runs),
        _stage("external_operation", external_pass, "必要な外部操作の達成基準が未合格です", True,
               required_criteria=external_ids),
        _stage("final_decision", final_pass, gate.get("reason_code") or "目標達成または計画承認が未完了です", True),
    ]
    return {
        "project_id": project_id, "plan_signature": signature,
        "read_only": True, "crossed_approval_boundary": False,
        "stages": stages, "completion_gate": gate,
        "pending_summary": summary, "would_achieve": final_pass, "repair_blocking": repair_blocking,
    }
