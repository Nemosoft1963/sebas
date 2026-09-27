"""Map every GoalContract criterion to an execute task and a verify task."""
from __future__ import annotations

from app.goal_completion_store import GoalCompletionStore
from app.structured_planning import contract_of


def _tasks_by_key(mission: dict) -> dict:
    return {task.get("task_key"): task for task in mission.get("tasks") or [] if task.get("task_key")}


def _kind(task: dict) -> str:
    return str((contract_of(task) or {}).get("execution_kind") or "")


def _owns_criterion(task: dict, cid: str) -> bool:
    owned = (contract_of(task) or {}).get("criterion_ids") or []
    checks = (contract_of(task) or {}).get("check_ids") or []
    return cid in owned or cid in checks


def build(mission: dict, contract: dict) -> dict:
    by_key = _tasks_by_key(mission)
    kinds = {_kind(task): key for key, task in by_key.items()}
    rows = []
    issues = []
    for item in contract.get("criteria") or []:
        cid = item.get("criterion_id")
        exec_keys = [key for key in item.get("exec_task_keys") or [] if key in by_key]
        # Verify is valid only when this criterion names that task. Do not auto-assign
        # an arbitrary final_verification / vehicle_verify task.
        verify_keys = [key for key in item.get("verify_task_keys") or [] if key in by_key]
        if not exec_keys:
            for key, task in by_key.items():
                if key in verify_keys:
                    continue
                if (contract_of(task) or {}).get("final_verification") or _kind(task) == "vehicle_verify":
                    continue
                if _owns_criterion(task, cid):
                    exec_keys.append(key)
        if not exec_keys and kinds:
            preferred = item.get("exec_task_keys") or []
            for name in preferred:
                if name in kinds:
                    exec_keys.append(kinds[name])
                elif name in by_key:
                    exec_keys.append(name)
        exec_keys = list(dict.fromkeys(exec_keys))
        verify_keys = list(dict.fromkeys(verify_keys))
        status = "planned"
        if not exec_keys or not verify_keys:
            status = "uncovered"
            issues.append(f"{cid}: uncovered exec={exec_keys} verify={verify_keys}")
        elif set(exec_keys) == set(verify_keys):
            # A task cannot close both exec and verify. The previous empty `pass`
            # branch allowed this for final_verification; that hid coverage holes.
            status = "uncovered"
            issues.append(f"{cid}: exec and verify are the same task(s) {exec_keys}")
        artifacts = []
        for key in exec_keys + verify_keys:
            for output in (contract_of(by_key[key]) or {}).get("outputs") or []:
                path = output.get("path")
                if path:
                    artifacts.append(path)
        rows.append({
            "criterion_id": cid,
            "statement": item.get("statement") or "",
            "exec_task_key": exec_keys[0] if exec_keys else "",
            "verify_task_key": verify_keys[0] if verify_keys else "",
            "exec_task_keys": exec_keys,
            "verify_task_keys": verify_keys,
            "artifact_paths": list(dict.fromkeys(artifacts)),
            "status": status,
        })
    expected = {item.get("criterion_id") for item in contract.get("criteria") or []}
    present = {row["criterion_id"] for row in rows}
    if expected - present:
        issues.append("missing rows: " + ",".join(sorted(expected - present)))
    passed = not issues and all(row["status"] != "uncovered" for row in rows) and bool(rows)
    return {
        "schema": "sebas-plan-coverage/v1",
        "contract_hash": contract.get("content_hash") or "",
        "plan_version": mission.get("plan_version") or 0,
        "rows": rows,
        "issues": issues,
        "passed": passed,
    }


def save(manager, project_id: str, coverage: dict) -> dict:
    mission = manager.memory.get_mission(project_id)
    GoalCompletionStore(manager.memory.path).put_coverage(
        project_id,
        mission.get("plan_version") or 0,
        coverage.get("contract_hash") or "",
        coverage,
    )
    return coverage


def ensure_approvable(manager, project_id: str, contract: dict) -> dict:
    mission = manager.memory.get_mission(project_id)
    coverage = build(mission, contract)
    save(manager, project_id, coverage)
    if not coverage["passed"]:
        raise ValueError("COVERAGE_INCOMPLETE: " + "; ".join(coverage["issues"] or ["被覆表が不完全です"]))
    return coverage


def get(manager, project_id: str) -> dict | None:
    mission = manager.memory.get_mission(project_id)
    stored = GoalCompletionStore(manager.memory.path).get_coverage(project_id, mission.get("plan_version"))
    if stored:
        return stored
    from app.goal_contract import preview

    return build(mission, preview(manager, project_id))
