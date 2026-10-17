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


_HASH_UNREADABLE = "unknown:unreadable"


def _generic_artifact_hash(manager, project_id: str, mission: dict) -> str:
    from app.structured_planning import contract_of
    from app.vehicle_workflow import digest, resolve

    artifacts = []
    unreadable = False
    for task in mission.get("tasks") or []:
        for output in (contract_of(task) or {}).get("outputs") or []:
            path = str(output.get("path") or "")
            if not path:
                continue
            try:
                target = resolve(manager, project_id, path)
            except (ValueError, OSError, AttributeError, TypeError, KeyError) as exc:
                # F2b fail-closed: 成果物の解決不能を読めたものだけのハッシュに
                # しない。読取不能項目は不明ハッシュとして混ぜ、達成・通過の
                # 根拠(被覆あり)にしない。
                unreadable = True
                artifacts.append((path, f"{_HASH_UNREADABLE}:{type(exc).__name__}"))
                continue
            try:
                if target.is_file() and target.stat().st_size > 0:
                    artifacts.append((path, digest(target.read_bytes())))
                else:
                    # 空・不在の成果物も証拠不明として混ぜ、黙って除外しない。
                    unreadable = True
                    artifacts.append((path, f"{_HASH_UNREADABLE}:empty-or-missing"))
            except OSError as exc:
                unreadable = True
                artifacts.append((path, f"{_HASH_UNREADABLE}:{type(exc).__name__}"))
                continue
    artifacts.sort()
    return _HASH_UNREADABLE if unreadable else (digest(artifacts) if artifacts else "")


def _current_hashes(manager, project_id: str, contract: dict, mission: dict) -> dict:
    from app.goal_review import plan_snapshot
    from app.vehicle_workflow import applicable, digest, load_input, read_json, resolve, sources
    from app.structured_planning import contract_of

    try:
        plan_signature = plan_snapshot(manager, project_id)[1]
    except Exception:
        plan_signature = ""
    if not applicable(mission):
        try:
            source_hash = digest(sources(manager, project_id))
        except Exception:
            source_hash = ""
        try:
            input_hash = digest({
                "goal": mission.get("goal") or "",
                "success": mission.get("success_criteria") or "",
                "constraints": mission.get("constraints_text") or "",
                "instructions": [
                    x.get("message") for x in mission.get("instruction_messages") or []
                    if x.get("kind") == "mission_instruction_user"
                ],
                "plan_version": mission.get("plan_version") or 0,
            })
        except Exception:
            input_hash = ""
        try:
            artifact_hash = _generic_artifact_hash(manager, project_id, mission)
        except Exception:
            artifact_hash = ""
        return {
            "contract_hash": contract.get("content_hash") or "", "plan_signature": plan_signature,
            "input_hash": input_hash, "source_hash": source_hash, "artifact_hash": artifact_hash,
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
    unreadable = False
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
            except (ValueError, OSError, AttributeError, TypeError, KeyError) as exc:
                # F2b fail-closed: 検証成果物の読取不能を黙って飛ばさず、
                # 不明として記録する(達成・承認有効の根拠にしない)。
                # read_json の JSON 形式不正(ValueError)も証拠不明に倒す。
                unreadable = True
                artifact_hash = f"{_HASH_UNREADABLE}:{type(exc).__name__}"
                continue
            if result.get("workbook_hash"):
                if not unreadable:
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


def criteria_ready_for_human_confirmation(result: dict) -> bool:
    """Human final confirmation is accepted only after every criterion is PASS."""
    rows = list(result.get("criteria") or [])
    if not rows:
        return False
    return all(row.get("status") == "PASS" for row in rows)


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
        from app.generic_goal_checks import evaluate_generic_criteria
        hashes_preview = _current_hashes(manager, project_id, contract, mission)
        previous = GoalCompletionStore(manager.memory.path).latest_unrevoked_acceptance(project_id)
        binding = dict(hashes_preview)
        binding["previous_acceptance"] = previous or {}
        for item in evaluate_generic_criteria(manager, project_id, criteria, contract, hashes=binding):
            row = _record_from_check(item)
            rows.append(row)
            if row["status"] != "PASS":
                failed.append(row["criterion_id"])

    hashes = _current_hashes(manager, project_id, contract, mission)
    store = GoalCompletionStore(manager.memory.path)
    acceptance, human_acceptance = _acceptance_view(store, project_id, hashes)
    human_ok = bool(acceptance)
    require_human = bool(contract.get("completion_policy", {}).get("require_human_acceptance", True))
    is_vehicle = applicable(mission)
    if is_vehicle and require_human and not human_ok:
        if not failed:
            for row in rows:
                row["status"] = "FAIL"
                row["reason_code"] = REASON_HUMAN
                failed.append(row["criterion_id"])
    all_pass = bool(rows) and not failed and all(row["status"] == "PASS" for row in rows)
    # Generic: keep per-criterion PASS/FAIL/BLOCKED/UNTESTABLE. Human confirmation
    # is a separate gate and must not rewrite those statuses into success or 0.
    evidence_known = not str(hashes.get("artifact_hash") or "").startswith(_HASH_UNREADABLE)
    achieved = (all_pass and evidence_known) if is_vehicle else (all_pass and evidence_known and (human_ok or not require_human))
    artifact = "none"
    if achieved:
        artifact = "final"
    elif is_vehicle:
        envelope = load_input(manager, project_id)
        if envelope:
            artifact = "provisional"
    elif hashes.get("artifact_hash"):
        artifact = "provisional"
    if achieved:
        reason_code = ""
    elif (not is_vehicle) and all_pass and require_human and not human_ok:
        reason_code = REASON_HUMAN
    else:
        reason_code = failed and next((r["reason_code"] for r in rows if r["criterion_id"] == failed[0]), "") or ""
    result = {
        "achieved": bool(achieved),
        "reason_code": reason_code,
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
    from app.vehicle_workflow import applicable

    mission = manager.memory.get_mission(project_id)
    if applicable(mission):
        blocked_only_human = bool(preview_result.get("criteria")) and all(
            row.get("reason_code") == REASON_HUMAN or row.get("status") == "PASS"
            for row in preview_result["criteria"]
        )
        if not blocked_only_human:
            raise ValueError("COMPLETION_GATE_FAILED: 未達条件が残っているため確定できません")
    else:
        from app.goal_review import require_human_final_confirmation_ready
        require_human_final_confirmation_ready(preview_result)
    if (not preview_result.get("artifact_hash") or
            str(preview_result["artifact_hash"]).startswith(_HASH_UNREADABLE)):
        raise ValueError("COMPLETION_GATE_FAILED: 成果物がないか読取不能のため確定できません")
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
