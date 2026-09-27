"""SQLite ledger for GoalContract, coverage, gate evaluations, and canonical state."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _add_missing_columns(db: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    for name, definition in columns.items():
        if name not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _ensure_goal_state_tables(db: sqlite3.Connection) -> None:
    db.execute(
        """CREATE TABLE IF NOT EXISTS goal_states (
            project_id TEXT PRIMARY KEY,
            state TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '',
            last_event_id INTEGER
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS goal_state_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            from_state TEXT NOT NULL DEFAULT '',
            to_state TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            actor TEXT NOT NULL DEFAULT '',
            evidence_json TEXT NOT NULL DEFAULT '',
            legacy_status TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )"""
    )
    db.execute(
        """CREATE INDEX IF NOT EXISTS idx_goal_state_events_project
           ON goal_state_events(project_id, id DESC)"""
    )
    _add_missing_columns(
        db,
        "goal_states",
        {
            "state": "TEXT NOT NULL DEFAULT ''",
            "updated_at": "TEXT NOT NULL DEFAULT ''",
            "last_event_id": "INTEGER",
        },
    )
    _add_missing_columns(
        db,
        "goal_state_events",
        {
            "project_id": "TEXT NOT NULL DEFAULT ''",
            "from_state": "TEXT NOT NULL DEFAULT ''",
            "to_state": "TEXT NOT NULL DEFAULT ''",
            "reason": "TEXT NOT NULL DEFAULT ''",
            "actor": "TEXT NOT NULL DEFAULT ''",
            "evidence_json": "TEXT NOT NULL DEFAULT ''",
            "legacy_status": "TEXT NOT NULL DEFAULT ''",
            "created_at": "TEXT NOT NULL DEFAULT ''",
        },
    )


class GoalCompletionStore:
    def __init__(self, memory_path):
        self.path = Path(memory_path).parent / "goal_completion.sqlite3"
        with self.connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS goal_contracts (
                    project_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    goal_id TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    source_goal TEXT NOT NULL DEFAULT '',
                    source_success TEXT NOT NULL DEFAULT '',
                    source_constraints TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (project_id, version)
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS plan_coverage (
                    project_id TEXT NOT NULL,
                    plan_version INTEGER NOT NULL,
                    contract_hash TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    passed INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (project_id, plan_version, contract_hash)
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS completion_evaluations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL,
                    contract_hash TEXT NOT NULL,
                    plan_signature TEXT NOT NULL DEFAULT '',
                    achieved INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    evaluated_at TEXT NOT NULL
                )"""
            )
            # Phase-2 state machine tables. Existing DBs keep their rows; missing
            # columns are added only. DROP/DELETE of these tables is forbidden.
            _ensure_goal_state_tables(db)
            db.execute(
                """CREATE TABLE IF NOT EXISTS nac_executions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    action_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(project_id, idempotency_key)
                )"""
            )
            db.execute(
                """CREATE INDEX IF NOT EXISTS idx_nac_executions_project
                   ON nac_executions(project_id, id DESC)"""
            )
            # Additive, idempotent ledger migration. Existing tables and rows are untouched.
            db.execute(
                """CREATE TABLE IF NOT EXISTS human_acceptances (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL,
                    contract_hash TEXT NOT NULL,
                    plan_signature TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    artifact_hash TEXT NOT NULL,
                    accepted_by TEXT NOT NULL,
                    accepted_at TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    revoked_at TEXT,
                    revoked_reason TEXT NOT NULL DEFAULT ''
                )"""
            )
            db.execute(
                """CREATE INDEX IF NOT EXISTS idx_human_acceptances_project
                   ON human_acceptances(project_id, id DESC)"""
            )
            # Phase-3c human answers. Rows are append-only; corrections receive a new version.
            db.execute(
                """CREATE TABLE IF NOT EXISTS goal_facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL,
                    fact_key TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    value_json TEXT NOT NULL,
                    question_id TEXT NOT NULL,
                    answered_by TEXT NOT NULL,
                    answered_at TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    plan_version INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    UNIQUE(project_id, fact_key, version)
                )"""
            )
            db.execute(
                """CREATE INDEX IF NOT EXISTS idx_goal_facts_latest
                   ON goal_facts(project_id, fact_key, version DESC)"""
            )

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    def get_nac_execution(self, project_id: str, idempotency_key: str) -> dict | None:
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT * FROM nac_executions WHERE project_id=? AND idempotency_key=?",
                (project_id, idempotency_key),
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["result"] = json.loads(item.pop("result_json"))
        return item

    def append_nac_execution(self, project_id: str, idempotency_key: str,
                             action_id: str, status: str, result: dict) -> dict:
        if not str(idempotency_key or "").strip():
            raise ValueError("idempotency_key is required")
        try:
            with self.connect() as db:
                db.execute(
                    """INSERT INTO nac_executions(
                        project_id, idempotency_key, action_id, status, result_json, created_at
                    ) VALUES(?,?,?,?,?,?)""",
                    (project_id, idempotency_key, action_id, status,
                     json.dumps(result, ensure_ascii=False, default=str), _now()),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("NAC execution is append-only") from exc
        return self.get_nac_execution(project_id, idempotency_key)

    @staticmethod
    def _fact(row: sqlite3.Row) -> dict:
        item = dict(row)
        item["value"] = json.loads(item.pop("value_json"))
        return item

    def record_fact(
        self, project_id: str, fact_key: str, value: dict, question_id: str,
        answered_by: str, input_hash: str, source_hash: str,
        plan_version: int, reason: str,
    ) -> dict:
        actor = str(answered_by or "").strip()
        if not actor:
            raise ValueError("answered_by is required")
        key = str(fact_key or "").strip()
        if not key:
            raise ValueError("fact_key is required")
        answered_at = _now()
        with self.connect() as db:
            version = int(db.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM goal_facts WHERE project_id=? AND fact_key=?",
                (project_id, key),
            ).fetchone()[0])
            cursor = db.execute(
                """INSERT INTO goal_facts(
                    project_id, fact_key, version, value_json, question_id, answered_by,
                    answered_at, input_hash, source_hash, plan_version, reason
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (project_id, key, version, json.dumps(value, ensure_ascii=False), question_id,
                 actor, answered_at, input_hash or "", source_hash or "", int(plan_version), reason or ""),
            )
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM goal_facts WHERE id=?", (cursor.lastrowid,)).fetchone()
        return self._fact(row)

    def latest_facts(self, project_id: str) -> dict[str, dict]:
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                """SELECT f.* FROM goal_facts f JOIN (
                       SELECT fact_key, MAX(version) AS version FROM goal_facts
                       WHERE project_id=? GROUP BY fact_key
                   ) latest ON latest.fact_key=f.fact_key AND latest.version=f.version
                   WHERE f.project_id=? ORDER BY f.fact_key""",
                (project_id, project_id),
            ).fetchall()
        return {row["fact_key"]: self._fact(row) for row in rows}

    def fact_history(self, project_id: str, fact_key: str) -> list[dict]:
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT * FROM goal_facts WHERE project_id=? AND fact_key=? ORDER BY version",
                (project_id, fact_key),
            ).fetchall()
        return [self._fact(row) for row in rows]

    def latest_contract(self, project_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT payload, status, version FROM goal_contracts WHERE project_id=? ORDER BY version DESC LIMIT 1",
                (project_id,),
            ).fetchone()
        if not row:
            return None
        payload = json.loads(row[0])
        payload["_status"] = row[1]
        payload["_stored_version"] = row[2]
        return payload

    def active_contract(self, project_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT payload, version FROM goal_contracts WHERE project_id=? AND status='active' ORDER BY version DESC LIMIT 1",
                (project_id,),
            ).fetchone()
        if not row:
            return None
        payload = json.loads(row[0])
        payload["_status"] = "active"
        payload["_stored_version"] = row[1]
        return payload

    def put_contract(self, project_id: str, payload: dict, status: str, sources: dict | None = None) -> dict:
        sources = sources or {}
        version = int(payload.get("version") or 1)
        with self.connect() as db:
            if status == "active":
                db.execute(
                    "UPDATE goal_contracts SET status='superseded', updated_at=? WHERE project_id=? AND status='active'",
                    (_now(), project_id),
                )
            existing = db.execute(
                "SELECT version FROM goal_contracts WHERE project_id=? AND version=?",
                (project_id, version),
            ).fetchone()
            body = json.dumps(payload, ensure_ascii=False)
            if existing:
                db.execute(
                    """UPDATE goal_contracts SET goal_id=?, content_hash=?, status=?, payload=?,
                       source_goal=?, source_success=?, source_constraints=?, updated_at=?
                       WHERE project_id=? AND version=?""",
                    (
                        payload.get("goal_id") or "",
                        payload.get("content_hash") or "",
                        status,
                        body,
                        sources.get("goal") or "",
                        sources.get("success") or "",
                        sources.get("constraints") or "",
                        _now(),
                        project_id,
                        version,
                    ),
                )
            else:
                db.execute(
                    """INSERT INTO goal_contracts(
                        project_id, version, goal_id, content_hash, status, payload,
                        source_goal, source_success, source_constraints, created_at, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        project_id,
                        version,
                        payload.get("goal_id") or "",
                        payload.get("content_hash") or "",
                        status,
                        body,
                        sources.get("goal") or "",
                        sources.get("success") or "",
                        sources.get("constraints") or "",
                        _now(),
                        _now(),
                    ),
                )
        payload = dict(payload)
        payload["_status"] = status
        payload["_stored_version"] = version
        return payload

    def next_version(self, project_id: str) -> int:
        with self.connect() as db:
            row = db.execute(
                "SELECT MAX(version) FROM goal_contracts WHERE project_id=?",
                (project_id,),
            ).fetchone()
        return int(row[0] or 0) + 1

    def put_coverage(self, project_id: str, plan_version: int, contract_hash: str, payload: dict) -> dict:
        body = json.dumps(payload, ensure_ascii=False)
        with self.connect() as db:
            db.execute(
                """INSERT OR REPLACE INTO plan_coverage(project_id, plan_version, contract_hash, payload, passed, created_at)
                   VALUES(?,?,?,?,?,?)""",
                (project_id, int(plan_version), contract_hash, body, 1 if payload.get("passed") else 0, _now()),
            )
        return payload

    def get_coverage(self, project_id: str, plan_version: int | None = None) -> dict | None:
        with self.connect() as db:
            if plan_version is None:
                row = db.execute(
                    "SELECT payload FROM plan_coverage WHERE project_id=? ORDER BY plan_version DESC LIMIT 1",
                    (project_id,),
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT payload FROM plan_coverage WHERE project_id=? AND plan_version=? ORDER BY created_at DESC LIMIT 1",
                    (project_id, int(plan_version)),
                ).fetchone()
        return json.loads(row[0]) if row else None

    def put_evaluation(self, project_id: str, contract_hash: str, plan_signature: str, payload: dict) -> dict:
        with self.connect() as db:
            db.execute(
                """INSERT INTO completion_evaluations(
                    project_id, contract_hash, plan_signature, achieved, payload, evaluated_at
                ) VALUES(?,?,?,?,?,?)""",
                (
                    project_id,
                    contract_hash,
                    plan_signature or "",
                    1 if payload.get("achieved") else 0,
                    json.dumps(payload, ensure_ascii=False),
                    _now(),
                ),
            )
        return payload

    def latest_evaluation(self, project_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT payload FROM completion_evaluations WHERE project_id=? ORDER BY id DESC LIMIT 1",
                (project_id,),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def record_acceptance(
        self, project_id: str, contract_hash: str, plan_signature: str,
        input_hash: str, source_hash: str, artifact_hash: str,
        accepted_by: str, note: str = "",
    ) -> dict:
        actor = str(accepted_by or "").strip()
        if not actor:
            raise ValueError("accepted_by is required")
        accepted_at = _now()
        with self.connect() as db:
            cursor = db.execute(
                """INSERT INTO human_acceptances(
                    project_id, contract_hash, plan_signature, input_hash, source_hash,
                    artifact_hash, accepted_by, accepted_at, note
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    project_id, contract_hash or "", plan_signature or "", input_hash or "",
                    source_hash or "", artifact_hash or "", actor, accepted_at, note or "",
                ),
            )
            row_id = cursor.lastrowid
        return {
            "id": row_id, "project_id": project_id, "contract_hash": contract_hash or "",
            "plan_signature": plan_signature or "", "input_hash": input_hash or "",
            "source_hash": source_hash or "", "artifact_hash": artifact_hash or "",
            "accepted_by": actor, "accepted_at": accepted_at, "note": note or "",
            "revoked_at": None, "revoked_reason": "",
        }

    def latest_valid_acceptance(
        self, project_id: str, contract_hash: str, plan_signature: str,
        input_hash: str, source_hash: str, artifact_hash: str,
    ) -> dict | None:
        # An absent artifact can never make an acceptance valid.
        if not artifact_hash:
            return None
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                """SELECT * FROM human_acceptances
                   WHERE project_id=? AND contract_hash=? AND plan_signature=?
                     AND input_hash=? AND source_hash=? AND artifact_hash=?
                     AND revoked_at IS NULL
                   ORDER BY id DESC LIMIT 1""",
                (project_id, contract_hash, plan_signature, input_hash, source_hash, artifact_hash),
            ).fetchone()
        return dict(row) if row else None

    def latest_unrevoked_acceptance(self, project_id: str) -> dict | None:
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                """SELECT * FROM human_acceptances
                   WHERE project_id=? AND revoked_at IS NULL ORDER BY id DESC LIMIT 1""",
                (project_id,),
            ).fetchone()
        return dict(row) if row else None

    def revoke_acceptance(self, acceptance_id: int, reason: str = "") -> dict | None:
        revoked_at = _now()
        with self.connect() as db:
            db.execute(
                """UPDATE human_acceptances SET revoked_at=?, revoked_reason=?
                   WHERE id=? AND revoked_at IS NULL""",
                (revoked_at, reason or "", int(acceptance_id)),
            )
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM human_acceptances WHERE id=?", (int(acceptance_id),)).fetchone()
        return dict(row) if row else None
