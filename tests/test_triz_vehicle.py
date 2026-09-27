"""P2-1 受入条件 1〜7。候補保存・encoding 復元を業務回復にしない。"""
from pathlib import Path

import pytest
from app.automatic_triz import (
    AdapterSpec,
    VehicleFailure,
    adopt_adapter,
    adapters_for_prepare,
    apply_explicit_adapter,
    can_adopt,
    candidate_json_is_not_business_recovered,
    classify_error_code,
    encoding_repair_is_not_business_success,
    is_business_success,
    library_status_label,
    register_adapter,
    reset_adapter_registry,
    revoke_adapter,
    run_limited_rerun,
    save_vehicle_failure,
    triz_display,
    validate_vehicle_business,
)
from app.structured_planning import contract_of
from app.triz_general import create, defined_library_display
from app.triz_import import load_export_items
from app.upgrade_runtime import ReviewRequired
from app.vehicle_auto import apply_adopted_adapters, extract
from app.workflow_readiness import UNIMPLEMENTED_ACTIONS, build_readiness
from test_vehicle_auto import ledger
from test_vehicle_workflow import fixture, setup


ROOT = Path(__file__).resolve().parents[1]
EXPORT = ROOT / 'inputs' / 'triz-problems-export.json'


def _passing_data():
    return fixture()


def _fixture_expected(data, vehicle_id, month='2026-01'):
    expected = []
    for record in data['records']:
        allocations = record.get('allocations') or [{}]
        vid = allocations[0].get('vehicle_id')
        if vid != vehicle_id:
            continue
        expected.append({
            'vehicle_id': vid,
            'month': record['month'],
            'category': record['category'],
            'amount': record['amount'],
        })
    return {'expected': expected}


def _spec(**overrides):
    values = dict(
        adapter_id='trial_amount_map',
        version='1',
        function_id='trial_amount_map',
        params={},
        provenance={'origin': 'test'},
        status='candidate',
        mapping_rules=[],
        allocation_rules=[],
        applicability={'source_refs': ['context:s'], 'vendor': ['usami']},
    )
    values.update(overrides)
    return AdapterSpec(**values)


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_adapter_registry()
    yield
    reset_adapter_registry()


@pytest.mark.asyncio
async def test_vehicle_review_required_writes_triz_json(tmp_path):
    """1. 車両 execute が ReviewRequired のとき result/triz/...json が書ける。"""
    manager, pid = setup(tmp_path)
    mission = await manager.generate_plan(pid)
    task = next(t for t in mission['tasks'] if contract_of(t)['execution_kind'] == 'vehicle_calculate')
    with pytest.raises(ReviewRequired, match='計算は未実施'):
        await manager._execute_task(pid, task['id'])
    files = [p for p in (tmp_path / 'workspace').rglob('*.json') if 'triz' in p.parts]
    assert files, '車両失敗の TRIZ JSON が保存されていません'
    import json
    body = json.loads(files[0].read_text(encoding='utf-8'))
    assert body.get('vehicle_failure')
    assert body.get('status') in {'framed', 'development_required'}
    assert body.get('business_passed') is False
    assert is_business_success(body['status']) is False


def test_unreadable_zero_is_development_required_not_hidden_by_candidates(tmp_path):
    """2. 要確認0円化は development_required。candidates の採用で隠れない。"""
    manager, pid = setup(tmp_path)
    data = extract([ledger([['車両番号', '年月', '売上', '燃料費'], ['足立101か1234', '2026-01', 1000, '要確認']])], ['2026-01'], True)
    task = {'id': 'vehicle_extract', 'title': '抽出', 'acceptance_criteria': '{}'}
    error = ReviewRequired('要確認の読取失敗を0円として確定しません')
    record = save_vehicle_failure(manager, pid, task, error, library_items=[{'domain': 'operations', 'goal': 'dummy', 'evidence': '抽出'}])
    assert record['status'] == 'development_required'
    assert record['known_p0_defect'] is True
    assert 'test_vehicle_auto' in record.get('test_ref', '')
    with pytest.raises(ValueError, match='既知P0'):
        adopt_adapter(
            pid, _spec(), 'tester', 'proof', {'source_refs': ['context:s']},
            {'business_passed': True, 'reproduction_ok': True, 'transfer_ok': True},
            {'business_passed': True, 'reproduction_ok': True, 'transfer_ok': True},
            record=record,
        )
    assert record['status'] == 'development_required'
    assert not adapters_for_prepare(pid)


def test_adopt_requires_both_reproduction_and_transfer(tmp_path):
    """3. 人工adapterが reproduction と transfer の両方合格したときだけ adopted 可。"""
    pid = 'p'
    data = _passing_data()
    snapshot = [{'id': 's', 'quality': 'readable'}]
    repro = _fixture_expected(data, 'A-1234')
    transfer_data = fixture()
    for row in transfer_data['records'] + transfer_data['source_controls'] + transfer_data['source_dispositions']:
        if row.get('month') == '2026-01':
            row['month'] = '2026-02'
    transfer_data['months'] = ['2026-02']
    for control in transfer_data['source_controls']:
        control['month'] = '2026-02'
    transfer = _fixture_expected(transfer_data, 'B-1234', '2026-02')
    repro_ok = validate_vehicle_business(data, snapshot, reproduction=repro, transfer=None, params={'amounts': {'A-1234|2026-01|fuel': 100}})
    # 片側だけ: transfer 未実施
    transfer_fail = validate_vehicle_business(data, snapshot, reproduction=None, transfer=transfer, params={'amounts': {'A-1234|2026-01|fuel': 100}})
    assert can_adopt(repro_ok, transfer_fail) is False
    with pytest.raises(ValueError, match='両方'):
        adopt_adapter(pid, _spec(), 'tester', 'proof', {'source_refs': ['context:s']}, repro_ok, transfer_fail)
    transfer_ok = validate_vehicle_business(transfer_data, snapshot, reproduction=None, transfer=transfer, params={
        'amounts': {
            'A-1234|2026-01|fuel': 100,
            'B-1234|2026-02|fuel': 100,
        }
    })
    adopted = adopt_adapter(pid, _spec(params={
        'amounts': {
            'A-1234|2026-01|fuel': 100,
            'B-1234|2026-02|fuel': 100,
        }
    }), 'tester', '両方合格の証拠', {'source_refs': ['context:s']}, repro_ok, transfer_ok)
    assert adopted['status'] == 'adopted'
    assert adapters_for_prepare(pid, source_ref='context:s')


def test_candidate_json_or_encoding_repaired_is_not_business_recovered():
    """4. 候補JSONを置いただけ、または encoding_repaired だけでは business_recovered にならない。"""
    assert candidate_json_is_not_business_recovered({'status': 'candidate_generated', 'candidates': [{}]}) is True
    assert is_business_success('candidate_generated') is False
    assert is_business_success('encoding_repaired') is False
    assert encoding_repair_is_not_business_success('encoding_repaired') is True
    assert triz_display('encoding_repaired')['recovery_success'] is False
    assert triz_display('framed')['recovery_success'] is False
    assert triz_display('artifact_trial_passed')['business_recovered'] is False


def test_p0_2_difference_fails_business_validation():
    """5. P0-2 差額が残る候補は business validation 不合格。"""
    data = fixture()
    data['source_controls'][0]['amount'] += 400
    snapshot = [{'id': 's', 'quality': 'readable'}]
    result = validate_vehicle_business(data, snapshot)
    assert result['business_passed'] is False
    assert 'p0_2_reconciliation_not_matched' in result['errors']


def test_candidate_not_applied_before_adopt_and_limited_rerun_revokes(tmp_path):
    """6. 人間採用前に本処理へ候補を適用しない。限定再実行失敗時に adopted→revoked。"""
    pid = 'p'
    spec = _spec()
    register_adapter(pid, spec, adopted=False)
    data = _passing_data()
    unchanged = apply_adopted_adapters(pid, data, source_ref='context:s')
    assert unchanged['records'][0]['amount'] == data['records'][0]['amount']
    with pytest.raises(ValueError, match='人間採用前'):
        run_limited_rerun({'status': 'candidate', 'adapter_id': 'trial_amount_map'}, True)
    adopted = adopt_adapter(
        pid, spec, 'tester', 'proof', {'source_refs': ['context:s']},
        {'business_passed': True, 'reproduction_ok': True, 'transfer_ok': True},
        {'business_passed': True, 'reproduction_ok': True, 'transfer_ok': True},
    )
    revoked, status = run_limited_rerun(adopted, False)
    assert status == 'revoked'
    assert revoked['status'] == 'revoked' and revoked.get('disabled') is True
    revoke_adapter(pid, adopted['adapter_id'])
    assert adapters_for_prepare(pid, source_ref='context:s') == []
    recovered, recovered_status = run_limited_rerun(adopted, True)
    assert recovered_status == 'business_recovered'
    assert recovered['status'] == 'adopted'


def test_defined_library_is_not_business_recovered():
    """7. 74件 defined を業務回復済みとみなす試験が無いこと。表示は定義済みであり検証済みにならない。"""
    items = load_export_items(EXPORT)
    assert len(items) == 74
    from app.triz_import import export_item_to_values
    item = {}
    session = create(item, export_item_to_values(items[0]))
    assert session['status'] == 'defined'
    display = defined_library_display(session)
    assert display['label'] == '定義済み'
    assert display['verified'] is False
    assert display['business_recovered'] is False
    assert library_status_label('defined') == '定義済み'
    assert library_status_label('defined') != '検証済み'
    assert is_business_success('defined') is False
    assert triz_display('defined')['recovery_success'] is False
    for row in items:
        values = export_item_to_values(row)
        assert values['domain']
        # defined 件数を採用数・回復数として数えない
        assert 'business_recovered' not in values
        assert 'verified' not in values


def test_adapter_spec_forbids_dynamic_exec():
    with pytest.raises(ValueError, match='動的exec'):
        AdapterSpec(adapter_id='evil', function_id='eval')
        apply_explicit_adapter('eval', {})
    with pytest.raises(ValueError, match='動的exec'):
        apply_explicit_adapter('__import__', {})
    out = apply_explicit_adapter('identity_passthrough', {'ok': 1})
    assert out == {'ok': 1}


def test_readiness_does_not_treat_framed_as_recovery(tmp_path):
    manager, pid = setup(tmp_path)
    row = build_readiness(manager, pid)
    assert 'triz_adopt' not in row['allowed_actions']
    blocked = {x['id']: x['reason'] for x in row['blocked_actions']}
    assert 'triz_adopt' in blocked
    assert '回復成功ではありません' in blocked['triz_adopt'] or 'blocked' in blocked['triz_adopt']
    unimplemented = {ident for ident, _ in UNIMPLEMENTED_ACTIONS}
    assert unimplemented.isdisjoint(row['allowed_actions'])
    assert row['triz']['recovery_success'] is False
    assert row['triz']['adopt_allowed'] is False


def test_vehicle_failure_dataclass_fields():
    failure = VehicleFailure(
        stage='vehicle_calculate', error_code='unknown_format', source_hash='a',
        requirements_hash='b', input_hash=None, source_refs=['context:s'],
        reproduction_fixture_ref='fx', acceptance_contract={},
    )
    assert failure.error_code == 'unknown_format'
    assert classify_error_code('要確認の読取失敗') == 'p0_unreadable_or_zero'
    assert classify_error_code('未対応形式の adapter 失敗') == 'unknown_format'
