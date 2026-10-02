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


def set_enabled(manager, project_id: str, value: bool) -> dict:
    if value and os.environ.get("LOCALSAPORTER_AUTO_RESUME_AVAILABLE") != "1":
        raise ValueError("auto resume is not enabled by the server operator")
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


def _claim(manager, project_id: str, task_key: str, input_hash: str, key: str, retry_limit: int):
    run_id = uuid.uuid4().hex
    with _connect(manager) as db:
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute(
            "SELECT status,retry_count FROM runs WHERE project_id=? AND task_key=? AND idempotency_key=?",
            (project_id, task_key, key)
        ).fetchone()
        if previous and (previous["status"] in {"running", "completed"} or int(previous["retry_count"]) >= retry_limit):
            return None
        attempts = int(previous["retry_count"]) if previous else 0
        db.execute("""INSERT INTO runs(project_id,task_key,input_hash,artifact_hash,run_id,idempotency_key,
            status,evidence,retry_count,retry_limit,failure_kind,error,updated)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(project_id,task_key,idempotency_key) DO UPDATE SET
            status=excluded.status,run_id=excluded.run_id,updated=excluded.updated,
            failure_kind='',error=''""",
            (project_id, task_key, input_hash, "", run_id, key, "running", "",
             attempts, retry_limit, "", "", time.time()))
    return run_id, attempts


def _save(manager, project_id, task_key, input_hash, key, **fields):
    row = {
        "artifact_hash": "", "run_id": uuid.uuid4().hex, "status": "pending", "evidence": "",
        "retry_count": 0, "retry_limit": MAX_RETRIES, "failure_kind": "", "error": "", **fields,
    }
    with _connect(manager) as db:
        db.execute("""INSERT INTO runs(project_id,task_key,input_hash,artifact_hash,run_id,idempotency_key,
          status,evidence,retry_count,retry_limit,failure_kind,error,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(project_id,task_key,idempotency_key) DO UPDATE SET
          artifact_hash=excluded.artifact_hash,run_id=excluded.run_id,status=excluded.status,
          evidence=excluded.evidence,retry_count=excluded.retry_count,retry_limit=excluded.retry_limit,
          failure_kind=excluded.failure_kind,error=excluded.error,updated=excluded.updated""",
          (project_id, task_key, input_hash, row["artifact_hash"], row["run_id"], key, row["status"],
           row["evidence"], row["retry_count"], row["retry_limit"], row["failure_kind"], row["error"], time.time()))
    return row


async def resume(manager, project_id: str, executor=None, retry_limit: int = MAX_RETRIES) -> dict:
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
