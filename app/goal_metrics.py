"""Read-only baseline measurements for goal-completion targets."""
from __future__ import annotations

from app.completion_gate import evaluate
from app.goal_completion_store import GoalCompletionStore
from app.vehicle_workflow import applicable, load_input
from app.workflow_readiness import _vehicle_view


def collect_baseline(manager, project_id: str) -> dict:
    """Collect observable baseline values without persisting an evaluation."""
    mission = manager.memory.get_mission(project_id)
    vehicle = _vehicle_view(manager, project_id, mission) if applicable(mission) else None
    gate = evaluate(manager, project_id, persist=False)
    counts = {"PASS": 0, "FAIL": 0, "UNTESTABLE": 0}
    criteria = gate.get("criteria")
    if criteria is not None:
        for row in criteria:
            status = str(row.get("status") or "UNTESTABLE")
            if status == "PASS":
                counts["PASS"] += 1
            elif status == "FAIL":
                counts["FAIL"] += 1
            else:
                counts["UNTESTABLE"] += 1
    acceptance = GoalCompletionStore(manager.memory.path).latest_unrevoked_acceptance(project_id)
    plan_version = mission.get("plan_version")
    return {
        "project_id": project_id,
        "plan_versions": int(plan_version) if plan_version is not None else None,
        "unresolved_allocations": vehicle["unresolved_allocations"] if vehicle is not None else None,
        "unresolved_reads": vehicle["unresolved_reads"] if vehicle is not None else None,
        "gate": {
            "achieved": bool(gate.get("achieved")),
            "criteria_counts": counts if criteria is not None else None,
        },
        "stop_count": None,
        "provisional_artifact": bool(vehicle["xlsx"] or vehicle["needs_review"]) if vehicle is not None else None,
        "human_acceptance": bool(acceptance) if acceptance is not None else False,
    }
