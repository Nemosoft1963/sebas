"""P1 事例入口(セバス側): 取込時の原本登録と承認時照合の接続。

実在のExperienceStore・実API・実コンテキストファイルで検証する。外部通信なし。
本番の tests/test_rag_source_recheck.py の流儀に合わせる。
"""
import hashlib
import json
import time

from fastapi.testclient import TestClient

from app.experience_memory import (
    ExperienceMemory,
    configured_memory,
    current_index_identity,
    source_original_filename,
)
import app.web as web


def _setup(tmp_path, monkeypatch):
    from app.memory.short_term import ShortTermMemory

    db = tmp_path / 'memory' / 'conversations.db'
    mem = ShortTermMemory(db)
    pid = mem.create_project('p1-source-body')['id']
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


def _item(suffix, body, ref='source.md', extra_evidence=None, applicability=None):
    evidence = {
        'source': 'synthetic-p1-%s' % suffix,
        'source_ref': ref,
        'source_hash': hashlib.sha256(body.encode('utf-8')).hexdigest(),
        'fetched_at': '2026-10-05T00:00:00+00:00',
        'extraction_method': 'manual',
        'prohibitions': [],
    }
    if extra_evidence:
        evidence.update(extra_evidence)
    item_applicability = {'input_version': 'v1'} if applicability is None else applicability
    return {
        'kind': 'success',
        'content': (
            '【事例】P1通し%s【状況】P1案件%sで停止が頻発していた。'
            '【施策】P1案件%s向けに点検を日次化した。【成果】P1案件%sの停止時間が半減した。'
            % (suffix, suffix, suffix, suffix)
        ),
        'applicability': item_applicability,
        'evidence': evidence,
        'source_body': body,
    }


def test_p1_end_to_end_import_verify_index_reference(tmp_path, monkeypatch):
    """通し1本: LAN取込(原本付き・input_versionなし)→承認時指定→verified→indexed→計画参照。

    input_version を除いて承認できる候補は approvable=true・needs_at_approval=['input_version']。
    無指定承認は409でcandidateのまま。指定承認でverified化し索引・計画参照に残る。
    """
    client, mem, pid, db = _setup(tmp_path, monkeypatch)
    body = 'P1 end-to-end original body 001'
    item = _item('e2e', body, applicability={})
    resp = client.post(
        f'/api/projects/{pid}/experience/import-success-cases',
        json={'items': [item], 'actor': 'collector-48g'},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data['registered'] == 1 and data['accepted_status'] == 'candidate'
    assert data['original_registered'] == {'0': 'registered'}

    service, _ = configured_memory(db, pid)
    cands = service.store.list_candidates(pid)
    assert len(cands) == 1
    rid = cands[0]['id']
    assert cands[0]['approvable'] is True
    assert cands[0]['needs_at_approval'] == ['input_version']
    assert 'input_version' not in (cands[0]['missing'] or [])
    # 原本がコンテキストファイルとして登録されている。
    files = [f for f in mem.list_context_files(pid) if f.get('source') != 'memo']
    assert [f['filename'] for f in files] == ['source.md']

    listed = client.get(f'/api/projects/{pid}/experience/candidates').json()['candidates']
    assert len(listed) == 1 and listed[0]['approvable'] is True
    assert listed[0]['needs_at_approval'] == ['input_version']

    denied = client.post(
        f'/api/projects/{pid}/experience/candidates/{rid}/approve',
        json={'reviewer': 'human', 'reason': '原本と要約を突合した'},
    )
    assert denied.status_code == 409, denied.text
    assert denied.json()['detail']['code'] == 'INPUT_VERSION_REQUIRED'
    assert service.store.get(pid, rid)['status'] == 'candidate'

    ok = client.post(
        f'/api/projects/{pid}/experience/candidates/{rid}/approve',
        json={'reviewer': 'human', 'reason': '原本と要約を突合した', 'input_version': 'v1'},
    )
    assert ok.status_code == 200, ok.text
    assert service.store.get(pid, rid)['status'] == 'verified'
    assert json.loads(service.store.get(pid, rid)['applicability'])['input_version'] == 'v1'
    state = service.store.get_index_state(pid, rid)
    assert state and state['status'] == 'indexed'

    # 計画参照に参照IDが残る(実索引の代わりに決定的な固定索引を使う)。
    from app import plan_case_reference as pcr

    class FixedIndex:
        def search(self, project, query, limit):
            return [rid]

    service.index = FixedIndex()
    # input_version は事例の適用条件と一致させる。
    result = pcr.get_case_references(
        service, pid,
        [{'criterion_id': 'SC01', 'statement': 'P1案件e2eの停止時間を半減する'}],
        'v1', 1, 'sig-p1-e2e', {},
    )
    used = [r for c in result['criteria'] for r in c['references'] if r['used_or_rejected'] == 'used']
    assert [r['case_id'] for r in used] == [rid]
    view = pcr.current_view(db, pid, 1)
    kept = [r for c in view['criteria'] for r in c['references'] if r['case_id'] == rid]
    assert kept and kept[0]['used_or_rejected'] == 'used'


def test_p1_approval_input_version_mismatch_not_referenced(tmp_path, monkeypatch):
    """承認時指定と現行入力版の完全一致のみ計画参照に採用し、不一致は不採用。"""
    client, mem, pid, db = _setup(tmp_path, monkeypatch)
    body = 'P1 input-version mismatch body'
    item = _item('mismatch-version', body, ref='mismatch.md', applicability={})
    resp = client.post(
        f'/api/projects/{pid}/experience/import-success-cases',
        json={'items': [item], 'actor': 'collector-48g'},
    )
    assert resp.json()['registered'] == 1
    service, _ = configured_memory(db, pid)
    rid = service.store.list_candidates(pid)[0]['id']
    ok = client.post(
        f'/api/projects/{pid}/experience/candidates/{rid}/approve',
        json={'reviewer': 'human', 'reason': '原本と要約を突合した', 'input_version': 'v1'},
    )
    assert ok.status_code == 200, ok.text

    from app import plan_case_reference as pcr

    class FixedIndex:
        def search(self, project, query, limit):
            return [rid]

    service.index = FixedIndex()
    criteria = [{'criterion_id': 'SC01', 'statement': 'P1案件の停止時間を半減する'}]
    matched = pcr.get_case_references(service, pid, criteria, 'v1', 1, 'sig-p1-match', {})
    used = [r for c in matched['criteria'] for r in c['references'] if r['used_or_rejected'] == 'used']
    assert [r['case_id'] for r in used] == [rid]
    mismatched = pcr.get_case_references(service, pid, criteria, 'v2', 2, 'sig-p1-mismatch', {})
    rejected = [r for c in mismatched['criteria'] for r in c['references'] if r['case_id'] == rid]
    assert rejected and all(r['used_or_rejected'] == 'rejected' for r in rejected)
    assert any(r['applicability_verdict'] == 'input_version_mismatch' for r in rejected)


def test_p1_hash_mismatch_rejected_without_side_effects(tmp_path, monkeypatch):
    client, mem, pid, db = _setup(tmp_path, monkeypatch)
    item = _item('mismatch', 'real body for mismatch')
    item['source_hash'] = '0' * 64
    resp = client.post(
        f'/api/projects/{pid}/experience/import-success-cases',
        json={'items': [item], 'actor': 'collector-48g'},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data['registered'] == 0 and len(data['failed']) == 1
    assert data['failed'][0]['reason'] == 'source_hash_mismatch'
    assert data['original_registered'] == {'0': 'rejected'}
    service, _ = configured_memory(db, pid)
    assert service.store.list_candidates(pid) == []
    assert [f for f in mem.list_context_files(pid) if f.get('source') != 'memo'] == []


def test_p1_conflict_not_overwritten_and_same_content_reused(tmp_path, monkeypatch):
    client, mem, pid, db = _setup(tmp_path, monkeypatch)
    mem.add_context_file(pid, 'source.md', 'existing', 8, data=b'existing-original')
    conflict = _item('conflict', 'different body for conflict')
    resp = client.post(
        f'/api/projects/{pid}/experience/import-success-cases',
        json={'items': [conflict], 'actor': 'collector-48g'},
    )
    assert resp.json()['failed'][0]['reason'] == 'source_ref_conflict'
    assert resp.json()['original_registered'] == {'0': 'rejected'}
    kept = mem.get_context_file(pid, mem.list_context_files(pid)[0]['id'])
    assert kept['original_data'] == b'existing-original'

    same = _item('same', 'existing-original')
    resp2 = client.post(
        f'/api/projects/{pid}/experience/import-success-cases',
        json={'items': [same], 'actor': 'collector-48g'},
    )
    assert resp2.json()['registered'] == 1
    assert resp2.json()['original_registered'] == {'0': 'reused'}
    assert len([f for f in mem.list_context_files(pid) if f.get('source') != 'memo']) == 1


def test_p1_tampered_original_blocks_approval(tmp_path, monkeypatch):
    client, mem, pid, db = _setup(tmp_path, monkeypatch)
    body = 'tamper check body'
    resp = client.post(
        f'/api/projects/{pid}/experience/import-success-cases',
        json={'items': [_item('tamper', body)], 'actor': 'collector-48g'},
    )
    assert resp.json()['registered'] == 1
    service, _ = configured_memory(db, pid)
    rid = service.store.list_candidates(pid)[0]['id']
    assert client.get(f'/api/projects/{pid}/experience/candidates').json()['candidates'][0]['approvable'] is True
    file_id = mem.list_context_files(pid)[0]['id']
    with mem._connect() as conn:
        conn.execute(
            'UPDATE project_context_files SET original_data=?,content=? WHERE id=?',
            (b'tampered', 'tampered', file_id),
        )
    assert client.get(f'/api/projects/{pid}/experience/candidates').json()['candidates'][0]['approvable'] is False
    denied = client.post(
        f'/api/projects/{pid}/experience/candidates/{rid}/approve',
        json={'reviewer': 'human', 'reason': '改ざん確認'},
    )
    assert denied.status_code == 409


def test_p1_legacy_without_body_stays_unapprovable(tmp_path, monkeypatch):
    client, mem, pid, db = _setup(tmp_path, monkeypatch)
    legacy = {
        'kind': 'success',
        'content': '【事例】旧形式【状況】旧案件で停止が頻発【施策】点検を行う【成果】停止時間が半減',
        'applicability': {'input_version': 'v1'},
        'evidence': {'source': 'legacy'},
    }
    resp = client.post(
        f'/api/projects/{pid}/experience/import-success-cases',
        json={'items': [legacy], 'actor': 'collector-48g'},
    )
    assert resp.json()['registered'] == 1
    assert resp.json()['original_registered'] == {'0': 'none'}
    service, _ = configured_memory(db, pid)
    rid = service.store.list_candidates(pid)[0]['id']
    assert service.store.get_candidate(pid, rid)['approvable'] is False
    assert client.post(
        f'/api/projects/{pid}/experience/candidates/{rid}/approve',
        json={'reviewer': 'human', 'reason': '旧形式の確認'},
    ).status_code == 409


def test_p1_resend_supplements_existing_without_status_change(tmp_path, monkeypatch):
    from app.experience_memory import import_success_cases

    client, mem, pid, db = _setup(tmp_path, monkeypatch)
    legacy = {
        'kind': 'success',
        'content': '【事例】再送付与【状況】再送案件で停止が頻発【施策】点検を行う【成果】停止時間が半減',
        'applicability': {},
        'evidence': {'source': 'legacy-resend'},
    }
    first = client.post(
        f'/api/projects/{pid}/experience/import-success-cases',
        json={'items': [legacy], 'actor': 'collector-48g'},
    )
    assert first.json()['registered'] == 1
    service, _ = configured_memory(db, pid)
    target = service.store.list_candidates(pid)[0]['id']
    # needs_review 側も用意する。直接DBで状態を移す(既存の隔離状態の再現)。
    with service.store.connect() as conn:
        conn.execute("UPDATE experiences SET status='needs_review' WHERE id=?", (target,))
    needs_id = target
    # candidate 側の2件目(別内容)を用意する。
    legacy2 = dict(legacy)
    legacy2['content'] = '【事例】再送付与2【状況】再送案件2で停止が頻発【施策】点検を行う【成果】停止時間が半減'
    second = client.post(
        f'/api/projects/{pid}/experience/import-success-cases',
        json={'items': [legacy2], 'actor': 'collector-48g'},
    )
    assert second.json()['registered'] == 1
    cand_id = next(c['id'] for c in service.store.list_candidates(pid))

    # 原本付きで同じ内容を再送する。convert後のlessonが同一になるよう同じcontentを使う。
    body_needs = 'resend body for needs_review'
    resend_needs = _item('resend-needs', body_needs, ref='needs.md')
    resend_needs['content'] = legacy['content']
    resend_needs['evidence']['source_hash'] = hashlib.sha256(body_needs.encode()).hexdigest()
    body_cand = 'resend body for candidate'
    resend_cand = _item('resend-cand', body_cand, ref='cand.md')
    resend_cand['content'] = legacy2['content']
    resend_cand['evidence']['source_hash'] = hashlib.sha256(body_cand.encode()).hexdigest()
    # applicabilityのinput_versionも再送で補えることを確認する。
    resend_needs['applicability'] = {'input_version': 'v1'}
    resend_cand['applicability'] = {'input_version': 'v1'}
    out = import_success_cases(db, pid, [resend_needs, resend_cand], 'proof', 'collector-48g')
    assert out['skipped_duplicate'] == 2
    assert out['original_registered'] == {'0': 'supplemented', '1': 'supplemented'}

    needs_row = service.store.get(pid, needs_id)
    assert needs_row['status'] == 'needs_review'
    needs_ev = json.loads(needs_row['evidence'])
    assert needs_ev['source_hash'] == hashlib.sha256(body_needs.encode()).hexdigest()
    assert needs_ev['source_ref'] == 'needs.md'
    cand_row = service.store.get(pid, cand_id)
    assert cand_row['status'] == 'candidate'
    cand_ev = json.loads(cand_row['evidence'])
    assert cand_ev['source_hash'] == hashlib.sha256(body_cand.encode()).hexdigest()
    cand_applies = json.loads(cand_row['applicability'])
    assert cand_applies.get('input_version') == 'v1'

    # verifiedは変更しない(内容は取込変換後のlesson形で用意する)。
    verified_id = service.store.add(pid, 'success', '確定済みの教訓。', {'input_version': 'v1'}, {'source': 't'})
    service.store.review(pid, verified_id, 'verified', 'reviewer', 'proof', time.time() + 3600)
    before = service.store.get(pid, verified_id)
    out2 = import_success_cases(
        db, pid,
        [{'kind': 'success', 'content': '確定済みの教訓。',
          'applicability': {'input_version': 'v1'},
          'evidence': {'source': 't', 'source_ref': 'source.md',
                       'source_hash': hashlib.sha256(b'x').hexdigest(),
                       'fetched_at': '2026-10-05T00:00:00+00:00',
                       'extraction_method': 'm', 'prohibitions': []},
          'source_body': 'x'}],
        'proof', 'collector-48g')
    # 内容重複だがverifiedは追記対象外のため、原本情報で上書きされない。
    after = service.store.get(pid, verified_id)
    assert before['evidence'] == after['evidence'] and after['status'] == 'verified'
    assert out2['skipped_duplicate'] == 1


def test_p1_invalid_source_body_rejected(tmp_path, monkeypatch):
    client, mem, pid, db = _setup(tmp_path, monkeypatch)
    big = 'x' * (512 * 1024 + 1)
    cases = [
        ('empty', '', 'source_body_empty'),
        ('blank', '   ', 'source_body_empty'),
        ('too_large', big, 'source_body_too_large'),
        ('traversal', 'body', 'source_ref_invalid'),
    ]
    for suffix, body, reason in cases:
        item = _item('invalid-%s' % suffix, body if body else 'placeholder')
        if suffix in ('empty', 'blank', 'too_large'):
            item['source_body'] = body
        else:
            item['source_body'] = body
            item['evidence']['source_ref'] = '../evil.md'
        resp = client.post(
            f'/api/projects/{pid}/experience/import-success-cases',
            json={'items': [item], 'actor': 'collector-48g'},
        )
        assert resp.json()['failed'][0]['reason'] == reason, (suffix, resp.json())
    # source_hash欠落も拒否する。
    missing_hash = _item('missing-hash', 'some body')
    del missing_hash['evidence']['source_hash']
    resp = client.post(
        f'/api/projects/{pid}/experience/import-success-cases',
        json={'items': [missing_hash], 'actor': 'collector-48g'},
    )
    assert resp.json()['failed'][0]['reason'] == 'source_hash_required'
    service, _ = configured_memory(db, pid)
    assert service.store.list(pid) == []
    assert [f for f in mem.list_context_files(pid) if f.get('source') != 'memo'] == []


def test_p1_url_source_ref_resolves_to_deterministic_filename(tmp_path, monkeypatch):
    client, mem, pid, db = _setup(tmp_path, monkeypatch)
    body = 'url original body'
    ref = 'https://example.test/articles/123?x=1'
    expected = source_original_filename(ref)
    assert expected.startswith('source-') and expected.endswith('.md')
    resp = client.post(
        f'/api/projects/{pid}/experience/import-success-cases',
        json={'items': [_item('url', body, ref=ref)], 'actor': 'collector-48g'},
    )
    assert resp.json()['registered'] == 1
    assert resp.json()['original_registered'] == {'0': 'registered'}
    files = [f for f in mem.list_context_files(pid) if f.get('source') != 'memo']
    assert [f['filename'] for f in files] == [expected]
    service, _ = configured_memory(db, pid)
    rid = service.store.list_candidates(pid)[0]['id']
    assert client.get(f'/api/projects/{pid}/experience/candidates').json()['candidates'][0]['approvable'] is True
    ok = client.post(
        f'/api/projects/{pid}/experience/candidates/{rid}/approve',
        json={'reviewer': 'human', 'reason': 'URL原本を突合した', 'input_version': 'v1'},
    )
    assert ok.status_code == 200, ok.text
