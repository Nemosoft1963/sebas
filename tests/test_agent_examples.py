import copy
import json
import pytest
from app.agent_examples import ExampleStore,Conflict
from app.vehicle_profit import calculate,build_workbook

def fixture():
    return {'schema':'vehicle-profit-input-v1','rounding':'yen_half_up','basis_note':'架空入力。円・税抜。給与非課税。',
      'vehicles':[{'id':'A-0001'},{'id':'B-0001'}],'months':['2026-09'],
      'records':[{'id':'r1','month':'2026-09','company':'C','category':'revenue','tax_basis':'exclusive','quality':'actual','amount':1000,'source_ref':'fixture','allocations':[{'vehicle_id':'A-0001','ratio':1}]},
      {'id':'r2','month':'2026-09','company':'C','category':'payroll','tax_basis':'non_taxable','quality':'actual','amount':400,'source_ref':'fixture','allocations':[{'vehicle_id':'A-0001','ratio':0.5},{'vehicle_id':'B-0001','ratio':0.5}]}]}

def test_store_isolation_conflicts_reported_and_invalidation(tmp_path):
    s=ExampleStore(tmp_path);x=s.import_text('p','report.md','# 作業\n完了・承認済み\n## 課題\n- 元資料を確認\n')
    assert s.import_text('p','report.md',x['source'])['id']==x['id']
    assert s.list('other')==[]
    with pytest.raises(KeyError):s.get('other',x['id'])
    x=s.parse('p',x['id'],1)
    assert x['extraction']['reported_checks'][0]['status']=='reported'
    assert not x['extraction']['observed_checks']
    with pytest.raises(Conflict):s.parse('p',x['id'],1)
    x=s.procedure('p',x['id'],x['revision'],'vehicle_profit_v1')
    p=x['procedures'][0]
    with pytest.raises(ValueError):s.review('p',x['id'],x['revision'],1,p['hash'],'reusable','tester','proof')
    x=s.resolve_issue('p',x['id'],x['revision'],x['issues'][0]['id'],'checked','fixture')
    assert x['procedures'][0]['status']=='revoked'

def test_ids_allocation_and_reconciliation():
    data=fixture();result=calculate(data)
    assert [r['profit'] for r in result['rows']]==[800,-200]
    assert not result['warnings']
    for r in result['reconciliation']:assert r['input']==r['allocated']+r['unallocated']
    data['records'][0]['allocations'][0]['vehicle_id']='0001'
    with pytest.raises(ValueError):calculate(data)

@pytest.mark.parametrize('edit',[lambda d:d['records'].append(copy.deepcopy(d['records'][0])),lambda d:d['records'][0].update(amount='NaN'),lambda d:d['records'][0]['allocations'].append({'vehicle_id':'B-0001','ratio':1}),lambda d:d['vehicles'].append({'id':'A-0001'}),lambda d:d['records'][0].update(quality='estimated')])
def test_bad_inputs_fail(edit):
    d=fixture();edit(d)
    with pytest.raises(ValueError):calculate(d)

def test_missing_estimates_residual_not_success():
    d=fixture();d['records'][0].update(amount=None,quality='missing')
    result=calculate(d);assert result['warnings'];assert result['details'][0]['allocated'] is None
    d=fixture();d['records'][0]['allocations']=[]
    result=calculate(d);assert result['details'][0]['unallocated']==1000;assert result['warnings']

def test_workbook_recalculation_and_formula_injection(tmp_path,monkeypatch):
    from openpyxl import load_workbook
    d=fixture();d['vehicles'][0]['label']='=HYPERLINK("https://invalid")'
    monkeypatch.setattr('app.vehicle_profit.shutil.which',lambda _:None)
    result=build_workbook(d,tmp_path)
    assert result['status']=='needs_review'
    wb=load_workbook(result['file']);assert wb['サマリー']['B2'].data_type=='s'
    assert wb['サマリー']['K2'].value=='=D2-SUM(E2:J2)'

def test_api_project_boundary_and_no_shared_context(tmp_path):
    from types import SimpleNamespace
    from fastapi import FastAPI,HTTPException
    from fastapi.testclient import TestClient
    from app.agent_examples_api import install
    app=FastAPI()
    def require(pid):
        if pid not in {'p','q'}:raise HTTPException(404)
    env=SimpleNamespace(DATA_DIR=tmp_path,require_project=require,DB_PATH=tmp_path/'memory'/'db')
    install(app,env)
    with TestClient(app) as client:
        resp=client.post('/api/projects/p/agent-examples',json={'filename':'x.md','source':'# example\nreported complete'})
        assert resp.status_code==200;x=resp.json()
        assert client.get('/api/projects/q/agent-examples/'+x['id']).status_code==404
        assert client.get('/api/projects/no/agent-examples').status_code==404
        assert not (tmp_path/'memory').exists()
        assert client.post('/api/projects/p/agent-examples/'+x['id']+'/parse',json={'revision':1}).status_code==200
        assert client.post('/api/projects/p/agent-examples/'+x['id']+'/parse',json={'revision':1}).status_code==409
        assert client.get('/api/projects/p/agent-examples/template').json()['schema']=='vehicle-profit-input-v1'


def test_instruction_version_guard(tmp_path):
    from app.memory.short_term import ShortTermMemory
    memory=ShortTermMemory(tmp_path/'memory.db')
    project=memory.create_project('fixture');pid=project['id']
    memory.save_mission(pid,'fixture','','',False,[])
    with memory._connect() as db:
        db.execute("UPDATE project_missions SET goal='fixture',plan_version=3,status='planning' WHERE project_id=?",(pid,))
    with pytest.raises(ValueError):memory.add_mission_instruction(pid,'reference',expected_plan_version=2)
    assert 'reference' not in memory.get_mission(pid)['constraints_text']
    memory.add_mission_instruction(pid,'reference',expected_plan_version=3)
    assert memory.get_mission(pid)['status']=='planning'

def test_revoked_procedure_excluded_from_rag(tmp_path):
    import time
    from app.experience_memory import ExperienceMemory
    s=ExampleStore(tmp_path/'agent_examples');x=s.import_text('p','a.md','# A')
    x=s.parse('p',x['id'],x['revision']);x=s.procedure('p',x['id'],x['revision'],'vehicle_profit_v1');p=x['procedures'][0]
    def reviewed(item):item['procedures'][0]['status']='reusable'
    x=s.change('p',x['id'],x['revision'],'fixture',reviewed)
    class Index:
        def search(self,*args):return [rid]
    memory=ExperienceMemory(tmp_path/'memory'/'experience_memory',{},Index())
    rid=memory.store.add('p','success','generic lesson',{}, {'example_id':x['id'],'procedure_hash':p['hash']})
    memory.store.review('p',rid,'verified','test','fixture',time.time()+1000)
    assert memory.retrieve({'project':'p','mode':'enforce'},'query')
    s.resolve_issue('p',x['id'],x['revision'],x['issues'][0]['id'],'changed','new evidence')
    assert not memory.retrieve({'project':'p','mode':'enforce'},'query')
