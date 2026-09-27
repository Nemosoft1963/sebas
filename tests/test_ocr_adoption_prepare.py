"""OCR採用署名と run_prepare 再抽出・stale判定のテスト。

実OCR/GPU/実PDF/実在の会計情報は使わない。
"""
from __future__ import annotations

import pytest

from app.ocr_store import OcrStore
from app.vehicle_auto import current_ocr_adoption_signature, ocr_adoption_signature
from app.vehicle_service import run_prepare, state
from app.vehicle_workflow import INPUT_PATH, digest, load_input, resolve, sources, write_json
from app.workflow_readiness import build_readiness
from test_vehicle_auto import ledger
from test_vehicle_workflow import setup as vehicle_setup


def _setup_vehicle_project(tmp_path):
    manager, pid = vehicle_setup(tmp_path)
    src, raw, _ = ledger()
    manager.memory.add_context_file(pid, src['filename'], '月次原本', len(raw), source='upload', data=raw)
    return manager, pid


def _add_source_file(manager, pid, filename='燃料/202601/宇佐美_関東.pdf', content='00001 1234 1\n小計 100\n自動車用潤滑油 100\n'):
    return manager.memory.add_context_file(pid, filename, content, len(content), source='upload')


def _seed_ocr_run(store, *, pid, file_id, run_id='run-1', status='needs_review'):
    run, _ = store.create_run(
        project_id=pid,
        context_file_id=file_id,
        source_sha256='a' * 64,
        status=status,
        run_id=run_id,
    )
    return run


def test_ocr_adoption_signature_function(tmp_path, monkeypatch):
    manager, pid = _setup_vehicle_project(tmp_path)
    item = _add_source_file(manager, pid)
    store = OcrStore(manager.memory.path)

    # 1. フラグ無効時は空文字
    monkeypatch.setattr('app.capability_registry.ocr_feature_enabled', lambda: False)
    assert ocr_adoption_signature(manager, pid, sources(manager, pid)) == ''
    assert current_ocr_adoption_signature(manager, pid) == ''

    # 2. フラグ有効だが run 無しなら空文字
    monkeypatch.setattr('app.capability_registry.ocr_feature_enabled', lambda: True)
    assert ocr_adoption_signature(manager, pid, sources(manager, pid)) == ''
    assert current_ocr_adoption_signature(manager, pid) == ''

    # 3. run あり
    _seed_ocr_run(store, pid=pid, file_id=item['id'], run_id='run-1', status='needs_review')
    sig_unapproved = current_ocr_adoption_signature(manager, pid)
    assert sig_unapproved != ''

    # 4. 承認で署名変化
    store.save_review('run-1', decision='approved', reviewer='alice', signature='sig-1')
    sig_approved = current_ocr_adoption_signature(manager, pid)
    assert sig_approved != ''
    assert sig_approved != sig_unapproved

    # 5. 却下で署名変化
    store.save_review('run-1', decision='rejected', reviewer='alice', signature='sig-2')
    sig_rejected = current_ocr_adoption_signature(manager, pid)
    assert sig_rejected != ''
    assert sig_rejected != sig_approved
    assert sig_rejected != sig_unapproved


def test_a_b_ocr_review_change_re_executes_run_prepare_and_caches_when_unchanged(tmp_path, monkeypatch):
    """(a) フラグ有効で承認変更で再実行、(b) 署名不変なら短縮。"""
    monkeypatch.setattr('app.capability_registry.ocr_feature_enabled', lambda: True)
    manager, pid = _setup_vehicle_project(tmp_path)
    item = _add_source_file(manager, pid)
    store = OcrStore(manager.memory.path)
    _seed_ocr_run(store, pid=pid, file_id=item['id'], run_id='run-1', status='needs_review')

    # 初回 prepare
    res1 = run_prepare(manager, pid)
    env1 = load_input(manager, pid)
    sig1 = current_ocr_adoption_signature(manager, pid)
    assert env1.get('ocr_adoption_hash') == sig1
    history_dir = resolve(manager, pid, 'vehicle_profit/history')
    hist_count1 = len(list(history_dir.glob('*.json'))) if history_dir.exists() else 0

    # (b) 署名が変わらなければ短縮（prepare を呼ばず history も増えない）
    res2 = run_prepare(manager, pid)
    assert res2['input_hash'] == res1['input_hash']
    hist_count2 = len(list(history_dir.glob('*.json'))) if history_dir.exists() else 0
    assert hist_count2 == hist_count1

    # (a) 人間承認を行い署名が変わると、再実行される
    store.save_review('run-1', decision='approved', reviewer='alice', signature='sig-alice-1')
    sig2 = current_ocr_adoption_signature(manager, pid)
    assert sig1 != sig2

    res3 = run_prepare(manager, pid)
    env3 = load_input(manager, pid)
    assert env3.get('ocr_adoption_hash') == sig2
    hist_count3 = len(list(history_dir.glob('*.json'))) if history_dir.exists() else 0
    assert hist_count3 > hist_count2


def test_c_flag_disabled_signature_is_empty_and_no_reexecution(tmp_path, monkeypatch):
    """(c) フラグ無効なら署名は '' で、承認状態が変わっても再実行されない。"""
    monkeypatch.setattr('app.capability_registry.ocr_feature_enabled', lambda: False)
    manager, pid = _setup_vehicle_project(tmp_path)
    item = _add_source_file(manager, pid)
    store = OcrStore(manager.memory.path)
    _seed_ocr_run(store, pid=pid, file_id=item['id'], run_id='run-1', status='needs_review')

    assert current_ocr_adoption_signature(manager, pid) == ''

    res1 = run_prepare(manager, pid)
    env1 = load_input(manager, pid)
    assert env1.get('ocr_adoption_hash') == ''
    history_dir = resolve(manager, pid, 'vehicle_profit/history')
    hist_count1 = len(list(history_dir.glob('*.json'))) if history_dir.exists() else 0

    # 承認を行ってもフラグ無効なら署名は '' のまま
    store.save_review('run-1', decision='approved', reviewer='alice', signature='sig-alice-1')
    assert current_ocr_adoption_signature(manager, pid) == ''

    res2 = run_prepare(manager, pid)
    assert res2['input_hash'] == res1['input_hash']
    hist_count2 = len(list(history_dir.glob('*.json'))) if history_dir.exists() else 0
    assert hist_count2 == hist_count1


def test_d_failed_to_new_run_changes_signature(tmp_path, monkeypatch):
    """(d) run が failed→新しい run に変わると署名が変わる。"""
    monkeypatch.setattr('app.capability_registry.ocr_feature_enabled', lambda: True)
    manager, pid = _setup_vehicle_project(tmp_path)
    item = _add_source_file(manager, pid)
    store = OcrStore(manager.memory.path)

    _seed_ocr_run(store, pid=pid, file_id=item['id'], run_id='run-fail', status='failed')
    sig_fail = current_ocr_adoption_signature(manager, pid)
    assert sig_fail != ''

    # 新しい run を追加
    _seed_ocr_run(store, pid=pid, file_id=item['id'], run_id='run-new', status='needs_review')
    sig_new = current_ocr_adoption_signature(manager, pid)
    assert sig_new != ''
    assert sig_new != sig_fail


@pytest.mark.asyncio
async def test_e_workflow_readiness_stale_on_ocr_approval(tmp_path, monkeypatch):
    """(e) workflow_readiness の stale が承認後に True になり、prepare 後に False に戻る。"""
    monkeypatch.setattr('app.capability_registry.ocr_feature_enabled', lambda: True)
    manager, pid = _setup_vehicle_project(tmp_path)
    item = _add_source_file(manager, pid)
    store = OcrStore(manager.memory.path)
    _seed_ocr_run(store, pid=pid, file_id=item['id'], run_id='run-1', status='needs_review')

    run_prepare(manager, pid)
    view1 = build_readiness(manager, pid)
    assert view1['vehicle']['prepared'] is True
    assert view1['vehicle']['stale'] is False

    # OCR 承認
    store.save_review('run-1', decision='approved', reviewer='alice', signature='sig-1')
    view2 = build_readiness(manager, pid)
    assert view2['vehicle']['stale'] is True

    # prepare 実行で stale 解消
    run_prepare(manager, pid)
    view3 = build_readiness(manager, pid)
    assert view3['vehicle']['stale'] is False


def test_f_legacy_envelope_without_ocr_adoption_hash_shortcircuits(tmp_path, monkeypatch):
    """(f) 旧形式 envelope(ocr_adoption_hash が無い)で OCR無し・フラグ無効なら短縮される。"""
    monkeypatch.setattr('app.capability_registry.ocr_feature_enabled', lambda: False)
    manager, pid = _setup_vehicle_project(tmp_path)
    _add_source_file(manager, pid)

    run_prepare(manager, pid)
    env = load_input(manager, pid)
    env.pop('ocr_adoption_hash', None)
    target = resolve(manager, pid, INPUT_PATH)
    write_json(target, env)

    history_dir = resolve(manager, pid, 'vehicle_profit/history')
    hist_count1 = len(list(history_dir.glob('*.json'))) if history_dir.exists() else 0

    # 再度 run_prepare
    res = run_prepare(manager, pid)
    hist_count2 = len(list(history_dir.glob('*.json'))) if history_dir.exists() else 0
    assert hist_count2 == hist_count1
