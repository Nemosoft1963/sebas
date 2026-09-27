"""Converge orphaned goal-completion work after a process restart."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from app.goal_completion_flag import enabled
from app.goal_review import ORCHESTRATION_STAGES, RUNNING_JOB, ReviewStore, finish_job


LOGGER = logging.getLogger(__name__)
ORPHAN_ERROR = "RESTART_ORPHANED: 再起動により実行が中断されました"


def _final_stage(job: dict) -> str:
    explicit = job.get("final_stage")
    if explicit:
        return str(explicit)
    return {
        "review_plan": "external_review",
        "propose": "local_proposal",
        "apply": "apply",
    }.get(str(job.get("kind") or ""), "")


def _artifact_exists(memory, pid: str, item) -> bool:
    path = ""
    if isinstance(item, str):
        path = item
    elif isinstance(item, dict):
        path = str(item.get("path") or item.get("result_path") or "")
    if not path:
        return False
    target = Path(path)
    if not target.is_absolute():
        target = Path(memory.path).parent / target
    return target.is_file()


def _job_proven_complete(store: ReviewStore, memory, pid: str, job: dict) -> bool:
    """Require both the terminal checkpoint and a corresponding persisted result."""
    final = _final_stage(job)
    if not final or job.get("last_completed_stage") != final:
        return False
    artifacts = job.get("artifacts") or job.get("result_files") or []
    if artifacts:
        return all(_artifact_exists(memory, pid, item) for item in artifacts)
    signature = str(job.get("plan_signature") or "")
    if job.get("kind") == "review_plan" and signature:
        row = store.get(pid, "plan", signature) or {}
        return row.get("job_id") == job.get("id") and row.get("status") in {
            "passed", "not_passed", "awaiting_external", "waiting_budget",
        }
    result_kind = str(job.get("result_kind") or "")
    result_signature = str(job.get("result_signature") or "")
    if result_kind and result_signature:
        row = store.get(pid, result_kind, result_signature) or {}
        return bool(row and row.get("status") not in RUNNING_JOB)
    return False


def _empty_summary() -> dict:
    return {
        "projects_changed": 0, "jobs_succeeded": 0,
        "jobs_needs_attention": 0, "plans_unverified": 0,
        "missions_paused": 0, "changed": 0, "items": [], "errors": [],
    }


def reconcile_after_restart(memory, orchestrator=None) -> dict:
    """Converge flag-enabled projects; errors are isolated per project."""
    summary = _empty_summary()
    store = ReviewStore(memory.path)
    for project in memory.list_projects():
        pid = project["id"]
        if not enabled(memory.path, pid):
            continue
        items = []
        try:
            for job_id, job in store.list(pid, "job"):
                if job.get("status") not in RUNNING_JOB:
                    continue
                if _job_proven_complete(store, memory, pid, job):
                    finish_job(store, pid, job_id, "succeeded",
                               last_completed_stage=job.get("last_completed_stage") or "")
                    summary["jobs_succeeded"] += 1
                    items.append({"kind": "job", "id": job_id, "to": "succeeded",
                                  "reason": "terminal_checkpoint_and_result_verified"})
                else:
                    finish_job(store, pid, job_id, "needs_attention",
                               blocking_error=ORPHAN_ERROR,
                               resume_from=job.get("resume_from") or "")
                    summary["jobs_needs_attention"] += 1
                    items.append({"kind": "job", "id": job_id, "to": "needs_attention",
                                  "reason": "restart_orphaned_unproven"})
            for signature, plan in store.list(pid, "plan"):
                if plan.get("status") != "running":
                    continue
                row = dict(plan)
                row["status"] = "unverified"
                row["error"] = ORPHAN_ERROR
                row["updated_at"] = time.time()
                store.put(pid, "plan", signature, row)
                summary["plans_unverified"] += 1
                items.append({"kind": "plan", "id": signature, "to": "unverified",
                              "reason": "restart_requires_revalidation"})
            mission = memory.get_mission(pid)
            if mission.get("status") == "running":
                memory.set_mission_status(
                    pid, "paused", "再起動により実行中ワーカーが失われたため安全に一時停止しました",
                    "restart_reconciled",
                )
                summary["missions_paused"] += 1
                items.append({"kind": "mission", "id": pid, "to": "paused",
                              "reason": "restart_no_active_worker"})
            if items:
                summary["projects_changed"] += 1
                summary["changed"] += len(items)
                summary["items"].extend(dict(item, project_id=pid) for item in items)
                memory.add_event(
                    pid, "restart_convergence", f"再起動時の孤児状態を{len(items)}件収束しました",
                    detail=json.dumps({"items": items}, ensure_ascii=False),
                )
        except Exception as exc:
            LOGGER.warning("restart convergence failed for project %s: %s", pid, exc,
                           exc_info=True)
            summary["errors"].append({"project_id": pid, "error": type(exc).__name__})
            continue
    return summary
