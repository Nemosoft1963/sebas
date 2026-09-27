import io
import pytest
from openpyxl import Workbook
from fastapi import HTTPException

from app.vehicle_auto import extract, drop_assumed_zero_when_actual, REVISION
from app.vehicle_profit import calculate
from app.vehicle_workflow import (
    AllocationRuleError, make_allocation_rule, validate_allocation_rule, APPLY_THIS_MONTH,
)
from app.vehicle_service import decide, reuse_decisions, state
from app.web import VehicleDecisionPayload


def book(sheets):
    b = Workbook()
    first = True
    for name, rows in sheets:
        s = b.active if first else b.create_sheet(name)
        if first:
            s.title = name
            first = False
        for row in rows:
            s.append(row)
    stream = io.BytesIO()
    b.save(stream)
    return stream.getvalue()


def master_rows():
    header = ['車両番号', 'x', 'x', 'x', '会社', '月額税込', '開始', '終了', 'x', 'x', 'x', 'x', 'x', '氏名']
    return [
        header,
        ['足立101か1234', '', '', '', '関東', 50, '2026-01', '2026-03', '', '', '', '', '', '山田太郎'],
        ['足立102か5678', '', '', '', '関東', 50, '2026-04', '2026-07', '', '', '', '', '', '山田太郎'],
    ]


def payroll_rows(insurance_header=None, extra_headers=None, amounts=None, names=None):
    headers = ['氏名', '月/回', '支給合計']
    if insurance_header:
        headers.append(insurance_header)
    if extra_headers:
        headers.extend(extra_headers)
    rows = [headers]
    names = names or ['山田太郎'] * 7
    amounts = amounts or [1000] * 7
    for i, mon in enumerate([f'2026-{m:02d}' for m in range(1, 8)]):
        row = [names[i] if i < len(names) else names[-1], mon, amounts[i] if i < len(amounts) else amounts[-1]]
        if insurance_header:
            row.append(30)
        rows.append(row)
    return rows


def docs_transfer():
    raw = book([('台帳', master_rows()), ('給与', payroll_rows())])
    return [({'id': 'm', 'filename': '関東台帳.xlsx', 'quality': 'readable'}, raw, '')]


def test_transfer_does_not_assign_all_months_to_one_vehicle():
    data = extract(docs_transfer(), [f'2026-{m:02d}' for m in range(1, 8)], False)
    payroll = [r for r in data['records'] if r['category'] == 'payroll' and r['quality'] == 'actual']
    by_month = {r['month']: r for r in payroll}
    assert by_month['2026-01']['allocations'] == [{'vehicle_id': '足立101か1234', 'ratio': 1}]
    assert by_month['2026-03']['allocations'] == [{'vehicle_id': '足立101か1234', 'ratio': 1}]
    assert by_month['2026-04']['allocations'] == [{'vehicle_id': '足立102か5678', 'ratio': 1}]
    assert by_month['2026-07']['allocations'] == [{'vehicle_id': '足立102か5678', 'ratio': 1}]
    assert {a['vehicle_id'] for r in payroll for a in r['allocations']} == {'足立101か1234', '足立102か5678'}


def test_split_allocation_is_saved_and_calculate_splits_both_vehicles():
    raw = book([
        ('台帳', [
            ['車両番号', 'x', 'x', 'x', '会社', '月額税込', '開始', '終了', 'x', 'x', 'x', 'x', 'x', '氏名'],
            ['足立101か1234', '', '', '', '関東', 50, '', '', '', '', '', '', '', '山田太郎'],
            ['足立102か5678', '', '', '', '関東', 50, '', '', '', '', '', '', '', '山田太郎'],
        ]),
        ('給与', [['氏名', '月/回', '支給合計'], ['山田太郎', '2026-01', 1000]]),
    ])
    data = extract([({'id': 'm', 'filename': '関東台帳.xlsx', 'quality': 'readable'}, raw, '')], ['2026-01'], False)
    payroll = next(r for r in data['records'] if r['category'] == 'payroll' and r['quality'] == 'actual')
    assert payroll['allocations'] == []
    issue = next(i for i in data['auto_extraction']['issues'] if i['kind'] == 'allocation')
    payroll['allocations'] = [{'vehicle_id': '足立101か1234', 'ratio': '0.5'}, {'vehicle_id': '足立102か5678', 'ratio': '0.5'}]
    issue['status'] = 'resolved'
    result = calculate(data)
    rows = {r['vehicle']: r for r in result['rows'] if r['month'] == '2026-01'}
    assert rows['足立101か1234']['payroll'] == 500
    assert rows['足立102か5678']['payroll'] == 500
    assert payroll.get('unallocated') in (None, 0) or next(d['unallocated'] for d in result['details'] if d['id'] == payroll['id']) == 0


def test_allocation_rule_rejects_sum_over_one_nonpositive_company_mismatch_overlap():
    vehicles = [{'id': '足立101か1234', 'company': '関東'}, {'id': '足立102か5678', 'company': '弘和'}]
    with pytest.raises(AllocationRuleError, match='合計は1'):
        validate_allocation_rule(make_allocation_rule(
            subject_key={'company': '関東', 'employee': '山田太郎'},
            effective_from='2026-01', effective_to='2026-01',
            allocations=[{'vehicle_id': '足立101か1234', 'ratio': '0.6'}, {'vehicle_id': '足立102か5678', 'ratio': '0.6'}],
        ), vehicles)
    with pytest.raises(AllocationRuleError, match='0より大きい'):
        validate_allocation_rule(make_allocation_rule(
            subject_key={'company': '関東', 'employee': '山田太郎'},
            allocations=[{'vehicle_id': '足立101か1234', 'ratio': '0'}],
        ), [{'id': '足立101か1234', 'company': '関東'}])
    with pytest.raises(AllocationRuleError, match='会社'):
        validate_allocation_rule(make_allocation_rule(
            subject_key={'company': '関東', 'employee': '山田太郎'},
            allocations=[{'vehicle_id': '足立102か5678', 'ratio': '1'}],
        ), vehicles, records=[{'company': '関東'}])
    existing = [make_allocation_rule(
        subject_key={'company': '関東', 'employee': '山田太郎'},
        effective_from='2026-01', effective_to='2026-03',
        allocations=[{'vehicle_id': '足立101か1234', 'ratio': '1'}],
    )]
    with pytest.raises(AllocationRuleError, match='重複'):
        validate_allocation_rule(make_allocation_rule(
            subject_key={'company': '関東', 'employee': '山田太郎'},
            effective_from='2026-03', effective_to='2026-05',
            allocations=[{'vehicle_id': '足立101か1234', 'ratio': '1'}],
        ), [{'id': '足立101か1234', 'company': '関東'}], existing)


def _two_vehicle_master():
    return [
        ['車両番号', 'x', 'x', 'x', '会社', '月額税込', '開始', '終了', 'x', 'x', 'x', 'x', 'x', '氏名'],
        ['足立101か1234', '', '', '', '関東', 50, '', '', '', '', '', '', '', '山田太郎'],
        ['足立102か5678', '', '', '', '関東', 50, '', '', '', '', '', '', '', '山田太郎'],
    ]


@pytest.mark.asyncio
async def test_source_change_is_stale_same_hash_is_reused(tmp_path):
    from test_vehicle_workflow import setup
    from app.vehicle_auto import prepare
    from app.vehicle_workflow import load_input
    manager, pid = setup(tmp_path)
    manager.memory.save_mission(pid, '車両別損益をExcelで作成', '2026年1月から1月まで\n車両ごとに月別集計', '', False, [])
    raw = book([('台帳', _two_vehicle_master()), ('給与', [['氏名', '月/回', '支給合計'], ['山田太郎', '2026-01', 1000]])])
    manager.memory.add_context_file(pid, '関東台帳.xlsx', '台帳', len(raw), source='upload', data=raw)
    mission = await manager.generate_plan(pid)
    prepare(manager, pid, mission)
    issue = next(i for i in load_input(manager, pid)['data']['auto_extraction']['issues'] if i['kind'] == 'allocation' and i.get('month') == '2026-01')
    decide(manager, pid, mission['plan_version'], state(manager, pid)['input_hash'], issue['id'],
           '足立101か1234', False, '当月は車両A', apply_to='this_month')
    same = prepare(manager, pid, mission)
    jan = next(r for r in same['data']['records'] if r['category'] == 'payroll' and r['month'] == '2026-01')
    assert jan['allocations'] and jan['allocations'][0]['vehicle_id'] == '足立101か1234'
    assert jan.get('allocation_reused') or any(i.get('resolution') == 'reused' for i in same['data']['auto_extraction']['issues'])
    changed = book([('台帳', _two_vehicle_master()), ('給与', [['氏名', '月/回', '支給合計'], ['山田太郎', '2026-01', 9999]])])
    manager.memory.add_context_file(pid, '関東台帳.xlsx', '差替', len(changed), source='upload', data=changed)
    stale = prepare(manager, pid, mission)
    jan2 = next(r for r in stale['data']['records'] if r['category'] == 'payroll' and r['month'] == '2026-01')
    assert not jan2.get('allocation_reused')
    assert not jan2.get('allocations')
    issues = [i for i in stale['data']['auto_extraction']['issues'] if i.get('kind') == 'allocation' and i.get('month') == '2026-01']
    assert issues and issues[0]['status'] == 'unresolved'
    assert issues[0].get('reuse_reason') in ('stale_decision', 'needs_revalidation', 'invalid')


def test_zero_vehicle_match_is_business_fact_read_failure_not_in_allocation_ui():
    raw = book([
        ('台帳', [
            ['車両番号', 'x', 'x', 'x', '会社', '月額税込', '開始', '終了', 'x', 'x', 'x', 'x', 'x', '氏名'],
            ['足立101か1234', '', '', '', '関東', 50, '', '', '', '', '', '', '', '山田太郎'],
        ]),
        ('給与', [['氏名', '月/回', '支給合計'], ['佐藤花子', '2026-01', 800]]),
    ])
    data = extract([({'id': 'm', 'filename': '関東台帳.xlsx', 'quality': 'readable'}, raw, '')], ['2026-01'], False)
    alloc = [i for i in data['auto_extraction']['issues'] if i['kind'] == 'allocation']
    assert alloc
    assert any('帰属' in i['message'] or '確認' in i['message'] for i in alloc)
    unread = extract([
        ({'id': 's', 'filename': '関東月次.xlsx', 'quality': 'readable'}, book([('汎用', [
            ['車両番号', '年月', '売上', '燃料費'],
            ['足立101か1234', '2026-01', 1000, '要確認'],
        ])]), ''),
    ], ['2026-01'], True)
    reads = [i for i in unread['auto_extraction']['issues'] if i['kind'] == 'read']
    assert reads
    questions = [i for i in unread['auto_extraction']['issues'] if i['status'] == 'unresolved' and i['kind'] != 'read']
    assert not any(i['kind'] == 'read' for i in questions)
    assert not any('要確認' in (i.get('message') or '') and i['kind'] == 'allocation' for i in unread['auto_extraction']['issues'])


def test_insurance_explicit_column_actual_missing_column_issue_personal_rejected():
    with_col = book([('台帳', master_rows()[:2]), ('給与', payroll_rows(insurance_header='社保会社負担'))])
    data = extract([({'id': 'm', 'filename': '関東台帳.xlsx', 'quality': 'readable'}, with_col, '')], ['2026-01'], False)
    ins = [r for r in data['records'] if r['category'] == 'insurance' and r['quality'] == 'actual']
    assert ins and ins[0]['amount'] == 30
    assert ins[0].get('insurance_basis', {}).get('kind') == 'explicit_employer_amount'
    no_col = book([('台帳', master_rows()[:2]), ('給与', payroll_rows())])
    missing = extract([({'id': 'n', 'filename': '関東台帳.xlsx', 'quality': 'readable'}, no_col, '')], ['2026-01'], True)
    assert not any(r['category'] == 'insurance' and r['quality'] == 'actual' for r in missing['records'])
    assert any(i['kind'] == 'insurance_basis' for i in missing['auto_extraction']['issues'])
    assert not any(r['category'] == 'insurance' and r['quality'] == 'assumed_zero' for r in missing['records'])
    personal = book([('台帳', master_rows()[:2]), ('給与', [
        ['氏名', '月/回', '支給合計', '社会保険料'],
        ['山田太郎', '2026-01', 1000, 40],
    ])])
    rejected = extract([({'id': 'p', 'filename': '関東台帳.xlsx', 'quality': 'readable'}, personal, '')], ['2026-01'], True)
    assert not any(r['category'] == 'insurance' and r['quality'] == 'actual' for r in rejected['records'])
    assert any(i.get('reason_code') == 'personal_deduction_rejected' or '本人控除' in i.get('message', '') for i in rejected['auto_extraction']['issues'])


def test_removed_vehicle_invalidates_old_decision():
    vehicles = [{'id': '足立101か1234', 'company': '関東'}]
    envelope = {
        'source_hash': 'newhash',
        'data': {
            'vehicles': vehicles,
            'records': [dict(id='r1', month='2026-01', category='payroll', amount=100, company='関東',
                             allocation_subject='山田太郎', allocations=[], quality='actual',
                             tax_basis='non_taxable', source_ref='context:s', source_locator='A1')],
            'auto_extraction': {'issues': [dict(id='i1', kind='allocation', status='unresolved', month='2026-01',
                                                allocation_subject='山田太郎', record_id='r1', message='帰属',
                                                source_ref='context:s', locator='A1')]},
            'decision_log': [],
        },
        'assignments': [],
        'decisions': [],
    }
    old = {
        'source_hash': 'newhash',
        'assignments': [make_allocation_rule(
            subject_key={'company': '関東', 'employee': '山田太郎'},
            effective_from='2026-01', effective_to='2026-01',
            allocations=[{'vehicle_id': '足立102か5678', 'ratio': 1}],
            source_hash='newhash',
        )],
        'decisions': [dict(subject_key={'company': '関東', 'employee': '山田太郎'}, months=['2026-01'],
                           allocations=[{'vehicle_id': '足立102か5678', 'ratio': 1}], source_hash='newhash')],
        'data': {},
    }
    reuse_decisions(envelope, old)
    issue = envelope['data']['auto_extraction']['issues'][0]
    assert issue['status'] == 'unresolved'
    assert issue.get('reuse_reason') == 'invalid'
    assert any(a.get('status') == 'invalid' for a in envelope['assignments'])


def test_assumed_zero_dropped_only_after_allocated_actual():
    records = [
        dict(id='zA', quality='assumed_zero', month='2026-01', category='fuel', amount=0,
             allocations=[{'vehicle_id': 'A', 'ratio': 1}]),
        dict(id='zB', quality='assumed_zero', month='2026-01', category='fuel', amount=0,
             allocations=[{'vehicle_id': 'B', 'ratio': 1}]),
        dict(id='u', quality='actual', month='2026-01', category='fuel', amount=100, allocations=[]),
    ]
    log = []
    drop_assumed_zero_when_actual(records, log, allocated_only=True)
    assert {r['id'] for r in records} == {'zA', 'zB', 'u'}
    records.append(dict(id='aA', quality='actual', month='2026-01', category='fuel', amount=40,
                        allocations=[{'vehicle_id': 'A', 'ratio': 1}]))
    drop_assumed_zero_when_actual(records, log, allocated_only=True)
    ids = {r['id'] for r in records}
    assert 'zA' not in ids and 'zB' in ids
    assert any(x.get('action') == 'drop_assumed_zero' for x in log)


def test_decision_payload_defaults_to_this_month():
    payload = VehicleDecisionPayload(version=1, input_hash='h', issue_id='i', reason='当月のみ配賦する')
    assert payload.apply_to == APPLY_THIS_MONTH
    assert payload.allocations == []


@pytest.mark.asyncio
async def test_decide_this_month_does_not_apply_to_all_group_months(tmp_path):
    from test_vehicle_workflow import setup
    from app.vehicle_auto import prepare
    from app.vehicle_workflow import load_input
    manager, pid = setup(tmp_path)
    manager.memory.save_mission(pid, '車両別損益をExcelで作成', '2026年1月から2月まで\n車両ごとに月別集計', '', False, [])
    raw = book([
        ('台帳', [
            ['車両番号', 'x', 'x', 'x', '会社', '月額税込', '開始', '終了', 'x', 'x', 'x', 'x', 'x', '氏名'],
            ['足立101か1234', '', '', '', '関東', 50, '', '', '', '', '', '', '', '山田太郎'],
            ['足立102か5678', '', '', '', '関東', 50, '', '', '', '', '', '', '', '山田太郎'],
        ]),
        ('給与', [['氏名', '月/回', '支給合計'], ['山田太郎', '2026-01', 1000], ['山田太郎', '2026-02', 1000]]),
    ])
    manager.memory.add_context_file(pid, '関東台帳.xlsx', '台帳', len(raw), source='upload', data=raw)
    mission = await manager.generate_plan(pid)
    prepare(manager, pid, mission)
    status = state(manager, pid)
    jan_issue = next(q for q in status['questions'] if q['kind'] == 'allocation' and (q.get('month') == '2026-01' or True))
    issues = [i for i in load_input(manager, pid)['data']['auto_extraction']['issues'] if i['kind'] == 'allocation']
    jan = next(i for i in issues if i.get('month') == '2026-01')
    decide(manager, pid, mission['plan_version'], state(manager, pid)['input_hash'], jan['id'],
           '足立101か1234', False, '1月のみ車両Aへ', apply_to='this_month')
    data = load_input(manager, pid)['data']
    jan_rec = next(r for r in data['records'] if r['category'] == 'payroll' and r['month'] == '2026-01')
    feb_rec = next(r for r in data['records'] if r['category'] == 'payroll' and r['month'] == '2026-02')
    assert jan_rec['allocations'] == [{'vehicle_id': '足立101か1234', 'ratio': 1}]
    assert not feb_rec['allocations']


@pytest.mark.asyncio
async def test_decide_over_ratio_returns_422(tmp_path, monkeypatch):
    from test_vehicle_workflow import setup
    from app.vehicle_auto import prepare
    from app.vehicle_workflow import load_input
    import app.web as web
    manager, pid = setup(tmp_path)
    manager.memory.save_mission(pid, '車両別損益をExcelで作成', '2026年1月から1月まで\n車両ごとに月別集計', '', False, [])
    raw = book([
        ('台帳', [
            ['車両番号', 'x', 'x', 'x', '会社', '月額税込', '開始', '終了', 'x', 'x', 'x', 'x', 'x', '氏名'],
            ['足立101か1234', '', '', '', '関東', 50, '', '', '', '', '', '', '', '山田太郎'],
            ['足立102か5678', '', '', '', '関東', 50, '', '', '', '', '', '', '', '山田太郎'],
        ]),
        ('給与', [['氏名', '月/回', '支給合計'], ['山田太郎', '2026-01', 1000]]),
    ])
    manager.memory.add_context_file(pid, '関東台帳.xlsx', '台帳', len(raw), source='upload', data=raw)
    mission = await manager.generate_plan(pid)
    prepare(manager, pid, mission)
    issue = next(i for i in load_input(manager, pid)['data']['auto_extraction']['issues'] if i['kind'] == 'allocation')
    monkeypatch.setattr(web, 'memory', manager.memory)
    monkeypatch.setattr(web, 'orchestrator', manager)
    payload = VehicleDecisionPayload(
        version=mission['plan_version'], input_hash=state(manager, pid)['input_hash'], issue_id=issue['id'],
        reason='比率超過の確認', allocations=[
            {'vehicle_id': '足立101か1234', 'ratio': '0.6'},
            {'vehicle_id': '足立102か5678', 'ratio': '0.6'},
        ],
    )
    with pytest.raises(HTTPException) as err:
        await web.decide_vehicle_profit(pid, payload)
    assert err.value.status_code == 422


def test_revision_p13():
    assert REVISION == '20260927.2'
