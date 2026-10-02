"""P3 ordered recovery record connecting verified experience, TRIZ and limited rerun."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path

from app.experience_store import ExperienceStore
from app.goal_review import plan_snapshot

ORDER = ("classified", "experience_checked", "triz_candidates", "isolated_trial",
         "business_check", "human_approval", "limited_rerun", "recovered")
TERMINAL = {"rejected", "blocked", "handed_off"}


def _path(manager):
    p = Path(manager.memory.path)
    return p.with_name(p.name + ".recovery.sqlite3")


def _connect(manager):
    db = sqlite3.connect(_path(manager)); db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE IF NOT EXISTS recoveries(
      id TEXT PRIMARY KEY, project_id TEXT NOT NULL, task_key TEXT NOT NULL,
      input_version TEXT NOT NULL, source_hash TEXT NOT NULL, plan_signature TEXT NOT NULL,
      classification TEXT NOT NULL, state TEXT NOT NULL, human_required INTEGER NOT NULL,
      business_passed INTEGER NOT NULL DEFAULT 0, human_approved INTEGER NOT NULL DEFAULT 0,
      rerun_passed INTEGER NOT NULL DEFAULT 0, rerun_evidence TEXT NOT NULL DEFAULT '',
      experience_result TEXT NOT NULL DEFAULT '{}', triz_candidates TEXT NOT NULL DEFAULT '[]',
      history TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL)""")
    return db


def _decode(row):
    out = dict(row)
    for key in ("experience_result", "triz_candidates", "history"):
        out[key] = json.loads(out[key])
    out["human_required"] = bool(out["human_required"])
    out["business_passed"] = bool(out["business_passed"])
    out["human_approved"] = bool(out["human_approved"])
    out["rerun_passed"] = bool(out["rerun_passed"])
    return out


def list_records(manager, project_id):
    if not _path(manager).exists(): return []
    with _connect(manager) as db:
        return [_decode(x) for x in db.execute(
            "SELECT * FROM recoveries WHERE project_id=? ORDER BY created DESC", (project_id,)).fetchall()]


def get(manager, project_id, rid):
    with _connect(manager) as db:
        row = db.execute("SELECT * FROM recoveries WHERE project_id=? AND id=?", (project_id, rid)).fetchone()
    if not row: raise ValueError("recovery record not found")
    return _decode(row)


def _classify(failure):
    text = " ".join(str(failure.get(k) or "") for k in ("kind", "classification", "code", "detail", "error")).lower()
    if any(x in text for x in ("plan_conflict", "conflict_unresolved", "計画矛盾", "指摘の未解決")):
        return "plan_repair"
    if any(x in text for x in ("connection", "timeout", "接続")): return "connection"
    return "content"


def _experience_path(manager):
    return Path(manager.memory.path).parent / "experience_memory" / "experience.sqlite3"


def check_experiences(manager, project_id, input_version, source_hash, ocr_needs_review=False):
    path = _experience_path(manager)
    result = {"accepted": [], "rejected": []}
    if not path.exists(): return result
    for row in ExperienceStore(path).list(project_id):
        applies = json.loads(row["applicability"]); evidence = json.loads(row["evidence"])
        reasons = []
        if row["status"] != "verified": reasons.append("approval_revoked" if row["status"] == "revoked" else "not_verified")
        if row["status"] == "verified" and float(row["expires"] or 0) <= time.time(): reasons.append("approval_expired")
        if str(applies.get("project", project_id)) != project_id: reasons.append("project_mismatch")
        if str(applies.get("input_version", "")) != str(input_version): reasons.append("input_version_mismatch")
        expected_source = str(applies.get("source_hash") or evidence.get("source_hash") or "")
        if not source_hash or not expected_source: reasons.append("source_hash_unverified")
        elif expected_source != source_hash: reasons.append("source_hash_changed")
        if ocr_needs_review or evidence.get("ocr_status") in {"failed", "needs_review", "pending_review", "unapproved"}: reasons.append("ocr_needs_review")
        item = {"id": row["id"], "reasons": list(dict.fromkeys(reasons))}
        (result["rejected"] if reasons else result["accepted"]).append(item)
    return result


def start(manager, project_id, task_key, failure, actor, input_version, source_hash="", ocr_needs_review=False):
    if not str(actor).strip() or not str(task_key).strip() or not str(input_version).strip():
        raise ValueError("actor, task_key and input_version are required")
    mission = manager.memory.get_mission(project_id)
    task = next((x for x in mission.get("tasks", []) if str(x.get("task_key") or x.get("id") or "") == str(task_key)), None)
    if not task: raise ValueError("task is not present in the current plan")
    if str(mission.get("plan_version") or "") != str(input_version):
        raise ValueError("input_version does not match the current plan")
    _snapshot, signature = plan_snapshot(manager, project_id)
    classification = "plan_repair" if mission.get("status") == "plan_conflict" else _classify(failure)
    if classification == "plan_repair" and mission.get("status") != "plan_conflict":
        raise ValueError("current plan has no confirmed plan conflict")
    if classification != "plan_repair":
        from app.safe_auto_resume import records as auto_resume_records
        failed = any(str(x.get("task_key") or "") == str(task_key) and x.get("status") == "failed"
                     for x in auto_resume_records(manager, project_id))
        if task.get("status") not in {"failed", "needs_review"} and not failed:
            raise ValueError("task has no recorded failure")
    from app.goal_contract import get_active
    from app.completion_gate import _current_hashes
    contract = get_active(manager, project_id)
    if contract:
        current_source = _current_hashes(manager, project_id, contract, mission).get("source_hash") or ""
        if source_hash and source_hash != current_source:
            raise ValueError("source_hash does not match current sources")
        source_hash = current_source
    else:
        source_hash = ""

    rid = uuid.uuid4().hex; now = time.time()
    state = "handed_off" if classification == "plan_repair" else "classified"
    evidence = {"failure": failure}
    if state == "handed_off":
        from app.local_patch_preview import preview
        evidence["local_patch_preview"] = preview(manager, project_id)
        evidence["reason"] = "計画矛盾はP1計画修復器へ引き渡しました"
    history = [{"from": None, "to": state, "actor": actor, "reason": evidence.get("reason", "failure classified"), "evidence": evidence, "at": now}]
    with _connect(manager) as db:
        db.execute("INSERT INTO recoveries VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (rid, project_id, task_key, input_version, source_hash, signature, classification, state, 0,
                    0, 0, 0, "", "{}", "[]", json.dumps(history, ensure_ascii=False), now, now))
    if state == "classified":
        # Persist the authoritative check result without registering any experience.
        result = check_experiences(manager, project_id, input_version, source_hash, ocr_needs_review)
        _update(manager, project_id, rid, experience_result=result)
    return get(manager, project_id, rid)


def _update(manager, project_id, rid, **values):
    serial = {k: json.dumps(v, ensure_ascii=False) if k in {"history", "experience_result", "triz_candidates"} else v for k,v in values.items()}
    serial["updated"] = time.time()
    with _connect(manager) as db:
        expected = values.get("history", [{}])[-1].get("from") if "history" in values else None
        suffix = " AND state=?" if expected else ""
        args = (*serial.values(), project_id, rid, expected) if expected else (*serial.values(), project_id, rid)
        if db.execute("UPDATE recoveries SET "+",".join(f"{k}=?" for k in serial)+" WHERE project_id=? AND id=?"+suffix,
                      args).rowcount != 1: raise ValueError("recovery state changed or record not found")


def advance(manager, project_id, rid, next_state, reason, evidence, actor):
    if not str(actor).strip() or not str(reason).strip() or not isinstance(evidence, dict):
        raise ValueError("actor, reason and evidence are required")
    row = get(manager, project_id, rid)
    _, current_signature = plan_snapshot(manager, project_id)
    if current_signature != row["plan_signature"]:
        raise ValueError("plan or source changed; start a new recovery")
    if row["state"] in TERMINAL or row["state"] == "recovered": raise ValueError("recovery is terminal")
    if next_state in TERMINAL:
        allowed = True
    else:
        try: allowed = ORDER.index(next_state) == ORDER.index(row["state"]) + 1
        except ValueError: allowed = False
    if not allowed: raise ValueError(f"invalid recovery transition: {row['state']} -> {next_state}")
    values = {}
    if next_state == "triz_candidates":
        candidates = _stored_triz(manager, project_id, row).get("candidates")
        if not isinstance(candidates, list) or not candidates or any(not isinstance(x, dict) for x in candidates):
            raise ValueError("TRIZ candidates must be non-empty objects")
        normalized = []
        seen = set()
        for index, candidate in enumerate(candidates, 1):
            item = dict(candidate)
            item["id"] = str(item.get("id") or f"candidate-{index:02d}")
            if item["id"] in seen:
                raise ValueError("TRIZ candidate ids must be unique")
            seen.add(item["id"]); normalized.append(item)
        values["triz_candidates"] = normalized
    if next_state == "isolated_trial":
        from app.triz_adapters import CAPABILITIES
        trial_result = _stored_triz(manager, project_id, row)
        candidate_id = str(evidence.get("candidate_id") or "")
        capability = str(evidence.get("capability") or "")
        if not any(str(x.get("id") or "") == candidate_id for x in row["triz_candidates"]):
            raise ValueError("stored TRIZ candidate is required")
        if capability not in CAPABILITIES or not CAPABILITIES[capability].get("available"):
            raise ValueError("registered available adapter capability is required")
        source_candidate = next((x for x in trial_result.get("candidates", []) if x.get("id") == candidate_id), None)
        if (not source_candidate or source_candidate.get("missing")
                or not any(step.get("capability") == capability for step in source_candidate.get("steps", []))):
            raise ValueError("trial capability must belong to a runnable stored candidate")
        matches = [x for x in trial_result.get("experiments", []) if x.get("candidate") == candidate_id]
        if trial_result.get("status") != "artifact_trial_passed" or not matches:
            raise ValueError("persisted isolated trial is required")
        if len(matches[-1].get("cases", [])) < 2 or not all(x.get("checks", {}).get("passed") is True for x in matches[-1]["cases"]):
            raise ValueError("persisted isolated trial did not pass")
        evidence = {"candidate_id": candidate_id, "capability": capability,
                    "trial_signature": trial_result["signature"], "scope": "isolated_artifact_only"}
    if next_state == "business_check":
        from app.completion_gate import evaluate
        checked = evaluate(manager, project_id, persist=False)
        if not checked.get("criteria") or any(x.get("status") != "PASS" and x.get("reason_code") != "HUMAN_ACCEPTANCE_MISSING" for x in checked["criteria"]):
            raise ValueError("independent goal criteria have not passed")
        if not checked.get("coverage_passed") or not checked.get("retention_passed"):
            raise ValueError("independent coverage or retention check failed")
        evidence = {"criteria": [x.get("criterion_id") for x in checked["criteria"]],
                    "artifact_hash": checked.get("artifact_hash"), "source_hash": checked.get("source_hash")}
        values["business_passed"] = 1
    if next_state == "human_approval":
        from app.completion_gate import evaluate
        checked = evaluate(manager, project_id, persist=False)
        approval = checked.get("human_acceptance") or {}
        if not checked.get("human_accepted") or not approval.get("valid"):
            raise ValueError("current source and artifact require persisted human acceptance")
        evidence = {"accepted_by": approval.get("accepted_by"), "accepted_at": approval.get("accepted_at"),
                    "artifact_hash": checked.get("artifact_hash")}
        values.update(human_required=1, human_approved=1)
    if next_state == "limited_rerun":
        from app.safe_auto_resume import records as auto_resume_records
        run_id = str(evidence.get("run_id") or "")
        run = next((x for x in auto_resume_records(manager, project_id)
                    if str(x.get("run_id") or "") == run_id), None)
        if (not run or str(run.get("task_key") or "") != row["task_key"]
                or run.get("status") != "completed" or not run.get("evidence")
                or not run.get("artifact_hash") or float(run.get("updated") or 0) <= row["created"]):
            raise ValueError("completed P2 run for the recovery task with evidence is required")
        _verify_run_artifact(manager, project_id, run)
        server_evidence = {"run_id": run_id, "artifact_hash": run["artifact_hash"],
                           "evidence": run["evidence"]}
        evidence = server_evidence
        values.update(rerun_passed=1, rerun_evidence=json.dumps(server_evidence, ensure_ascii=False))
    if next_state == "recovered":
        from app.completion_gate import evaluate
        from app.safe_auto_resume import records as auto_resume_records
        checked = evaluate(manager, project_id, persist=False)
        if not row["business_passed"] or not row["rerun_passed"] or not row["human_approved"] or not checked.get("achieved") or not checked.get("human_accepted"):
            raise ValueError("current goal, business criteria, rerun and human acceptance must pass")
        prior = json.loads(row["rerun_evidence"] or "{}")
        run = next((x for x in auto_resume_records(manager, project_id) if x.get("run_id") == prior.get("run_id")), None)
        if not run or run.get("status") != "completed" or run.get("artifact_hash") != prior.get("artifact_hash"):
            raise ValueError("limited rerun evidence changed")
        _verify_run_artifact(manager, project_id, run)
        evidence = {"goal_achieved": True, "artifact_hash": checked.get("artifact_hash"),
                    "acceptance": checked.get("human_acceptance"), "run_id": prior.get("run_id")}
    history = row["history"] + [{"from": row["state"], "to": next_state, "actor": actor,
                                 "reason": reason, "evidence": evidence, "at": time.time()}]
    values.update(state=next_state, history=history)
    _update(manager, project_id, rid, **values)
    return get(manager, project_id, rid)


def _stored_triz(manager, project_id, row):
    """Read the existing local TRIZ result; caller JSON is never a trial."""
    from app.vehicle_workflow import digest, resolve, requirements_hash, sources
    mission = manager.memory.get_mission(project_id)
    task = next((x for x in mission.get("tasks", []) if str(x.get("task_key") or x.get("id") or "") == row["task_key"]), None)
    if not task:
        raise ValueError("recovery task no longer exists")
    root = resolve(manager, project_id, f'result/triz/{task["id"]}')
    matches = []
    if root.is_dir():
        for path in root.glob("*.json"):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            error = str((item.get("problem") or {}).get("evidence") or "")
            expected = digest([requirements_hash(mission), task.get("acceptance_criteria"), sources(manager, project_id), error]) if error else ""
            if (item.get("task_id") == task["id"] and item.get("goal_hash") == requirements_hash(mission)
                    and expected and item.get("signature") == expected and path.stem == expected):
                matches.append((path.stat().st_mtime, item))
    if not matches:
        raise ValueError("no persisted TRIZ result for current goal and task")
    return max(matches, key=lambda x: x[0])[1]


def _verify_run_artifact(manager, project_id, run):
    from app.vehicle_workflow import resolve
    try:
        proof = json.loads(run["evidence"])
    except (TypeError, ValueError) as exc:
        raise ValueError("rerun evidence is invalid") from exc
    if not isinstance(proof, list) or len(proof) != 1 or not isinstance(proof[0], dict):
        raise ValueError("one artifact evidence is required")
    item = proof[0]
    path = str(item.get("path") or "")
    if not path or item.get("sha256") != run["artifact_hash"]:
        raise ValueError("rerun artifact hash is not bound to evidence")
    target = resolve(manager, project_id, path, exists=True)
    if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != run["artifact_hash"]:
        raise ValueError("rerun artifact changed")
