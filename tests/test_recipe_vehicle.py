"""P2-2 人間承認済み手順の実行再利用。金額再掲ではなく版管理された手順の再実行。"""
import time

from app.executable_recipes import (
    RecipeSpec,
    default_applicability,
    default_validation_contract,
    execute_recipe,
    is_reuse_demonstration,
    match_recipe,
    normalize_recipe,
    recipe_learning_success,
    select,
)
from app.goal_review import ReviewStore, evidence_valid
from app.vehicle_auto import REVISION, extract
from app.vehicle_workflow import source_reconciliation_report
from test_vehicle_auto import ledger
from test_vehicle_workflow import setup


def _adapter():
    return {
        'adapter_id': 'vehicle_extract_pipeline',
        'version': '1',
        'function_id': 'vehicle_extract_pipeline',
        'params': {},
    }


def _recipe(**overrides):
    recipe = {
        'recipe_id': 'recipe-month-a',
        'version': 1,
        'status': 'verified',
        'executor': 'vehicle-auto-v1',
        'extractor_revision_min': REVISION,
        'adapters': [_adapter()],
        'mapping_rules': [],
        'allocation_rules': [],
        'applicability': default_applicability(
            source_format_signatures=['excel:workbook'],
            vendor=[],
            required_columns=['車両番号', '年月'],
            required_source_kinds=['master'],
            months_pattern=r'2026-0[1-7]',
            missing_policy='unknown',
        ),
        'validation_contract': default_validation_contract(),
        'provenance': {
            'human_result': 'sig-month-a',
            'source_hashes_used_for_validation': ['hash-month-a'],
            'months_used_for_validation': ['2026-01'],
        },
        'expires': time.time() + 86400,
        'experience_id': 'exp-1',
    }
    for key, value in overrides.items():
        if key in {'applicability', 'validation_contract', 'provenance'} and isinstance(value, dict):
            recipe[key] = {**recipe[key], **value}
        else:
            recipe[key] = value
    return recipe


def _snapshot(docs, months, hashes=None, missing_policy='unknown'):
    from app.executable_recipes import build_source_snapshot
    sources = []
    for source, raw, content in docs:
        item = dict(source)
        item.setdefault('sha256', (hashes or {}).get(source['id'], source['id'] + '-' + (source.get('filename') or '')))
        sources.append(item)
    return build_source_snapshot(docs, sources, months, REVISION, missing_policy)


def _month_docs(month, revenue):
    return [ledger([
        ['車両番号', '年月', '売上', '総支給額', '会社負担社会保険', '燃料費', '税込リース'],
        ['足立101か1234', month, revenue, 200, 30, 100, 50],
        ['合計', month, revenue, 200, 30, 100, 50],
    ])]


def test_recipe_spec_does_not_keep_amounts_or_verify_without_adapters():
    spec = RecipeSpec(
        recipe_id='r', status='verified', adapters=[],
        provenance={'human_result': 'h', 'amount': 999, 'copied_amounts': [1]},
    )
    row = spec.to_dict()
    assert row['status'] == 'needs_revalidation'
    assert 'amount' not in row['provenance']
    assert 'copied_amounts' not in row['provenance']
    normalized = normalize_recipe({
        'status': 'verified', 'adapters': [_adapter()],
        'human_result': 'h', 'amount': 1234,
        'applicability': {'no_amount_reuse': False},
    })
    assert 'amount' not in normalized
    assert normalized['applicability']['no_amount_reuse'] is True
    assert normalized['provenance']['human_result'] == 'h'


def test_transfer_to_other_month_and_source_hash_reuses_procedure_not_amounts():
    """1. 別月・別 source_hash の原本で recipe が再利用される。金額は月B。"""
    docs_a = _month_docs('2026-01', 1000)
    docs_b = _month_docs('2026-02', 7777)
    recipe = _recipe()
    recipe['adapters'][0]['params'] = {'amounts': {'足立101か1234|2026-01|revenue': 1000}}
    recipe['copied_amounts'] = [1000]
    snapshot_b = _snapshot(docs_b, ['2026-02'], hashes={'s': 'hash-month-b'})
    match = match_recipe(recipe, snapshot_b)
    assert match.matched, match.reasons
    result = execute_recipe(recipe, docs_b, ['2026-02'], source_hash='hash-month-b', snapshot=snapshot_b)
    assert result.applied is True and result.rejected is False
    assert result.reuse_demonstration is True
    amounts = {(r['month'], r['category']): r['amount'] for r in result.data['records'] if r.get('quality') == 'actual'}
    assert amounts[('2026-02', 'revenue')] == 7777
    assert 1000 not in amounts.values()
    assert not any(r.get('month') == '2026-01' for r in result.data['records'])
    assert result.recipe_id == 'recipe-month-a' and result.version == 1
    assert result.adapter_results and result.adapter_results[0]['status'] == 'applied'


def test_condition_mismatch_is_explicit_reject_not_silent_fallback():
    """2. 必須列欠落・vendor不一致・format signature不一致は明示拒否。silent fallbackしない。"""
    docs = _month_docs('2026-01', 1000)
    snapshot = _snapshot(docs, ['2026-01'], hashes={'s': 'hash-b'})
    missing_cols = _recipe(applicability={'required_columns': ['存在しない必須列']})
    vendor = _recipe(applicability={'vendor': ['usami']})
    signature = _recipe(applicability={'source_format_signatures': ['csv:table']})
    kinds = _recipe(applicability={'required_source_kinds': ['fuel']})
    for recipe, code in (
        (missing_cols, 'required_columns_missing'),
        (vendor, 'vendor_mismatch'),
        (signature, 'format_signature_mismatch'),
        (kinds, 'required_source_kinds_missing'),
    ):
        match = match_recipe(recipe, snapshot)
        assert match.matched is False
        assert any(r['code'] == code for r in match.reasons), (code, match.reasons)
        executed = execute_recipe(recipe, docs, ['2026-01'], source_hash='hash-b', snapshot=snapshot)
        assert executed.rejected is True and executed.applied is False
        assert executed.data is None
        assert any(r['code'] == code for r in executed.reasons)
        assert executed.envelope_fields()['recipe_applied'] is False
        assert executed.envelope_fields()['recipe_rejected'] is True
        assert executed.envelope_fields()['recipe_rejection']
    fallback = extract(docs, ['2026-01'], False)
    assert fallback['records']


def test_amounts_are_not_copied_from_recipe(tmp_path):
    """3. 金額が recipe から再利用されない。新原本の extract 結果のみ。"""
    docs = _month_docs('2026-02', 4321)
    recipe = _recipe(
        provenance={'human_result': 'sig-a', 'source_hashes_used_for_validation': ['hash-a'],
                    'months_used_for_validation': ['2026-01'], 'amount': 1000},
    )
    recipe['adapters'][0]['params'] = {'amounts': {'足立101か1234|2026-02|revenue': 1000}}
    snapshot = _snapshot(docs, ['2026-02'], hashes={'s': 'hash-b'})
    result = execute_recipe(recipe, docs, ['2026-02'], source_hash='hash-b', snapshot=snapshot)
    assert result.applied is True
    revenue = next(r for r in result.data['records'] if r['category'] == 'revenue' and r['quality'] == 'actual')
    assert revenue['amount'] == 4321
    assert revenue['amount'] != 1000


def test_reconciliation_failure_is_not_rag_success():
    """4. 独立原本照合に合格しない限り RAG 成功と表示しない。"""
    docs = [ledger([
        ['車両番号', '年月', '売上', '燃料費'],
        ['足立101か1234', '2026-02', 1000, 100],
    ])]
    recipe = _recipe()
    snapshot = _snapshot(docs, ['2026-02'], hashes={'s': 'hash-b'})
    result = execute_recipe(recipe, docs, ['2026-02'], source_hash='hash-b', snapshot=snapshot)
    assert result.applied is True
    _, summary, _ = source_reconciliation_report(result.data, snapshot['sources'])
    assert summary.get('passed') is not True
    assert recipe_learning_success(True, summary, True) is False
    assert result.rag_success is False
    fields = result.envelope_fields()
    assert fields['recipe_learning_success'] is False
    assert recipe_learning_success(True, {'passed': True}, False) is False
    assert recipe_learning_success(True, {'passed': True}, True) is True
    assert recipe_learning_success(False, {'passed': True}, True) is False


def test_same_hash_reprocess_is_not_reuse_demonstration():
    """5. 同一 hash の原本再処理は recipe 再利用の実証としてカウントされない。"""
    recipe = _recipe()
    assert is_reuse_demonstration(recipe, 'hash-month-a', ['2026-02']) is False
    assert is_reuse_demonstration(recipe, 'hash-month-b', ['2026-01']) is False
    assert is_reuse_demonstration(recipe, 'hash-month-a', ['2026-01']) is False
    assert is_reuse_demonstration(recipe, 'hash-month-b', ['2026-02']) is True
    docs = _month_docs('2026-01', 1000)
    snapshot = _snapshot(docs, ['2026-01'], hashes={'s': 'hash-month-a'})
    result = execute_recipe(recipe, docs, ['2026-01'], source_hash='hash-month-a', snapshot=snapshot)
    assert result.applied is True
    assert result.reuse_demonstration is False
    assert result.envelope_fields()['recipe_reuse_demonstration'] is False


def test_revoked_recipe_is_not_selected_regardless_of_evidence_valid(tmp_path):
    """6. revoked な recipe は evidence_valid に関わらず選択されない。"""
    manager, pid = setup(tmp_path)
    store = ReviewStore(manager.memory.path)
    human = 'sig-approved'
    snapshot = {'plan': 'p', 'artifacts': [], 'sources': [], 'workspace_path': 'projects/vehicle', 'workspace_root': str(tmp_path / 'workspace')}
    store.put(pid, 'result', human, {
        'status': 'approved', 'snapshot': snapshot, 'experience_id': 'exp-1',
        'expires': time.time() + 86400,
    })
    recipe = _recipe(status='revoked', provenance={'human_result': human, 'source_hashes_used_for_validation': ['h']})
    store.put(pid, 'executable_recipe', human, recipe)
    evidence = {'human_result': human, 'experience_id': 'exp-1'}
    # 当時の承認成果物が残っていても revoked recipe は選ばない
    assert evidence_valid(manager.memory.path, pid, evidence) in {True, False}
    assert select(manager, pid) is None
    match = match_recipe(recipe, _snapshot(_month_docs('2026-02', 1), ['2026-02'], hashes={'s': 'x'}))
    assert match.matched is False
    assert any(r['code'] == 'recipe_revoked' for r in match.reasons)
    live = _recipe(provenance={'human_result': human, 'source_hashes_used_for_validation': ['h']})
    store.put(pid, 'executable_recipe', human, live)
    assert select(manager, pid)['recipe_id'] == 'recipe-month-a'
    store.put(pid, 'result', human, {
        'status': 'revoked', 'snapshot': snapshot, 'experience_id': 'exp-1',
        'expires': time.time() + 86400,
    })
    assert select(manager, pid) is None
    store.put(pid, 'result', human, {
        'status': 'approved', 'snapshot': snapshot, 'experience_id': 'exp-1',
        'expires': time.time() + 86400,
    })
    live['status'] = 'revoked'
    store.put(pid, 'executable_recipe', human, live)
    assert select(manager, pid) is None


def test_prepare_records_recipe_id_version_and_does_not_copy_reference_only(tmp_path):
    manager, pid = setup(tmp_path)
    src, raw, _ = ledger([
        ['車両番号', '年月', '売上', '総支給額', '会社負担社会保険', '燃料費', '税込リース'],
        ['足立101か1234', '2026-01', 1000, 200, 30, 100, 50],
        ['合計', '2026-01', 1000, 200, 30, 100, 50],
    ])
    manager.memory.add_context_file(pid, src['filename'], '月次原本', len(raw), source='upload', data=raw)
    recipe = _recipe(applicability={'source_format_signatures': ['excel:workbook'], 'vendor': [],
                                    'required_columns': ['車両番号'], 'required_source_kinds': ['master']})
    ReviewStore(manager.memory.path).put(pid, 'executable_recipe', 'sig-a', recipe)
    from app.vehicle_auto import prepare
    mission = manager.memory.get_mission(pid)
    envelope = prepare(manager, pid, mission)
    assert envelope.get('recipe_reference') in (None, '')
    assert envelope.get('recipe_applied') is True
    assert envelope.get('recipe_id') == 'recipe-month-a'
    assert envelope.get('recipe_version') == 1
    assert envelope.get('recipe_rule_results')
    revenue = next(r for r in envelope['data']['records'] if r['category'] == 'revenue' and r['quality'] == 'actual')
    assert revenue['amount'] == 1000
    from app.vehicle_workflow import read_json, resolve
    proof = read_json(resolve(manager, pid, 'vehicle_profit/automatic_proof.json'))
    assert proof.get('recipe_id') == 'recipe-month-a'
    assert proof.get('recipe_version') == 1
    assert proof.get('recipe_applied') is True
    assert proof.get('recipe_learning_success') is False


def test_prepare_rejects_mismatch_then_runs_unapplied_extract(tmp_path):
    manager, pid = setup(tmp_path)
    src, raw, _ = ledger()
    manager.memory.add_context_file(pid, src['filename'], '月次原本', len(raw), source='upload', data=raw)
    recipe = _recipe(applicability={'vendor': ['usami'], 'required_source_kinds': ['fuel']})
    ReviewStore(manager.memory.path).put(pid, 'executable_recipe', 'sig-a', recipe)
    from app.vehicle_auto import prepare
    envelope = prepare(manager, pid, manager.memory.get_mission(pid))
    assert envelope.get('recipe_applied') is False
    assert envelope.get('recipe_rejected') is True
    assert envelope.get('recipe_rejection')
    codes = {r['code'] for r in envelope['recipe_rejection']}
    assert 'vendor_mismatch' in codes or 'required_source_kinds_missing' in codes
    assert envelope['data']['records']
    assert envelope.get('recipe_learning_success') is False
