"""承認時 input_version 指定: 実Store・実API・実コンテキストファイルで検証する。外部通信なし。"""
import hashlib
import json

from fastapi.testclient import TestClient

from app.experience_memory import (
    ExperienceMemory,
    configured_memory,
    current_index_identity,
)
import app.web as web


def _setup(tmp_path, monkeypatch):
    from app.memory.short_term import ShortTermMemory

    db = tmp_path / 'memory' / 'conversations.db'
    mem = ShortTermMemory(db)
    pid = mem.create_project('approval-input-version')['id']
    (db.parent / 'experience_memory.json').write_text(
        json.dumps({'projects': {pid: 'enforce'}, 'embedding_model': 'test-model'}),
        encoding='utf-8',
    )
    monkeypatch.setattr(web, 'memory', mem)
    monkeypatch.setattr(web, 'DB_PATH', db)

    def stub(self, project, rid):
        identity = current_index_identity(self.config)
        self.store.set_index_state(project, rid, 'indexed', '', identity)
        return {'id': rid, 'indexed': True}

    monkeypatch.setattr(ExperienceMemory, 'reindex_verified_case', stub)
    return TestClient(web.app), mem, pid, db


def _content(suffix):
    return (
        '【事例】承認時版指定%s【状況】承認案件%sで停止が頻発していた。'
        '【施策】承認案件%s向けに点検を日次化した。【成果】承認案件%sの停止時間が半減した。'
        % (suffix, suffix, suffix, suffix)
    )


def _evidence(body, ref='approve.md', extra=None):
    evidence = {
        'source': 'synthetic-approval-%s' % ref,
        'source_ref': ref,
        'source_hash': hashlib.sha256(body.encode('utf-8')).hexdigest(),
        'fetched_at': '2026-10-05T00:00:00+00:00',
        'extraction_method': 'manual',
        'prohibitions': [],
    }
    if extra:
        evidence.update(extra)
    return evidence


def _import_with_pid(client, pid, suffix, body, ref='approve.md', applicability=None, extra=None):
    item = {
        'kind': 'success',
        'content': _content(suffix),
        'applicability': {} if applicability is None else applicability,
        'evidence': _evidence(body, ref, extra),
        'source_body': body,
    }
    resp = client.post(
        f'/api/projects/{pid}/experience/import-success-cases',
        json={'items': [item], 'actor': 'collector-48g'},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()['registered'] == 1, resp.text


def test_approve_without_input_version_is_409_and_stays_candidate(tmp_path, monkeypatch):
    client, _mem, pid, db = _setup(tmp_path, monkeypatch)
    _import_with_pid(client, pid, 'missing', 'approve body missing', 'missing.md')
    service, _ = configured_memory(db, pid)
    rid = service.store.list_candidates(pid)[0]['id']
    denied = client.post(
        f'/api/projects/{pid}/experience/candidates/{rid}/approve',
        json={'reviewer': 'human', 'reason': '版の指定なし'},
    )
    assert denied.status_code == 409, denied.text
    assert denied.json()['detail']['code'] == 'INPUT_VERSION_REQUIRED'
    assert service.store.get(pid, rid)['status'] == 'candidate'


def test_approve_with_input_version_writes_and_logs(tmp_path, monkeypatch):
    client, _mem, pid, db = _setup(tmp_path, monkeypatch)
    _import_with_pid(client, pid, 'written', 'approve body written', 'written.md')
    service, _ = configured_memory(db, pid)
    rid = service.store.list_candidates(pid)[0]['id']
    ok = client.post(
        f'/api/projects/{pid}/experience/candidates/{rid}/approve',
        json={'reviewer': '確認者A', 'reason': '原本と要約を突合した', 'input_version': ' v3 '},
    )
    assert ok.status_code == 200, ok.text
    row = service.store.get(pid, rid)
    assert row['status'] == 'verified'
    assert json.loads(row['applicability'])['input_version'] == 'v3'
    events = service.store.candidate_events(pid, rid)
    approved = [e for e in events if e['action'] == 'approved']
    assert approved
    assert 'input_version=v3' in approved[-1]['reason']
    assert '確認者A' in approved[-1]['reason']
    assert '原本と要約を突合した' in approved[-1]['reason']


def test_approve_conflicting_input_version_is_409_without_overwrite(tmp_path, monkeypatch):
    client, _mem, pid, db = _setup(tmp_path, monkeypatch)
    _import_with_pid(
        client, pid, 'preset', 'approve body preset', 'preset.md',
        applicability={'input_version': 'v1'},
    )
    service, _ = configured_memory(db, pid)
    rid = service.store.list_candidates(pid)[0]['id']
    denied = client.post(
        f'/api/projects/{pid}/experience/candidates/{rid}/approve',
        json={'reviewer': 'human', 'reason': '版の不一致', 'input_version': 'v2'},
    )
    assert denied.status_code == 409, denied.text
    assert denied.json()['detail']['code'] == 'INPUT_VERSION_CONFLICT'
    assert service.store.get(pid, rid)['status'] == 'candidate'
    assert json.loads(service.store.get(pid, rid)['applicability'])['input_version'] == 'v1'
    ok = client.post(
        f'/api/projects/{pid}/experience/candidates/{rid}/approve',
        json={'reviewer': 'human', 'reason': '既存版で承認'},
    )
    assert ok.status_code == 200, ok.text
    assert json.loads(service.store.get(pid, rid)['applicability'])['input_version'] == 'v1'


def test_approve_input_version_format_is_validated(tmp_path, monkeypatch):
    client, _mem, pid, db = _setup(tmp_path, monkeypatch)
    bodies = [
        ('blank', '   ', 'INPUT_VERSION_REQUIRED'),
        ('control', 'v1\x01', 'INPUT_VERSION_INVALID'),
        ('long', 'x' * 65, 'INPUT_VERSION_INVALID'),
    ]
    for suffix, version, code in bodies:
        _import_with_pid(
            client, pid, 'format-%s' % suffix, 'approve body %s' % suffix, '%s.md' % suffix,
        )
    service, _ = configured_memory(db, pid)
    expected_by_suffix = {suffix: code for suffix, _version, code in bodies}
    version_by_suffix = {suffix: version for suffix, version, _code in bodies}
    rows = {json.loads(service.store.get(pid, cand['id'])['evidence']).get('source_ref'): cand
            for cand in service.store.list_candidates(pid)}
    assert set(rows) == {'blank.md', 'control.md', 'long.md'}
    for suffix in ('blank', 'control', 'long'):
        cand = rows['%s.md' % suffix]
        expected = expected_by_suffix[suffix]
        denied = client.post(
            f'/api/projects/{pid}/experience/candidates/{cand["id"]}/approve',
            json={'reviewer': 'human', 'reason': '形式確認', 'input_version': version_by_suffix[suffix]},
        )
        assert denied.status_code == 409, (suffix, denied.text)
        assert denied.json()['detail']['code'] == expected
        assert service.store.get(pid, cand['id'])['status'] == 'candidate'


def test_missing_evidence_still_blocks_even_with_input_version(tmp_path, monkeypatch):
    client, _mem, pid, db = _setup(tmp_path, monkeypatch)
    legacy = {
        'kind': 'success',
        'content': _content('legacy-evidence'),
        'applicability': {},
        'evidence': {'source': 'legacy'},
    }
    resp = client.post(
        f'/api/projects/{pid}/experience/import-success-cases',
        json={'items': [legacy], 'actor': 'collector-48g'},
    )
    assert resp.json()['registered'] == 1
    service, _ = configured_memory(db, pid)
    rid = service.store.list_candidates(pid)[0]['id']
    candidate = service.store.get_candidate(pid, rid)
    assert candidate['approvable'] is False
    assert candidate['needs_at_approval'] == ['input_version']
    denied = client.post(
        f'/api/projects/{pid}/experience/candidates/{rid}/approve',
        json={'reviewer': 'human', 'reason': '版を指定しても不可', 'input_version': 'v1'},
    )
    assert denied.status_code == 409, denied.text
    assert denied.json()['detail']['code'] == 'CANDIDATE_NOT_APPROVABLE'
    assert service.store.get(pid, rid)['status'] == 'candidate'


def test_missing_original_still_blocks_even_with_input_version(tmp_path, monkeypatch):
    client, _mem, pid, db = _setup(tmp_path, monkeypatch)
    body = 'approve body without registered original'
    item = {
        'kind': 'success',
        'content': _content('no-original'),
        'applicability': {},
        'evidence': _evidence(body, 'no-original.md'),
        'source_body': body,
    }
    # 原本登録を避けるため source_body なしの同等証拠で取込直後に原本だけ消すのではなく、
    # 取込は原本付きで行い、承認前に原本を改ざんして照合失敗を再現する。
    resp = client.post(
        f'/api/projects/{pid}/experience/import-success-cases',
        json={'items': [item], 'actor': 'collector-48g'},
    )
    assert resp.json()['registered'] == 1
    service, _ = configured_memory(db, pid)
    rid = service.store.list_candidates(pid)[0]['id']
    mem = web.memory
    file_id = mem.list_context_files(pid)[0]['id']
    with mem._connect() as conn:
        conn.execute(
            'UPDATE project_context_files SET original_data=?,content=? WHERE id=?',
            (b'tampered', 'tampered', file_id),
        )
    denied = client.post(
        f'/api/projects/{pid}/experience/candidates/{rid}/approve',
        json={'reviewer': 'human', 'reason': '原本改ざん確認', 'input_version': 'v1'},
    )
    assert denied.status_code == 409, denied.text
    assert service.store.get(pid, rid)['status'] == 'candidate'


def test_candidate_before_approval_not_in_search_or_rag(tmp_path, monkeypatch):
    client, _mem, pid, db = _setup(tmp_path, monkeypatch)
    _import_with_pid(client, pid, 'unindexed', 'approve body unindexed', 'unindexed.md')
    service, _ = configured_memory(db, pid)
    rid = service.store.list_candidates(pid)[0]['id']
    assert service.store.list(pid, verified_only=True) == []

    class FixedIndex:
        def search(self, project, query, limit):
            return [rid]

    service.index = FixedIndex()
    import asyncio as _asyncio
    from app.experience_memory import augment_local_prompt, memory_scope

    async def _run():
        with memory_scope(service, pid, 'v9', mode='enforce'):
            return await augment_local_prompt('停止の対策')

    assert '過去の経験' not in _asyncio.new_event_loop().run_until_complete(_run())
