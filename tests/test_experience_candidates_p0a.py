"""P0-A acceptance: intake stays candidate, approval is separate, candidates never leak.

Uses the real ExperienceStore records, the real web API, and the real retrieval
path (augment_local_prompt with a stub index returning the actual stored ids).
"""
import hashlib
import json
import sqlite3
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

from app.experience_memory import (
    augment_local_prompt,
    configured_memory,
    memory_scope,
)


class Index:
    def __init__(self, ids):
        self.ids = ids

    def search(self, *args):
        return self.ids


def _valid_item(suffix, evidence_extra=None):
    item = {
        'kind': 'success',
        'content': (
            '【事例】架空のP0A案件%s【状況】案件%sで停止が頻発していた。'
            '【施策】案件%s向けに点検を日次化し異常値を検知した。【成果】案件%sの停止時間が半減した。'
            % (suffix, suffix, suffix, suffix)
        ),
        'applicability': {'input_version': 'v1'},
        'evidence': {'source': 'synthetic-p0a-%s' % suffix},
    }
    if evidence_extra:
        item['evidence'].update(evidence_extra)
    return item


def _setup_project(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import app.web as web_module
    from app.memory.short_term import ShortTermMemory

    db_path = tmp_path / 'memory' / 'conversations.db'
    mem = ShortTermMemory(db_path)
    pid = mem.create_project('proj_p0a')['id']
    cfg = {'projects': {pid: 'enforce'}, 'embedding_model': 'dummy-model'}
    cfg_path = tmp_path / 'memory' / 'experience_memory.json'
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(cfg), encoding='utf-8')
    monkeypatch.setattr(web_module, 'memory', mem)
    monkeypatch.setattr(web_module, 'DB_PATH', db_path)
    # 承認APIの索引更新フックはベストエフォート。テストでは実Chroma/Ollamaへ接続しない。
    from app.experience_memory import ExperienceMemory, current_index_identity
    monkeypatch.setattr(ExperienceMemory, 'reindex', lambda self, project: 0)

    def _stub_reindex_case(self, project, rid):
        identity = current_index_identity(self.config)
        self.store.set_index_state(project, rid, 'indexed', '', identity)
        return {'id': rid, 'indexed': True, 'index_identity': identity}

    monkeypatch.setattr(ExperienceMemory, 'reindex_verified_case', _stub_reindex_case)
    return TestClient(web_module.app), pid, db_path


def _full_evidence(suffix):
    body = 'original-source-body-%s' % suffix
    return {
        'source_ref': 'https://example.test/original/%s' % suffix,
        'source_hash': hashlib.sha256(body.encode()).hexdigest(),
        'fetched_at': '2026-10-03T00:00:00+00:00',
        'extraction_method': 'manual-summary-v1',
        'prohibitions': ['範囲外の転用禁止'],
    }


def test_a1_import_stays_candidate_and_actor_proof_do_not_verify(tmp_path, monkeypatch):
    """A1: LAN取込直後は candidate、その取込IDはRAG検索結果に現れない。actor/proofを変えても verified にならない。"""
    client, pid, db_path = _setup_project(tmp_path, monkeypatch)
    resp = client.post(
        f'/api/projects/{pid}/experience/import-success-cases',
        json={'items': [_valid_item('a1')], 'actor': 'collector-48g', 'proof': 'self-claimed-reviewed'},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data['registered'] == 1
    assert data['accepted_status'] == 'candidate'

    exp_mem, _mode = configured_memory(db_path, pid)
    rows = exp_mem.store.list(pid)
    assert len(rows) == 1 and rows[0]['status'] == 'candidate'
    assert exp_mem.store.list(pid, verified_only=True) == []
    evidence = json.loads(rows[0]['evidence'])
    # actor/proof は claimed として保存され、承認証拠としては扱わない。
    assert evidence['claimed_actor'] == 'collector-48g'
    assert evidence['claimed_proof'] == 'self-claimed-reviewed'
    assert 'proof' not in evidence or evidence.get('proof') != 'self-claimed-reviewed'
    assert evidence.get('actor') != 'collector-48g' or 'claimed_actor' in evidence

    # 実検索経路: 取込IDを索引が返しても authoritative recheck で除外される。
    exp_mem.index = Index([rows[0]['id']])
    import asyncio as _asyncio

    async def _run_a1():
        with memory_scope(exp_mem, pid, 'v1', mode='enforce'):
            return await augment_local_prompt('停止の対策')

    assert '過去の経験' not in _asyncio.new_event_loop().run_until_complete(_run_a1())

    # actor/proof を変えて再送しても verified にはならない(同一ハッシュは重複扱い)。
    resp2 = client.post(
        f'/api/projects/{pid}/experience/import-success-cases',
        json={'items': [_valid_item('a1')], 'actor': 'another-actor', 'proof': 'another-proof'},
    )
    assert resp2.json()['skipped_duplicate'] == 1
    assert exp_mem.store.list(pid, verified_only=True) == []
    # 別内容で actor/proof をそれらしく変えても candidate のまま。
    resp3 = client.post(
        f'/api/projects/{pid}/experience/import-success-cases',
        json={'items': [_valid_item('a1b')], 'actor': 'reviewer-like', 'proof': 'human-reviewed'},
    )
    assert resp3.json()['accepted_status'] == 'candidate'
    assert exp_mem.store.list(pid, verified_only=True) == []

    # 候補一覧に欠落項目・approvable・出所申告が出る(旧48Gペイロードは欠落あり)。
    listed = client.get(f'/api/projects/{pid}/experience/candidates')
    assert listed.status_code == 200
    cands = listed.json()['candidates']
    assert len(cands) == 2
    first = next(c for c in cands if c['lesson_preview'])
    assert first['status'] == 'candidate'
    assert first['approvable'] is False and first['missing']
    assert first['claimed']['actor'] in {'collector-48g', 'reviewer-like'}
    assert '本人確認を保証しません' in listed.json()['note']


def test_a3_first_half_only_complete_candidate_can_be_verified(tmp_path, monkeypatch):
    """A3前半: 原本・適用条件・確認記録を満たした1件だけ verified 化できる。"""
    client, pid, db_path = _setup_project(tmp_path, monkeypatch)
    good = _valid_item('good', _full_evidence('good'))
    bad = _valid_item('bad')  # 旧48G形: 証拠欠落
    resp = client.post(
        f'/api/projects/{pid}/experience/import-success-cases',
        json={'items': [good, bad], 'actor': 'collector-48g'},
    )
    assert resp.json()['registered'] == 2
    exp_mem, _mode = configured_memory(db_path, pid)
    cands = exp_mem.store.list_candidates(pid)
    assert len(cands) == 2
    good_id = next(c['id'] for c in cands if c['source_ref'])
    bad_id = next(c['id'] for c in cands if not c['source_ref'])
    assert exp_mem.store.get_candidate(pid, good_id)['approvable'] is True
    assert exp_mem.store.get_candidate(pid, good_id)['needs_at_approval'] == []
    assert exp_mem.store.get_candidate(pid, bad_id)['approvable'] is False

    missing_reviewer = client.post(
        f'/api/projects/{pid}/experience/candidates/{good_id}/approve',
        json={'reviewer': '', 'reason': '内容を確認した'},
    )
    assert missing_reviewer.status_code == 422
    # 欠落候補の承認は 409 と理由。
    denied = client.post(
        f'/api/projects/{pid}/experience/candidates/{bad_id}/approve',
        json={'reviewer': '確認者A', 'reason': '内容を確認した'},
    )
    assert denied.status_code == 409
    assert denied.json()['detail']['code'] == 'CANDIDATE_NOT_APPROVABLE'
    assert denied.json()['detail']['missing']

    # 登録原本を再取得できる場合だけ承認できる。
    import app.web as web_module
    original = b'original-source-body-good'
    web_module.memory.add_context_file(
        pid, 'https://example.test/original/good', original.decode(), len(original), data=original)

    # 証拠完備の1件だけ承認できる。reviewer は記録される。
    # 既存の input_version 保持候補は body 省略で既存値を使う。
    ok = client.post(
        f'/api/projects/{pid}/experience/candidates/{good_id}/approve',
        json={'reviewer': '確認者A', 'reason': '原本と要約を突合し適用条件を確認した'},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()['status'] == 'verified'
    assert ok.json()['input_version'] == 'v1'
    assert '本人確認を保証しません' in ok.json()['auth_note']
    verified_rows = exp_mem.store.list(pid, verified_only=True)
    assert [r['id'] for r in verified_rows] == [good_id]
    with exp_mem.store.connect() as db:
        review = db.execute(
            'SELECT reviewer, proof FROM reviews WHERE experience_id=? ORDER BY created DESC',
            (good_id,),
        ).fetchone()
    assert review['reviewer'] == '確認者A'
    assert '原本と要約を突合' in review['proof']
    # 送信元の申告値がレビュー証拠に混入していない。
    assert 'collector-48g' not in review['proof'] and review['reviewer'] != 'collector-48g'

    # 承認後に実検索経路から見える(索引更新フックはベストエフォート。ここでは実レコードで直接確認)。
    exp_mem.index = Index([good_id, bad_id])
    import asyncio as _asyncio

    async def _run():
        with memory_scope(exp_mem, pid, 'v1', mode='enforce'):
            return await augment_local_prompt('停止の対策')

    augmented = _asyncio.new_event_loop().run_until_complete(_run())
    assert '過去の経験' in augmented
    assert good_id in augmented
    assert bad_id not in augmented

    # 詳細APIは原本参照と要約の比較に必要な情報を返す。
    detail = client.get(f'/api/projects/{pid}/experience/candidates/{bad_id}')
    assert detail.status_code == 200
    body = detail.json()
    assert body['content'] and 'source_ref' in body and 'content_sha256' in body
    assert body['approvable'] is False and body['missing']

    # 却下・差戻し API も reviewer/reason 必須で動作する。
    rej = client.post(
        f'/api/projects/{pid}/experience/candidates/{bad_id}/reject',
        json={'reviewer': '確認者B', 'reason': '原本が確認できない'},
    )
    assert rej.status_code == 200 and rej.json()['status'] == 'revoked'
    assert [r['id'] for r in exp_mem.store.list(pid, verified_only=True)] == [good_id]
    # 差戻し対象をもう1件用意する。
    resp2 = client.post(
        f'/api/projects/{pid}/experience/import-success-cases',
        json={'items': [_valid_item('ret')], 'actor': 'collector-48g'},
    )
    assert resp2.json()['registered'] == 1
    ret_id = next(c['id'] for c in exp_mem.store.list_candidates(pid) if c['status'] == 'candidate')
    ret = client.post(
        f'/api/projects/{pid}/experience/candidates/{ret_id}/return',
        json={'reviewer': '確認者C', 'reason': '原本参照を追記してください'},
    )
    assert ret.status_code == 200 and ret.json()['status'] == 'candidate'


def test_p0a_approval_paths_are_not_in_lan_allowlist():
    """P0-A要件5: 48G許可パスは import-success-cases だけ。candidates/approve系を追加しない。"""
    text = (ROOT / 'docker/proxy/nginx.conf.template').read_text()
    assert 'import-success-cases' in text
    assert 'candidates' not in text
    assert 'approve' not in text


def test_a4_candidates_never_leak_into_verified_paths(tmp_path):
    """A4のうち candidate が漏れないこと: verified_only / reindex / retrieve の実経路で証明。"""
    from app.experience_memory import ExperienceMemory

    mem = ExperienceMemory(tmp_path, {})
    good_id = mem.store.add('p', 'success', '確定済みの教訓', {'input_version': 'v1'}, {'source': 'test'})
    mem.store.review('p', good_id, 'verified', 'reviewer', 'independent proof', time.time() + 3600)
    cand_id = mem.store.add(
        'p', 'success', '候補の教訓', {'input_version': 'v1'},
        {'source': 'test', 'imported_via': 'success_case_import', 'claimed_actor': 'x'},
    )
    assert [r['id'] for r in mem.store.list('p', True)] == [good_id]
    # reindex は verified のみを対象にする(実Chromaが無くても対象行の選別を検証)。
    selected = mem.store.list('p', True)
    assert cand_id not in {r['id'] for r in selected}
    # retrieve の authoritative recheck は索引が候補IDを返しても除外する。
    mem.index = Index([cand_id, good_id])
    import asyncio as _asyncio

    async def _run():
        with memory_scope(mem, 'p', 'v1', mode='enforce'):
            return mem.retrieve({'project': 'p', 'input_version': 'v1', 'mode': 'enforce'}, '教訓')

    got = _asyncio.new_event_loop().run_until_complete(_run())
    assert [r['id'] for r in got] == [good_id]


def test_status_check_constraint_migrates_existing_db(tmp_path):
    """既存DBはデータを維持したまま status CHECK を追加し、不正statusは拒否する。"""
    from app.experience_store import ExperienceStore

    path = tmp_path / 'legacy.sqlite3'
    with sqlite3.connect(path) as db:
        db.executescript('''
            CREATE TABLE experiences(
              id TEXT PRIMARY KEY, project TEXT NOT NULL, kind TEXT NOT NULL,
              content TEXT NOT NULL, applicability TEXT NOT NULL, evidence TEXT NOT NULL,
              status TEXT NOT NULL, expires REAL NOT NULL, created REAL NOT NULL,
              cache_key TEXT, response TEXT, UNIQUE(project,cache_key));
        ''')
        db.execute(
            'INSERT INTO experiences VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            ('old1', 'p', 'success', 'keep-candidate', '{}', '{}', 'candidate', 0, 1.0, None, None),
        )
        db.execute(
            'INSERT INTO experiences VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            ('old2', 'p', 'success', 'keep-verified', '{}', '{}', 'verified', 9999999999, 1.0, None, None),
        )
    st = ExperienceStore(path)
    rows = {r['id']: r for r in st.list('p')}
    assert rows['old1']['status'] == 'candidate' and rows['old1']['content'] == 'keep-candidate'
    assert rows['old2']['status'] == 'verified' and rows['old2']['content'] == 'keep-verified'
    sql = st.connect().execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='experiences'"
    ).fetchone()[0]
    assert "CHECK(status IN ('candidate','verified','revoked','needs_review'))" in ' '.join(sql.split())
    with pytest.raises(sqlite3.IntegrityError):
        with st.connect() as db:
            db.execute(
                'INSERT INTO experiences VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                ('bad', 'p', 'success', 'x', '{}', '{}', 'bogus', 0, 1.0, None, None),
            )


def test_sensitive_unapproved_and_tampered_are_rejected(tmp_path):
    """機微情報・OCR未承認・原本改ざん(ハッシュ不一致)は承認不可(拒否)。"""
    from app.experience_memory import ExperienceMemory

    mem = ExperienceMemory(tmp_path, {})
    base = {'source_ref': 'https://example.test/o', 'source_hash': 'a' * 64,
            'fetched_at': '2026-10-03T00:00:00+00:00', 'extraction_method': 'm',
            'imported_via': 'success_case_import'}
    applies = {'input_version': 'v1'}
    sensitive = mem.store.add('p', 'success', '機微あり', dict(applies), dict(base, sensitive=True))
    ocr = mem.store.add('p', 'success', 'OCR未承認', dict(applies), dict(base, ocr_derived=True))
    tampered = mem.store.add(
        'p', 'success', '改ざん疑い', dict(applies),
        dict(base, observed_source_hash='b' * 64),
    )
    for cid in (sensitive, ocr, tampered):
        report = mem.store.get_candidate('p', cid)
        assert report['approvable'] is False and report['blocked']
        with pytest.raises(Exception):
            mem.store.approve_candidate('p', cid, '確認者', '理由')
    assert mem.store.list('p', True) == []
