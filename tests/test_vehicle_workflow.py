import copy
import json
from pathlib import Path
import pytest
from app.structured_planning import extract_criteria,validate_plan,contract_of
from app.vehicle_workflow import requested_months,make_plan,normalize_input,validate_evidence,goal_failures,sources,digest,write_json,resolve,INPUT_PATH,source_reconciliation_report
from app.vehicle_profit import calculate
from app.memory.short_term import ShortTermMemory
from app.workspace_files import WorkspaceSandbox
from app.project_manager import ProjectOrchestrator
from app.upgrade_runtime import ReviewRequired


def fixture():
    data={'schema':'vehicle-profit-input-v1','rounding':'yen_half_up','basis_note':'架空テスト。税込・給与非課税。',
          'months':['2026-01'],'vehicles':[{'id':'A-1234'},{'id':'B-1234'}],'records':[],
          'scope_evidence':'原本の全2車両・1月を照合','source_dispositions':[{'source_ref':'context:s','status':'included','evidence':'全明細照合'}],'source_controls':[]}
    for vehicle in data['vehicles']:
        for category in ['revenue','payroll','insurance','fuel','toll','lease','other']:
            amount={'revenue':1000,'payroll':200,'insurance':30,'fuel':100,'lease':50}.get(category,0)
            data['records'].append({'id':vehicle['id']+category,'month':'2026-01','company':'C','category':category,'amount':amount,
                                    'tax_basis':'non_taxable' if category in {'payroll','insurance'} else 'inclusive','quality':'actual',
                                    'source_ref':'context:s','source_locator':vehicle['id']+'/'+category,'allocations':[{'vehicle_id':vehicle['id'],'ratio':1}]})
    for category in ['revenue','payroll','insurance','fuel','toll','lease','other']:
        records=[r for r in data['records'] if r['category']==category]
        data['source_controls'].append({'source_ref':'context:s','company':'C','month':'2026-01','category':category,
                                       'tax_basis':records[0]['tax_basis'],'invoice_id':None,'origin':'source_total',
                                       'status':'available','control_kind':'category_total',
                                       'amount':sum(r['amount'] for r in records),'source_locator':'原本合計/'+category})
    return data


def setup(tmp_path):
    memory=ShortTermMemory(tmp_path/'memory.db');p=memory.create_project('車両計算',workspace_path='projects/vehicle');pid=p['id']
    memory.save_mission(pid,'車両別損益をExcelで作成','2026年1月から1月まで\n車両ごとに月別集計','',False,[])
    class NeverLlm:
        async def complete(self,*args,**kwargs):raise AssertionError('No LLM or external AI needed')
    manager=ProjectOrchestrator(memory,NeverLlm(),lambda p:('',[]),None,lambda:[],workspace=WorkspaceSandbox(tmp_path/'workspace'))
    return manager,pid


def test_unnumbered_criteria_and_period_are_preserved():
    assert extract_criteria('', '2026年1月から7月\n車両ごとの集計')==['2026年1月から7月','車両ごとの集計']
    assert requested_months({'success_criteria':'２０２６年１月から７月','goal':''})==[f'2026-{m:02d}' for m in range(1,8)]


@pytest.mark.asyncio
async def test_typed_plan_covers_criteria_and_can_be_approved(tmp_path):
    manager,pid=setup(tmp_path);mission=await manager.generate_plan(pid)
    assert validate_plan({'tasks':mission['tasks']},{'SC01','SC02'})['passed']
    assert any(o['path'].endswith('.xlsx') for t in mission['tasks'] for o in contract_of(t)['outputs'])
    assert manager.approve(pid)['status']=='ready'
    task=next(t for t in mission['tasks'] if contract_of(t)['execution_kind']=='vehicle_calculate')
    with pytest.raises(ReviewRequired,match='計算は未実施'):
        await manager._execute_task(pid,task['id'])
    assert goal_failures(manager,pid)


def test_missing_one_vehicle_does_not_become_zero_or_hide_other_vehicle():
    data=fixture();data['records']=[r for r in data['records'] if r['id']!='A-1234insurance']
    result=calculate(normalize_input(data,['2026-01']))
    assert result['rows'][0]['insurance'] is None and result['rows'][0]['profit'] is None
    assert result['rows'][1]['profit']==620


def test_mixed_tax_basis_and_unallocated_amount_block_final_profit():
    data=fixture();data['records'][0]['tax_basis']='exclusive'
    assert calculate(normalize_input(data,['2026-01']))['rows'][0]['profit'] is None
    data=fixture();data['records'][0]['allocations'][0]['ratio']=0.5
    assert all(row['profit'] is None for row in calculate(normalize_input(data,['2026-01']))['rows'])


def test_evidence_rejects_unknown_source_unreadable_and_control_mismatch():
    data=fixture();snapshot=[{'id':'s','quality':'readable'}]
    assert not validate_evidence(data,snapshot,True)
    assert validate_evidence(data,snapshot,False)
    data['source_controls'][0]['amount']+=1
    assert any('不一致' in x for x in validate_evidence(data,snapshot,True))
    assert any('読取不足' in x for x in validate_evidence(fixture(),[{'id':'s','quality':'garbled'}],True))


@pytest.mark.asyncio
async def test_old_documents_cannot_satisfy_vehicle_goal(tmp_path):
    manager,pid=setup(tmp_path)
    _,status,unmet=await manager._final_report(pid)
    assert status=='failed' and '実計算工程' in unmet[0]
    assert not manager._append_goal_supplement(pid,unmet,'')


@pytest.mark.asyncio
async def test_missing_recalculator_preserves_partial_and_requires_review(tmp_path,monkeypatch):
    manager,pid=setup(tmp_path)
    item=manager.memory.add_context_file(pid,'source.txt','原本',6,source='upload')
    mission=await manager.generate_plan(pid);task=next(t for t in mission['tasks'] if contract_of(t)['execution_kind']=='vehicle_calculate')
    data=fixture()
    for row in data['records']+data['source_controls']+data['source_dispositions']:row['source_ref']='context:'+item['id']
    snapshot=sources(manager,pid)
    write_json(resolve(manager,pid,INPUT_PATH),{'data':data,'confirmed':True,'source_hash':digest(snapshot)})
    monkeypatch.setattr('app.vehicle_profit.shutil.which',lambda _:None)
    with pytest.raises(ReviewRequired,match='途中版Excel'):
        await manager._execute_task(pid,task['id'])
    assert resolve(manager,pid,contract_of(task)['outputs'][1]['path']).exists()
    assert goal_failures(manager,pid)


def test_suffix_matching_rejects_ambiguous_vehicle_ids():
    from app.vehicle_workflow import match_vehicle
    vehicles=[{'id':'足立101か1845','company':'A'},{'id':'足立101か2845','company':'B'}]
    with pytest.raises(ValueError,match='2件'):match_vehicle('845',vehicles)
    assert match_vehicle('845',vehicles,'A')=='足立101か1845'
    assert match_vehicle('2845',vehicles)=='足立101か2845'


@pytest.mark.asyncio
async def test_input_endpoint_conflicts_and_resets_only_affected_steps(tmp_path,monkeypatch):
    import app.web as web
    from fastapi import HTTPException
    manager,pid=setup(tmp_path);source=manager.memory.add_context_file(pid,'source.txt','原本',6,source='upload')
    mission=await manager.generate_plan(pid)
    await manager._execute_task(pid,mission['tasks'][0]['id'])
    manager.memory.update_task(mission['tasks'][0]['id'],'completed',result='読取完了')
    manager.memory.update_task(mission['tasks'][1]['id'],'needs_review',error='入力待ち')
    monkeypatch.setattr(web,'memory',manager.memory);monkeypatch.setattr(web,'orchestrator',manager)
    data=fixture();snapshot=sources(manager,pid);data['source_snapshot']=snapshot
    for row in data['records']+data['source_controls']+data['source_dispositions']:row['source_ref']='context:'+source['id']
    payload=web.VehicleInputPayload(version=mission['plan_version'],data=data,confirmed=True)
    response=await web.save_vehicle_profit_input(pid,payload)
    tasks=manager.memory.get_mission(pid)['tasks']
    assert response['changed'] and tasks[0]['status']=='completed' and tasks[1]['status']=='pending'
    with pytest.raises(HTTPException) as error:await web.save_vehicle_profit_input(pid,payload)
    assert error.value.status_code==409
    payload.previous_hash=response['input_hash']
    response=await web.save_vehicle_profit_input(pid,payload)
    assert not response['changed']


def _fuel_case(lines, control_amount=1000, origin='source_total', status='available', excluded=None, extra_controls=None, extra_records=None):
    data={'schema':'vehicle-profit-input-v1','rounding':'yen_half_up','basis_note':'照合試験',
          'months':['2026-01'],'vehicles':[{'id':'A-1234'}],
          'scope_evidence':'試験','source_dispositions':[{'source_ref':'context:s','status':'included','evidence':'明細'}],
          'source_controls':[],'records':[],'excluded_records':list(excluded or [])}
    if extra_controls is None:
        data['source_controls'].append({'source_ref':'context:s','company':'C','month':'2026-01','category':'fuel',
                                        'tax_basis':'inclusive','invoice_id':None,'origin':origin,'status':status,
                                        'control_kind':'invoice_total','amount':control_amount,'source_locator':'鑑!B20'})
    else:
        data['source_controls'].extend(extra_controls)
    for i,(amount,locator,alloc) in enumerate(lines,1):
        data['records'].append({'id':'fuel'+str(i),'month':'2026-01','company':'C','category':'fuel','amount':amount,
                                'tax_basis':'inclusive','quality':'actual','source_ref':'context:s','source_locator':locator,
                                'allocations':alloc})
    if extra_records:
        data['records'].extend(extra_records)
    return data


def _row_status(data, snapshot=None, **parts):
    _, summary, rows = source_reconciliation_report(data, snapshot)
    matches=[r for r in rows if all(r.get(k)==v for k,v in parts.items())]
    assert matches, (parts, rows)
    return matches[0], summary


def test_source_total_1000_with_600_and_400_is_matched():
    data=_fuel_case([(600,'CSV行2',[{'vehicle_id':'A-1234','ratio':1}]),(400,'CSV行3',[{'vehicle_id':'A-1234','ratio':1}])])
    row, summary=_row_status(data, category='fuel')
    assert row['status']=='matched' and row['difference']==0
    assert summary['passed'] is True
    snapshot=[{'id':'s','quality':'readable'}]
    assert not validate_evidence(data,snapshot,True)


def test_missing_400_is_mismatched_and_not_passed_by_empty_issues():
    data=_fuel_case([(600,'CSV行2',[{'vehicle_id':'A-1234','ratio':1}])])
    row, summary=_row_status(data, category='fuel')
    assert row['status']=='mismatched' and row['difference']==400
    assert summary['passed'] is not True
    snapshot=[{'id':'s','quality':'readable'}]
    issues=validate_evidence(data,snapshot,True)
    assert any('不一致' in x for x in issues)
    assert (summary['passed'] is True) is False


def test_duplicate_line_is_detected_as_mismatch_or_double_count():
    data=_fuel_case([(600,'CSV行2',[{'vehicle_id':'A-1234','ratio':1}]),
                    (400,'CSV行3',[{'vehicle_id':'A-1234','ratio':1}]),
                    (400,'CSV行3',[{'vehicle_id':'A-1234','ratio':1}])])
    data['records'][2]['id']='fuel3'
    row, summary=_row_status(data, category='fuel')
    assert row['duplicate_identities']>=1 or row['difference']
    assert row['status']=='mismatched'
    assert summary['passed'] is not True
    snapshot=[{'id':'s','quality':'readable'}]
    issues=validate_evidence(data,snapshot,True)
    assert any('二重計上' in x or '不一致' in x for x in issues)


def test_grounded_exclusion_matches_and_ungrounded_exclusion_fails():
    grounded=dict(source_ref='context:s',company='C',month='2026-01',category='fuel',tax_basis='inclusive',
                  amount=100,source_locator='CSV行9',reason='対象外車両',approver='human')
    ok=_fuel_case([(900,'CSV行2',[{'vehicle_id':'A-1234','ratio':1}])], excluded=[grounded])
    row, summary=_row_status(ok, category='fuel')
    assert row['status']=='matched' and row['excluded_total']==100
    assert summary['passed'] is True
    snapshot=[{'id':'s','quality':'readable'}]
    assert not validate_evidence(ok,snapshot,True)
    ungrounded=dict(source_ref='context:s',company='C',month='2026-01',category='fuel',tax_basis='inclusive',amount=100)
    bad=_fuel_case([(900,'CSV行2',[{'vehicle_id':'A-1234','ratio':1}])], excluded=[ungrounded])
    bad_row, bad_summary=_row_status(bad, category='fuel')
    assert bad_row['status']!='matched' and bad_summary['passed'] is not True
    assert validate_evidence(bad,snapshot,True)


def test_unavailable_control_does_not_set_source_controls_true():
    data=_fuel_case([(1000,'CSV行2',[{'vehicle_id':'A-1234','ratio':1}])], extra_controls=[
        {'source_ref':'context:s','company':'C','month':'2026-01','category':'fuel','tax_basis':'inclusive',
         'invoice_id':None,'origin':'none','status':'unavailable','control_kind':'invoice_total','amount':None,
         'reason_code':'no_independent_total','reason':'明細のみ','source_locator':''}])
    row, summary=_row_status(data,[{'id':'s','quality':'readable'}], category='fuel')
    assert row['status']=='unavailable'
    assert summary['passed'] is not True
    assert summary['unavailable']>=1
    issues=validate_evidence(data,[{'id':'s','quality':'readable'}],True)
    assert issues
    assert (summary['passed'] is True) is False
    empty=_fuel_case([(1000,'CSV行2',[{'vehicle_id':'A-1234','ratio':1}])], extra_controls=[])
    _, empty_summary, _=source_reconciliation_report(empty, [{'id':'s','quality':'readable'}])
    assert empty_summary['passed'] is not True
    assert empty_summary['unavailable']>=1


def test_unallocated_is_included_in_source_match_but_blocks_vehicle_completion():
    data=_fuel_case([(600,'CSV行2',[{'vehicle_id':'A-1234','ratio':1}]),(400,'CSV行3',[])])
    row, summary=_row_status(data, category='fuel')
    assert row['status']=='matched' and row['included_total']==1000 and row['unallocated_total']==400
    assert summary['passed'] is True
    snapshot=[{'id':'s','quality':'readable'}]
    assert not validate_evidence(data,snapshot,True)


def test_distinct_source_ref_or_invoice_id_are_not_mixed():
    extra_controls=[
        {'source_ref':'context:a','company':'C','month':'2026-01','category':'fuel','tax_basis':'inclusive','invoice_id':None,
         'origin':'source_total','status':'available','control_kind':'invoice_total','amount':500,'source_locator':'合計A'},
        {'source_ref':'context:b','company':'C','month':'2026-01','category':'fuel','tax_basis':'inclusive','invoice_id':None,
         'origin':'source_total','status':'available','control_kind':'invoice_total','amount':800,'source_locator':'合計B'},
    ]
    data=_fuel_case([], extra_controls=extra_controls)
    data['source_dispositions']=[
        {'source_ref':'context:a','status':'included','evidence':'a'},
        {'source_ref':'context:b','status':'included','evidence':'b'},
    ]
    data['records']=[
        {'id':'ra','month':'2026-01','company':'C','category':'fuel','amount':500,'tax_basis':'inclusive','quality':'actual',
         'source_ref':'context:a','source_locator':'A1','allocations':[{'vehicle_id':'A-1234','ratio':1}]},
        {'id':'rb','month':'2026-01','company':'C','category':'fuel','amount':800,'tax_basis':'inclusive','quality':'actual',
         'source_ref':'context:b','source_locator':'B1','allocations':[{'vehicle_id':'A-1234','ratio':1}]},
    ]
    _, summary, rows=source_reconciliation_report(data)
    assert all(r['status']=='matched' for r in rows) and summary['passed'] is True
    invoiced=_fuel_case([
        (100,'C1',[{'vehicle_id':'A-1234','ratio':1}]),
        (200,'C2',[{'vehicle_id':'A-1234','ratio':1}]),
    ])
    invoiced['records'][0]['invoice_id']='INV-1'
    invoiced['records'][1]['invoice_id']='INV-2'
    invoiced['source_controls']=[
        {'source_ref':'context:s','company':'C','month':'2026-01','category':'fuel','tax_basis':'inclusive','invoice_id':'INV-1',
         'origin':'source_total','status':'available','control_kind':'invoice_total','amount':100,'source_locator':'鑑1'},
        {'source_ref':'context:s','company':'C','month':'2026-01','category':'fuel','tax_basis':'inclusive','invoice_id':'INV-2',
         'origin':'source_total','status':'available','control_kind':'invoice_total','amount':200,'source_locator':'鑑2'},
    ]
    inv_row,_=_row_status(invoiced, invoice_id='INV-1')
    other,_=_row_status(invoiced, invoice_id='INV-2')
    assert inv_row['status']=='matched' and other['status']=='matched'
    mixed=_fuel_case([(100,'C1',[{'vehicle_id':'A-1234','ratio':1}]),(200,'C2',[{'vehicle_id':'A-1234','ratio':1}])])
    mixed['records'][0]['invoice_id']='INV-1'
    mixed['records'][1]['invoice_id']='INV-2'
    mixed['source_controls']=[{'source_ref':'context:s','company':'C','month':'2026-01','category':'fuel','tax_basis':'inclusive',
                               'invoice_id':'INV-1','origin':'source_total','status':'available','control_kind':'invoice_total',
                               'amount':300,'source_locator':'鑑混同'}]
    mixed_one,_=_row_status(mixed, invoice_id='INV-1')
    mixed_two,_=_row_status(mixed, invoice_id='INV-2')
    assert mixed_one['included_total']==100
    assert mixed_two['status']=='unavailable'


def test_sum_of_lines_only_controls_do_not_pass():
    data=_fuel_case([(600,'CSV行2',[{'vehicle_id':'A-1234','ratio':1}]),(400,'CSV行3',[{'vehicle_id':'A-1234','ratio':1}])],
                   origin='sum_of_lines')
    row, summary=_row_status(data, category='fuel')
    assert row['origin']=='sum_of_lines' and row['status']=='unavailable'
    assert summary['passed'] is not True
    snapshot=[{'id':'s','quality':'readable'}]
    assert validate_evidence(data,snapshot,True)
    assert (summary['passed'] is True) is False


def test_manual_json_control_mismatch_is_still_rejected():
    data=fixture();snapshot=[{'id':'s','quality':'readable'}]
    assert not validate_evidence(data,snapshot,True)
    assert validate_evidence(data,snapshot,False)
    data['source_controls'][0]['amount']+=1
    assert any('不一致' in x for x in validate_evidence(data,snapshot,True))
    assert any('読取不足' in x for x in validate_evidence(fixture(),[{'id':'s','quality':'garbled'}],True))


def test_auto_extraction_empty_issues_without_independent_controls_is_not_a_pass():
    data=fixture()
    data['auto_extraction']={'issues':[],'recoveries':[]}
    data['source_controls']=[]
    snapshot=[{'id':'s','quality':'readable'}]
    issues=validate_evidence(data,snapshot,True)
    assert issues
    _, summary, _=source_reconciliation_report(data, snapshot)
    assert summary['passed'] is not True


def test_auto_extraction_joins_common_reconciliation_and_keeps_read_issues():
    data=fixture()
    data['auto_extraction']={'issues':[{'id':'r1','kind':'read','source_ref':'context:s','locator':'Sheet!C1',
                                        'message':'金額を数値として読み取れません','status':'unresolved','raw_text':'要確認',
                                        'reason_code':'non_numeric','month':'2026-01','category':'fuel'}],'recoveries':[]}
    snapshot=[{'id':'s','quality':'readable'}]
    issues=validate_evidence(data,snapshot,True)
    assert issues
    assert any('読み取れません' in x for x in issues)


def _fake_completed_workbook(data, directory, cancelled=lambda: False, source_reconciliation=None):
    from openpyxl import Workbook
    from app.vehicle_profit import calculate as calc
    directory=Path(directory); directory.mkdir(parents=True, exist_ok=True)
    path=directory/'profit.xlsx'
    wb=Workbook(); sheet=wb.active; sheet.title='サマリー'
    sheet.append(['車両ID','車両名','年月','売上','給与','会社負担保険','燃料','高速','リース（税込）','その他','利益','状態'])
    computed=calc(data)
    for r in computed['rows']:
        sheet.append([r['vehicle'],r.get('label',r['vehicle']),r['month'],r['revenue'],r['payroll'],r['insurance'],
                      r['fuel'],r['toll'],r['lease'],r['other'],r['profit'],r['quality']])
    wb.save(path)
    return dict(status='completed',warnings=[],file=str(path),
                checks={'recalculation':True,'formula_errors':0,'independent_profit_match':True,
                        'vehicle_summary_match':True,'input_allocation_reconciled':True})


@pytest.mark.asyncio
async def test_matched_controls_can_complete(tmp_path, monkeypatch):
    manager,pid=setup(tmp_path)
    item=manager.memory.add_context_file(pid,'source.txt','原本',6,source='upload')
    mission=await manager.generate_plan(pid)
    task=next(t for t in mission['tasks'] if contract_of(t)['execution_kind']=='vehicle_calculate')
    data=fixture()
    for row in data['records']+data['source_controls']+data['source_dispositions']:
        row['source_ref']='context:'+item['id']
    snapshot=sources(manager,pid)
    write_json(resolve(manager,pid,INPUT_PATH),{'data':data,'confirmed':True,'source_hash':digest(snapshot)})
    monkeypatch.setattr('app.vehicle_workflow.build_workbook', _fake_completed_workbook)
    message=await manager._execute_task(pid,task['id'])
    from app.vehicle_workflow import read_json
    result=read_json(resolve(manager,pid,contract_of(task)['outputs'][0]['path']))
    assert result['status']=='completed'
    assert result['checks']['source_controls'] is True
    recon=result['checks']['source_reconciliation']
    assert recon['passed'] is True and recon['mismatched']==0 and recon['unavailable']==0
    assert recon['artifact']
    assert '完了' in message


@pytest.mark.asyncio
async def test_missing_line_blocks_completion_despite_excel_recalc_match(tmp_path, monkeypatch):
    manager,pid=setup(tmp_path)
    item=manager.memory.add_context_file(pid,'source.txt','原本',6,source='upload')
    mission=await manager.generate_plan(pid)
    task=next(t for t in mission['tasks'] if contract_of(t)['execution_kind']=='vehicle_calculate')
    data=fixture()
    data['records']=[r for r in data['records'] if r['id']!='A-1234fuel']
    for row in data['records']+data['source_controls']+data['source_dispositions']:
        row['source_ref']='context:'+item['id']
    snapshot=sources(manager,pid)
    write_json(resolve(manager,pid,INPUT_PATH),{'data':data,'confirmed':True,'source_hash':digest(snapshot)})
    monkeypatch.setattr('app.vehicle_workflow.build_workbook', _fake_completed_workbook)
    with pytest.raises(ReviewRequired):
        await manager._execute_task(pid,task['id'])
    from app.vehicle_workflow import read_json
    result=read_json(resolve(manager,pid,contract_of(task)['outputs'][0]['path']))
    assert result['status']!='completed'
    assert result['checks']['independent_profit_match'] is True
    assert result['checks']['source_controls'] is not True
    assert result['checks']['source_reconciliation']['passed'] is not True
    assert resolve(manager,pid,contract_of(task)['outputs'][1]['path']).exists()


@pytest.mark.asyncio
async def test_unallocated_blocks_vehicle_completion_gate(tmp_path, monkeypatch):
    manager,pid=setup(tmp_path)
    item=manager.memory.add_context_file(pid,'source.txt','原本',6,source='upload')
    mission=await manager.generate_plan(pid)
    task=next(t for t in mission['tasks'] if contract_of(t)['execution_kind']=='vehicle_calculate')
    data=fixture()
    next(r for r in data['records'] if r['id']=='A-1234fuel')['allocations']=[]
    for row in data['records']+data['source_controls']+data['source_dispositions']:
        row['source_ref']='context:'+item['id']
    snapshot=sources(manager,pid)
    write_json(resolve(manager,pid,INPUT_PATH),{'data':data,'confirmed':True,'source_hash':digest(snapshot)})
    monkeypatch.setattr('app.vehicle_workflow.build_workbook', _fake_completed_workbook)
    with pytest.raises(ReviewRequired):
        await manager._execute_task(pid,task['id'])
    from app.vehicle_workflow import read_json
    result=read_json(resolve(manager,pid,contract_of(task)['outputs'][0]['path']))
    recon=result['checks']['source_reconciliation']
    fuel_rows=[r for r in source_reconciliation_report(data, snapshot)[2] if r.get('category')=='fuel']
    assert fuel_rows and fuel_rows[0]['status']=='matched' and fuel_rows[0]['unallocated_total']==100
    assert result['status']!='completed'
    assert any('未配賦' in x for x in result['issues'])
