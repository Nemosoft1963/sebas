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
    def __init__(self, path, readonly=False):
        self.path = Path(path)
        self.readonly = bool(readonly)
        if self.readonly:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS experiences(
              id TEXT PRIMARY KEY, project TEXT NOT NULL, kind TEXT NOT NULL,
              content TEXT NOT NULL, applicability TEXT NOT NULL, evidence TEXT NOT NULL,
              status TEXT NOT NULL CHECK(status IN ('candidate','verified','revoked','needs_review')),
              expires REAL NOT NULL, created REAL NOT NULL,
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
            CREATE TABLE IF NOT EXISTS candidate_events(
              id TEXT PRIMARY KEY, experience_id TEXT NOT NULL, project TEXT NOT NULL,
              action TEXT NOT NULL, reviewer TEXT NOT NULL, reason TEXT NOT NULL,
              created REAL NOT NULL);
            ''')
            self._ensure_status_constraint(db)
            self._ensure_index_state_table(db)

    def _ensure_index_state_table(self, db):
        """P1-A: 事例ごとの索引状態を永続化する。索引は再生成可能なキャッシュであり、
        正本の承認状態より強い権限を持たせない。冪等=同じ事例に何度 upsert しても1行。"""
        db.execute('''CREATE TABLE IF NOT EXISTS experience_index_state(
          experience_id TEXT PRIMARY KEY, project TEXT NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('pending','indexed','failed')),
          last_attempt REAL NOT NULL, fail_reason TEXT NOT NULL DEFAULT '',
          index_identity TEXT NOT NULL DEFAULT '')''')

    INDEX_STATUSES = frozenset({'pending', 'indexed', 'failed'})

    def get_index_state(self, project, rid):
        with self.connect() as db:
            try:
                row = db.execute(
                    'SELECT * FROM experience_index_state WHERE project=? AND experience_id=?',
                    (project, rid)).fetchone()
            except sqlite3.OperationalError:
                return None
        return dict(row) if row else None

    def set_index_state(self, project, rid, status, fail_reason='', index_identity=''):
        if status not in self.INDEX_STATUSES:
            raise ValueError('Invalid index status')
        if not str(project or '').strip() or not str(rid or '').strip():
            raise ValueError('project and experience id are required')
        now = time.time()
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS experience_index_state(
              experience_id TEXT PRIMARY KEY, project TEXT NOT NULL,
              status TEXT NOT NULL CHECK(status IN ('pending','indexed','failed')),
              last_attempt REAL NOT NULL, fail_reason TEXT NOT NULL DEFAULT '',
              index_identity TEXT NOT NULL DEFAULT '')''')
            db.execute(
                'INSERT OR REPLACE INTO experience_index_state'
                '(experience_id,project,status,last_attempt,fail_reason,index_identity)'
                ' VALUES(?,?,?,?,?,?)',
                (rid, project, status, now, str(fail_reason or '')[:1000],
                 str(index_identity or '')[:100]))

    def delete_index_state(self, project, rid):
        with self.connect() as db:
            try:
                db.execute(
                    'DELETE FROM experience_index_state WHERE project=? AND experience_id=?',
                    (project, rid))
            except sqlite3.OperationalError:
                pass

    def list_index_states(self, project):
        with self.connect() as db:
            try:
                rows = db.execute(
                    'SELECT * FROM experience_index_state WHERE project=? ORDER BY experience_id',
                    (project,)).fetchall()
            except sqlite3.OperationalError:
                return []
        return [dict(r) for r in rows]

    def connect(self):
        db = (sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=15)
              if self.readonly else sqlite3.connect(self.path, timeout=15))
        db.row_factory = sqlite3.Row
        return db

    def _ensure_status_constraint(self, db):
        """既存DBへ CHECK を安全追加する。不正statusがある場合はデータを壊さずスキップ。"""
        row = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='experiences'"
        ).fetchone()
        if not row or not row[0]:
            return
        sql = ' '.join(str(row[0]).split())
        if "'needs_review'" in sql or '"needs_review"' in sql:
            return
        invalid = db.execute(
            "SELECT 1 FROM experiences WHERE status NOT IN ('candidate','verified','revoked','needs_review') LIMIT 1"
        ).fetchone()
        if invalid:
            return
        db.execute('''
            CREATE TABLE experiences__p0b_status(
              id TEXT PRIMARY KEY, project TEXT NOT NULL, kind TEXT NOT NULL,
              content TEXT NOT NULL, applicability TEXT NOT NULL, evidence TEXT NOT NULL,
              status TEXT NOT NULL CHECK(status IN ('candidate','verified','revoked','needs_review')),
              expires REAL NOT NULL, created REAL NOT NULL,
              cache_key TEXT, response TEXT, UNIQUE(project,cache_key))
        ''')
        db.execute('INSERT INTO experiences__p0b_status SELECT * FROM experiences')
        db.execute('DROP TABLE experiences')
        db.execute('ALTER TABLE experiences__p0b_status RENAME TO experiences')
        db.execute('''CREATE TABLE IF NOT EXISTS quarantine_events(
          id TEXT PRIMARY KEY, experience_id TEXT NOT NULL, project TEXT NOT NULL,
          action TEXT NOT NULL, reviewer TEXT NOT NULL, reason TEXT NOT NULL,
          from_status TEXT NOT NULL, created REAL NOT NULL)''')

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

    # ---- P0-A: success-case intake is stored as candidate; approval is separate.
    # The LAN collector's actor/proof is kept as a claimed origin only and is never
    # treated as approval evidence. Candidates never appear in verified-only reads
    # (list(verified_only=True), reindex, retrieve), which are left untouched.
    CANDIDATE_IMPORT_MARKER = 'success_case_import'

    def get(self, project, rid):
        with self.connect() as db:
            row = db.execute('SELECT * FROM experiences WHERE project=? AND id=?', (project, rid)).fetchone()
        return dict(row) if row else None

    def log_candidate_event(self, project, rid, action, reviewer, reason):
        if action not in {'imported', 'approved', 'rejected', 'returned',
                           'quarantined', 'recheck_requested', 'rechecked_to_candidate',
                           'recheck_failed'}:
            raise ValueError('Invalid candidate action')
        if not str(reviewer or '').strip() or not str(reason or '').strip():
            raise ValueError('Reviewer and reason are required')
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS candidate_events(
              id TEXT PRIMARY KEY, experience_id TEXT NOT NULL, project TEXT NOT NULL,
              action TEXT NOT NULL, reviewer TEXT NOT NULL, reason TEXT NOT NULL,
              created REAL NOT NULL)''')
            db.execute('INSERT INTO candidate_events VALUES(?,?,?,?,?,?,?)',
                       (uuid.uuid4().hex, rid, project, action,
                        str(reviewer).strip(), str(reason).strip(), time.time()))

    def candidate_events(self, project, rid):
        with self.connect() as db:
            try:
                rows = db.execute('SELECT * FROM candidate_events WHERE project=? AND experience_id=? ORDER BY created',
                                  (project, rid)).fetchall()
            except sqlite3.OperationalError:
                return []
        return [dict(r) for r in rows]

    def assess_row(self, row, evidence):
        try:
            applies = json.loads(row['applicability']) if isinstance(row['applicability'], str) else dict(row['applicability'] or {})
        except (ValueError, TypeError):
            applies = {}
        if not isinstance(applies, dict):
            applies = {}
        missing = []
        if not str(evidence.get('source_ref') or '').strip():
            missing.append('source_ref')
        source_hash = str(evidence.get('source_hash') or '').strip()
        if not source_hash:
            missing.append('source_hash')
        if not str(evidence.get('fetched_at') or evidence.get('collected_at') or '').strip():
            missing.append('fetched_at')
        if not str(evidence.get('extraction_method') or evidence.get('summary_method') or '').strip():
            missing.append('extraction_method')
        needs_at_approval = []
        if not str(applies.get('input_version') or '').strip():
            needs_at_approval.append('input_version')
        if 'prohibitions' not in evidence and 'prohibited_conditions' not in evidence:
            missing.append('prohibitions')
        blocked = []
        for key in ('sensitive', 'pii_suspected', 'secret_included', 'quarantine', 'blocked'):
            if evidence.get(key) is True:
                blocked.append(key)
        if evidence.get('ocr_derived') is True and evidence.get('ocr_approved') is not True:
            blocked.append('ocr_not_approved')
        if evidence.get('ocr_unapproved') is True and 'ocr_not_approved' not in blocked:
            blocked.append('ocr_not_approved')
        observed = str(evidence.get('observed_source_hash') or evidence.get('source_hash_observed') or '').strip()
        if source_hash and observed and observed != source_hash:
            blocked.append('source_hash_mismatch')
        if source_hash and (len(source_hash) != 64 or any(c not in '0123456789abcdefABCDEF' for c in source_hash)):
            blocked.append('source_hash_invalid')
        stored_content_hash = str(evidence.get('content_sha256') or '').strip()
        if stored_content_hash and hashlib.sha256(str(row['content']).encode()).hexdigest() != stored_content_hash:
            blocked.append('content_hash_mismatch')
        reasons = []
        if missing:
            reasons.append('証拠が欠落しているため承認不可: ' + ', '.join(missing))
        if blocked:
            reasons.append('拒否条件に該当するため承認不可: ' + ', '.join(blocked))
        return {
            'id': row['id'], 'project': row['project'], 'status': row['status'],
            'kind': row['kind'], 'created': row['created'],
            'lesson_preview': str(row['content'])[:200],
            'claimed': {'actor': evidence.get('claimed_actor', ''), 'proof': evidence.get('claimed_proof', '')},
            'source_ref': evidence.get('source_ref', ''),
            'source_hash': source_hash,
            'fetched_at': evidence.get('fetched_at') or evidence.get('collected_at', ''),
            'extraction_method': evidence.get('extraction_method') or evidence.get('summary_method', ''),
            'applicability': applies,
            'prohibitions': evidence.get('prohibitions', evidence.get('prohibited_conditions', [])),
            'content_sha256': stored_content_hash,
            'missing': missing, 'blocked': blocked, 'reasons': reasons,
            'needs_at_approval': needs_at_approval,
            'approvable': not missing and not blocked,
        }

    def _candidate_row(self, project, rid):
        row = self.get(project, rid)
        if not row:
            raise ValueError('Candidate not found in project')
        try:
            evidence = json.loads(row['evidence']) if isinstance(row['evidence'], str) else dict(row['evidence'] or {})
        except (ValueError, TypeError):
            evidence = {}
        if row['status'] != 'candidate' or evidence.get('imported_via') != self.CANDIDATE_IMPORT_MARKER:
            raise ValueError('Not an approvable candidate')
        return row, evidence

    def list_candidates(self, project):
        with self.connect() as db:
            rows = [dict(r) for r in db.execute(
                "SELECT * FROM experiences WHERE project=? AND status='candidate' ORDER BY created DESC", (project,))]
        out = []
        for row in rows:
            try:
                evidence = json.loads(row['evidence']) if isinstance(row['evidence'], str) else {}
            except (ValueError, TypeError):
                continue
            if not isinstance(evidence, dict) or evidence.get('imported_via') != self.CANDIDATE_IMPORT_MARKER:
                continue
            summary = self.assess_row(row, evidence)
            events = self.candidate_events(project, row['id'])
            summary['last_event'] = events[-1] if events else None
            out.append(summary)
        return out

    def get_candidate(self, project, rid):
        row = self.get(project, rid)
        if not row:
            return None
        try:
            evidence = json.loads(row['evidence']) if isinstance(row['evidence'], str) else {}
        except (ValueError, TypeError):
            return None
        if not isinstance(evidence, dict) or row['status'] != 'candidate' \
                or evidence.get('imported_via') != self.CANDIDATE_IMPORT_MARKER:
            return None
        try:
            applies = json.loads(row['applicability']) if isinstance(row['applicability'], str) else {}
        except (ValueError, TypeError):
            applies = {}
        detail = self.assess_row(row, evidence)
        detail['content'] = row['content']
        detail['applicability'] = applies if isinstance(applies, dict) else {}
        detail['evidence'] = evidence
        detail['events'] = self.candidate_events(project, rid)
        return detail

    INPUT_VERSION_MAX_LENGTH = 64

    @staticmethod
    def normalize_approval_input_version(value):
        """承認時に確認者が指定する入力版の形式検証。前後空白除去・64文字以内・制御文字なし。"""
        text = '' if value is None else str(value)
        normalized = text.strip()
        if not normalized:
            raise InputVersionError('承認時に適用する入力版の指定が必要')
        if len(normalized) > ExperienceStore.INPUT_VERSION_MAX_LENGTH:
            raise InputVersionError('入力版は64文字以内で指定してください')
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in normalized):
            raise InputVersionError('入力版に制御文字は使えません')
        return normalized

    def approve_candidate(self, project, rid, reviewer, reason, expires=None, input_version=None):
        reviewer = str(reviewer or '').strip()
        reason = str(reason or '').strip()
        if not reviewer or not reason:
            raise ValueError('reviewer and reason are required')
        row, evidence = self._candidate_row(project, rid)
        report = self.assess_row(row, evidence)
        if not report['approvable']:
            raise CandidateNotApprovable(report['missing'], report['blocked'])
        try:
            applies = json.loads(row['applicability']) if isinstance(row['applicability'], str) else dict(row['applicability'] or {})
        except (ValueError, TypeError):
            applies = {}
        if not isinstance(applies, dict):
            applies = {}
        existing_version = str(applies.get('input_version') or '').strip()
        if existing_version:
            if input_version is None or str(input_version).strip() == '':
                resolved_version = existing_version
            else:
                resolved_version = self.normalize_approval_input_version(input_version)
                if resolved_version != existing_version:
                    raise InputVersionConflict(existing_version, resolved_version)
        else:
            if input_version is None or str(input_version).strip() == '':
                raise InputVersionRequired()
            resolved_version = self.normalize_approval_input_version(input_version)
        exp = float(expires) if expires is not None else time.time() + 365 * 86400
        # review() と同じ有効期限条件を先に検証する(承認の同一トランザクション内で判定する)。
        if not math.isfinite(exp) or not time.time() < exp <= time.time() + 366 * 86400:
            raise ValueError('A finite future expiry within 366 days is required')
        # review() enforces a finite future expiry within 366 days.
        # The claimed actor/proof from the collector are never used here.
        applies['input_version'] = resolved_version
        event_reason = '%s [input_version=%s reviewer=%s]' % (reason, resolved_version, reviewer)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            changed = db.execute(
                'UPDATE experiences SET applicability=?,status=?,expires=? WHERE project=? AND id=? AND status=?',
                (canonical(applies), 'verified', exp, project, rid, 'candidate')).rowcount
            if changed != 1:
                raise ValueError('Candidate not found in project')
            db.execute('INSERT INTO reviews VALUES(?,?,?,?,?,?,?)',
                       (uuid.uuid4().hex, rid, project, 'verified', reviewer, reason, time.time()))
            db.execute('''CREATE TABLE IF NOT EXISTS candidate_events(
              id TEXT PRIMARY KEY, experience_id TEXT NOT NULL, project TEXT NOT NULL,
              action TEXT NOT NULL, reviewer TEXT NOT NULL, reason TEXT NOT NULL,
              created REAL NOT NULL)''')
            db.execute('INSERT INTO candidate_events VALUES(?,?,?,?,?,?,?)',
                       (uuid.uuid4().hex, rid, project, 'approved', reviewer, event_reason, time.time()))
        return {'id': rid, 'status': 'verified', 'expires': exp, 'reviewer': reviewer, 'reason': event_reason,
                'input_version': resolved_version}

    def reject_candidate(self, project, rid, reviewer, reason):
        reviewer = str(reviewer or '').strip()
        reason = str(reason or '').strip()
        if not reviewer or not reason:
            raise ValueError('reviewer and reason are required')
        self._candidate_row(project, rid)
        self.review(project, rid, 'revoked', reviewer, reason, 0)
        self.log_candidate_event(project, rid, 'rejected', reviewer, reason)
        return {'id': rid, 'status': 'revoked', 'reviewer': reviewer, 'reason': reason}

    def return_candidate(self, project, rid, reviewer, reason):
        reviewer = str(reviewer or '').strip()
        reason = str(reason or '').strip()
        if not reviewer or not reason:
            raise ValueError('reviewer and reason are required')
        self._candidate_row(project, rid)
        self.log_candidate_event(project, rid, 'returned', reviewer, reason)
        events = self.candidate_events(project, rid)
        return {'id': rid, 'status': 'candidate', 'reviewer': reviewer, 'reason': reason,
                'last_event': events[-1] if events else None}

    # ---- P1-A: 撤回・期限切れ・原本変更・needs_review遷移時は正本DBで即時不採用とする。
    # 索引側の削除は ExperienceMemory.remove_from_index が行い、ここは正本の状態遷移だけを担う。
    def revoke_verified(self, project, rid, reviewer, reason):
        """verified を正本で不採用(revoked)にする。索引削除は別途行う。"""
        reviewer = str(reviewer or '').strip()
        reason = str(reason or '').strip()
        if not reviewer or not reason:
            raise ValueError('reviewer and reason are required')
        row = self.get(project, rid)
        if not row:
            raise ValueError('Experience not found in project')
        if row['status'] != 'verified':
            raise ValueError('Only verified records can be revoked here')
        self.review(project, rid, 'revoked', reviewer, reason, 0)
        try:
            self.log_candidate_event(project, rid, 'rejected', reviewer, reason)
        except ValueError:
            pass
        self.delete_index_state(project, rid)
        return {'id': rid, 'status': 'revoked', 'reviewer': reviewer, 'reason': reason}

    def mark_source_changed(self, project, rid, reviewer, observed_source_hash):
        """原本変更時: 正本で不採用にする。呼び出し側は索引削除も行う。"""
        reviewer = str(reviewer or '').strip()
        observed = str(observed_source_hash or '').strip()
        if not reviewer or not observed:
            raise ValueError('reviewer and observed_source_hash are required')
        row = self.get(project, rid)
        if not row:
            raise ValueError('Experience not found in project')
        evidence = self._decode_evidence(row.get('evidence'))
        stored = str(evidence.get('source_hash') or '').strip().lower()
        if stored and stored != observed.lower():
            return self.revoke_verified(
                project, rid, reviewer,
                '原本ハッシュ変更を検出したため不採用(記録=%s, 観測=%s)' % (stored[:12], observed[:12]))
        if row['status'] == 'verified':
            return self.revoke_verified(project, rid, reviewer, '原本変更のため不採用')
        if row['status'] != 'needs_review':
            raise ValueError('Only verified/needs_review records carry indexed content')
        self.delete_index_state(project, rid)
        return {'id': rid, 'status': row['status'], 'reviewer': reviewer}

    def move_to_needs_review(self, project, rid, reviewer, reason_detail):
        """OCR needs_review 等への遷移時: 正本で不採用相当(needs_review)にし索引状態を消す。"""
        reviewer = str(reviewer or '').strip()
        reason_detail = str(reason_detail or '').strip()
        if not reviewer or not reason_detail:
            raise ValueError('reviewer and reason are required')
        row = self.get(project, rid)
        if not row:
            raise ValueError('Experience not found in project')
        from_status = row['status']
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            changed = db.execute(
                "UPDATE experiences SET status='needs_review',expires=0 WHERE project=? AND id=? AND status='verified'",
                (project, rid)).rowcount
            if changed != 1:
                raise ValueError('Only verified records move to needs_review here')
        try:
            self.log_candidate_event(
                project, rid, 'quarantined', reviewer,
                '再審査待ちへ(旧状態=%s)。%s' % (from_status, reason_detail))
        except ValueError:
            pass
        self.delete_index_state(project, rid)
        return {'id': rid, 'status': 'needs_review', 'from_status': from_status}

    # ---- P0-B: legacy externally-imported verified records are quarantined.
    # needs_review never appears in verified-only reads (list(verified_only=True),
    # reindex, retrieve) because those paths keep filtering on status='verified'.
    P0B_QUARANTINE_PROOF_PHRASE = '成功事例収集エージェントでの人手レビュー済み'
    P0B_QUARANTINE_ACTION = 'quarantined'

    def _decode_evidence(self, evidence):
        try:
            parsed = json.loads(evidence) if isinstance(evidence, str) else dict(evidence or {})
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _decode_applicability(self, applicability):
        try:
            parsed = json.loads(applicability) if isinstance(applicability, str) else dict(applicability or {})
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def quarantine_reason(self, row):
        """Return (is_target, reason_key) for P0-B quarantine selection."""
        if not isinstance(row, dict) or row.get('status') != 'verified':
            return False, 'not_verified'
        try:
            evidence = self._decode_evidence(row.get('evidence'))
            applies = self._decode_applicability(row.get('applicability'))
        except Exception:
            return False, 'undecodable'
        # Self-made deliverables (P3 recovery / human-confirmed results) are out of scope.
        if evidence.get('human_result'):
            return False, 'self_made_human_result'
        if evidence.get('attempt_id') or evidence.get('validation_required'):
            return False, 'self_made_attempt'
        if str(evidence.get('ocr_derived') or '').strip().lower() == 'true' or evidence.get('ocr_derived') is True:
            return False, 'self_made_ocr'
        has_import_mark = (
            evidence.get('imported_via') == self.CANDIDATE_IMPORT_MARKER
            or 'imported_via' in evidence
            or 'claimed_actor' in evidence
            or 'claimed_proof' in evidence
        )
        proof_text = ' '.join(str(v) for v in (
            evidence.get('claimed_proof'), evidence.get('proof'),
            evidence.get('review_note'), evidence.get('note'),
        ) if v)
        legacy_proof = self.P0B_QUARANTINE_PROOF_PHRASE in proof_text
        if not (has_import_mark or legacy_proof):
            return False, 'no_import_provenance'
        if str(evidence.get('source_hash') or '').strip():
            return False, 'has_source_hash'
        if str(applies.get('input_version') or '').strip():
            return False, 'has_input_version'
        if has_import_mark and legacy_proof:
            return True, 'import_provenance+legacy_proof+missing_hashes'
        if legacy_proof:
            return True, 'legacy_proof+missing_hashes'
        return True, 'import_provenance+missing_hashes'

    def quarantine_targets(self, project=None):
        with self.connect() as db:
            if project:
                rows = [dict(r) for r in db.execute(
                    'SELECT * FROM experiences WHERE project=? ORDER BY created', (project,))]
            else:
                rows = [dict(r) for r in db.execute('SELECT * FROM experiences ORDER BY created')]
        out = []
        for row in rows:
            is_target, reason = self.quarantine_reason(row)
            if is_target:
                out.append({**row, 'quarantine_reason': reason})
        return out

    def has_quarantine_event(self, project, rid):
        events = self.candidate_events(project, rid)
        return any(e.get('action') == self.P0B_QUARANTINE_ACTION for e in events)

    def quarantine_record(self, project, rid, reviewer, reason_detail):
        reviewer = str(reviewer or '').strip()
        reason_detail = str(reason_detail or '').strip()
        if not reviewer or not reason_detail:
            raise ValueError('reviewer and reason are required')
        row = self.get(project, rid)
        if not row:
            raise ValueError('Experience not found in project')
        is_target, reason_key = self.quarantine_reason(row)
        if row['status'] == 'needs_review':
            if not self.has_quarantine_event(project, rid):
                self.log_candidate_event(
                    project, rid, self.P0B_QUARANTINE_ACTION, reviewer,
                    '旧外部取込由来のため再審査待ちへ(旧状態=%s, 根拠=%s)。%s'
                    % (row['status'], reason_key, reason_detail))
            return {'id': rid, 'status': 'needs_review', 'changed': False,
                    'reason_key': reason_key}
        if not is_target:
            raise ValueError('Not a quarantine target: ' + reason_key)
        from_status = row['status']
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            changed = db.execute(
                "UPDATE experiences SET status=?,expires=? WHERE project=? AND id=? AND status='verified'",
                ('needs_review', 0, project, rid)).rowcount
            if changed != 1:
                raise ValueError('Experience not found in project')
            db.execute('''CREATE TABLE IF NOT EXISTS quarantine_events(
              id TEXT PRIMARY KEY, experience_id TEXT NOT NULL, project TEXT NOT NULL,
              action TEXT NOT NULL, reviewer TEXT NOT NULL, reason TEXT NOT NULL,
              from_status TEXT NOT NULL, created REAL NOT NULL)''')
            db.execute('INSERT INTO quarantine_events VALUES(?,?,?,?,?,?,?,?)',
                       (uuid.uuid4().hex, rid, project, self.P0B_QUARANTINE_ACTION,
                        reviewer, '%s(旧状態=%s, 根拠=%s)' % (reason_detail, from_status, reason_key),
                        from_status, time.time()))
            db.execute('''CREATE TABLE IF NOT EXISTS candidate_events(
              id TEXT PRIMARY KEY, experience_id TEXT NOT NULL, project TEXT NOT NULL,
              action TEXT NOT NULL, reviewer TEXT NOT NULL, reason TEXT NOT NULL,
              created REAL NOT NULL)''')
            already = db.execute(
                'SELECT 1 FROM candidate_events WHERE project=? AND experience_id=? AND action=?',
                (project, rid, self.P0B_QUARANTINE_ACTION)).fetchone()
            if not already:
                db.execute('INSERT INTO candidate_events VALUES(?,?,?,?,?,?,?)',
                           (uuid.uuid4().hex, rid, project, self.P0B_QUARANTINE_ACTION,
                            reviewer, '旧外部取込由来のため再審査待ちへ(旧状態=%s, 根拠=%s)。%s'
                            % (from_status, reason_key, reason_detail), time.time()))
        # P1-A: needs_review へ遷移した正本は索引状態も消す(索引は再生成可能なキャッシュ)。
        self.delete_index_state(project, rid)
        return {'id': rid, 'status': 'needs_review', 'changed': True,
                'from_status': from_status, 'reason_key': reason_key}

    def quarantine_events(self, project, rid):
        with self.connect() as db:
            try:
                rows = db.execute(
                    'SELECT * FROM quarantine_events WHERE project=? AND experience_id=? ORDER BY created',
                    (project, rid)).fetchall()
            except sqlite3.OperationalError:
                return []
        return [dict(r) for r in rows]

    def list_needs_review(self, project):
        with self.connect() as db:
            rows = [dict(r) for r in db.execute(
                "SELECT * FROM experiences WHERE project=? AND status='needs_review' ORDER BY created DESC",
                (project,))]
        out = []
        for row in rows:
            evidence = self._decode_evidence(row.get('evidence'))
            applies = self._decode_applicability(row.get('applicability'))
            _target, reason_key = self.quarantine_reason({**row, 'status': 'verified'})
            events = self.candidate_events(project, row['id'])
            moves = self.quarantine_events(project, row['id'])
            out.append({
                'id': row['id'], 'project': row['project'], 'status': row['status'],
                'kind': row['kind'], 'created': row['created'],
                'lesson_preview': str(row['content'])[:200],
                'quarantine_reason': reason_key,
                'claimed': {'actor': evidence.get('claimed_actor', ''),
                            'proof': evidence.get('claimed_proof', '')},
                'source_ref': evidence.get('source_ref', ''),
                'has_source_hash': bool(str(evidence.get('source_hash') or '').strip()),
                'input_version': applies.get('input_version', ''),
                'events': events,
                'quarantine_events': moves,
                'last_event': events[-1] if events else None,
            })
        return out

    def get_needs_review(self, project, rid):
        row = self.get(project, rid)
        if not row or row['status'] != 'needs_review':
            return None
        evidence = self._decode_evidence(row.get('evidence'))
        applies = self._decode_applicability(row.get('applicability'))
        _target, reason_key = self.quarantine_reason({**row, 'status': 'verified'})
        return {
            'id': row['id'], 'project': row['project'], 'status': row['status'],
            'kind': row['kind'], 'created': row['created'],
            'content': row['content'], 'applicability': applies, 'evidence': evidence,
            'quarantine_reason': reason_key,
            'events': self.candidate_events(project, rid),
            'quarantine_events': self.quarantine_events(project, rid),
        }

    def recheck_needs_review(self, project, rid, reviewer, observed_source,
                             fetch_source=None):
        """Re-verify one quarantined record against a re-fetched original.

        The server never trusts the caller's hash claim alone: it re-fetches the
        original via ``fetch_source(source_ref)`` (injectable in tests) and only
        moves to candidate when the fetched bytes hash to the caller-supplied
        ``observed_source_hash``. A recorded ``source_hash`` is then stored and
        the record becomes a candidate for the P0-A review API (no direct
        re-verification for legacy rows without a stored hash).
        """
        reviewer = str(reviewer or '').strip()
        if not reviewer:
            raise ValueError('reviewer is required')
        if not isinstance(observed_source, dict):
            raise ValueError('observed source evidence is required')
        row = self.get(project, rid)
        if not row or row['status'] != 'needs_review':
            raise ValueError('Needs-review record not found in project')
        evidence = self._decode_evidence(row.get('evidence'))
        source_ref = str(observed_source.get('source_ref')
                         or evidence.get('source_ref') or '').strip()
        observed_hash = str(observed_source.get('observed_source_hash') or '').strip().lower()
        if not source_ref:
            self.log_candidate_event(
                project, rid, 'recheck_failed', reviewer,
                '原本参照(source_ref)が無いため確認不能。needs_reviewを維持する。')
            return {'id': rid, 'status': 'needs_review', 'moved': False,
                    'reason': 'missing_source_ref'}
        if not observed_hash or len(observed_hash) != 64 \
                or any(c not in '0123456789abcdef' for c in observed_hash):
            self.log_candidate_event(
                project, rid, 'recheck_failed', reviewer,
                '原本ハッシュ証拠が不正のため確認不能。needs_reviewを維持する。')
            return {'id': rid, 'status': 'needs_review', 'moved': False,
                    'reason': 'invalid_observed_hash'}
        if fetch_source is None:
            def fetch_source(ref):
                raise ValueError('fetch_source is required')
        try:
            fetched = fetch_source(source_ref)
        except Exception as exc:
            self.log_candidate_event(
                project, rid, 'recheck_failed', reviewer,
                '原本の再取得に失敗したため確認不能(%s)。needs_reviewを維持する。'
                % type(exc).__name__)
            return {'id': rid, 'status': 'needs_review', 'moved': False,
                    'reason': 'fetch_failed'}
        if fetched is None:
            fetched_bytes = b''
        elif isinstance(fetched, bytes):
            fetched_bytes = fetched
        else:
            fetched_bytes = str(fetched).encode('utf-8')
        actual_hash = hashlib.sha256(fetched_bytes).hexdigest()
        if actual_hash != observed_hash:
            self.log_candidate_event(
                project, rid, 'recheck_failed', reviewer,
                '原本ハッシュ不一致のため確認不能。needs_reviewを維持する。')
            return {'id': rid, 'status': 'needs_review', 'moved': False,
                    'reason': 'source_hash_mismatch'}
        # Match confirmed: record the newly re-fetched source_hash and move to
        # candidate only. Approval afterwards goes through the P0-A review API.
        # Legacy rows without a stored source_hash are never auto re-verified.
        evidence['source_hash'] = observed_hash
        evidence['source_ref'] = source_ref
        evidence['refetched_at'] = time.strftime('%Y-%m-%dT%H:%M:%S+00:00', time.gmtime())
        evidence['recheck_reviewer'] = reviewer
        # P0-Aの承認APIは imported_via=success_case_import の candidate のみを扱う。
        if evidence.get('imported_via') != self.CANDIDATE_IMPORT_MARKER:
            evidence['imported_via'] = self.CANDIDATE_IMPORT_MARKER
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            changed = db.execute(
                "UPDATE experiences SET status='candidate',evidence=? WHERE project=? AND id=? AND status='needs_review'",
                (canonical(evidence), project, rid)).rowcount
            if changed != 1:
                raise ValueError('Needs-review record not found in project')
        self.log_candidate_event(
            project, rid, 'rechecked_to_candidate', reviewer,
            '原本再取得のSHA-256一致を確認しcandidateへ進めた(再承認はP0-AレビューAPI経由)。')
        return {'id': rid, 'status': 'candidate', 'moved': True,
                'source_ref': source_ref, 'source_hash': observed_hash}


class InputVersionRequired(ValueError):
    def __init__(self, message='承認時に適用する入力版の指定が必要'):
        super().__init__(message)
        self.code = 'INPUT_VERSION_REQUIRED'


class InputVersionError(ValueError):
    def __init__(self, message):
        super().__init__(message)
        self.code = 'INPUT_VERSION_INVALID'


class InputVersionConflict(ValueError):
    def __init__(self, existing, requested):
        self.existing = existing
        self.requested = requested
        self.code = 'INPUT_VERSION_CONFLICT'
        super().__init__('候補の入力版(%s)と指定(%s)が異なるため承認不可' % (existing, requested))


class CandidateNotApprovable(ValueError):
    def __init__(self, missing, blocked):
        self.missing = list(missing or [])
        self.blocked = list(blocked or [])
        parts = []
        if self.missing:
            parts.append('欠落: ' + ', '.join(self.missing))
        if self.blocked:
            parts.append('拒否: ' + ', '.join(self.blocked))
        super().__init__('候補は承認できません(' + '; '.join(parts) + ')')
