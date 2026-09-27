"""Additive, project-scoped audit storage. Never modifies legacy task records."""
import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    raw = value if isinstance(value, bytes) else str(value).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


AUDIT_TABLES = ('capability_checks', 'failure_records', 'presentation_manifests',
                'validation_runs', 'recovery_attempts', 'claims', 'claim_evidence_links')


class UpgradeStore:
    def __init__(self, path):
        self.path = path
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS upgrade_schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)')
            db.execute('''CREATE TABLE IF NOT EXISTS task_attempts(
                attempt_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, task_id TEXT NOT NULL,
                plan_version INTEGER NOT NULL, contract_hash TEXT NOT NULL, policy_version TEXT NOT NULL,
                mode TEXT NOT NULL CHECK(mode IN ('shadow','enforce')), state TEXT NOT NULL,
                started_at TEXT NOT NULL, ended_at TEXT, payload TEXT NOT NULL,
                UNIQUE(project_id, attempt_id))''')
            db.execute('''CREATE TABLE IF NOT EXISTS capability_definitions(
                capability_id TEXT NOT NULL, definition_version TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(capability_id, definition_version))''')
            for table in AUDIT_TABLES:
                db.execute(f'''CREATE TABLE IF NOT EXISTS {table}(
                    record_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
                    created_at TEXT NOT NULL, payload TEXT NOT NULL,
                    FOREIGN KEY(project_id,attempt_id) REFERENCES task_attempts(project_id,attempt_id))''')
                db.execute(f'CREATE INDEX IF NOT EXISTS idx_upgrade_{table} ON {table}(project_id,attempt_id)')
            db.execute('''CREATE TABLE IF NOT EXISTS source_versions(
                version_id TEXT NOT NULL, project_id TEXT NOT NULL, source_doc_id TEXT NOT NULL,
                payload TEXT NOT NULL, PRIMARY KEY(project_id,version_id))''')
            db.execute('''CREATE TABLE IF NOT EXISTS source_units(
                unit_id TEXT NOT NULL, project_id TEXT NOT NULL, version_id TEXT NOT NULL,
                payload TEXT NOT NULL, PRIMARY KEY(project_id,unit_id),
                FOREIGN KEY(project_id,version_id) REFERENCES source_versions(project_id,version_id))''')
            db.execute('''CREATE TABLE IF NOT EXISTS task_contract_extensions(
                project_id TEXT NOT NULL, task_id TEXT NOT NULL, plan_version INTEGER NOT NULL,
                legacy_hash TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(project_id,task_id,plan_version,legacy_hash))''')
            db.execute('INSERT OR IGNORE INTO upgrade_schema_migrations VALUES(1,?)', (utcnow(),))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def begin(self, project_id, task, plan_version, mode, policy):
        attempt = uuid.uuid4().hex
        contract_hash = digest(task.get('acceptance_criteria', ''))
        with self.connect() as db:
            db.execute('INSERT INTO task_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (attempt, project_id, task['id'], plan_version, contract_hash,
                        'capability-upgrade/v1', mode, 'running', utcnow(), None, canonical(policy)))
        return attempt

    def finish(self, project_id, attempt_id, state):
        if state not in {'completed', 'failed', 'needs_review', 'cancelled', 'observed'}:
            raise ValueError('Invalid attempt state')
        with self.connect() as db:
            db.execute('UPDATE task_attempts SET state=?,ended_at=? WHERE project_id=? AND attempt_id=?',
                       (state, utcnow(), project_id, attempt_id))

    def record(self, table, project_id, attempt_id, payload):
        if table not in AUDIT_TABLES:
            raise ValueError('Unknown audit table')
        record_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute(f'INSERT INTO {table} VALUES(?,?,?,?,?)',
                       (record_id, project_id, attempt_id, utcnow(), canonical(payload)))
        return record_id

    def records(self, table, project_id, attempt_id):
        if table not in AUDIT_TABLES:
            raise ValueError('Unknown audit table')
        with self.connect() as db:
            return [json.loads(row['payload']) for row in db.execute(
                f'SELECT payload FROM {table} WHERE project_id=? AND attempt_id=? ORDER BY created_at,record_id',
                (project_id, attempt_id))]

    def save_source(self, project_id, version, units):
        if any(u.get('project_id') != project_id or u.get('version_id') != version['version_id'] for u in units):
            raise ValueError('Source unit scope mismatch')
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO source_versions VALUES(?,?,?,?)',
                       (version['version_id'], project_id, version['source_doc_id'], canonical(version)))
            for unit in units:
                db.execute('INSERT OR IGNORE INTO source_units VALUES(?,?,?,?)',
                           (unit['unit_id'], project_id, version['version_id'], canonical(unit)))

    def read_units(self, project_id, version_id, selectors, budget=12000):
        if not isinstance(selectors, list) or not 1 <= len(selectors) <= 16 or not 1 <= budget <= 20000:
            raise ValueError('Invalid read bounds')
        with self.connect() as db:
            version = db.execute('SELECT 1 FROM source_versions WHERE project_id=? AND version_id=?',
                                 (project_id, version_id)).fetchone()
            if not version:
                raise ValueError('Source version not available in this project')
            units = {row['unit_id']: json.loads(row['payload']) for row in db.execute(
                'SELECT unit_id,payload FROM source_units WHERE project_id=? AND version_id=?', (project_id, version_id))}
        result, omitted, used = [], [], 0
        for selector in selectors:
            if not isinstance(selector, str) or selector not in units:
                raise ValueError('Unknown source unit')
            unit = units[selector]
            if used + len(unit['content']) > budget:
                omitted.append(selector)
                continue
            result.append(unit)
            used += len(unit['content'])
        return {'units': result, 'omitted': omitted}

    def extension(self, project_id, task, plan_version):
        with self.connect() as db:
            rows = db.execute('SELECT legacy_hash,payload FROM task_contract_extensions WHERE project_id=? AND task_id=? AND plan_version=?',
                              (project_id, task['id'], plan_version)).fetchall()
        if not rows:
            return None
        expected = digest(task.get('acceptance_criteria', ''))
        exact = [r for r in rows if r['legacy_hash'] == expected]
        if not exact:
            raise ValueError('unsupported_contract: legacy contract changed')
        return json.loads(exact[0]['payload'])

    def set_extension(self, project_id, task, plan_version, extension):
        from app.document_contracts import validate_extension
        validate_extension(extension)
        if task.get('project_id') != project_id or task.get('status') == 'completed':
            raise ValueError('Cannot extend another project or a completed task')
        with self.connect() as db:
            existing = db.execute('SELECT status,acceptance_criteria FROM project_tasks WHERE id=? AND project_id=?', (task['id'], project_id)).fetchone()
            if not existing or existing['status'] in {'completed','running'} or existing['acceptance_criteria'] != task['acceptance_criteria']:
                raise ValueError('Task changed, completed, running or not in project')
            db.execute('INSERT INTO task_contract_extensions VALUES(?,?,?,?,?,?)',
                       (project_id, task['id'], plan_version, digest(task['acceptance_criteria']), canonical(extension), utcnow()))

    def latest(self, project_id, limit=20):
        with self.connect() as db:
            rows = db.execute('SELECT * FROM task_attempts WHERE project_id=? ORDER BY started_at DESC LIMIT ?',
                              (project_id, min(max(limit, 1), 50))).fetchall()
        return [{**dict(row), 'payload': json.loads(row['payload'])} for row in rows]

    def consume_execution_budget(self, project_id, budget_key, kind, limit):
        if kind not in {'llm', 'tool'}:
            raise ValueError('Invalid budget kind')
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS execution_budgets(project_id TEXT NOT NULL, budget_key TEXT NOT NULL, kind TEXT NOT NULL, used INTEGER NOT NULL, PRIMARY KEY(project_id,budget_key,kind))')
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR IGNORE INTO execution_budgets VALUES(?,?,?,0)', (project_id,budget_key,kind))
            changed = db.execute('UPDATE execution_budgets SET used=used+1 WHERE project_id=? AND budget_key=? AND kind=? AND used<?', (project_id,budget_key,kind,limit)).rowcount
            if not changed:
                from app.recovery_policy import RecoveryStopped
                raise RecoveryStopped('同一契約・入力の共有呼出し予算に達しました: ' + kind)
            return db.execute('SELECT used FROM execution_budgets WHERE project_id=? AND budget_key=? AND kind=?', (project_id,budget_key,kind)).fetchone()[0]
