from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


DEFAULT_PROJECT_ID = "default"
LOGGER = logging.getLogger(__name__)


def ensure_ocr_tables(db: sqlite3.Connection) -> None:
    """仕様 10.2 の OCR 6テーブルを既存テーブルを変えず追加する。何度呼んでも冪等。"""
    db.execute(
        """CREATE TABLE IF NOT EXISTS ocr_runs (
            run_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            context_file_id TEXT NOT NULL,
            source_sha256 TEXT NOT NULL,
            status TEXT NOT NULL,
            trigger TEXT NOT NULL DEFAULT '',
            engine TEXT NOT NULL DEFAULT '{}',
            version TEXT NOT NULL DEFAULT 'ocr-fallback/v1',
            parameters_hash TEXT NOT NULL DEFAULT '',
            started_at TEXT,
            finished_at TEXT,
            elapsed_ms INTEGER,
            error_code TEXT NOT NULL DEFAULT '',
            execution_key TEXT NOT NULL DEFAULT '',
            idempotency_key TEXT NOT NULL DEFAULT '',
            adoption_status TEXT NOT NULL DEFAULT 'unapproved',
            rag_registered INTEGER NOT NULL DEFAULT 0,
            published_at TEXT,
            artifact_root TEXT NOT NULL DEFAULT '',
            rag_experience_id TEXT NOT NULL DEFAULT '',
            rag_revoked INTEGER NOT NULL DEFAULT 0
        )"""
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_ocr_runs_project ON ocr_runs(project_id, context_file_id, started_at)"
    )
    db.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_ocr_runs_execution_key
            ON ocr_runs(execution_key) WHERE execution_key<>''"""
    )
    run_columns = {row[1] for row in db.execute("PRAGMA table_info(ocr_runs)")}
    run_migrations = {
        "idempotency_key": "TEXT NOT NULL DEFAULT ''",
        "adoption_status": "TEXT NOT NULL DEFAULT 'unapproved'",
        "rag_registered": "INTEGER NOT NULL DEFAULT 0",
        "published_at": "TEXT",
        "artifact_root": "TEXT NOT NULL DEFAULT ''",
        "rag_experience_id": "TEXT NOT NULL DEFAULT ''",
        "rag_revoked": "INTEGER NOT NULL DEFAULT 0",
    }
    for column, definition in run_migrations.items():
        if column not in run_columns:
            db.execute(f"ALTER TABLE ocr_runs ADD COLUMN {column} {definition}")
    db.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_ocr_runs_idempotency
            ON ocr_runs(project_id, context_file_id, idempotency_key)
            WHERE idempotency_key<>''"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS ocr_pages (
            run_id TEXT NOT NULL,
            page_no INTEGER NOT NULL,
            image_sha256 TEXT NOT NULL DEFAULT '',
            width REAL,
            height REAL,
            status TEXT NOT NULL DEFAULT 'pending',
            PRIMARY KEY(run_id, page_no)
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS ocr_blocks (
            run_id TEXT NOT NULL,
            page_no INTEGER NOT NULL,
            block_id TEXT NOT NULL,
            label TEXT NOT NULL DEFAULT 'text',
            bbox_json TEXT NOT NULL DEFAULT '[]',
            text TEXT NOT NULL DEFAULT '',
            confidence REAL,
            PRIMARY KEY(run_id, page_no, block_id)
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS ocr_fields (
            run_id TEXT NOT NULL,
            field_id TEXT NOT NULL,
            name TEXT NOT NULL,
            value_text TEXT NOT NULL DEFAULT '',
            value_type TEXT NOT NULL DEFAULT 'string',
            page_no INTEGER,
            block_id TEXT NOT NULL DEFAULT '',
            validation_status TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(run_id, field_id)
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS ocr_validations (
            run_id TEXT NOT NULL,
            check_id TEXT NOT NULL,
            status TEXT NOT NULL,
            expected TEXT NOT NULL DEFAULT '',
            actual TEXT NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(run_id, check_id)
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS ocr_reviews (
            run_id TEXT PRIMARY KEY,
            decision TEXT,
            reviewer TEXT,
            evidence TEXT NOT NULL DEFAULT '',
            corrected_values_json TEXT NOT NULL DEFAULT '{}',
            reviewed_at TEXT,
            signature TEXT NOT NULL DEFAULT ''
        )"""
    )


class ShortTermMemory:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, context_text TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            project_columns = {row[1] for row in db.execute("PRAGMA table_info(projects)")}
            if "workspace_path" not in project_columns:
                db.execute("ALTER TABLE projects ADD COLUMN workspace_path TEXT NOT NULL DEFAULT ''")
            db.execute(
                "INSERT OR IGNORE INTO projects(id,name,context_text,created_at,updated_at) VALUES(?,?,?,?,?)",
                (DEFAULT_PROJECT_ID, "既定プロジェクト", "", now, now),
            )
            db.execute("""CREATE TABLE IF NOT EXISTS project_context_files (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, filename TEXT NOT NULL,
                content TEXT NOT NULL, size_bytes INTEGER NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_context_files_project ON project_context_files(project_id,created_at,id)")
            context_columns = {row[1] for row in db.execute("PRAGMA table_info(project_context_files)")}
            context_migrations = {
                "mime_type": "TEXT NOT NULL DEFAULT 'text/markdown'",
                "file_kind": "TEXT NOT NULL DEFAULT 'markdown'",
                "extraction_note": "TEXT NOT NULL DEFAULT ''",
                "sha256": "TEXT NOT NULL DEFAULT ''",
                "original_data": "BLOB",
                "source": "TEXT NOT NULL DEFAULT 'upload'",
            }
            for column, definition in context_migrations.items():
                if column not in context_columns:
                    db.execute(f"ALTER TABLE project_context_files ADD COLUMN {column} {definition}")

            db.execute("""CREATE TABLE IF NOT EXISTS turns (
                id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, created_at TEXT NOT NULL,
                user_text TEXT NOT NULL, assistant_text TEXT NOT NULL, model TEXT NOT NULL,
                latency_ms REAL NOT NULL, interrupted INTEGER NOT NULL DEFAULT 0,
                project_id TEXT NOT NULL DEFAULT 'default')""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(turns)")}
            if "project_id" not in columns:
                db.execute("ALTER TABLE turns ADD COLUMN project_id TEXT NOT NULL DEFAULT 'default'")
            db.execute("CREATE INDEX IF NOT EXISTS idx_turns_project_session_id ON turns(project_id,session_id,id DESC)")
            db.execute("""CREATE TABLE IF NOT EXISTS project_missions (
                project_id TEXT PRIMARY KEY, goal TEXT NOT NULL DEFAULT '',
                success_criteria TEXT NOT NULL DEFAULT '', constraints_text TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'draft', plan_summary TEXT NOT NULL DEFAULT '',
                final_report TEXT NOT NULL DEFAULT '', allow_external_ai INTEGER NOT NULL DEFAULT 0,
                external_providers TEXT NOT NULL DEFAULT '[]', plan_version INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            mission_columns = {row[1] for row in db.execute("PRAGMA table_info(project_missions)")}
            mission_migrations = {
                "max_parallel_tasks": "INTEGER NOT NULL DEFAULT 2",
                "plan_reviews": "TEXT NOT NULL DEFAULT '[]'",
            }
            for column, definition in mission_migrations.items():
                if column not in mission_columns:
                    db.execute(f"ALTER TABLE project_missions ADD COLUMN {column} {definition}")
            db.execute("""CREATE TABLE IF NOT EXISTS project_tasks (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, position INTEGER NOT NULL,
                title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
                acceptance_criteria TEXT NOT NULL DEFAULT '', mode TEXT NOT NULL DEFAULT 'local',
                status TEXT NOT NULL DEFAULT 'pending', result TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0,
                started_at TEXT, completed_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_project_tasks_project ON project_tasks(project_id,position)")
            task_columns = {row[1] for row in db.execute("PRAGMA table_info(project_tasks)")}
            task_migrations = {
                "task_key": "TEXT NOT NULL DEFAULT ''",
                "depends_on": "TEXT NOT NULL DEFAULT '[]'",
                "agent_label": "TEXT NOT NULL DEFAULT ''",
            }
            for column, definition in task_migrations.items():
                if column not in task_columns:
                    db.execute(f"ALTER TABLE project_tasks ADD COLUMN {column} {definition}")
            db.execute("""CREATE TABLE IF NOT EXISTS project_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL,
                task_id TEXT, kind TEXT NOT NULL, message TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_project_events_project ON project_events(project_id,id DESC)")
            db.execute("""CREATE TABLE IF NOT EXISTS project_actions (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, task_id TEXT,
                kind TEXT NOT NULL, target TEXT NOT NULL, content TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending_approval',
                evidence TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, approved_at TEXT, executed_at TEXT,
                updated_at TEXT NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_project_actions_project ON project_actions(project_id,created_at,id)")
            db.execute("""CREATE TABLE IF NOT EXISTS premarketing_campaigns (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, public_token TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL, audience TEXT NOT NULL, offer TEXT NOT NULL,
                call_to_action TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
                assets_path TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_premarketing_campaign_project ON premarketing_campaigns(project_id,created_at,id)")
            campaign_columns = {row[1] for row in db.execute("PRAGMA table_info(premarketing_campaigns)")}
            campaign_migrations = {
                "publication_status": "TEXT NOT NULL DEFAULT 'local_only'",
                "google_form_id": "TEXT NOT NULL DEFAULT ''",
                "google_form_url": "TEXT NOT NULL DEFAULT ''",
                "google_site_edit_url": "TEXT NOT NULL DEFAULT ''",
                "google_site_url": "TEXT NOT NULL DEFAULT ''",
                "google_question_map": "TEXT NOT NULL DEFAULT '{}'",
                "publication_error": "TEXT NOT NULL DEFAULT ''",
                "publication_attempts": "INTEGER NOT NULL DEFAULT 0",
                "publication_approved_at": "TEXT",
                "published_at": "TEXT",
                "last_synced_at": "TEXT",
                "landing_assets_path": "TEXT NOT NULL DEFAULT ''",
                "site_publication_status": "TEXT NOT NULL DEFAULT 'not_requested'",
                "site_publication_approved_at": "TEXT",
                "site_publication_error": "TEXT NOT NULL DEFAULT ''",
                "landing_revision": "INTEGER NOT NULL DEFAULT 0",
                "landing_revision_status": "TEXT NOT NULL DEFAULT ''",
                "landing_revision_instruction": "TEXT NOT NULL DEFAULT ''",
                "landing_revision_mode": "TEXT NOT NULL DEFAULT 'local'",
                "landing_revision_started_at": "TEXT",
                "landing_asset_version": "TEXT NOT NULL DEFAULT ''",
                "published_asset_version": "TEXT NOT NULL DEFAULT ''",
            }
            for column, definition in campaign_migrations.items():
                if column not in campaign_columns:
                    db.execute(f"ALTER TABLE premarketing_campaigns ADD COLUMN {column} {definition}")
            db.execute("""CREATE TABLE IF NOT EXISTS campaign_creatives (
                campaign_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'not_started', provider TEXT NOT NULL DEFAULT 'canva',
                brand_json TEXT NOT NULL DEFAULT '{}', brief_path TEXT NOT NULL DEFAULT '',
                manifest_path TEXT NOT NULL DEFAULT '', quality_report_path TEXT NOT NULL DEFAULT '',
                design_url TEXT NOT NULL DEFAULT '', image_url TEXT NOT NULL DEFAULT '',
                review_text TEXT NOT NULL DEFAULT '', quality_score INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '', approved_at TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_campaign_creatives_project ON campaign_creatives(project_id,updated_at)")
            db.execute("""CREATE TABLE IF NOT EXISTS campaign_social_shares (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, campaign_id TEXT NOT NULL,
                channel TEXT NOT NULL, label TEXT NOT NULL, mode TEXT NOT NULL,
                variant TEXT NOT NULL DEFAULT 'primary', post_text TEXT NOT NULL,
                tracking_url TEXT NOT NULL, compose_url TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'draft_ready', evidence_url TEXT NOT NULL DEFAULT '',
                open_count INTEGER NOT NULL DEFAULT 0, approved_at TEXT, last_opened_at TEXT,
                evidence_registered_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(campaign_id,channel,variant))""")
            db.execute("""CREATE INDEX IF NOT EXISTS idx_social_shares_project_campaign
                ON campaign_social_shares(project_id,campaign_id,channel)""")
            db.execute("""CREATE TABLE IF NOT EXISTS project_leads (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, campaign_id TEXT NOT NULL,
                name TEXT NOT NULL, email TEXT NOT NULL, company TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL DEFAULT '', problem TEXT NOT NULL DEFAULT '',
                timeline TEXT NOT NULL DEFAULT '', budget TEXT NOT NULL DEFAULT '',
                consent INTEGER NOT NULL DEFAULT 0, score INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'new', source TEXT NOT NULL DEFAULT 'capture_form',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_project_leads_project ON project_leads(project_id,score DESC,created_at,id)")
            lead_columns = {row[1] for row in db.execute("PRAGMA table_info(project_leads)")}
            if "external_ref" not in lead_columns:
                db.execute("ALTER TABLE project_leads ADD COLUMN external_ref TEXT NOT NULL DEFAULT ''")
            db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_project_leads_external
                ON project_leads(campaign_id,source,external_ref) WHERE external_ref<>''""")
            interrupted = [row[0] for row in db.execute(
                "SELECT project_id FROM project_missions WHERE status='running'"
            ).fetchall()]
            for project_id in interrupted:
                db.execute(
                    "UPDATE project_tasks SET status='pending',started_at=NULL,updated_at=? WHERE project_id=? AND status='running'",
                    (now, project_id),
                )
                db.execute(
                    "UPDATE project_missions SET status='paused',updated_at=? WHERE project_id=?",
                    (now, project_id),
                )
                db.execute(
                    "INSERT INTO project_events(project_id,task_id,kind,message,detail,created_at) VALUES(?,?,?,?,?,?)",
                    (project_id, None, "recovered", "再起動を検出したため安全に一時停止しました", "", now),
                )
            ensure_ocr_tables(db)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path)
        try:
            with db:
                yield db
        finally:
            db.close()

    def list_projects(self) -> list[dict]:
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("""
                SELECT p.id,p.name,p.context_text,p.workspace_path,p.created_at,p.updated_at,
                       (SELECT COUNT(1) FROM turns t WHERE t.project_id=p.id) AS turn_count,
                       (SELECT COUNT(1) FROM project_context_files f WHERE f.project_id=p.id) AS context_file_count,
                       (SELECT COALESCE(SUM(LENGTH(f.content)),0) FROM project_context_files f WHERE f.project_id=p.id) AS context_file_chars
                FROM projects p
                ORDER BY CASE WHEN p.id='default' THEN 0 ELSE 1 END,p.created_at,p.name
            """).fetchall()
        return [dict(row) for row in rows]

    def get_project(self, project_id: str = DEFAULT_PROJECT_ID) -> dict | None:
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT id,name,context_text,workspace_path,created_at,updated_at FROM projects WHERE id=?",
                (project_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_context_files(self, project_id: str, include_content: bool = False) -> list[dict]:
        fields = """id,project_id,filename,size_bytes,LENGTH(content) AS char_count,
            mime_type,file_kind,extraction_note,sha256,source,
            CASE WHEN original_data IS NULL THEN 0 ELSE 1 END AS has_original,
            created_at,updated_at"""
        if include_content:
            fields += ",content"
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                f"SELECT {fields} FROM project_context_files WHERE project_id=? "
                "ORDER BY CASE WHEN source='memo' THEN 0 ELSE 1 END,created_at,id",
                (project_id,),
            ).fetchall()
        result = [dict(row) for row in rows]
        for item in result:
            item["has_original"] = bool(item["has_original"])
        return result

    def add_context_file(self, project_id: str, filename: str, content: str, size_bytes: int,
                         data: bytes | None = None, mime_type: str = "text/markdown",
                         file_kind: str = "markdown", extraction_note: str = "",
                         sha256: str = "", source: str = "upload") -> dict:
        file_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute("""INSERT INTO project_context_files(
                id,project_id,filename,content,size_bytes,mime_type,file_kind,extraction_note,
                sha256,original_data,source,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (file_id, project_id, filename, content, size_bytes, mime_type, file_kind,
                 extraction_note, sha256, data, source, now, now))
        return next(item for item in self.list_context_files(project_id) if item["id"] == file_id)

    def get_context_file(self, project_id: str, file_id: str) -> dict | None:
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute("""SELECT id,project_id,filename,content,size_bytes,mime_type,
                file_kind,extraction_note,sha256,original_data,source,created_at,updated_at
                FROM project_context_files WHERE project_id=? AND id=?""",
                (project_id, file_id)).fetchone()
        return dict(row) if row else None

    def upsert_context_memo(self, project_id: str, filename: str, content: str) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        data = content.encode("utf-8")
        with self._connect() as db:
            row = db.execute("""SELECT id FROM project_context_files
                WHERE project_id=? AND filename=? AND source='memo'""",
                (project_id, filename)).fetchone()
            if row:
                file_id = row[0]
                db.execute("""UPDATE project_context_files SET content=?,size_bytes=?,mime_type=?,
                    file_kind='memo',extraction_note=?,original_data=?,updated_at=? WHERE id=?""",
                    (content, len(data), "text/markdown", "システムが自動更新するプロジェクト備忘録です。",
                     data, now, file_id))
            else:
                file_id = uuid.uuid4().hex
                db.execute("""INSERT INTO project_context_files(
                    id,project_id,filename,content,size_bytes,mime_type,file_kind,extraction_note,
                    sha256,original_data,source,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (file_id, project_id, filename, content, len(data), "text/markdown", "memo",
                     "システムが自動更新するプロジェクト備忘録です。", "", data, "memo", now, now))
        return next(item for item in self.list_context_files(project_id) if item["id"] == file_id)

    def delete_context_memo(self, project_id: str, filename: str) -> None:
        with self._connect() as db:
            db.execute(
                "DELETE FROM project_context_files WHERE project_id=? AND filename=? AND source='memo'",
                (project_id, filename),
            )

    def delete_context_file(self, project_id: str, file_id: str) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                "DELETE FROM project_context_files WHERE id=? AND project_id=?",
                (file_id, project_id),
            )
        return bool(cursor.rowcount)

    def context_file_chars(self, project_id: str) -> int:
        with self._connect() as db:
            row = db.execute(
                "SELECT COALESCE(SUM(LENGTH(content)),0) FROM project_context_files WHERE project_id=?",
                (project_id,),
            ).fetchone()
        return int(row[0]) if row else 0

    def context_file_bytes(self, project_id: str) -> int:
        with self._connect() as db:
            row = db.execute(
                "SELECT COALESCE(SUM(size_bytes),0) FROM project_context_files WHERE project_id=?",
                (project_id,),
            ).fetchone()
        return int(row[0]) if row else 0


    def create_project(self, name: str, context_text: str = "",
                       workspace_path: str = "") -> dict:
        project_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute(
                "INSERT INTO projects(id,name,context_text,workspace_path,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (project_id, name.strip(), context_text.strip(), workspace_path.strip(), now, now),
            )
        return self.get_project(project_id) or {}

    def update_project(self, project_id: str, name: str, context_text: str = "",
                       workspace_path: str = "") -> dict | None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE projects SET name=?,context_text=?,workspace_path=?,updated_at=? WHERE id=?",
                (name.strip(), context_text.strip(), workspace_path.strip(), now, project_id),
            )
        return self.get_project(project_id) if cursor.rowcount else None

    def delete_project(self, project_id: str) -> int:
        if project_id == DEFAULT_PROJECT_ID:
            raise ValueError("The default project cannot be deleted")
        with self._connect() as db:
            exists = db.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone()
            if not exists:
                raise KeyError(project_id)
            cursor = db.execute("DELETE FROM turns WHERE project_id=?", (project_id,))
            db.execute("DELETE FROM project_context_files WHERE project_id=?", (project_id,))
            db.execute("DELETE FROM project_events WHERE project_id=?", (project_id,))
            db.execute("DELETE FROM project_actions WHERE project_id=?", (project_id,))
            db.execute("DELETE FROM project_leads WHERE project_id=?", (project_id,))
            db.execute("DELETE FROM campaign_social_shares WHERE project_id=?", (project_id,))
            db.execute("DELETE FROM campaign_creatives WHERE project_id=?", (project_id,))
            db.execute("DELETE FROM premarketing_campaigns WHERE project_id=?", (project_id,))
            db.execute("DELETE FROM project_tasks WHERE project_id=?", (project_id,))
            db.execute("DELETE FROM project_missions WHERE project_id=?", (project_id,))
            db.execute("DELETE FROM projects WHERE id=?", (project_id,))
        return cursor.rowcount

    def get_mission(self, project_id: str, event_limit: int = 100) -> dict:
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            mission = db.execute(
                "SELECT * FROM project_missions WHERE project_id=?", (project_id,)
            ).fetchone()
            tasks = db.execute(
                "SELECT * FROM project_tasks WHERE project_id=? ORDER BY position,id", (project_id,)
            ).fetchall()
            events = db.execute(
                "SELECT * FROM project_events WHERE project_id=? ORDER BY id DESC LIMIT ?",
                (project_id, max(1, min(event_limit, 500))),
            ).fetchall()
            instruction_events = db.execute(
                """SELECT * FROM (
                    SELECT * FROM project_events
                    WHERE project_id=? AND kind IN ('mission_instruction_user','mission_instruction_system')
                    ORDER BY id DESC LIMIT 200
                ) ORDER BY id""",
                (project_id,),
            ).fetchall()
        if mission:
            result = dict(mission)
            result["allow_external_ai"] = bool(result["allow_external_ai"])
            try:
                result["external_providers"] = json.loads(result["external_providers"])
            except (TypeError, json.JSONDecodeError):
                result["external_providers"] = []
            try:
                result["plan_reviews"] = json.loads(result.get("plan_reviews") or "[]")
            except (TypeError, json.JSONDecodeError):
                result["plan_reviews"] = []
            result["max_parallel_tasks"] = max(1, min(int(result.get("max_parallel_tasks", 2)), 4))
        else:
            result = {
                "project_id": project_id, "goal": "", "success_criteria": "",
                "constraints_text": "", "status": "draft", "plan_summary": "",
                "final_report": "", "allow_external_ai": False,
                "external_providers": [], "plan_version": 0, "plan_reviews": [],
                "max_parallel_tasks": 2,
                "created_at": None, "updated_at": None,
            }
        result["tasks"] = []
        for row in tasks:
            task = dict(row)
            try:
                task["depends_on"] = json.loads(task.get("depends_on") or "[]")
            except (TypeError, json.JSONDecodeError):
                task["depends_on"] = []
            result["tasks"].append(task)
        result["events"] = [dict(row) for row in events]
        result["instruction_messages"] = [dict(row) for row in instruction_events]
        total = len(tasks)
        completed = sum(1 for row in tasks if row["status"] in {"completed", "skipped"})
        result["progress"] = {
            "completed": completed, "total": total,
            "percent": round(completed * 100 / total) if total else 0,
        }
        return result

    def save_mission(self, project_id: str, goal: str, success_criteria: str,
                     constraints_text: str, allow_external_ai: bool,
                     external_providers: list[str], max_parallel_tasks: int = 2,
                     reset_plan: bool = False) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        providers = json.dumps(list(dict.fromkeys(external_providers)), ensure_ascii=False)
        with self._connect() as db:
            exists = db.execute(
                "SELECT 1 FROM project_missions WHERE project_id=?", (project_id,)
            ).fetchone()
            if exists:
                db.execute("""UPDATE project_missions SET goal=?,success_criteria=?,constraints_text=?,
                    allow_external_ai=?,external_providers=?,max_parallel_tasks=?,updated_at=? WHERE project_id=?""",
                    (goal.strip(), success_criteria.strip(), constraints_text.strip(),
                     int(allow_external_ai), providers, max(1, min(max_parallel_tasks, 4)), now, project_id))
            else:
                db.execute("""INSERT INTO project_missions(
                    project_id,goal,success_criteria,constraints_text,status,allow_external_ai,
                    external_providers,max_parallel_tasks,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (project_id, goal.strip(), success_criteria.strip(), constraints_text.strip(),
                     "draft", int(allow_external_ai), providers, max(1, min(max_parallel_tasks, 4)), now, now))
            if reset_plan:
                db.execute("DELETE FROM project_tasks WHERE project_id=?", (project_id,))
                db.execute("""UPDATE project_missions SET status='draft',plan_summary='',final_report='',
                    updated_at=? WHERE project_id=?""", (now, project_id))
                db.execute("""INSERT INTO project_events(project_id,task_id,kind,message,detail,created_at)
                    VALUES(?,?,?,?,?,?)""", (project_id, None, "goal_updated",
                    "目標が変更されたため計画をリセットしました", "", now))
            elif not exists:
                db.execute("""INSERT INTO project_events(project_id,task_id,kind,message,detail,created_at)
                    VALUES(?,?,?,?,?,?)""", (project_id, None, "goal_saved", "目標を保存しました", "", now))
        return self.get_mission(project_id)

    def set_plan_reviews(self, project_id: str, reviews: list[dict]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute(
                "UPDATE project_missions SET plan_reviews=?,updated_at=? WHERE project_id=?",
                (json.dumps(reviews, ensure_ascii=False), now, project_id),
            )

    def set_task_agent(self, task_id: str, agent_label: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE project_tasks SET agent_label=? WHERE id=?", (agent_label, task_id))

    def replace_plan(self, project_id: str, summary: str, tasks: list[dict], task_extensions: dict | None = None, expected_version: int | None = None) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        if task_extensions is not None:
            from app.upgrade_store import UpgradeStore, canonical, digest
            from app.document_contracts import validate_extension
            if set(task_extensions)!={t['task_key'] for t in tasks}:
                raise ValueError('全タスクの拡張契約が必要です')
            for extension in task_extensions.values():validate_extension(extension)
            UpgradeStore(self.path)
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            if expected_version is not None:
                db.execute('BEGIN IMMEDIATE')
                current=db.execute('SELECT plan_version,status FROM project_missions WHERE project_id=?',(project_id,)).fetchone()
                if not current or current['plan_version']!=expected_version or current['status']=='running' or db.execute("SELECT 1 FROM project_tasks WHERE project_id=? AND status='running'",(project_id,)).fetchone():
                    raise ValueError('再作成中に計画または実行状態が変更されました')
            previous = {
                row['task_key']: dict(row)
                for row in db.execute(
                    'SELECT * FROM project_tasks WHERE project_id=?',
                    (project_id,),
                ).fetchall()
            }
            db.execute("DELETE FROM project_tasks WHERE project_id=?", (project_id,))
            for position, task in enumerate(tasks, 1):
                db.execute("""INSERT INTO project_tasks(
                    id,project_id,position,title,description,acceptance_criteria,mode,status,
                    task_key,depends_on,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (uuid.uuid4().hex, project_id, position, task["title"],
                     task.get("description", ""), task.get("acceptance_criteria", ""),
                     task.get("mode", "local"), "pending", task.get("task_key", f"task_{position}"),
                     json.dumps(task.get("depends_on", [])), now, now))
                task_key = task.get('task_key', f'task_{position}')
                old = previous.get(task_key)
                try:
                    old_contract = json.loads(old['acceptance_criteria']) if old else {}
                    new_contract = json.loads(task.get('acceptance_criteria', ''))
                    old_criterion = old_contract.get('criterion')
                    new_criterion = new_contract.get('criterion')
                    requirements_match = (
                        old_contract.get('action_requirements', [])
                        == new_contract.get('action_requirements', [])
                    )
                except (TypeError, ValueError):
                    old_criterion = new_criterion = None
                    requirements_match = False
                preserve = (
                    old
                    and task_key != 'final_verification'
                    and old['status'] in {'completed', 'skipped'}
                    and old_criterion == new_criterion
                    and requirements_match
                    and old['title'] == task['title']
                    and old['description'] == task.get('description', '')
                    and old['mode'] == task.get('mode', 'local')
                    and old_contract.get('outputs', []) == new_contract.get('outputs', [])
                    and old_contract.get('public_web_research')
                    == new_contract.get('public_web_research')
                )
                if preserve:
                    db.execute(
                        '''UPDATE project_tasks SET id=?,status=?,result=?,error=?,
                        attempts=?,agent_label=?,started_at=?,completed_at=?,created_at=?
                        WHERE project_id=? AND task_key=?''',
                        (
                            old['id'], old['status'], old['result'], old['error'],
                            old['attempts'], old['agent_label'], old['started_at'],
                            old['completed_at'], old['created_at'], project_id, task_key,
                        ),
                    )
            changed = True
            while changed:
                changed = False
                rows = [
                    dict(row) for row in db.execute(
                        'SELECT task_key,status,depends_on FROM project_tasks WHERE project_id=?',
                        (project_id,),
                    ).fetchall()
                ]
                statuses = {row['task_key']: row['status'] for row in rows}
                for row in rows:
                    if row['status'] not in {'completed', 'skipped'}:
                        continue
                    dependencies = json.loads(row['depends_on'] or '[]')
                    if any(statuses.get(dep) not in {'completed', 'skipped'} for dep in dependencies):
                        db.execute(
                            '''UPDATE project_tasks SET status='pending',
                            error='上流工程の変更により再確認が必要です',
                            started_at=NULL,completed_at=NULL,updated_at=?
                            WHERE project_id=? AND task_key=?''',
                            (now, project_id, row['task_key']),
                        )
                        changed = True
            db.execute("""UPDATE project_missions SET status='planning',plan_summary=?,final_report='',
                plan_version=plan_version+1,updated_at=? WHERE project_id=?""",
                (summary.strip(), now, project_id))
            if task_extensions is not None:
                db.execute("UPDATE project_missions SET plan_reviews='[]' WHERE project_id=?",(project_id,))
                version=db.execute('SELECT plan_version FROM project_missions WHERE project_id=?',(project_id,)).fetchone()[0]
                for row in db.execute('SELECT id,task_key,acceptance_criteria FROM project_tasks WHERE project_id=?',(project_id,)).fetchall():
                    db.execute('INSERT INTO task_contract_extensions VALUES(?,?,?,?,?,?)',(project_id,row['id'],version,digest(row['acceptance_criteria']),canonical(task_extensions[row['task_key']]),now))
            db.execute("""INSERT INTO project_events(project_id,task_id,kind,message,detail,created_at)
                VALUES(?,?,?,?,?,?)""", (project_id, None, "plan_generated",
                f"実行計画を生成しました（{len(tasks)}タスク）", summary[:2000], now))
        return self.get_mission(project_id)

    def set_mission_status(self, project_id: str, status: str,
                           message: str | None = None, kind: str = "status") -> dict:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute(
                "UPDATE project_missions SET status=?,updated_at=? WHERE project_id=?",
                (status, now, project_id),
            )
            if message:
                db.execute("""INSERT INTO project_events(project_id,task_id,kind,message,detail,created_at)
                    VALUES(?,?,?,?,?,?)""", (project_id, None, kind, message, "", now))
        try:
            from app.goal_state_machine import observe_legacy_status
            observe_legacy_status(self.path, project_id, status, kind)
        except Exception as exc:
            LOGGER.warning(
                "goal_completion: observe_legacy_status failed: %s", exc, exc_info=True
            )
        return self.get_mission(project_id)

    def update_task(self, task_id: str, status: str, result: str = "", error: str = "") -> None:
        now = datetime.now(timezone.utc).isoformat()
        started_at = now if status == "running" else None
        completed_at = now if status in {"completed", "failed", "skipped"} else None
        with self._connect() as db:
            row = db.execute("SELECT 1 FROM project_tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise KeyError(task_id)
            if status == "running":
                db.execute("""UPDATE project_tasks SET status=?,result='',error='',attempts=attempts+1,
                    started_at=?,completed_at=NULL,updated_at=? WHERE id=?""",
                    (status, started_at, now, task_id))
            else:
                db.execute("""UPDATE project_tasks SET status=?,result=?,error=?,completed_at=?,
                    started_at=CASE WHEN ?='pending' THEN NULL ELSE started_at END,updated_at=? WHERE id=?""",
                    (status, result, error, completed_at, status, now, task_id))

    def add_event(self, project_id: str, kind: str, message: str,
                  task_id: str | None = None, detail: str = "") -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            cursor = db.execute("""INSERT INTO project_events(
                project_id,task_id,kind,message,detail,created_at) VALUES(?,?,?,?,?,?)""",
                (project_id, task_id, kind, message, detail, now))
        return int(cursor.lastrowid)

    def create_action(self, project_id: str, kind: str, target: str, content: str,
                      task_id: str | None = None) -> dict:
        action_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute("""INSERT INTO project_actions(
                id,project_id,task_id,kind,target,content,status,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (action_id, project_id, task_id, kind, target, content,
                 "pending_approval", now, now))
        return self.get_action(project_id, action_id) or {}

    def list_actions(self, project_id: str) -> list[dict]:
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT * FROM project_actions WHERE project_id=? ORDER BY created_at,id",
                (project_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_action(self, project_id: str, action_id: str) -> dict | None:
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT * FROM project_actions WHERE project_id=? AND id=?",
                (project_id, action_id),
            ).fetchone()
        return dict(row) if row else None

    def update_action(self, project_id: str, action_id: str, status: str,
                      evidence: str = "", error: str = "") -> dict:
        if status not in {"pending_approval", "approved", "executed", "failed", "cancelled"}:
            raise ValueError("Invalid external action status")
        now = datetime.now(timezone.utc).isoformat()
        approved_at = now if status == "approved" else None
        executed_at = now if status in {"executed", "failed"} else None
        with self._connect() as db:
            current = db.execute(
                "SELECT status FROM project_actions WHERE project_id=? AND id=?",
                (project_id, action_id),
            ).fetchone()
            if not current:
                raise KeyError(action_id)
            db.execute("""UPDATE project_actions SET status=?,evidence=?,error=?,
                approved_at=COALESCE(?,approved_at),executed_at=COALESCE(?,executed_at),
                updated_at=? WHERE project_id=? AND id=?""",
                (status, evidence, error, approved_at, executed_at, now,
                 project_id, action_id))
        return self.get_action(project_id, action_id) or {}

    def create_campaign(self, project_id: str, title: str, audience: str,
                        offer: str, call_to_action: str, assets_path: str) -> dict:
        campaign_id, token = uuid.uuid4().hex, uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute("""INSERT INTO premarketing_campaigns(
                id,project_id,public_token,title,audience,offer,call_to_action,status,
                assets_path,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (campaign_id, project_id, token, title, audience, offer,
                 call_to_action, "active", assets_path, now, now))
        return self.get_campaign_by_token(token) or {}

    def list_campaigns(self, project_id: str) -> list[dict]:
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT * FROM premarketing_campaigns WHERE project_id=? ORDER BY created_at DESC",
                (project_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_campaign_by_token(self, token: str) -> dict | None:
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT * FROM premarketing_campaigns WHERE public_token=?", (token,),
            ).fetchone()
        return dict(row) if row else None

    def get_campaign(self, project_id: str, campaign_id: str) -> dict | None:
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT * FROM premarketing_campaigns WHERE project_id=? AND id=?",
                (project_id, campaign_id),
            ).fetchone()
        return dict(row) if row else None

    def update_campaign_publication(self, project_id: str, campaign_id: str,
                                    status: str, **fields) -> dict:
        allowed_statuses = {
            "local_only", "awaiting_approval", "approved", "publishing_form",
            "awaiting_site", "published", "monitoring", "reauth_required", "failed",
        }
        if status not in allowed_statuses:
            raise ValueError("Invalid campaign publication status")
        allowed_fields = {
            "google_form_id", "google_form_url", "google_site_edit_url",
            "google_site_url", "google_question_map", "publication_error",
            "publication_attempts", "publication_approved_at", "published_at",
            "last_synced_at",
        }
        updates = {key: value for key, value in fields.items() if key in allowed_fields}
        updates["publication_status"] = status
        updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        assignments = ",".join(f"{key}=?" for key in updates)
        with self._connect() as db:
            cursor = db.execute(
                f"UPDATE premarketing_campaigns SET {assignments} WHERE project_id=? AND id=?",
                (*updates.values(), project_id, campaign_id),
            )
            if not cursor.rowcount:
                raise KeyError(campaign_id)
        return self.get_campaign(project_id, campaign_id) or {}

    def claim_form_publication(self, project_id: str, campaign_id: str) -> dict:
        """Atomically claim the one approved remote create attempt."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            cursor = db.execute("""UPDATE premarketing_campaigns
                SET publication_status='publishing_form',
                    publication_attempts=publication_attempts+1,
                    publication_error='',updated_at=?
                WHERE project_id=? AND id=? AND publication_status='approved'
                  AND google_form_id='' AND publication_approved_at IS NOT NULL""",
                (now, project_id, campaign_id))
            if cursor.rowcount != 1:
                raise ValueError("承認済みの新規フォーム作成だけを開始できます")
        return self.get_campaign(project_id, campaign_id) or {}

    def update_campaign_assets(self, campaign_id: str, assets_path: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE premarketing_campaigns SET assets_path=?,updated_at=? WHERE id=?",
                (assets_path, now, campaign_id),
            )
            if not cursor.rowcount:
                raise KeyError(campaign_id)

    def get_campaign_creative(self, project_id: str, campaign_id: str) -> dict | None:
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT * FROM campaign_creatives WHERE project_id=? AND campaign_id=?",
                (project_id, campaign_id),
            ).fetchone()
        return dict(row) if row else None

    def update_campaign_creative(self, project_id: str, campaign_id: str,
                                 status: str, **fields) -> dict:
        allowed_statuses = {"not_started", "brief_ready", "reviewed", "approved", "failed"}
        if status not in allowed_statuses:
            raise ValueError("Invalid creative status")
        allowed_fields = {
            "provider", "brand_json", "brief_path", "manifest_path",
            "quality_report_path", "design_url", "image_url", "review_text",
            "quality_score", "error", "approved_at",
        }
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            exists = db.execute(
                "SELECT 1 FROM premarketing_campaigns WHERE project_id=? AND id=?",
                (project_id, campaign_id),
            ).fetchone()
            if not exists:
                raise KeyError(campaign_id)
            db.execute("""INSERT OR IGNORE INTO campaign_creatives(
                campaign_id,project_id,status,created_at,updated_at
                ) VALUES(?,?,?,?,?)""", (campaign_id, project_id, status, now, now))
            updates = {key: value for key, value in fields.items() if key in allowed_fields}
            updates.update({"status": status, "updated_at": now})
            assignments = ",".join(f"{key}=?" for key in updates)
            db.execute(
                f"UPDATE campaign_creatives SET {assignments} WHERE project_id=? AND campaign_id=?",
                (*updates.values(), project_id, campaign_id),
            )
        return self.get_campaign_creative(project_id, campaign_id) or {}

    def update_campaign_site(self, project_id: str, campaign_id: str,
                             status: str, **fields) -> dict:
        allowed_statuses = {
            "not_requested", "draft_ready", "awaiting_approval", "approved",
            "published", "failed", "reauth_required",
        }
        if status not in allowed_statuses:
            raise ValueError("Invalid Google Sites publication status")
        allowed_fields = {
            "landing_assets_path", "site_publication_approved_at",
            "site_publication_error", "google_site_edit_url", "google_site_url",
            "published_at", "landing_revision", "landing_revision_status",
            "landing_revision_instruction", "landing_revision_mode",
            "landing_revision_started_at", "landing_asset_version",
            "published_asset_version",
        }
        updates = {key: value for key, value in fields.items() if key in allowed_fields}
        updates["site_publication_status"] = status
        updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        assignments = ",".join(f"{key}=?" for key in updates)
        with self._connect() as db:
            cursor = db.execute(
                f"UPDATE premarketing_campaigns SET {assignments} WHERE project_id=? AND id=?",
                (*updates.values(), project_id, campaign_id),
            )
            if not cursor.rowcount:
                raise KeyError(campaign_id)
        return self.get_campaign(project_id, campaign_id) or {}

    def save_social_drafts(self, project_id: str, campaign_id: str,
                           items: list[dict]) -> list[dict]:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            campaign = db.execute(
                "SELECT 1 FROM premarketing_campaigns WHERE project_id=? AND id=?",
                (project_id, campaign_id),
            ).fetchone()
            if not campaign:
                raise KeyError(campaign_id)
            for item in items:
                db.execute("""INSERT INTO campaign_social_shares(
                    id,project_id,campaign_id,channel,label,mode,variant,post_text,
                    tracking_url,compose_url,status,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(campaign_id,channel,variant) DO UPDATE SET
                    label=excluded.label,mode=excluded.mode,post_text=excluded.post_text,
                    tracking_url=excluded.tracking_url,compose_url=excluded.compose_url,
                    status='draft_ready',evidence_url='',approved_at=NULL,last_opened_at=NULL,
                    evidence_registered_at=NULL,open_count=0,updated_at=excluded.updated_at
                    WHERE campaign_social_shares.status<>'evidence_registered'""",
                    (uuid.uuid4().hex, project_id, campaign_id, item["channel"],
                     item["label"], item["mode"], item.get("variant", "primary"),
                     item["post_text"], item["tracking_url"], item.get("compose_url", ""),
                     "draft_ready", now, now))
        return self.list_social_shares(project_id, campaign_id)

    def list_social_shares(self, project_id: str, campaign_id: str) -> list[dict]:
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("""SELECT * FROM campaign_social_shares
                WHERE project_id=? AND campaign_id=? ORDER BY channel,variant""",
                (project_id, campaign_id)).fetchall()
        return [dict(row) for row in rows]

    def get_social_share(self, project_id: str, campaign_id: str,
                         channel: str, variant: str = "primary") -> dict | None:
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute("""SELECT * FROM campaign_social_shares
                WHERE project_id=? AND campaign_id=? AND channel=? AND variant=?""",
                (project_id, campaign_id, channel, variant)).fetchone()
        return dict(row) if row else None

    def update_social_campaign_status(self, project_id: str, campaign_id: str,
                                      current_status: str, new_status: str) -> list[dict]:
        if new_status not in {"awaiting_approval", "approved"}:
            raise ValueError("Invalid social campaign status")
        now = datetime.now(timezone.utc).isoformat()
        approved_at = now if new_status == "approved" else None
        with self._connect() as db:
            cursor = db.execute("""UPDATE campaign_social_shares SET status=?,
                approved_at=COALESCE(?,approved_at),updated_at=?
                WHERE project_id=? AND campaign_id=? AND status=?""",
                (new_status, approved_at, now, project_id, campaign_id, current_status))
            if not cursor.rowcount:
                raise ValueError("No social drafts in the required state")
        return self.list_social_shares(project_id, campaign_id)

    def mark_social_opened(self, project_id: str, campaign_id: str,
                           channel: str, variant: str = "primary") -> dict:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            cursor = db.execute("""UPDATE campaign_social_shares SET
                status=CASE WHEN status='evidence_registered' THEN status ELSE 'composer_opened' END,
                open_count=open_count+1,last_opened_at=?,updated_at=?
                WHERE project_id=? AND campaign_id=? AND channel=? AND variant=?
                AND status IN ('approved','composer_opened','evidence_registered')""",
                (now, now, project_id, campaign_id, channel, variant))
            if not cursor.rowcount:
                raise ValueError("Social share is not approved")
        return self.get_social_share(project_id, campaign_id, channel, variant) or {}

    def register_social_evidence(self, project_id: str, campaign_id: str,
                                 channel: str, evidence_url: str,
                                 variant: str = "primary") -> dict:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("""SELECT status,evidence_url FROM campaign_social_shares
                WHERE project_id=? AND campaign_id=? AND channel=? AND variant=?""",
                (project_id, campaign_id, channel, variant)).fetchone()
            if not current or current[0] not in ('approved', 'composer_opened', 'evidence_registered'):
                raise ValueError("Social share is not approved")
            if current[0] == 'evidence_registered':
                if current[1] != evidence_url:
                    raise ValueError("公開投稿URLは登録済みです。異なるURLで上書きできません")
            else:
                db.execute("""UPDATE campaign_social_shares SET status='evidence_registered',
                    evidence_url=?,evidence_registered_at=?,updated_at=?
                    WHERE project_id=? AND campaign_id=? AND channel=? AND variant=?""",
                    (evidence_url, now, now, project_id, campaign_id, channel, variant))
        return self.get_social_share(project_id, campaign_id, channel, variant) or {}

    def create_lead(self, campaign: dict, name: str, email: str, company: str,
                    role: str, problem: str, timeline: str, budget: str,
                    consent: bool, score: int, source: str = "capture_form",
                    external_ref: str = "") -> dict:
        if external_ref:
            with self._connect() as db:
                db.row_factory = sqlite3.Row
                existing = db.execute(
                    "SELECT * FROM project_leads WHERE campaign_id=? AND source=? AND external_ref=?",
                    (campaign["id"], source, external_ref),
                ).fetchone()
            if existing:
                result = dict(existing)
                result["consent"] = bool(result["consent"])
                return result
        lead_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute("""INSERT INTO project_leads(
                id,project_id,campaign_id,name,email,company,role,problem,timeline,
                budget,consent,score,status,source,created_at,updated_at,external_ref
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (lead_id, campaign["project_id"], campaign["id"], name, email,
                 company, role, problem, timeline, budget, int(consent),
                 max(0, min(int(score), 100)), "qualified" if score >= 60 else "new",
                 source, now, now, external_ref))
        return self.get_lead(campaign["project_id"], lead_id) or {}

    def list_leads(self, project_id: str) -> list[dict]:
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT * FROM project_leads WHERE project_id=? ORDER BY score DESC,created_at DESC",
                (project_id,),
            ).fetchall()
        result = [dict(row) for row in rows]
        for item in result:
            item["consent"] = bool(item["consent"])
        return result

    def add_mission_instruction(self, project_id: str, instruction: str, *, expected_plan_version: int | None = None) -> dict:
        """Persist an operator instruction without discarding the current plan."""
        text = instruction.strip()
        if not text:
            raise ValueError("追加指示を入力してください")
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.row_factory = sqlite3.Row
            mission = db.execute(
                "SELECT * FROM project_missions WHERE project_id=?", (project_id,)
            ).fetchone()
            if not mission or not str(mission["goal"] or "").strip():
                raise ValueError("先にプロジェクト目標を保存してください")
            if expected_plan_version is not None and (mission['plan_version'] != expected_plan_version or mission['status'] == 'running'):
                raise ValueError('計画版の競合、または計画が実行中です')
            current = str(mission["constraints_text"] or "").strip()
            addition = f"## 追加指示\n{text}"
            merged = f"{current}\n\n{addition}".strip() if current else addition
            if len(merged) > 20000:
                raise ValueError("制約・追加指示の合計が20000文字を超えます。既存内容を整理してください")
            status = str(mission["status"] or "draft")
            if status == "running":
                reply = "追加指示を記録しました。実行中の工程には即時反映されず、次の工程から処理条件として参照します。"
            elif status in {"completed", "cancelled"}:
                reply = "追加指示を記録しました。計画は終了済みのため、反映して実行するには計画を再生成してください。"
            else:
                reply = "追加指示を記録し、現在の進捗を保持したまま今後の処理条件へ反映しました。計画構成を変える場合は「AIで計画生成」を実行してください。"
            db.execute(
                "UPDATE project_missions SET constraints_text=?,updated_at=? WHERE project_id=?",
                (merged, now, project_id),
            )
            db.execute(
                """INSERT INTO project_events(project_id,task_id,kind,message,detail,created_at)
                    VALUES(?,?,?,?,?,?)""",
                (project_id, None, "mission_instruction_user", text, "", now),
            )
            db.execute(
                """INSERT INTO project_events(project_id,task_id,kind,message,detail,created_at)
                    VALUES(?,?,?,?,?,?)""",
                (project_id, None, "mission_instruction_system", reply, "", now),
            )
        return self.get_mission(project_id)

    def get_lead(self, project_id: str, lead_id: str) -> dict | None:
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT * FROM project_leads WHERE project_id=? AND id=?",
                (project_id, lead_id),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["consent"] = bool(result["consent"])
        return result

    def get_lead_by_external_ref(self, campaign_id: str, source: str,
                                 external_ref: str) -> dict | None:
        if not external_ref:
            return None
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT * FROM project_leads WHERE campaign_id=? AND source=? AND external_ref=?",
                (campaign_id, source, external_ref),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["consent"] = bool(result["consent"])
        return result

    def update_lead_status(self, project_id: str, lead_id: str, status: str) -> dict:
        if status not in {"new", "qualified", "contact_queued", "contacted", "converted", "disqualified"}:
            raise ValueError("Invalid lead status")
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE project_leads SET status=?,updated_at=? WHERE project_id=? AND id=?",
                (status, now, project_id, lead_id),
            )
            if not cursor.rowcount:
                raise KeyError(lead_id)
        return self.get_lead(project_id, lead_id) or {}

    def set_final_report(self, project_id: str, report: str,
                         status: str = "completed", message: str | None = None) -> dict:
        if status not in {"completed", "failed"}:
            raise ValueError("Final report status must be completed or failed")
        now = datetime.now(timezone.utc).isoformat()
        kind = "completed" if status == "completed" else "goal_verification_failed"
        final_message = message or (
            "すべてのタスクと成果物を検証し、最終報告を作成しました"
            if status == "completed"
            else "最終目標の未達を検出し、未完了レポートを作成しました"
        )
        with self._connect() as db:
            db.execute("""UPDATE project_missions SET final_report=?,status=?,updated_at=?
                WHERE project_id=?""", (report, status, now, project_id))
            db.execute("""INSERT INTO project_events(project_id,task_id,kind,message,detail,created_at)
                VALUES(?,?,?,?,?,?)""", (project_id, None, kind,
                final_message, report[:2000], now))
        return self.get_mission(project_id)

    def append_plan_tasks(self, project_id: str, tasks: list[dict], summary_note: str = "") -> dict:
        if not tasks:
            return self.get_mission(project_id)
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            row = db.execute(
                "SELECT COALESCE(MAX(position),0) AS position FROM project_tasks WHERE project_id=?",
                (project_id,),
            ).fetchone()
            start = int(row[0] if row else 0)
            for offset, task in enumerate(tasks, 1):
                position = start + offset
                db.execute("""INSERT INTO project_tasks(
                    id,project_id,position,title,description,acceptance_criteria,mode,status,
                    task_key,depends_on,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (uuid.uuid4().hex, project_id, position, task["title"],
                     task.get("description", ""), task.get("acceptance_criteria", ""),
                     task.get("mode", "local"), "pending", task.get("task_key", f"task_{position}"),
                     json.dumps(task.get("depends_on", [])), now, now))
            db.execute("""UPDATE project_missions SET status='running',
                plan_summary=CASE WHEN ?='' THEN plan_summary ELSE plan_summary || '\n\n' || ? END,
                plan_version=plan_version+1,updated_at=? WHERE project_id=?""",
                (summary_note.strip(), summary_note.strip(), now, project_id))
        return self.get_mission(project_id)

    def save(self, session_id: str, user: str, assistant: str, model: str,
             latency_ms: float, interrupted: bool = False,
             project_id: str = DEFAULT_PROJECT_ID) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO turns(session_id,created_at,user_text,assistant_text,model,latency_ms,interrupted,project_id) VALUES(?,?,?,?,?,?,?,?)",
                (session_id, datetime.now(timezone.utc).isoformat(), user, assistant, model, latency_ms, int(interrupted), project_id),
            )

    def recent(self, session_id: str, limit: int = 8,
               project_id: str = DEFAULT_PROJECT_ID) -> list[dict[str, str]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT user_text,assistant_text FROM turns WHERE session_id=? AND project_id=? ORDER BY id DESC LIMIT ?",
                (session_id, project_id, limit),
            ).fetchall()
        messages: list[dict[str, str]] = []
        for user, assistant in reversed(rows):
            messages.extend(({"role": "user", "content": user}, {"role": "assistant", "content": assistant}))
        return messages

    def context_text(self, session_id: str, limit: int = 12, max_chars: int = 24000,
                     project_id: str = DEFAULT_PROJECT_ID) -> str:
        messages = self.recent(session_id, limit, project_id)
        lines = [
            f"{'ユーザー' if message['role'] == 'user' else 'フロントAI'}: {message['content']}"
            for message in messages
        ]
        text = "\n\n".join(lines)
        if len(text) <= max_chars:
            return text
        return "[古いコンテキストは上限により省略]\n" + text[-max_chars:]

    def turn_count(self, session_id: str, project_id: str = DEFAULT_PROJECT_ID) -> int:
        with self._connect() as db:
            row = db.execute(
                "SELECT COUNT(*) FROM turns WHERE session_id=? AND project_id=?",
                (session_id, project_id),
            ).fetchone()
        return int(row[0]) if row else 0

    def clear(self, session_id: str, project_id: str = DEFAULT_PROJECT_ID) -> int:
        with self._connect() as db:
            cursor = db.execute(
                "DELETE FROM turns WHERE session_id=? AND project_id=?",
                (session_id, project_id),
            )
        return cursor.rowcount
