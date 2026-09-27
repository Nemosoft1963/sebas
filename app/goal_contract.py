"""GoalContract: structured parent of plan, execution, and completion."""
from __future__ import annotations

import hashlib
import json
import uuid

from app.goal_completion_store import GoalCompletionStore
from app.structured_planning import extract_criteria


SCHEMA = "sebas-goal-contract/v1"
COLLAPSE_GOAL_CHARS = 400


def content_hash(payload: dict) -> str:
    material = {
        "objective": payload.get("objective"),
        "scope": payload.get("scope"),
        "exclusions": payload.get("exclusions"),
        "constraints": payload.get("constraints"),
        "criteria": [
            {
                "criterion_id": item.get("criterion_id"),
                "statement": item.get("statement"),
                "type": item.get("type"),
                "test_method": item.get("test_method"),
            }
            for item in payload.get("criteria") or []
        ],
        "source_versions": payload.get("source_versions"),
    }
    raw = json.dumps(material, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def store_of(manager) -> GoalCompletionStore:
    return GoalCompletionStore(manager.memory.path)


def get_active(manager, project_id: str) -> dict | None:
    return store_of(manager).active_contract(project_id)


def get_latest(manager, project_id: str) -> dict | None:
    return store_of(manager).latest_contract(project_id)


def _vehicle_source_versions(manager, project_id: str, mission: dict) -> dict:
    """Return version material only for vehicle contracts; failures stay unapproved."""
    from app.vehicle_workflow import applicable, digest, load_input, sources

    if not applicable(mission):
        return {}
    try:
        source_hash = digest(sources(manager, project_id))
    except Exception:
        source_hash = ""
    try:
        envelope = load_input(manager, project_id)
        input_hash = digest(envelope) if envelope else ""
    except Exception:
        input_hash = ""
    adopted = []
    try:
        from app.ocr_store import OcrStore
        from app.ocr_review import manifest_hash

        store = OcrStore(manager.memory.path)
        with store.connect() as db:
            db.row_factory = __import__("sqlite3").Row
            rows = db.execute(
                """SELECT * FROM ocr_runs WHERE project_id=?
                   AND adoption_status IN ('approved','published','adopted') ORDER BY run_id""",
                (project_id,),
            ).fetchall()
        for row in rows:
            item = dict(row)
            root = item.get("artifact_root") or item.get("output_dir") or ""
            try:
                mh = manifest_hash(root) if root else (item.get("manifest_hash") or "")
            except Exception:
                mh = item.get("manifest_hash") or ""
            adopted.append({"run_id": item.get("run_id") or "", "manifest_hash": mh})
    except Exception:
        adopted = []
    return {"source_hash": source_hash, "input_hash": input_hash, "ocr_adopted_versions": adopted}


def from_mission(mission: dict, manager=None, project_id: str = "") -> dict:
    from app.requirement_retention import inspect as inspect_retention
    from app.vehicle_workflow import applicable, requested_months

    goal = str(mission.get("goal") or "")
    success = str(mission.get("success_criteria") or "")
    constraints = str(mission.get("constraints_text") or "")
    extracted = extract_criteria(goal, success)
    criteria = []
    template_id = ""
    if applicable(mission):
        from app.goal_templates.vehicle_monthly_pnl import CRITERIA, TEMPLATE_ID

        template_id = TEMPLATE_ID
        for item in CRITERIA:
            criteria.append({
                "criterion_id": item["criterion_id"],
                "statement": item["statement"],
                "type": item["type"],
                "test_method": item["test_method"],
                "required_evidence": list(item.get("required_evidence") or []),
                "human_decision_required": bool(item.get("human_decision_required")),
                "weight": float(item.get("weight") or 1),
                "source_spans": _spans(goal + "\n" + success, item.get("topics") or []),
                "exec_task_keys": list(item.get("exec_task_keys") or []),
                "verify_task_keys": list(item.get("verify_task_keys") or []),
            })
    else:
        for index, statement in enumerate(extracted, 1):
            criteria.append({
                "criterion_id": f"SC{index:02d}",
                "statement": statement,
                "type": "factual",
                "test_method": "artifact_headings",
                "required_evidence": ["result_markdown"],
                "human_decision_required": False,
                "weight": 1.0,
                "source_spans": [{"field": "success_criteria", "excerpt": statement[:180]}],
                "exec_task_keys": [f"SC{index:02d}"],
                "verify_task_keys": ["final_verification"],
            })
    payload = {
        "schema": SCHEMA,
        "goal_id": "gc_" + uuid.uuid4().hex[:12],
        "version": int(mission.get("plan_version") or 0) + 1,
        "objective": goal.strip() or extracted[0] if extracted else "",
        "scope": {
            "period": requested_months(mission) if applicable(mission) else [],
            "template": template_id,
        },
        "exclusions": [],
        "constraints": [line.strip() for line in constraints.splitlines() if line.strip()],
        "completion_policy": {
            "require_independent_reconciliation": bool(applicable(mission)),
            "require_human_acceptance": True,
            "provisional_is_not_achieved": True,
        },
        "source_versions": {
            "mission_plan_version": mission.get("plan_version") or 0,
            "vehicle_engine_revision": "vehicle-auto-v1" if applicable(mission) else None,
        },
        "extracted_criteria": extracted,
        "criteria": criteria,
    }
    if applicable(mission) and manager is not None:
        payload["source_versions"].update(_vehicle_source_versions(manager, project_id, mission))
    payload["retention"] = inspect_retention(mission, payload)
    payload["content_hash"] = content_hash(payload)
    return payload


def _spans(text: str, topics: list[str]) -> list[dict]:
    spans = []
    for topic in topics:
        pos = text.find(topic)
        if pos >= 0:
            start = max(0, pos - 20)
            spans.append({"field": "goal_or_success", "excerpt": text[start:start + 80], "topic": topic})
    return spans


def put_draft(manager, project_id: str, payload: dict | None = None) -> dict:
    mission = manager.memory.get_mission(project_id)
    body = payload or from_mission(mission, manager, project_id)
    if not body.get("version"):
        body["version"] = store_of(manager).next_version(project_id)
    body["content_hash"] = content_hash(body)
    return store_of(manager).put_contract(
        project_id,
        body,
        "draft",
        sources={
            "goal": mission.get("goal") or "",
            "success": mission.get("success_criteria") or "",
            "constraints": mission.get("constraints_text") or "",
        },
    )


def activate(manager, project_id: str, payload: dict | None = None) -> dict:
    mission = manager.memory.get_mission(project_id)
    body = payload or get_latest(manager, project_id) or from_mission(mission, manager, project_id)
    if body.get("_status") != "active" or payload is not None:
        if not body.get("version"):
            body["version"] = store_of(manager).next_version(project_id)
        body["content_hash"] = content_hash(body)
    return store_of(manager).put_contract(
        project_id,
        {k: v for k, v in body.items() if not str(k).startswith("_")},
        "active",
        sources={
            "goal": mission.get("goal") or "",
            "success": mission.get("success_criteria") or "",
            "constraints": mission.get("constraints_text") or "",
        },
    )


def preview(manager, project_id: str) -> dict:
    active = get_active(manager, project_id)
    if active:
        return active
    latest = get_latest(manager, project_id)
    if latest:
        return latest
    return from_mission(manager.memory.get_mission(project_id), manager, project_id)
