"""P2 safe, sequential auto-resume using existing task executors.

Disabled by default. State is persisted in a small SQLite sidecar next to the existing
mission database; no external or human-gated operation is ever executed here.
"""
from __future__ import annotations

import hashlib
import os
import inspect
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from app.goal_review import ReviewStore, plan_snapshot, require_review, development_blockers
from app.plan_feedback import issues_for
from app.structured_planning import contract_of
from app.upgrade_runtime import configured_mode, execute_upgraded
from app.upgrade_store import UpgradeStore

MAX_RETRIES = 2
# P4 lease: execution upper bound + margin. Claims hold the task only until expiry;
# expired claims may be reclaimed, but artifact re-verification runs before any re-execution.
LEASE_SECONDS = 600
LEASE_EXPIRED_REASON = "lease_expired"
HUMAN_TERMS = (
    "公開", "投稿", "顧客", "連絡", "契約", "oauth", "業務事実", "最終結果",
    "rag", "confirm_rag", "送信", "承認", "外部", "個人情報",
    "publish", "post", "email", "customer", "contact", "approval",
    "contract", "external", "oauth", "payment", "http", "https",
)


def _db_path(manager) -> Path:
    source = Path(manager.memory.path)
    return source.with_name(source.name + ".auto_resume.sqlite3")


@contextmanager
def _connect(manager):
    db = sqlite3.connect(_db_path(manager))
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE IF NOT EXISTS settings(
        project_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS runs(
        project_id TEXT NOT NULL, task_key TEXT NOT NULL, input_hash TEXT NOT NULL,
        artifact_hash TEXT NOT NULL DEFAULT '', run_id TEXT NOT NULL,
        idempotency_key TEXT NOT NULL, status TEXT NOT NULL, evidence TEXT NOT NULL DEFAULT '',
        retry_count INTEGER NOT NULL DEFAULT 0, retry_limit INTEGER NOT NULL,
        failure_kind TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '', updated REAL NOT NULL,
        PRIMARY KEY(project_id, task_key, idempotency_key))""")
    # P4 lease migration: additive only, never drops existing rows/columns.
    try:
        existing = {row[1] for row in db.execute("PRAGMA table_info(runs)").fetchall()}
        migrations = {
            "lease_expires": "REAL NOT NULL DEFAULT 0",
            "owner_id": "TEXT NOT NULL DEFAULT ''",
            "expired_count": "INTEGER NOT NULL DEFAULT 0",
            "last_expired_reason": "TEXT NOT NULL DEFAULT ''",
        }
        for column, definition in migrations.items():
            if column not in existing:
                db.execute(f"ALTER TABLE runs ADD COLUMN {column} {definition}")
    except sqlite3.Error:
        pass
    try:
        with db:
            yield db
    finally:
        db.close()


def _read_connection(manager):
    path = _db_path(manager)
    if not path.exists():
        return None
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def enabled(manager, project_id: str) -> bool:
    db = _read_connection(manager)
    if db is None:
        return False
    with db:
        row = db.execute("SELECT enabled FROM settings WHERE project_id=?", (project_id,)).fetchone()
    db.close()
    return bool(row and row["enabled"])


def stage_gate(manager, project_id: str) -> dict:
    """Staged-rollout gate built only from real functions (no self-reported trust)."""
    try:
        mission = manager.memory.get_mission(project_id)
    except Exception as exc:
        return {"blocked": True, "reason": f"mission_unavailable:{type(exc).__name__}",
                "verified": False, "unresolved_count": 0, "plan_approval_blocked": True}
    try:
        _snapshot, signature = plan_snapshot(manager, project_id)
    except Exception as exc:
        return {"blocked": True, "reason": f"plan_snapshot_unavailable:{type(exc).__name__}",
                "verified": False, "unresolved_count": 0, "plan_approval_blocked": True}
    try:
        review = ReviewStore(manager.memory.path).get(project_id, "plan", signature) or {}
    except Exception:
        review = {}
    verified = review.get("status") == "passed" and mission.get("status") in {"ready", "paused", "running"}
    if not verified:
        return {"blocked": True, "reason": "plan_not_verified_and_approved",
                "verified": False, "unresolved_count": 0, "plan_approval_blocked": True,
                "plan_signature": signature}
    try:
        from app.pending_ledger import build as pending_build
        ledger = pending_build(manager, project_id)
        summary = (ledger or {}).get("summary") or {}
    except Exception as exc:
        return {"blocked": True, "reason": f"pending_ledger_unavailable:{type(exc).__name__}",
                "verified": True, "unresolved_count": 0, "plan_approval_blocked": True,
                "plan_signature": signature}
    try:
        unresolved = int(summary.get("unresolved_count") or 0)
    except (TypeError, ValueError):
        unresolved = 0
    ledger_sig = str(summary.get("plan_signature") or "")
    # The ledger is version-bound; when it could not resolve the current
    # signature (e.g. minimal test doubles), never let its plan_state override
    # the direct verified check above. Unresolved pointers still block.
    approval_blocked = bool(summary.get("plan_approval_blocked")) if ledger_sig == signature else False
    repair_blocking = bool(summary.get("repair_blocking"))
    repair_reason = str(summary.get("repair_reason") or "")
    if unresolved > 0:
        return {"blocked": True, "reason": f"unresolved_issues:{unresolved}",
                "verified": True, "unresolved_count": unresolved,
                "plan_approval_blocked": approval_blocked, "plan_signature": signature}
    if approval_blocked:
        return {"blocked": True, "reason": "plan_approval_blocked",
                "verified": True, "unresolved_count": unresolved,
                "plan_approval_blocked": True, "plan_signature": signature}
    if repair_blocking:
        return {"blocked": True, "reason": repair_reason or "repair_loop_blocking",
                "verified": True, "unresolved_count": unresolved,
                "plan_approval_blocked": approval_blocked, "plan_signature": signature}
    return {"blocked": False, "reason": "", "verified": True, "unresolved_count": 0,
            "plan_approval_blocked": False, "plan_signature": signature}


def set_enabled(manager, project_id: str, value: bool) -> dict:
    if value and os.environ.get("LOCALSAPORTER_AUTO_RESUME_AVAILABLE") != "1":
        raise ValueError("auto resume is not enabled by the server operator")
    if value:
        gate = stage_gate(manager, project_id)
        if gate.get("blocked"):
            raise ValueError(str(gate.get("reason") or "plan_not_verified_and_approved"))
    with _connect(manager) as db:
        db.execute("""INSERT INTO settings(project_id,enabled,updated) VALUES(?,?,?)
            ON CONFLICT(project_id) DO UPDATE SET enabled=excluded.enabled,updated=excluded.updated""",
            (project_id, int(bool(value)), time.time()))
    return {"project_id": project_id, "enabled": bool(value)}


def records(manager, project_id: str) -> list[dict]:
    db = _read_connection(manager)
    if db is None:
        return []
    with db:
        rows = [dict(x) for x in db.execute(
            "SELECT * FROM runs WHERE project_id=? ORDER BY updated,task_key", (project_id,)
        ).fetchall()]
    db.close()
    return rows


def _canonical_hash(value) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _input_hash(signature: str, task: dict, completed: dict[str, dict]) -> str:
    dependencies = {key: completed[key].get("artifact_hash", "") for key in task.get("depends_on", []) if key in completed}
    return _canonical_hash({"signature": signature, "task": task, "dependencies": dependencies})


def _human_gated(task: dict) -> bool:
    text = " ".join(str(task.get(x) or "") for x in ("task_key", "title", "description", "acceptance_criteria")).lower()
    contract = contract_of(task) or {}
    if contract.get("action_requirements") or contract.get("final_verification"):
        return True
    if contract.get("public_web_research") or contract.get("execution_kind"):
        return True
    return any(term in text for term in HUMAN_TERMS)


def _local_safe(manager, project_id: str, task: dict, mission: dict) -> bool:
    if str(task.get("mode") or "") != "local" or _human_gated(task):
        return False
    contract = contract_of(task)
    if not contract:
        return False
    outputs = contract.get("outputs") or []
    if len(outputs) != 1 or not str(outputs[0].get("path") or "").lower().endswith(".md"):
        return False
    if configured_mode(manager.memory.path, project_id, task.get("id")) != "enforce":
        return False
    extension = UpgradeStore(manager.memory.path).extension(project_id, task, mission.get("plan_version"))
    return isinstance(extension, dict)


def classify_failure(exc: Exception) -> str:
    text = f"{type(exc).__name__} {exc}".lower()
    if any(x in text for x in ("connection", "timeout", "timed out", "network", "接続")):
        return "connection"
    return "content"


def _approved(manager, project_id: str, signature: str, mission: dict) -> bool:
    if not signature or mission.get("status") not in {"ready", "paused"}:
        return False
    worker = (getattr(manager, "workers", {}) or {}).get(project_id)
    if worker and not worker.done():
        return False
    store = ReviewStore(manager.memory.path)
    if development_blockers(manager, project_id):
        return False
    revision = store.get(project_id, "revision", signature) or {}
    if revision.get("blockers"):
        return False
    if issues_for(manager, project_id, signature):
        return False
    feedback = store.get(project_id, "plan_feedback", signature) or {}
    if feedback.get("issues"):
        return False
    review = store.get(project_id, "plan", signature) or {}
    if review.get("status") != "passed":
        return False
    require_review(manager, project_id)
    return True


async def _execute_trusted_document(manager, project_id: str, task: dict, signature: str) -> dict:
    task_id = str(task.get("id") or "")
    mission = manager.memory.get_mission(project_id)
    current = next((x for x in mission.get("tasks", []) if str(x.get("id") or "") == task_id), None)
    if not current or current.get("status") != "pending" or not _local_safe(manager, project_id, current, mission):
        raise ValueError("task is not an approved local document step")
    contract = contract_of(current)
    output = contract["outputs"][0]["path"]
    manager.memory.update_task(task_id, "running")
    manager.memory.add_event(project_id, "task_started", "P2 local document step started", task_id)
    try:
        result = await execute_upgraded(manager, project_id, task_id)
        latest = manager.memory.get_mission(project_id)
        _, latest_signature = plan_snapshot(manager, project_id)
        if latest_signature != signature or not _approved(manager, project_id, signature, latest):
            raise ValueError("plan approval changed during execution")
        project = manager.memory.get_project(project_id)
        if not manager.workspace:
            raise ValueError("workspace unavailable")
        _, _, path = manager.workspace.resolve_file(project.get("workspace_path", ""), project_id, output, must_exist=True)
        content = path.read_bytes()
        if not content:
            raise ValueError("document artifact is empty")
        artifact_hash = hashlib.sha256(content).hexdigest()
        manager.memory.update_task(task_id, "completed", result=str(result or "")[:100000])
        manager.memory.add_event(project_id, "task_completed", "P2 local document step completed", task_id,
                                 detail=json.dumps({"path": output, "sha256": artifact_hash}, ensure_ascii=False))
        return {"artifact_hash": artifact_hash,
                "evidence": [{"path": output, "sha256": artifact_hash, "size": len(content)}]}
    except BaseException as exc:
        manager.memory.update_task(task_id, "needs_review", error=str(exc)[:100000])
        manager.memory.add_event(project_id, "task_needs_review", "P2 local document step requires review", task_id,
                                 detail=type(exc).__name__)
        raise


def _has_column(db, table: str, column: str) -> bool:
    try:
        return column in {row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.Error:
        return False


def _columns(db) -> set[str]:
    try:
        return {row[1] for row in db.execute("PRAGMA table_info(runs)").fetchall()}
    except sqlite3.Error:
        return set()


def _verify_completed_run_artifact(manager, project_id: str, run: dict) -> None:
    """Re-verify a recorded completed run against the current artifact (idempotency gate).

    Shared with recovery_record._verify_run_artifact: exactly one evidence item,
    its hash bound to the run, and the current file must match.
    """
    from app.recovery_record import _verify_run_artifact

    _verify_run_artifact(manager, project_id, run)


def _claim(manager, project_id: str, task_key: str, input_hash: str, key: str, retry_limit: int):
    """Atomic claim with lease (P4). Return shape stays (run_id, attempts) or None.

    - Valid lease (unexpired running): never stolen -> None (no double execution).
    - Expired lease: re-verify recorded artifact BEFORE re-execution.
      * No recorded artifact (crash before evidence): reclaim for re-execution.
      * Recorded artifact verifies: adopt as completed without re-execution.
        Returns ("adopted:<run_id>", attempts) so the caller skips execution.
      * Recorded artifact mismatches: mark failed/needs_review, never re-execute.
    - Completed / retry-exhausted: None (unchanged).
    - Pre-migration DBs (no lease columns): legacy behavior (running never re-runs).
    """
    ADOPT_PREFIX = "adopted:"
    owner = uuid.uuid4().hex
    run_id = uuid.uuid4().hex
    now = time.time()
    with _connect(manager) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            previous = db.execute(
                "SELECT * FROM runs WHERE project_id=? AND task_key=? AND idempotency_key=?",
                (project_id, task_key, key)
            ).fetchone()
        except sqlite3.OperationalError:
            previous = None
        has_lease = _has_column(db, "runs", "lease_expires")
        previous_dict = dict(previous) if previous is not None else None
        if previous_dict:
            status = str(previous_dict.get("status") or "")
            attempts = int(previous_dict.get("retry_count") or 0)
            if status == "completed":
                return None
            if status == "running":
                if has_lease:
                    expires = float(previous_dict.get("lease_expires") or 0)
                    if expires and expires > now:
                        # (a) lease still valid: another execution owns it; never steal.
                        return None
                    if expires and expires <= now and expires:
                        # (b) expired: crash-recovery path. Artifact re-verification first.
                        expired_count = int(previous_dict.get("expired_count") or 0) + 1
                        has_recorded_artifact = bool(
                            previous_dict.get("artifact_hash") or previous_dict.get("evidence"))
                        if has_recorded_artifact:
                            try:
                                _verify_completed_run_artifact(manager, project_id, previous_dict)
                            except (OSError, ValueError, KeyError, AttributeError, TypeError) as exc:
                                # (d) recorded hash mismatches current artifact:
                                # never re-execute; fail closed for human review.
                                db.execute(
                                    """UPDATE runs SET input_hash=?, status='failed',
                                       failure_kind=?, error=?, expired_count=?,
                                       last_expired_reason=?, updated=?
                                       WHERE project_id=? AND task_key=? AND idempotency_key=?""",
                                    (input_hash, classify_failure(exc),
                                     ("artifact_mismatch:" + str(exc))[:2000],
                                     expired_count, LEASE_EXPIRED_REASON, now,
                                     project_id, task_key, key))
                                return None
                            # (c) recorded artifact verifies: adopt as completed, no re-run.
                            db.execute(
                                """UPDATE runs SET input_hash=?, status='completed',
                                   retry_count=?, retry_limit=?, failure_kind='',
                                   error=?, lease_expires=0, owner_id='',
                                   expired_count=?,
                                   last_expired_reason=?, updated=?
                                   WHERE project_id=? AND task_key=? AND idempotency_key=?""",
                                (input_hash, attempts, retry_limit, "",
                                 expired_count,
                                 LEASE_EXPIRED_REASON, now,
                                 project_id, task_key, key))
                            return ADOPT_PREFIX + str(previous_dict.get("run_id") or ""), attempts
                        # No recorded artifact left behind: allow reclaim for re-execution.
                        count = retry_limit if attempts >= retry_limit else attempts
                        db.execute(
                            """UPDATE runs SET input_hash=?, run_id=?, owner_id=?,
                               lease_expires=?, status='running', evidence='',
                               artifact_hash='', retry_count=?, retry_limit=?,
                               failure_kind='', error=?, expired_count=?,
                               last_expired_reason=?, updated=?
                               WHERE project_id=? AND task_key=? AND idempotency_key=?""",
                            (input_hash, run_id, owner, now + LEASE_SECONDS,
                             count, retry_limit, expired_count,
                             LEASE_EXPIRED_REASON, now,
                             project_id, task_key, key))
                        return run_id, attempts
                # No lease columns (pre-migration DB): legacy running claim never re-runs.
                # No expiry timestamp: fail closed, never steal.
                return None
            if int(previous_dict.get("retry_count") or 0) >= retry_limit:
                return None
        attempts = int(previous_dict["retry_count"]) if previous_dict else 0
        if has_lease:
            db.execute("""INSERT INTO runs(project_id,task_key,input_hash,artifact_hash,run_id,idempotency_key,
                status,evidence,retry_count,retry_limit,failure_kind,error,updated,
                lease_expires,owner_id,expired_count,last_expired_reason)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(project_id,task_key,idempotency_key) DO UPDATE SET
                status=excluded.status,run_id=excluded.run_id,updated=excluded.updated,
                lease_expires=excluded.lease_expires,owner_id=excluded.owner_id,
                failure_kind='',error=''""",
                (project_id, task_key, input_hash, "", run_id, key, "running", "",
                 attempts, retry_limit, "", "", now, now + LEASE_SECONDS, owner, 0, ""))
        else:
            db.execute("""INSERT INTO runs(project_id,task_key,input_hash,artifact_hash,run_id,idempotency_key,
                status,evidence,retry_count,retry_limit,failure_kind,error,updated)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(project_id,task_key,idempotency_key) DO UPDATE SET
                status=excluded.status,run_id=excluded.run_id,updated=excluded.updated,
                failure_kind='',error=''""",
                (project_id, task_key, input_hash, "", run_id, key, "running", "",
                 attempts, retry_limit, "", "", now))
    return run_id, attempts


def _save(manager, project_id, task_key, input_hash, key, **fields):
    row = {
        "artifact_hash": "", "run_id": uuid.uuid4().hex, "status": "pending", "evidence": "",
        "retry_count": 0, "retry_limit": MAX_RETRIES, "failure_kind": "", "error": "", **fields,
    }
    with _connect(manager) as db:
        columns = _columns(db)
        if {"lease_expires", "owner_id", "expired_count", "last_expired_reason"} <= columns:
            # Lease is single-owner: completed/failed writes clear it so a stale
            # owner can never be mistaken for an active claim.
            row.setdefault("lease_expires", 0)
            row.setdefault("owner_id", "")
            previous = db.execute(
                "SELECT expired_count, last_expired_reason FROM runs "
                "WHERE project_id=? AND task_key=? AND idempotency_key=?",
                (project_id, task_key, key)).fetchone()
            expired_count = int(previous["expired_count"] or 0) if previous else 0
            last_reason = str(previous["last_expired_reason"] or "") if previous else ""
            if str(row.get("status") or "") in {"completed", "failed"}:
                row["lease_expires"] = 0
                row["owner_id"] = ""
            db.execute("""INSERT INTO runs(project_id,task_key,input_hash,artifact_hash,run_id,idempotency_key,
              status,evidence,retry_count,retry_limit,failure_kind,error,updated,
              lease_expires,owner_id,expired_count,last_expired_reason)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(project_id,task_key,idempotency_key) DO UPDATE SET
              artifact_hash=excluded.artifact_hash,run_id=excluded.run_id,status=excluded.status,
              evidence=excluded.evidence,retry_count=excluded.retry_count,retry_limit=excluded.retry_limit,
              failure_kind=excluded.failure_kind,error=excluded.error,updated=excluded.updated,
              lease_expires=excluded.lease_expires,owner_id=excluded.owner_id,
              expired_count=excluded.expired_count,
              last_expired_reason=excluded.last_expired_reason""",
              (project_id, task_key, input_hash, row["artifact_hash"], row["run_id"], key, row["status"],
               row["evidence"], row["retry_count"], row["retry_limit"], row["failure_kind"], row["error"], time.time(),
               row["lease_expires"], row["owner_id"], expired_count, last_reason))
            return row
        db.execute("""INSERT INTO runs(project_id,task_key,input_hash,artifact_hash,run_id,idempotency_key,
          status,evidence,retry_count,retry_limit,failure_kind,error,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(project_id,task_key,idempotency_key) DO UPDATE SET
          artifact_hash=excluded.artifact_hash,run_id=excluded.run_id,status=excluded.status,
          evidence=excluded.evidence,retry_count=excluded.retry_count,retry_limit=excluded.retry_limit,
          failure_kind=excluded.failure_kind,error=excluded.error,updated=excluded.updated""",
          (project_id, task_key, input_hash, row["artifact_hash"], row["run_id"], key, row["status"],
           row["evidence"], row["retry_count"], row["retry_limit"], row["failure_kind"], row["error"], time.time()))
    return row


def _post_completion_reevaluation(manager, project_id: str, task_key: str, run_row: dict) -> dict:
    """P4: independent post-execution re-evaluation (read + record only).

    Runs immediately after an evidence-backed completion is saved:
    (a) re-verify original + artifact hash via the shared idempotency verifier,
    (b) independently re-evaluate goal conditions with completion_gate.evaluate(persist=False),
    (c) update the matching P2 resolution record(s): store the re-evaluation,
        propose verifying->resolved ONLY when the independent gate passes that
        condition. Self-report (task completion alone) never resolves.
    Never approves, executes, registers RAG, touches external ops, or enables auto-resume.
    Evaluation exceptions are recorded (unknown), never swallowed silently, and
    never resolve. When the coordinator is absent/uninitialized, existing
    auto-resume behavior is unchanged (the completion record itself is kept).
    """
    outcome: dict = {
        "task_key": str(task_key or ""),
        "run_id": str((run_row or {}).get("run_id") or ""),
        "artifact_verified": False,
        "gate": None,
        "resolutions_updated": [],
        "error": "",
    }
    try:
        _verify_completed_run_artifact(manager, project_id, dict(run_row or {}))
        outcome["artifact_verified"] = True
    except Exception as exc:
        outcome["error"] = f"artifact_reverify:{type(exc).__name__}:{str(exc)[:300]}"
    gate = None
    gate_error = ""
    try:
        from app.completion_gate import evaluate as gate_evaluate

        gate = gate_evaluate(manager, project_id, persist=False)
        outcome["gate"] = {
            "achieved": bool((gate or {}).get("achieved")),
            "failed_criteria": list((gate or {}).get("failed_criteria") or []),
            "reason_code": str((gate or {}).get("reason_code") or ""),
            "artifact_hash": str((gate or {}).get("artifact_hash") or ""),
        }
    except Exception as exc:
        gate_error = f"gate_evaluate:{type(exc).__name__}:{str(exc)[:300]}"
        outcome["gate"] = {"error": gate_error}
    if gate_error and not outcome["error"]:
        outcome["error"] = gate_error
    try:
        import app.resolution_coordinator as coordinator
    except Exception as exc:
        outcome["error"] = (outcome["error"] + f" coordinator_unavailable:{type(exc).__name__}").strip()
        return outcome
    try:
        resolutions = coordinator.list_resolutions(manager, project_id)
    except Exception as exc:
        outcome["error"] = (outcome["error"] + f" coordinator_list:{type(exc).__name__}").strip()
        return outcome
    for view in resolutions or []:
        if not isinstance(view, dict):
            continue
        if str(view.get("task_key") or "") != str(task_key or ""):
            continue
        if str(view.get("state") or "") in {"resolved", "stale", "blocked", "rejected", "failed"}:
            continue
        cid = str(view.get("criterion_id") or "")
        gate_status = ""
        gate_passed = False
        if isinstance(gate, dict):
            for item in gate.get("criteria") or []:
                if str((item or {}).get("criterion_id") or "") == cid:
                    gate_status = str((item or {}).get("status") or "")
                    gate_passed = gate_status == "PASS"
                    break
            if not gate_status:
                gate_status = "UNKNOWN"
        else:
            gate_status = "UNKNOWN"
        record = {
            "task_key": str(task_key or ""),
            "run_id": outcome["run_id"],
            "artifact_hash": str((run_row or {}).get("artifact_hash") or ""),
            "artifact_verified": bool(outcome["artifact_verified"]),
            "criterion_id": cid,
            "gate_status": gate_status,
            "gate_passed": bool(gate_passed and outcome["artifact_verified"] and not gate_error),
            "gate_achieved": bool((gate or {}).get("achieved")) if isinstance(gate, dict) else False,
            "error": str(outcome["error"] or ""),
            "at": time.time(),
            "note": ("独立再評価(completion_gate, persist=False)で当該条件PASSを確認"
                     if (gate_passed and outcome["artifact_verified"] and not gate_error)
                     else "独立再評価で当該条件がPASSしていないため resolved にしない"
                            + (f"({outcome['error']})" if outcome["error"] else "")),
        }
        try:
            updated = coordinator.record_reevaluation(
                manager, project_id, str(view.get("id") or ""), record)
            outcome["resolutions_updated"].append({
                "id": str(view.get("id") or ""),
                "criterion_id": cid,
                "state": str((updated or {}).get("state") or ""),
                "gate_status": gate_status,
            })
        except Exception as exc:
            outcome["error"] = (outcome["error"] + f" record:{type(exc).__name__}").strip()
    return outcome


async def resume(manager, project_id: str, executor=None, retry_limit: int = MAX_RETRIES) -> dict:
    """Run only trusted local document steps; all other steps wait for a human.

    L3: 論理削除中のPJは自動再開の起動対象から除外する。
    """
    try:
        from app.project_delete import is_deleted as _l3_is_deleted
        if _l3_is_deleted(manager.memory.path, project_id):
            return {"status": "excluded", "reason": "論理削除中のため自動再開しません",
                    "executed": [], "waiting_human": [], "failed": []}
    except Exception:
        pass
    """Run only trusted local document steps; all other steps wait for a human."""
    if os.environ.get("LOCALSAPORTER_AUTO_RESUME_AVAILABLE") != "1":
        return {"status": "disabled", "reason": "server feature flag is off", "executed": [], "waiting_human": [], "failed": []}
    if not enabled(manager, project_id):
        return {"status": "disabled", "executed": [], "waiting_human": [], "failed": []}
    mission = manager.memory.get_mission(project_id)
    _snapshot, signature = plan_snapshot(manager, project_id)
    try:
        approved = _approved(manager, project_id, signature, mission)
    except Exception:
        approved = False
    if not approved:
        return {"status": "blocked", "reason": "plan_not_verified_and_approved", "executed": [], "waiting_human": [], "failed": []}
    completed = {str(x.get("task_key") or ""): {"artifact_hash": _canonical_hash(x.get("result"))}
                 for x in mission.get("tasks", []) if x.get("status") == "completed" and str(x.get("result") or "").strip()}
    task_completed = set(completed)
    executed, waiting, failed = [], [], []
    progress = True
    while progress:
        progress = False
        mission = manager.memory.get_mission(project_id)
        for task in mission.get("tasks", []):
            key = str(task.get("task_key") or task.get("id") or "")
            if not key or key in task_completed or task.get("status") != "pending":
                continue
            if not set(task.get("depends_on") or []).issubset(task_completed):
                continue
            safe = bool(executor) and str(task.get("mode") or "") == "local" and not _human_gated(task)
            if executor is None:
                try:
                    safe = _local_safe(manager, project_id, task, mission)
                except Exception:
                    safe = False
            if not safe:
                if key not in waiting:
                    waiting.append(key)
                continue
            if executor is None:
                try:
                    _, current_signature = plan_snapshot(manager, project_id)
                    if current_signature != signature or not _approved(manager, project_id, signature, mission):
                        return {"status": "blocked", "reason": "plan_changed_or_unapproved",
                                "executed": executed, "waiting_human": waiting, "failed": failed}
                except Exception:
                    return {"status": "blocked", "reason": "plan_read_failed",
                            "executed": executed, "waiting_human": waiting, "failed": failed}
            ih = _input_hash(signature, task, completed)
            idem = _canonical_hash({"project": project_id, "task": key, "input": ih})
            claim = _claim(manager, project_id, key, ih, idem, retry_limit)
            if claim is None:
                failed.append(key)
                continue
            run_id, attempts = claim
            adopted_run_id = None
            if str(run_id or "").startswith("adopted:"):
                # Expired lease whose recorded artifact re-verified: already completed,
                # never re-executed. Count as executed-once ( Adopt, no duplicate run).
                adopted_run_id = str(run_id).split("adopted:", 1)[1]
                row = next((x for x in records(manager, project_id)
                            if str(x.get("run_id") or "") == adopted_run_id), None)
                if row is None:
                    failed.append(key)
                    continue
                completed[key] = {**row, "task_key": key}
                task_completed.add(key)
                executed.append(key)
                progress = True
                _post_completion_reevaluation(manager, project_id, key, {**row, "task_key": key})
                continue
            try:
                if executor is None:
                    evidence = await _execute_trusted_document(manager, project_id, task, signature)
                else:
                    value = executor(task, idem)
                    evidence = await value if inspect.isawaitable(value) else value
                if not isinstance(evidence, dict) or not evidence.get("artifact_hash") or not (evidence.get("evidence") or evidence.get("artifacts")):
                    raise ValueError("verified artifact evidence is required")
                proof = evidence.get("evidence") or evidence.get("artifacts")
                row = _save(manager, project_id, key, ih, idem, artifact_hash=str(evidence["artifact_hash"]),
                            run_id=run_id, status="completed", evidence=json.dumps(proof, ensure_ascii=False),
                            retry_count=attempts, retry_limit=retry_limit)
                # P4 re-evaluation hook: additive. Its failure never removes the
                # completion record itself (but never marks resolved either).
                try:
                    _post_completion_reevaluation(
                        manager, project_id, key, {**row, "task_key": key})
                except Exception:
                    pass
                completed[key] = {**row, "task_key": key}
                task_completed.add(key)
                executed.append(key)
                progress = True
            except BaseException as exc:
                kind = classify_failure(exc)
                # The real executor may have produced a partial artifact: never retry it automatically.
                count = retry_limit if executor is None else attempts + 1
                _save(manager, project_id, key, ih, idem, run_id=run_id, status="failed",
                      retry_count=count, retry_limit=retry_limit, failure_kind=kind,
                      error=str(exc)[:2000])
                failed.append(key)
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
    return {"status": "completed" if not failed else "partial", "executed": executed,
            "waiting_human": waiting, "failed": list(dict.fromkeys(failed)), "records": records(manager, project_id)}
