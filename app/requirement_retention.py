"""Detect collapsed goals and missing topics before plan generation."""
from __future__ import annotations

from app.structured_planning import extract_criteria

COLLAPSE_GOAL_CHARS = 400


def inspect(mission: dict, contract: dict | None = None) -> dict:
    goal = str(mission.get("goal") or "")
    success = str(mission.get("success_criteria") or "")
    extracted = list((contract or {}).get("extracted_criteria") or extract_criteria(goal, success))
    criteria = list((contract or {}).get("criteria") or [])
    collapsed = len(goal) >= COLLAPSE_GOAL_CHARS and len(extracted) <= 1
    missing = []
    from app.vehicle_workflow import applicable

    if applicable(mission):
        from app.goal_templates.vehicle_monthly_pnl import present_topics

        present = present_topics(goal + "\n" + success)
        covered_text = "\n".join(item.get("statement") or "" for item in criteria) + "\n" + "\n".join(extracted)
        covered_topics = present_topics(covered_text)
        for topic in sorted(present):
            if topic not in covered_topics:
                missing.append(topic)
        # Template criteria count as coverage even if extracted list is short.
        if criteria and all(str(item.get("criterion_id") or "").startswith("C") for item in criteria):
            missing = []
    elif collapsed:
        missing.append("decomposed_acceptance_criteria")

    if collapsed:
        passed = False
    else:
        passed = not missing
    return {
        "passed": bool(passed),
        "collapsed": bool(collapsed),
        "missing_topics": missing,
        "extracted_count": len(extracted),
        "criterion_count": len(criteria),
        "goal_chars": len(goal),
    }


def ensure_plannable(manager, project_id: str, contract: dict | None = None) -> dict:
    mission = manager.memory.get_mission(project_id)
    body = contract or inspect(mission)
    if isinstance(contract, dict) and "retention" in contract:
        retention = contract["retention"]
    else:
        retention = inspect(mission, contract)
    if not retention.get("passed"):
        reasons = []
        if retention.get("collapsed"):
            reasons.append("長い目標が少数の成功条件へ縮約されています")
        if retention.get("missing_topics"):
            reasons.append("未反映の必須論点: " + ",".join(retention["missing_topics"]))
        raise ValueError("RETENTION_FAILED: " + (" / ".join(reasons) or "要求保持検査に不合格です"))
    return retention
