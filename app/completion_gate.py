"""Shared completion gate. Artifacts alone never yield achieved."""
from __future__ import annotations

from app.goal_completion_store import GoalCompletionStore
from app.goal_contract import get_active, preview
from app.plan_coverage import build as build_coverage


REASON_NO_CONTRACT = "NO_CONTRACT"
REASON_RETENTION = "RETENTION_FAILED"
REASON_COVERAGE = "COVERAGE_INCOMPLETE"
REASON_HUMAN = "HUMAN_ACCEPTANCE_MISSING"
REASON_HASH_DRIFT = "HUMAN_ACCEPTANCE_HASH_DRIFT"


def _current_hashes(manager, project_id: str, contract: dict, mission: dict) -> dict:
    from app.goal_review import plan_snapshot
    from app.vehicle_workflow import applicable, digest, load_input, read_json, resolve, sources
    from app.structured_planning import contract_of

    try:
        plan_signature = plan_snapshot(manager, project_id)[1]
    except Exception:
        plan_signature = ""
    if not applicable(mission):
        return {
            "contract_hash": contract.get("content_hash") or "", "plan_signature": plan_signature,
            "input_hash": "", "source_hash": "", "artifact_hash": "",
        }
    try:
        envelope = load_input(manager, project_id)
        input_hash = digest(envelope) if envelope else ""
    except Exception:
        input_hash = ""
    try:
        source_hash = digest(sources(manager, project_id))
    except Exception:
        source_hash = ""
    artifact_hash = ""
    for task in mission.get("tasks") or []:
        task_contract = contract_of(task) or {}
        if task_contract.get("execution_kind") not in {"vehicle_calculate", "vehicle_verify"}:
            continue
        for output in task_contract.get("outputs") or []:
            path = str(output.get("path") or "")
            if not path.endswith(".json"):
                continue
            try:
                result = read_json(resolve(manager, project_id, path))
            except Exception:
                continue
            if result.get("workbook_hash"):
                artifact_hash = str(result["workbook_hash"])
    return {
        "contract_hash": contract.get("content_hash") or "", "plan_signature": plan_signature,
        "input_hash": input_hash, "source_hash": source_hash, "artifact_hash": artifact_hash,
    }


def _acceptance_view(store: GoalCompletionStore, project_id: str, hashes: dict) -> tuple[dict | None, dict]:
    current = store.latest_valid_acceptance(project_id, **hashes)
    if current:
        return current, {
            "valid": True, "accepted_by": current["accepted_by"],
            "accepted_at": current["accepted_at"], "reason": "",
        }
    previous = store.latest_unrevoked_acceptance(project_id)
    if not previous:
        reason = REASON_HUMAN
    else:
        changed = [key for key in ("contract_hash", "plan_signature", "input_hash", "source_hash", "artifact_hash")
                   if str(previous.get(key) or "") != str(hashes.get(key) or "")]
        reason = REASON_HASH_DRIFT + (":" + ",".join(changed) if changed else "")
    return None, {"valid": False, "accepted_by": "", "accepted_at": "", "reason": reason}


def _record(criterion_id, status, reason_code="", evidence_path="", message=""):
    return {
        "criterion_id": criterion_id,
        "status": status,
        "reason_code": reason_code,
        "evidence_path": evidence_path,
        "message": message,
    }


def _record_from_check(item: dict) -> dict:
    row = _record(
        item.get("criterion_id"),
        item.get("status") or "UNTESTABLE",
        item.get("reason_code") or "",
        item.get("evidence_path") or "",
        item.get("message") or "",
    )
    if item.get("check_id"):
        row["check_id"] = item["check_id"]
    if item.get("evidence_summary"):
        row["evidence_summary"] = item["evidence_summary"]
    return row


def evaluate(manager, project_id: str, *, persist: bool = False) -> dict:
    from app.vehicle_workflow import applicable, load_input

    mission = manager.memory.get_mission(project_id)
    contract = get_active(manager, project_id) or preview(manager, project_id)
    criteria = list((contract or {}).get("criteria") or [])
    if not criteria:
        result = {
            "achieved": False,
            "reason_code": REASON_NO_CONTRACT,
            "criteria": [],
            "failed_criteria": [],
            "artifact_class": "none",
            "human_accepted": False,
        }
        if persist:
            GoalCompletionStore(manager.memory.path).put_evaluation(project_id, "", "", result)
        return result

    retention = (contract or {}).get("retention") or {}
    coverage = build_coverage(mission, contract)
    rows = []
    failed = []

    if not retention.get("passed", True):
        for item in criteria:
            row = _record(item["criterion_id"], "BLOCKED", REASON_RETENTION, message="要求保持検査不合格")
            rows.append(row)
            failed.append(item["criterion_id"])
    elif not coverage.get("passed"):
        uncovered = {row["criterion_id"] for row in coverage.get("rows") or [] if row.get("status") == "uncovered"}
        for item in criteria:
            cid = item["criterion_id"]
            if cid in uncovered:
                rows.append(_record(cid, "FAIL", REASON_COVERAGE, message="計画被覆なし"))
                failed.append(cid)
            else:
                rows.append(_record(cid, "UNTESTABLE", REASON_COVERAGE, message="被覆検査不合格のため未評価"))
                failed.append(cid)
    elif applicable(mission):
        from app.goal_checks import evaluate_vehicle_criteria
        for item in evaluate_vehicle_criteria(manager, project_id, criteria, contract):
            row = _record_from_check(item)
            rows.append(row)
            if row["status"] != "PASS":
                failed.append(row["criterion_id"])
    else:
        # Generic: artifacts may be verified, but phase-1 does not grant achieved.
        for item in criteria:
            rows.append(_record(item["criterion_id"], "UNTESTABLE", "GENERIC_NOT_ACHIEVED", message="汎用計画は第1段階ではverified止め"))
            failed.append(item["criterion_id"])

    hashes = _current_hashes(manager, project_id, contract, mission)
    store = GoalCompletionStore(manager.memory.path)
    acceptance, human_acceptance = _acceptance_view(store, project_id, hashes)
    human_ok = bool(acceptance)
    if contract.get("completion_policy", {}).get("require_human_acceptance", True) and not human_ok:
        if not failed:
            for row in rows:
                row["status"] = "FAIL"
                row["reason_code"] = REASON_HUMAN
                failed.append(row["criterion_id"])

    achieved = not failed and all(row["status"] == "PASS" for row in rows) and bool(rows)
    artifact = "none"
    if achieved:
        artifact = "final"
    elif applicable(mission):
        envelope = load_input(manager, project_id)
        if envelope:
            artifact = "provisional"
    result = {
        "achieved": bool(achieved),
        "reason_code": "" if achieved else (failed and next((r["reason_code"] for r in rows if r["criterion_id"] == failed[0]), "") or ""),
        "criteria": rows,
        "failed_criteria": list(dict.fromkeys(failed)),
        "artifact_class": artifact,
        "human_accepted": bool(human_ok),
        "human_acceptance": human_acceptance,
        "contract_hash": hashes["contract_hash"],
        "plan_signature": hashes["plan_signature"],
        "input_hash": hashes["input_hash"],
        "source_hash": hashes["source_hash"],
        "artifact_hash": hashes["artifact_hash"],
        "coverage_passed": bool(coverage.get("passed")),
        "retention_passed": bool(retention.get("passed", True)),
    }
    if persist:
        store.put_evaluation(
            project_id, result["contract_hash"], hashes["plan_signature"], result
        )
    return result


def accept(manager, project_id: str, *, accepted_by: str, note: str = "") -> dict:
    actor = str(accepted_by or "").strip()
    if not actor:
        raise ValueError("accepted_by is required")
    preview_result = evaluate(manager, project_id, persist=False)
    blocked_only_human = bool(preview_result.get("criteria")) and all(
        row.get("reason_code") == REASON_HUMAN or row.get("status") == "PASS"
        for row in preview_result["criteria"]
    )
    if not blocked_only_human:
        raise ValueError("COMPLETION_GATE_FAILED: 未達条件が残っているため確定できません")
    if not preview_result.get("artifact_hash"):
        raise ValueError("COMPLETION_GATE_FAILED: 成果物がないため確定できません")
    store = GoalCompletionStore(manager.memory.path)
    store.record_acceptance(
        project_id=project_id,
        contract_hash=preview_result["contract_hash"],
        plan_signature=preview_result["plan_signature"],
        input_hash=preview_result["input_hash"],
        source_hash=preview_result["source_hash"],
        artifact_hash=preview_result["artifact_hash"],
        accepted_by=actor,
        note=note,
    )
    result = evaluate(manager, project_id, persist=True)
    if not result["achieved"]:
        raise ValueError("COMPLETION_GATE_FAILED: 確定条件を満たしていません")
    return result
