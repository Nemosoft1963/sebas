"""Local authoritative experience store; vector search is only a disposable index."""
import hashlib
import json
import math
import sqlite3
import time
import uuid
from pathlib import Path


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class MemoryPolicyError(RuntimeError):
    pass


class ExperienceStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS experiences(
              id TEXT PRIMARY KEY, project TEXT NOT NULL, kind TEXT NOT NULL,
              content TEXT NOT NULL, applicability TEXT NOT NULL, evidence TEXT NOT NULL,
              status TEXT NOT NULL, expires REAL NOT NULL, created REAL NOT NULL,
              cache_key TEXT, response TEXT, UNIQUE(project,cache_key));
            CREATE TABLE IF NOT EXISTS reviews(
              id TEXT PRIMARY KEY, experience_id TEXT NOT NULL, project TEXT NOT NULL,
              status TEXT NOT NULL, reviewer TEXT NOT NULL, proof TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS requests(
              id TEXT PRIMARY KEY, project TEXT NOT NULL, cache_key TEXT NOT NULL,
              period TEXT NOT NULL, reserved_micro INTEGER NOT NULL, state TEXT NOT NULL,
              usage TEXT NOT NULL, created REAL NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS active_request ON requests(project,cache_key) WHERE state='pending';
            CREATE TABLE IF NOT EXISTS retrievals(
              id TEXT PRIMARY KEY, project TEXT NOT NULL, query_hash TEXT NOT NULL,
              ids TEXT NOT NULL, mode TEXT NOT NULL, created REAL NOT NULL);
            ''')

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    def add(self, project, kind, content, applicability, evidence, *, cache_key=None, response=None):
        if not project or kind not in {'success', 'failure', 'external'} or not str(content).strip():
            raise ValueError('Invalid experience')
        rid = uuid.uuid4().hex
        with self.connect() as db:
            if cache_key:
                # Keep previous answers/reviews as audit history, only newest answer owns key.
                db.execute('UPDATE experiences SET cache_key=NULL WHERE project=? AND cache_key=?', (project, cache_key))
            db.execute('INSERT INTO experiences VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (rid, project, kind, content, canonical(applicability), canonical(evidence),
                        'candidate', 0, time.time(), cache_key, canonical(response) if response is not None else None))
        return rid

    def review(self, project, rid, status, reviewer, proof, expires):
        if status not in {'verified', 'revoked'} or not reviewer.strip() or not proof.strip():
            raise ValueError('Reviewer and validation evidence are required')
        if status == 'verified' and (not math.isfinite(expires) or not time.time() < expires <= time.time()+366*86400):
            raise ValueError('A finite future expiry within 366 days is required')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            changed = db.execute('UPDATE experiences SET status=?,expires=? WHERE project=? AND id=?',
                                 (status, expires, project, rid)).rowcount
            if changed != 1:
                raise ValueError('Experience not found in project')
            db.execute('INSERT INTO reviews VALUES(?,?,?,?,?,?,?)',
                       (uuid.uuid4().hex, rid, project, status, reviewer, proof, time.time()))

    def list(self, project, verified_only=False):
        sql = 'SELECT * FROM experiences WHERE project=?'
        args = [project]
        if verified_only:
            sql += ' AND status=? AND expires>?'
            args += ['verified', time.time()]
        with self.connect() as db:
            return [dict(r) for r in db.execute(sql+' ORDER BY created DESC', args)]

    def cached(self, project, key):
        with self.connect() as db:
            row = db.execute('SELECT * FROM experiences WHERE project=? AND cache_key=? AND status=? AND expires>?',
                             (project, key, 'verified', time.time())).fetchone()
        return dict(row) if row else None

    def reserve(self, project, key, budget):
        period = time.strftime('%Y-%m-%d', time.gmtime())
        values = [budget.get(k) for k in ('daily_calls','daily_micro_usd','per_call_micro_usd')]
        if any(type(v) is not int or v <= 0 for v in values):
            raise MemoryPolicyError('Positive integer external budgets required')
        calls, total, per_call = values
        rid = uuid.uuid4().hex
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM requests WHERE project=? AND cache_key=? AND state='pending'", (project,key)).fetchone():
                raise MemoryPolicyError('Identical request pending; do not resend automatically')
            used = db.execute('SELECT COUNT(*),COALESCE(SUM(reserved_micro),0) FROM requests WHERE project=? AND period=?', (project,period)).fetchone()
            if used[0] >= calls or used[1]+per_call > total:
                raise MemoryPolicyError('External budget exhausted')
            db.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?)',
                       (rid,project,key,period,per_call,'pending','{}',time.time()))
        return rid

    def finish(self, rid, state, usage=None):
        if state not in {'completed','failed','unknown'}:
            raise ValueError('Invalid request state')
        with self.connect() as db:
            if db.execute("UPDATE requests SET state=?,usage=? WHERE id=? AND state='pending'",
                          (state,canonical(usage or {}),rid)).rowcount != 1:
                raise ValueError('Pending request not found')

    def audit_retrieval(self, project, query, ids, mode):
        with self.connect() as db:
            db.execute('INSERT INTO retrievals VALUES(?,?,?,?,?,?)',
                       (uuid.uuid4().hex,project,fingerprint(query),canonical(ids),mode,time.time()))

    def stats(self, project):
        with self.connect() as db:
            return {'requests':[dict(r) for r in db.execute(
                'SELECT period,state,COUNT(*) AS calls,SUM(reserved_micro) AS reserved_micro_usd FROM requests WHERE project=? GROUP BY period,state', (project,))],
                'experiences':[dict(r) for r in db.execute('SELECT status,COUNT(*) AS count FROM experiences WHERE project=? GROUP BY status',(project,))]}
