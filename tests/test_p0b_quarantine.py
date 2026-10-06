"""P0-B acceptance: quarantine legacy external imports, re-review, stale index safety.

Uses real ExperienceStore records, the real retrieval path (augment_local_prompt
with a stub index returning actual stored ids), the real web API, and the real
quarantine script entry point (function call + subprocess).
"""
import hashlib
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from app.experience_memory import (
    ExperienceMemory,
    augment_local_prompt,
    configured_memory,
    memory_scope,
)


class Index:
    def __init__(self, ids):
        self.ids = ids

    def search(self, *args):
        return self.ids


LEGACY_PROOF = '成功事例収集エージェントでの人手レビュー済み'


def _legacy_evidence(extra=None):
    evidence = {
        'source': 'legacy-48g-export',
        'claimed_actor': 'collector-48g',
        'claimed_proof': LEGACY_PROOF,
        'imported_via': 'success_case_import',
    }
    if extra:
        evidence.update(extra)
    return evidence


def _seed_legacy_verified(store, project, suffix, evidence=None, applies=None):
    content = (
        '【事例】架空の旧取込案件%s【状況】案件%sで停止が頻発していた。'
        '【施策】案件%s向けに点検を日次化し異常値を検知した。【成果】案件%sの停止時間が半減した。'
        % (suffix, suffix, suffix, suffix)
    )
    rid = store.add(project, 'success', content, applies or {}, evidence or _legacy_evidence())
    with store.connect() as db:
        db.execute("UPDATE experiences SET status='verified',expires=? WHERE id=?",
                   (time.time() + 3600, rid))
        db.execute('INSERT INTO reviews VALUES(?,?,?,?,?,?,?)',
                   ('rev-%s' % rid[:8], rid, project, 'verified', 'legacy-sender',
                    LEGACY_PROOF, time.time()))
    return rid


def _setup_project(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import app.web as web_module
    from app.memory.short_term import ShortTermMemory

    db_path = tmp_path / 'memory' / 'conversations.db'
    mem = ShortTermMemory(db_path)
    pid = mem.create_project('proj_p0b')['id']
    other = mem.create_project('proj_p0b_other')['id']
    cfg = {'projects': {pid: 'enforce', other: 'enforce'}, 'embedding_model': 'dummy-model'}
    cfg_path = tmp_path / 'memory' / 'experience_memory.json'
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(cfg), encoding='utf-8')
    monkeypatch.setattr(web_module, 'memory', mem)
    monkeypatch.setattr(web_module, 'DB_PATH', db_path)
    # 承認・隔離後の索引更新はベストエフォート。テストでは実Chroma/Ollamaへ接続しない。
    from app.experience_memory import ExperienceMemory, current_index_identity
    monkeypatch.setattr(ExperienceMemory, 'reindex', lambda self, project: 0)

    def _stub_reindex_case(self, project, rid):
        identity = current_index_identity(self.config)
        self.store.set_index_state(project, rid, 'indexed', '', identity)
        return {'id': rid, 'indexed': True, 'index_identity': identity}

    monkeypatch.setattr(ExperienceMemory, 'reindex_verified_case', _stub_reindex_case)
    return TestClient(web_module.app), pid, other, db_path


def _run(coro):
    import asyncio as _asyncio
    return _asyncio.new_event_loop().run_until_complete(coro)


async def _augmented(exp_mem, project, version, prompt):
    with memory_scope(exp_mem, project, version, mode='enforce'):
        return await augment_local_prompt(prompt)


def test_a2_quarantine_keeps_history_and_hides_from_search(tmp_path, monkeypatch):
    """A2: 旧取込相当は履歴を残して検索除外。原本不明なら再承認不可。2回実行で重複なし。"""
    client, pid, _other, db_path = _setup_project(tmp_path, monkeypatch)
    exp_mem, _mode = configured_memory(db_path, pid)
    store = exp_mem.store
    legacy_id = _seed_legacy_verified(store, pid, 'legacy1')
    # 自前成果物は輸入印なし + attempt_id (capture_attempt と同じ印)。
    # human_result は goal_review.evidence_valid を要求するため、検索に載せる自前行には使わない。
    selfmade_id = _seed_legacy_verified(
        store, pid, 'selfmade',
        evidence={'source': 'selfmade', 'attempt_id': 'attempt-selfmade',
                  'validation_required': True},
        applies={'input_version': 'v1'},
    )
    with store.connect() as db:
        db.execute("UPDATE experiences SET status='verified',expires=? WHERE id=?",
                   (time.time() + 3600, selfmade_id))

    # スクリプトは関数呼び出しで実際に実行する(dry-runでは何も変わらない)。
    sys.path.insert(0, str(ROOT))
    from scripts.quarantine_external_imports import main as quarantine_main
    assert quarantine_main(['--db', str(db_path), '--project', pid]) == 0
    assert store.get(pid, legacy_id)['status'] == 'verified'

    assert quarantine_main(['--db', str(db_path), '--project', pid, '--apply']) == 0
    moved = store.get(pid, legacy_id)
    assert moved['status'] == 'needs_review'
    # 自前成果物は対象外のまま verified を維持する。
    assert store.get(pid, selfmade_id)['status'] == 'verified'
    # 既存レビュー履歴は改変せず、移行イベントが追加される(履歴が残る)。
    with store.connect() as db:
        reviews = db.execute(
            'SELECT reviewer, proof FROM reviews WHERE experience_id=?', (legacy_id,)).fetchall()
    assert len(reviews) == 1 and reviews[0]['reviewer'] == 'legacy-sender'
    events = store.candidate_events(pid, legacy_id)
    assert any(e['action'] == 'quarantined' for e in events)
    assert any('verified' in e['reason'] for e in events if e['action'] == 'quarantined')

    # 検索除外: 実検索経路で索引が旧IDを返しても needs_review は渡らない。
    exp_mem.index = Index([legacy_id, selfmade_id])
    augmented = _run(_augmented(exp_mem, pid, 'v1', '停止の対策'))
    assert selfmade_id in augmented
    assert legacy_id not in augmented
    assert store.list(pid, verified_only=True) != []
    assert legacy_id not in {r['id'] for r in store.list(pid, verified_only=True)}

    # 確認APIは件数・一覧を返す(読み取り専用)。
    listed = client.get(f'/api/projects/{pid}/experience/needs-review')
    assert listed.status_code == 200
    assert listed.json()['count'] == 1
    assert listed.json()['records'][0]['id'] == legacy_id

    # 原本不明(ハッシュ不一致・未取得)なら needs_review のまま理由を記録する。
    bad = client.post(
        f'/api/projects/{pid}/experience/needs-review/{legacy_id}/recheck',
        json={'reviewer': '確認者A', 'source_ref': 'missing-original',
              'observed_source_hash': '0' * 64},
    )
    assert bad.status_code == 200
    assert bad.json()['moved'] is False
    assert store.get(pid, legacy_id)['status'] == 'needs_review'

    # 移行を2回実行しても件数・履歴が重複しない(2回目は変更0件)。
    before_changes = store.candidate_events(pid, legacy_id)
    assert quarantine_main(['--db', str(db_path), '--project', pid, '--apply']) == 0
    after_changes = store.candidate_events(pid, legacy_id)
    assert len(before_changes) == len(after_changes)
    assert store.get(pid, legacy_id)['status'] == 'needs_review'


def test_a2_recheck_with_refetched_original_moves_to_candidate_only(tmp_path, monkeypatch):
    """原本再取得のSHA-256一致でのみcandidateへ。verified直行はしない。"""
    client, pid, _other, db_path = _setup_project(tmp_path, monkeypatch)
    exp_mem, _mode = configured_memory(db_path, pid)
    store = exp_mem.store
    legacy_id = _seed_legacy_verified(store, pid, 'legacy-recheck')
    sys.path.insert(0, str(ROOT))
    from scripts.quarantine_external_imports import main as quarantine_main
    assert quarantine_main(['--db', str(db_path), '--project', pid, '--apply']) == 0
    assert store.get(pid, legacy_id)['status'] == 'needs_review'

    # 原本を登録済みコンテキスト原本として用意し、偽ではなく実ストア経由で再取得させる。
    import app.web as web_module
    original_bytes = 'p0b-original-bytes-recheck'.encode('utf-8')
    mem = web_module.memory
    context_item = mem.add_context_file(
        pid, 'original-recheck.txt', original_bytes.decode('utf-8'), len(original_bytes),
        original_bytes, 'text/plain', 'text', '',
        hashlib.sha256(original_bytes).hexdigest(),
    )
    observed = hashlib.sha256(original_bytes).hexdigest()
    resp = client.post(
        f'/api/projects/{pid}/experience/needs-review/{legacy_id}/recheck',
        json={'reviewer': '確認者B', 'source_ref': context_item['filename'],
              'observed_source_hash': observed},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()['moved'] is True
    assert resp.json()['status'] == 'candidate'
    assert 'P0-A' in resp.json()['approval_note']
    row = store.get(pid, legacy_id)
    assert row['status'] == 'candidate'
    evidence = json.loads(row['evidence'])
    # 保存済みsource_hashが無かった旧行に新しいsource_hashが記録される。
    assert evidence['source_hash'] == observed
    # candidate止まりであり verified にはなっていない。承認はP0-AレビューAPI経由。
    assert store.list(pid, verified_only=True) == []
    exp_mem.index = Index([legacy_id])
    assert '過去の経験' not in _run(_augmented(exp_mem, pid, 'v1', '停止の対策'))


def test_a4_stale_index_and_cross_project_and_old_version_do_not_leak(tmp_path):
    """A4: needs_review・他案件・旧入力版は索引残留でも検索・計画・TRIZへ渡らない。"""
    mem = ExperienceMemory(tmp_path, {})
    good_id = mem.store.add('p', 'success', '確定済みの教訓', {'input_version': 'v1'}, {'source': 'test'})
    mem.store.review('p', good_id, 'verified', 'reviewer', 'independent proof', time.time() + 3600)
    needs_id = _seed_legacy_verified(mem.store, 'p', 'stale-index')
    mem.store.quarantine_record('p', needs_id, 'quarantine-script', '受入試験')
    other_id = mem.store.add('other', 'success', '他案件の教訓', {'input_version': 'v1'}, {'source': 'test'})
    mem.store.review('other', other_id, 'verified', 'reviewer', 'independent proof', time.time() + 3600)
    old_id = mem.store.add('p', 'success', '旧入力版の教訓', {'input_version': 'old'}, {'source': 'test'})
    mem.store.review('p', old_id, 'verified', 'reviewer', 'independent proof', time.time() + 3600)

    # 索引に古いID・他案件ID・旧版IDが残っていても authoritative recheck で除外される。
    mem.index = Index([needs_id, other_id, old_id, good_id])
    import asyncio as _asyncio

    async def _run():
        with memory_scope(mem, 'p', 'v1', mode='enforce'):
            return mem.retrieve({'project': 'p', 'input_version': 'v1', 'mode': 'enforce'}, '教訓')

    got = _asyncio.new_event_loop().run_until_complete(_run())
    assert [r['id'] for r in got] == [good_id]

    # 計画(A2/A4の計画経路=augment_local_prompt)へも渡らない。
    async def _plan():
        with memory_scope(mem, 'p', 'v1', mode='enforce'):
            return await augment_local_prompt('計画の相談')

    planned = _asyncio.new_event_loop().run_until_complete(_plan())
    assert good_id in planned
    assert needs_id not in planned and other_id not in planned and old_id not in planned
    # TRIZ側に経験RAGの直結経路はなく、唯一の混入経路である計画プロンプト補強でも渡らない。
    assert needs_id not in planned


def test_quarantine_script_subprocess_dry_run_and_backup(tmp_path):
    """スクリプトはサブプロセスでも呼べ、dry-runは無変更、--applyはバックアップを作る。"""
    store = ExperienceMemory(tmp_path, {}).store
    legacy_id = _seed_legacy_verified(store, 'p', 'subprocess')
    db_path = tmp_path / 'experience.sqlite3'
    script = ROOT / 'scripts' / 'quarantine_external_imports.py'

    dry = subprocess.run(
        [sys.executable, str(script), '--db', str(db_path), '--project', 'p'],
        capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT),
        env={'PYTHONPATH': str(ROOT), **dict(__import__('os').environ)},
    )
    assert dry.returncode == 0, dry.stderr
    assert 'dry-run' in dry.stdout
    assert store.get('p', legacy_id)['status'] == 'verified'

    applied = subprocess.run(
        [sys.executable, str(script), '--db', str(db_path), '--project', 'p', '--apply',
         '--index-dir', str(tmp_path / 'no-such-index'),
         '--config', str(tmp_path / 'no-such-config.json')],
        capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT),
        env={'PYTHONPATH': str(ROOT), **dict(__import__('os').environ)},
    )
    # 正本の隔離は索引再生成の成否に依存しない。索引ランタイムが無い環境では
    # reindex が非ゼロになり得るが、DB変更とバックアップは完了している。
    assert applied.returncode in {0, 4}, applied.stderr + applied.stdout
    assert 'バックアップ先' in applied.stdout
    assert store.get('p', legacy_id)['status'] == 'needs_review'
    backups = list((tmp_path).glob('quarantine-backup-*'))
    assert backups and (backups[0] / 'experience.sqlite3').exists()


def test_no_restore_operation_and_index_rebuild_only(tmp_path):
    """復元用の逆操作は作らず、復旧手順は索引の再生成のみである。"""
    import scripts.quarantine_external_imports as quarantine

    assert not hasattr(quarantine, 'restore')
    assert not hasattr(quarantine, 'rollback')
    assert not hasattr(quarantine, 'unquarantine')
    text = (ROOT / 'scripts' / 'quarantine_external_imports.py').read_text(encoding='utf-8')
    assert 'restore' not in text.lower() or '戻さ' in text
    assert 'reindex' in text.lower() or 'reindex_project' in text
    runbook = ROOT / 'docs' / 'P0B_QUARANTINE_RUNBOOK_2026-10-03.md'
    assert runbook.exists()
    body = runbook.read_text(encoding='utf-8')
    assert '索引の再生成' in body
    assert 'verifiedに戻さ' in body or 'verifiedには戻さ' in body


def test_status_check_accepts_needs_review_and_rejects_bogus(tmp_path):
    """needs_reviewは正規状態として保存でき、既存3状態も壊さない。不正値は拒否する。"""
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
    from app.experience_store import ExperienceStore

    st = ExperienceStore(path)
    rows = {r['id']: r for r in st.list('p')}
    assert rows['old1']['status'] == 'candidate'
    assert rows['old2']['status'] == 'verified'
    rid = st.add('p', 'success', 'quarantined-lesson', {}, {'source': 'test'})
    with st.connect() as db:
        db.execute("UPDATE experiences SET status='needs_review' WHERE id=?", (rid,))
    assert st.get('p', rid)['status'] == 'needs_review'
    assert st.list('p', True) == [rows['old2']]
    sql = st.connect().execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='experiences'"
    ).fetchone()[0]
    assert 'needs_review' in ' '.join(sql.split())
    import pytest as _pytest
    with _pytest.raises(sqlite3.IntegrityError):
        with st.connect() as db:
            db.execute(
                'INSERT INTO experiences VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                ('bad', 'p', 'success', 'x', '{}', '{}', 'bogus', 0, 1.0, None, None),
            )
