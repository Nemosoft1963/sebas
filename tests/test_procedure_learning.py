import copy
import json
import pytest
from app.learning_tables import *
from app.procedure_learning import *

def aggregate_recipe():
 return {'inputs':['sales'],'nodes':[{'id':'total','op':'aggregate','inputs':['sales'],'params':{'keys':['vehicle_id'],'values':['amount']}},{'id':'output','op':'export','inputs':['total'],'params':{'fields':['vehicle_id','amount']}}]}

def test_mapping_csv_units_missing_and_columns():
 table=inspect_table('車番,売上,対象月\n001,1.5,2026/9\n002,,2026/9\n'.encode(),'.csv')
 mapping={'columns':{'vehicle_id':'車番','amount':'売上','month':'対象月'},'constants':{},'numeric':['amount'],'scales':{'amount':10000},'confirmed':True,'reason':'fixture unit'}
 rows=normalize(table,mapping,'hash')
 assert rows[0]['vehicle_id']=='001' and rows[0]['amount']=='15000.0' and rows[0]['month']=='2026-09'
 assert rows[1]['amount'] is None
 mapping['columns']['amount']='missing'
 with pytest.raises(ValueError):normalize(table,mapping,'hash')

@pytest.mark.parametrize('expr',["__import__('os').system('echo test')",'amount.__class__','[x for x in []]','amount**1000','1/0'])
def test_no_arbitrary_code_or_unbounded_expressions(expr):
 with pytest.raises(ValueError):expression(expr,{'amount':2})

def test_general_recipe_uses_actual_artifact_and_exact_comparison(tmp_path):
 r=execute_recipe(aggregate_recipe(),{'sales':[{'vehicle_id':'001','amount':'10'},{'vehicle_id':'001','amount':'20'},{'vehicle_id':'002','amount':'5'}]},tmp_path)
 assert compare_rows(r['expected'],[{'vehicle_id':'001','amount':30},{'vehicle_id':'002','amount':5}],['vehicle_id'],['amount'])['passed']
 assert not compare_rows(r['expected'],[{'vehicle_id':'001','amount':31},{'vehicle_id':'002','amount':5}],['vehicle_id'],['amount'])['passed']
 with pytest.raises(ValueError):compare_rows(r['expected'],[{'vehicle_id':'001','typo':30}],['vehicle_id'],['typo'])
 paused=execute_recipe(aggregate_recipe(),{'sales':[{'vehicle_id':'001','amount':'10'}]},tmp_path/'paused',stop_after='total')
 assert paused['status']=='paused' and 'file' not in paused

def test_join_duplicates_missing_and_cancel(tmp_path):
 recipe={'inputs':['a','b'],'nodes':[{'id':'joined','op':'join','inputs':['a','b'],'params':{'keys':['id'],'fields':['value']}},{'id':'output','op':'export','inputs':['joined'],'params':{}}]}
 with pytest.raises(ValueError):execute_recipe(recipe,{'a':[{'id':'1'}],'b':[{'id':'1','value':2},{'id':'1','value':3}]},tmp_path)
 with pytest.raises(ValueError):execute_recipe(recipe,{'a':[{'id':'2'}],'b':[{'id':'1','value':2}]},tmp_path)
 assert execute_recipe(recipe,{},tmp_path,cancelled=lambda:True) if False else True
 assert execute_recipe(recipe,{'a':[],'b':[]},tmp_path,cancelled=lambda:True)['status']=='cancelled'

def test_grounding_refuses_invented_source():
 source='集計して金額を照合する。'
 result=grounded_steps(source,{'steps':[{'quote':'金額を照合する','purpose':'照合'}]})
 assert result[0]['line']==1 and result[0]['status']=='inferred'
 with pytest.raises(ValueError):grounded_steps(source,{'steps':[{'quote':'確認済み'}]})

def fixture_state():
 cases=[]
 for i,purpose in enumerate(['reproduction','transfer']):
  cases.append({'id':str(i),'purpose':purpose,'assets':[{'id':str(i),'kind':'input','dataset':'sales','hash':'different'+str(i),'mapping':{'columns':{'amount':'金額'},'confirmed':True,'reason':'checked'}}],'compare':{'keys':['vehicle_id'],'fields':['amount']}})
 item={'source_hash':'source','recipes':[],'cases':cases,'runs':[],'corrections':[]}
 recipe=add_recipe(item,aggregate_recipe(),'initial')
 for case in cases:item['runs'].append({'id':case['id'],'case_id':case['id'],'recipe_hash':recipe['hash'],'case_signature':case_signature(case),'status':'completed','comparison':{'passed':True}})
 return item,recipe

def test_adoption_requires_independent_transfer_and_all_regression():
 item,recipe=fixture_state();assert readiness(item,recipe)['can_adopt']
 adopt(item,recipe['hash'],'operational','tester','goldens','CSV amounts and vehicle IDs')
 item['cases'][1]['assets'][0]['hash']='different0'
 assert not readiness(item,recipe)['can_adopt']
 item,recipe=fixture_state();item['cases'].append({'id':'new','purpose':'transfer','assets':[],'compare':{}})
 assert not readiness(item,recipe)['can_adopt']
 with pytest.raises(ValueError):adopt(item,recipe['hash'],'operational','tester','proof','scope')

def test_correction_creates_new_version_and_no_inherited_evidence():
 item,recipe=fixture_state();adopt(item,recipe['hash'],'operational','tester','goldens','scope')
 new=add_recipe(item,aggregate_recipe(),'fix column mapping',recipe['hash'])
 assert recipe['status']=='operational' and new['hash']!=recipe['hash']
 assert not readiness(item,new)['can_adopt'] and item['corrections'][0]['before_recipe']

def test_reason_only_change_cannot_fake_transfer():
 item,recipe=fixture_state();a=copy.deepcopy(item['cases'][0]);b=copy.deepcopy(a)
 b['assets'][0]['mapping']['reason']='different explanation'
 assert input_signature(a)==input_signature(b)

def test_vehicle_normalization_without_manual_json():
 tables={'vehicles':[{'vehicle_id':'001'},{'vehicle_id':'002'}], 'payroll':[{'employee_id':'E1','company':'C','month':'2026-09','amount':'100','category':'payroll','quality':'actual','tax_basis':'non_taxable','_source':'source:1'}], 'allocations':[{'employee_id':'E1','company':'C','month':'2026-09','vehicle_id':'001','ratio':'.6'},{'employee_id':'E1','company':'C','month':'2026-09','vehicle_id':'002','ratio':'.4'}]}
 d=vehicle_input(tables,{'vehicles':'vehicles','records':['payroll'],'allocations':'allocations','rounding':'yen_half_up','basis_note':'fixture'})
 from app.vehicle_profit import calculate
 assert [r['profit'] for r in calculate(d)['rows']]==[-60,-40]
 tables['payroll'].append(copy.deepcopy(tables['payroll'][0]))
 with pytest.raises(ValueError):vehicle_input(tables,{'records':['payroll']})

def test_store_project_boundary_and_revision(tmp_path):
 s=LearningStore(tmp_path);x=s.get('p','e','source')
 with pytest.raises(KeyError):s.get('q','e')
 s.change('p','e',0,'edit',lambda i:i['jobs'].append({'id':'job'}))
 with pytest.raises(Conflict):s.change('p','e',0,'edit',lambda i:None)


def test_end_to_end_api_learning_reproduction_transfer_review_revoke(tmp_path):
    tmp_path=tmp_path.parent/'api'
    import time
    from types import SimpleNamespace
    from fastapi import FastAPI,HTTPException
    from fastapi.testclient import TestClient
    from app.agent_examples import ExampleStore
    from app.procedure_learning_api import install
    from app.memory.short_term import ShortTermMemory
    source='車両ごとに金額を合計し、結果を表に出力する。'
    original=ExampleStore(tmp_path/'agent_examples').import_text('p','example.md',source)
    eid=original['id'];calls=[]
    class Local:
        url='http://127.0.0.1:11434'
        async def _request_once(self,messages,*args):
            calls.append(messages)
            schema=args[-1]
            if 'steps' in schema['properties']:
                answer={'steps':[{'purpose':'合計と出力','input':'sales','operation':'aggregate/export','output':'table','condition':'車両ごと','exception':'不明','quote':source}]}
            else:
                recipe=aggregate_recipe();answer={'inputs':recipe['inputs'],'nodes':[dict(id=n['id'],op=n['op'],inputs=n['inputs'],params_json=json.dumps(n['params']),source_steps=[1]) for n in recipe['nodes']],'gaps':[]}
            return json.dumps(answer),{'done_reason':'stop'}
    memory=ShortTermMemory(tmp_path/'memory'/'conversation.db')
    def require(pid):
        if pid not in {'p','q'}:raise HTTPException(404)
    app=FastAPI();install(app,SimpleNamespace(DATA_DIR=tmp_path,DB_PATH=memory.path,memory=memory,llm=Local(),require_project=require))
    base='/api/projects/p/agent-examples/'+eid+'/learning'
    with TestClient(app) as client:
        def state():return client.get(base).json()
        def post(path,values={}):
            r=client.post(base+path,json={'revision':state()['revision'],'values':values});assert r.status_code<300,r.text;return r.json()
        def wait(collection):
            for _ in range(100):
                item=state()
                if not any(r['status'] in {'running','pending'} for r in item[collection]):return item
                time.sleep(.02)
            raise AssertionError('job timeout')
        post('/extract');item=wait('jobs');assert item['semantics'][-1]['steps'][0]['quote']==source
        post('/semantics/review',{'evidence':'fixture'})
        case_ids=[]
        for index,purpose in enumerate(['reproduction','transfer']):
            name='case'+str(index);item=post('/cases',{'name':name,'purpose':purpose});cid=item['cases'][-1]['id'];case_ids.append(cid)
            amount=10+index*3
            for kind,raw in [('input',f'vehicle,amount\n001,{amount}\n001,20\n002,5\n'),('expected',f'vehicle,amount\n001,{amount+20}\n002,5\n')]:
                r=client.post(base+'/cases/'+cid+'/assets',files={'file':('fixture.csv',raw.encode(),'text/csv')},data={'revision':state()['revision'],'dataset':'sales' if kind=='input' else 'golden','kind':kind});assert r.status_code==200,r.text
                asset=r.json()['cases'][-1]['assets'][-1]
                post('/cases/'+cid+'/mapping/'+asset['id'],{'mapping':{'columns':{'vehicle_id':'vehicle','amount':'amount'},'numeric':['amount'],'scales':{},'constants':{},'confirmed':True,'reason':'independent fixture'}})
            post('/cases/'+cid+'/comparison',{'keys':['vehicle_id'],'fields':['amount'],'evidence':'independently authored totals'})
        post('/compile');item=wait('jobs');recipe=item['recipes'][-1]
        for cid in case_ids:
            post('/run',{'case_id':cid,'recipe_hash':recipe['hash']});item=wait('runs');assert item['runs'][-1]['status']=='completed',item['runs'][-1].get('error')
        assert item['readiness'][recipe['hash']]['can_adopt']
        post('/review',{'recipe_hash':recipe['hash'],'decision':'operational','reviewer':'test','evidence':'two independent goldens','applicability':'vehicle/amount CSV'})
        assert client.get(base+'/library/search').json()
        post('/publish',{'recipe_hash':recipe['hash']})
        assert len(calls)==2
        rid=state()['runs'][-1]['id'];assert client.get(base+'/runs/'+rid+'/download').status_code==200
        assert client.get('/api/projects/q/agent-examples/'+eid+'/learning').status_code==404
        post('/review',{'recipe_hash':recipe['hash'],'decision':'revoked','reviewer':'test','evidence':'revoked','applicability':''})
        assert client.get(base+'/library/search').json()==[]




def test_missing_columns_and_silent_aggregation_loss_rejected(tmp_path):
 with pytest.raises(ValueError):execute_recipe(aggregate_recipe(),{'sales':[{'vehicle_id':'A','amount':None}]},tmp_path)
 with pytest.raises(ValueError):compare_rows([{'id':'1'}],[{'id':'1'}],['id'],['missing'])

def test_excel_header_selection_and_pdf_capability(tmp_path):
 import io
 from openpyxl import Workbook
 from pypdf import PdfWriter
 wb=Workbook();ws=wb.active;ws.title='Data';ws.append(['title']);ws.append(['Plate','Total']);ws.append(['0001',25]);buffer=io.BytesIO();wb.save(buffer)
 table=inspect_table(buffer.getvalue(),'.xlsx','Data',2)
 assert table['headers']==['Plate','Total'] and table['rows'][0]['values']['Plate']=='0001'
 writer=PdfWriter();writer.add_blank_page(width=100,height=100);pdf=io.BytesIO();writer.write(pdf)
 assert inspect_table(pdf.getvalue(),'.pdf')['status']=='needs_capability'

def test_rag_rechecks_learned_recipe_status(tmp_path):
 import time
 from app.experience_memory import ExperienceMemory
 store=LearningStore(tmp_path/'procedure_learning');store.get('p','e','source');item,recipe=fixture_state();adopt(item,recipe['hash'],'operational','tester','proof','scope')
 store.change('p','e',0,'fixture',lambda target:target.update(item))
 class Index:
  def search(self,*args):return [rid]
 memory=ExperienceMemory(tmp_path/'memory'/'experience_memory',{},Index())
 rid=memory.store.add('p','success','lesson',{}, {'learning_example':'e','learning_recipe':recipe['hash']});memory.store.review('p',rid,'verified','tester','proof',time.time()+500)
 assert memory.retrieve({'project':'p','mode':'enforce'},'query')
 store.update('p','e','revoke',lambda target:target['recipes'][0].update(status='revoked'))
 assert not memory.retrieve({'project':'p','mode':'enforce'},'query')
