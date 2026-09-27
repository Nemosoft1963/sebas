"""Goal-completion state machine with dual-write from legacy mission.status.

achieved is never written here except via settle_achieved(), which re-evaluates
the completion gate. Legacy completed maps only as far as produced.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.goal_completion_store import GoalCompletionStore


LOGGER = logging.getLogger(__name__)

STATES = frozenset({
    "draft",
    "planned",
    "approved",
    "running",
    "blocked_input",
    "blocked_approval",
    "blocked_external",
    "verifying",
    "produced",
    "verified",
    "achieved",
    "failed",
    "cancelled",
})

_BLOCKED = frozenset({"blocked_input", "blocked_approval", "blocked_external"})
_EXCEPT_ACHIEVED = STATES - {"achieved"}

# Explicit allow-list. achieved is never a destination here.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "": _EXCEPT_ACHIEVED,
    "draft": frozenset({"planned", "approved", "running", "cancelled", "failed", "produced"}) | _BLOCKED,
    "planned": frozenset({"draft", "approved", "running", "cancelled", "failed", "produced"}) | _BLOCKED,
    "approved": frozenset({"planned", "running", "cancelled", "failed", "produced"}) | _BLOCKED,
    "running": frozenset({"planned", "approved", "verifying", "produced", "failed", "cancelled"}) | _BLOCKED,
    "blocked_input": frozenset({"running", "planned", "approved", "draft", "produced", "failed", "cancelled"}) | _BLOCKED,
    "blocked_approval": frozenset({"running", "planned", "approved", "produced", "failed", "cancelled"}) | _BLOCKED,
    "blocked_external": frozenset({"running", "planned", "approved", "produced", "failed", "cancelled"}) | _BLOCKED,
    "verifying": frozenset({"produced", "verified", "running", "failed", "cancelled"}) | _BLOCKED,
    "produced": frozenset({"verifying", "verified", "running", "planned", "draft", "failed", "cancelled"}) | _BLOCKED,
    "verified": frozenset({"produced", "verifying", "running", "failed", "cancelled"}) | _BLOCKED,
    "achieved": frozenset({"produced", "verified"}),
    "failed": frozenset({"draft", "planned", "running", "cancelled"}) | _BLOCKED,
    "cancelled": frozenset({"draft", "planned"}),
}

_SIMPLE_LEGACY_MAP = {
    "planning": "draft",
    "running": "running",
    "cancelled": "cancelled",
    "failed": "failed",
    "completed": "produced",
}

_INPUT_KINDS = frozenset({
    "vehicle_decision",
    "needs_review",
    "vehicle_input_updated",
    "mission_state_reconciled",
})
_EXTERNAL_KINDS = frozenset({
    "external_execution_required",
})
_APPROVAL_KINDS = frozenset({
    "pending_approval",
    "awaiting_approval",
    "needs_approval",
    "approval_required",
    "plan_approval",
    "human_approval",
})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_store(store_or_manager) -> GoalCompletionStore:
    if isinstance(store_or_manager, GoalCompletionStore):
        return store_or_manager
    if hasattr(store_or_manager, "memory"):
        return GoalCompletionStore(store_or_manager.memory.path)
    return GoalCompletionStore(store_or_manager.path)


def _row_state(db: sqlite3.Connection, project_id: str) -> str:
    row = db.execute(
        "SELECT state FROM goal_states WHERE project_id=?",
        (project_id,),
    ).fetchone()
    return str(row[0]) if row and row[0] else ""


def current_state(store_or_manager, project_id: str) -> str:
    store = _as_store(store_or_manager)
    with store.connect() as db:
        return _row_state(db, project_id)


def allowed_transition(from_state: str, to_state: str) -> bool:
    return to_state in ALLOWED_TRANSITIONS.get(from_state or "", frozenset())


def map_legacy_status(status: str, kind: str = "", *, approved: bool = False) -> str | None:
    value = str(status or "").strip()
    event_kind = str(kind or "").strip()
    if value == "ready":
        return "approved" if approved else "planned"
    if value == "paused":
        if event_kind in _EXTERNAL_KINDS or event_kind == "external_execution_required":
            return "blocked_external"
        if event_kind in _APPROVAL_KINDS or "approval" in event_kind.lower():
            return "blocked_approval"
        if event_kind in _INPUT_KINDS or event_kind in {"vehicle_decision", "needs_review"}:
            return "blocked_input"
        return "blocked_input"
    return _SIMPLE_LEGACY_MAP.get(value)


def _evidence_text(evidence) -> str:
    if evidence is None:
        return ""
    if isinstance(evidence, str):
        return evidence
    return json.dumps(evidence, ensure_ascii=False)


def _persist(
    store: GoalCompletionStore,
    project_id: str,
    to_state: str,
    *,
    reason: str,
    actor: str,
    evidence=None,
    legacy_status: str = "",
    allow_achieved: bool = False,
) -> dict:
    if to_state not in STATES:
        raise ValueError(f"ILLEGAL_TRANSITION: unknown state {to_state!r}")
    if to_state == "achieved" and not allow_achieved:
        raise ValueError("ILLEGAL_TRANSITION: achieved requires settle_achieved")
    with store.connect() as db:
        from_state = _row_state(db, project_id)
        if from_state == to_state:
            row = db.execute(
                "SELECT project_id, state, updated_at, last_event_id FROM goal_states WHERE project_id=?",
                (project_id,),
            ).fetchone()
            return {
                "project_id": project_id,
                "state": to_state,
                "updated_at": row[2] if row else "",
                "last_event_id": row[3] if row else None,
                "changed": False,
            }
        if to_state == "achieved":
            legal = True
        else:
            legal = allowed_transition(from_state, to_state)
        if not legal:
            raise ValueError(f"ILLEGAL_TRANSITION: {from_state or '(unset)'} -> {to_state}")
        created_at = _now()
        cursor = db.execute(
            """INSERT INTO goal_state_events(
                project_id, from_state, to_state, reason, actor, evidence_json, legacy_status, created_at
            ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                project_id,
                from_state,
                to_state,
                reason or "",
                actor or "",
                _evidence_text(evidence),
                legacy_status or "",
                created_at,
            ),
        )
        event_id = cursor.lastrowid
        db.execute(
            """INSERT INTO goal_states(project_id, state, updated_at, last_event_id)
               VALUES(?,?,?,?)
               ON CONFLICT(project_id) DO UPDATE SET
                 state=excluded.state,
                 updated_at=excluded.updated_at,
                 last_event_id=excluded.last_event_id""",
            (project_id, to_state, created_at, event_id),
        )
    return {
        "project_id": project_id,
        "state": to_state,
        "updated_at": created_at,
        "last_event_id": event_id,
        "changed": True,
        "from_state": from_state,
    }


def transition(store_or_manager, project_id: str, to_state: str, *, reason: str, actor: str, evidence=None) -> dict:
    store = _as_store(store_or_manager)
    return _persist(store, project_id, to_state, reason=reason, actor=actor, evidence=evidence)


def settle_achieved(manager, project_id: str) -> dict:
    from app.completion_gate import evaluate

    gate = evaluate(manager, project_id, persist=False)
    achieved = bool(gate.get("achieved"))
    human_ok = bool(gate.get("human_accepted"))
    criteria = list(gate.get("criteria") or [])
    all_pass = bool(criteria) and all(row.get("status") == "PASS" for row in criteria)
    if not (achieved and human_ok and all_pass):
        raise ValueError("ACHIEVED_REQUIRES_GATE")
    store = GoalCompletionStore(manager.memory.path)
    return _persist(
        store,
        project_id,
        "achieved",
        reason="gate_settled",
        actor="settle_achieved",
        evidence={
            "contract_hash": gate.get("contract_hash") or "",
            "plan_signature": gate.get("plan_signature") or "",
            "artifact_hash": gate.get("artifact_hash") or "",
        },
        allow_achieved=True,
    )


def _record_rejected(store: GoalCompletionStore, project_id: str, from_state: str, attempted: str,
                     *, reason: str, actor: str, legacy_status: str, kind: str) -> None:
    with store.connect() as db:
        db.execute(
            """INSERT INTO goal_state_events(
                project_id, from_state, to_state, reason, actor, evidence_json, legacy_status, created_at
            ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                project_id,
                from_state,
                attempted,
                "rejected",
                actor or "legacy_status",
                json.dumps({
                    "rejected": True,
                    "attempted": attempted,
                    "detail": reason,
                    "kind": kind,
                }, ensure_ascii=False),
                legacy_status or "",
                _now(),
            ),
        )


def observe_legacy_status(memory_path, project_id: str, status: str, kind: str = "status") -> dict | None:
    from app.goal_completion_flag import enabled

    if not enabled(memory_path, project_id):
        return None
    try:
        store = GoalCompletionStore(memory_path)
        current = current_state(store, project_id)
        approved = current == "approved"
        mapped = map_legacy_status(status, kind, approved=approved)
        if mapped is None:
            LOGGER.warning(
                "goal_completion: unmapped legacy status %r kind=%r project=%s",
                status, kind, project_id,
            )
            return {"state": current, "mapped": False, "changed": False}
        if current == mapped:
            return {"state": current, "mapped": True, "changed": False}
        if not allowed_transition(current, mapped):
            LOGGER.warning(
                "goal_completion: rejected mapped transition %s -> %s (legacy %r/%r) project=%s",
                current or "(unset)", mapped, status, kind, project_id,
            )
            _record_rejected(
                store, project_id, current, mapped,
                reason=f"ILLEGAL_TRANSITION: {current or '(unset)'} -> {mapped}",
                actor="legacy_status",
                legacy_status=str(status or ""),
                kind=str(kind or ""),
            )
            return {"state": current, "mapped": True, "changed": False, "rejected": True}
        return _persist(
            store,
            project_id,
            mapped,
            reason=f"legacy:{kind or 'status'}",
            actor="legacy_status",
            evidence={"kind": kind, "legacy_status": status},
            legacy_status=str(status or ""),
        )
    except Exception as exc:
        LOGGER.warning("goal_completion: observe_legacy_status failed: %s", exc, exc_info=True)
        return None


def list_events(store_or_manager, project_id: str, limit: int = 20) -> list[dict]:
    store = _as_store(store_or_manager)
    limit = max(1, min(int(limit), 100))
    with store.connect() as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            """SELECT id, project_id, from_state, to_state, reason, actor, evidence_json,
                      legacy_status, created_at
               FROM goal_state_events WHERE project_id=? ORDER BY id DESC LIMIT ?""",
            (project_id, limit),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        raw = item.get("evidence_json") or ""
        if raw:
            try:
                item["evidence"] = json.loads(raw)
            except ValueError:
                item["evidence"] = raw
        else:
            item["evidence"] = {}
        result.append(item)
    return result


def _gate_view(manager, project_id: str) -> dict:
    path = Path(manager.memory.path).parent / "goal_completion.sqlite3"
    if not path.exists():
        return {
            "achieved": False,
            "reason_code": "",
            "human_accepted": False,
            "artifact_class": "none",
            "failed_criteria": [],
        }
    try:
        from app.completion_gate import evaluate
        gate = evaluate(manager, project_id, persist=False)
        return {
            "achieved": bool(gate.get("achieved")),
            "reason_code": gate.get("reason_code") or "",
            "human_accepted": bool(gate.get("human_accepted")),
            "artifact_class": gate.get("artifact_class") or "none",
            "failed_criteria": list(gate.get("failed_criteria") or []),
        }
    except Exception as exc:
        LOGGER.warning("goal_completion: gate read failed: %s", exc, exc_info=True)
        return {"achieved": False, "error": str(exc)[:500]}


def _read_recorded(memory_path, project_id: str) -> tuple[str, list[dict]]:
    path = Path(memory_path).parent / "goal_completion.sqlite3"
    if not path.exists():
        return "", []
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "goal_states" not in names:
            return "", []
        row = db.execute(
            "SELECT state FROM goal_states WHERE project_id=?",
            (project_id,),
        ).fetchone()
        state = str(row[0]) if row and row[0] else ""
        if not state or "goal_state_events" not in names:
            return state, []
        db.row_factory = sqlite3.Row
        rows = db.execute(
            """SELECT id, project_id, from_state, to_state, reason, actor, evidence_json,
                      legacy_status, created_at
               FROM goal_state_events WHERE project_id=? ORDER BY id DESC LIMIT 20""",
            (project_id,),
        ).fetchall()
        events = []
        for item in rows:
            event = dict(item)
            raw = event.get("evidence_json") or ""
            if raw:
                try:
                    event["evidence"] = json.loads(raw)
                except ValueError:
                    event["evidence"] = raw
            else:
                event["evidence"] = {}
            events.append(event)
        return state, events
    except sqlite3.OperationalError:
        return "", []
    finally:
        db.close()


def read_goal_state(manager, project_id: str) -> dict:
    memory_path = manager.memory.path
    mission = manager.memory.get_mission(project_id)
    legacy_status = str(mission.get("status") or "")
    recorded_state, events = _read_recorded(memory_path, project_id)
    recorded = bool(recorded_state)
    approved = recorded_state == "approved"
    mapped_state = map_legacy_status(legacy_status, "status", approved=approved)
    mapped = mapped_state is not None
    if recorded:
        state = recorded_state
    else:
        state = mapped_state or ""
        if not mapped:
            LOGGER.warning(
                "goal_completion: unmapped legacy status %r on read project=%s",
                legacy_status, project_id,
            )
    return {
        "project_id": project_id,
        "state": state,
        "legacy_status": legacy_status,
        "mapped": mapped,
        "recorded": recorded,
        "events": events,
        "gate": _gate_view(manager, project_id),
    }
