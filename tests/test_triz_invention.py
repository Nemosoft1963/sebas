import copy,json,time
from types import SimpleNamespace
import pytest
from fastapi import FastAPI,HTTPException
from fastapi.testclient import TestClient
from app.agent_examples import ExampleStore
from app.procedure_learning import LearningStore,add_recipe,readiness,adopt,case_signature
from app.procedure_learning_api import install
from app.triz_invention import *

def spec(mode='good'):
 nodes=[]
 if mode=='guard':nodes.append({'id':'check','op':'assert','inputs':['sales'],'params':{'kind':'nonempty','field':'vehicle_id'}})
 input_name='check' if nodes else 'sales'
 nodes.append({'id':'sum','op':'aggregate','inputs':[input_name],'params':{'keys':['vehicle_id'],'values':['amount']}})
 if mode=='bad':nodes.append({'id':'double','op':'derive','inputs':['sum'],'params':{'field':'amount','expression':'amount * 2'}})
 nodes.append({'id':'output','op':'export','inputs':['double' if mode=='bad' else 'sum'],'params':{'fields':['vehicle_id','amount']}})
 return {'inputs':['sales'],'nodes':nodes}

def problem():return {'goal':'全明細を保持して正確に集計','improve':'自動化','worsens':'重複した車両の行で失敗','ideal':'手入力なしで集計','physical':'同一IDを許容しつつ一意にする','constraints':'金額や期待結果を変えない','resources':'salesのIDと金額、既存表処理','evidence':'独立期待値と不一致','max_seconds':120,'max_steps':40}

def answer():
 ideas=[]
 for mode,principle in [('good','1'),('bad','13'),('guard','23')]:
  recipe=spec(mode);ideas.append({'title':mode,'hypothesis':'処理を変更する '+mode,'why':'明細を保持したまま集計で重複を解消する','constraint_check':'明細金額を変更しない','risk':'欠測は停止','stop':'検証不一致で停止','rollback':'原本と元版を保持','principles':[principle],'missing':[],'inputs':recipe['inputs'],'nodes':[{'id':n['id'],'op':n['op'],'inputs':n['inputs'],'params_json':json.dumps(n['params'])} for n in recipe['nodes']]})
 return {'analysis':{'contradiction':'明細は多数、結果は一意','ideal':'原本を保持して集計','resources':'表','unknowns':'なし'},'candidates':ideas}

@pytest.fixture
def api(tmp_path):
 tmp_path=tmp_path.parent/('triz_'+uuid.uuid4().hex[:6])
 source=ExampleStore(tmp_path/'agent_examples').import_text('p','test.md','架空の集計例')
 calls=[]
 class Local:
  url='http://127.0.0.1:11434'
  async def _request_once(self,messages,*args):calls.append(messages);return json.dumps(answer()),{'done_reason':'stop'}
 def require(pid):
  if pid not in {'p','q'}:raise HTTPException(404)
 app=FastAPI();env=SimpleNamespace(DATA_DIR=tmp_path,DB_PATH=tmp_path/'memory'/'db.sqlite',llm=Local(),require_project=require);install(app,env)
 with TestClient(app) as client:
  base='/api/projects/p/agent-examples/'+source['id']+'/learning'
  def state():return client.get(base).json()
  def post(path,values=None,code=200):
   r=client.post(base+path,json={'revision':state()['revision'],'values':values or {}});assert r.status_code==code,r.text;return r.json()
  def wait():
   for _ in range(200):
    s=state()
    if not any(j['status'] in {'pending','running'} for j in s['jobs']+s['runs']):return s
    time.sleep(.01)
   raise AssertionError('timeout')
  for i,purpose in enumerate(['reproduction','transfer']):
   cid=post('/cases',{'name':purpose,'purpose':purpose})['cases'][-1]['id']
   for kind,raw in [('input',f'id,amount\nA,{10+i}\nA,20\n'),('expected',f'id,amount\nA,{30+i}\n')]:
    r=client.post(base+'/cases/'+cid+'/assets',files={'file':('case.csv',raw.encode(),'text/csv')},data={'revision':state()['revision'],'kind':kind,'dataset':'sales' if kind=='input' else 'golden'});assert r.status_code==200,r.text
    aid=r.json()['cases'][-1]['assets'][-1]['id']
    post('/cases/'+cid+'/mapping/'+aid,{'mapping':{'columns':{'vehicle_id':'id','amount':'amount'},'numeric':['amount'],'constants':{},'scales':{},'confirmed':True,'reason':'independent synthetic fixture'}})
   post('/cases/'+cid+'/comparison',{'keys':['vehicle_id'],'fields':['amount'],'evidence':'independently computed totals'})
  yield client,base,state,post,wait,calls,tmp_path

def test_full_triz_invention_failed_candidate_review_and_adoption(api):
 client,base,state,post,wait,calls,root=api
 sid=post('/triz',problem())['inventions'][-1]['id']
 post('/triz/'+sid+'/generate',code=202);s=wait();assert s['jobs'][-1]['status']=='completed',s['jobs'][-1]
 candidates=s['inventions'][-1]['candidates'];assert len(candidates)==3
 assert 'golden' not in calls[0][1]['content'] and '30' not in calls[0][1]['content']
 post('/triz/'+sid+'/experiment',{'candidates':[c['id'] for c in candidates]},202);s=wait();e=s['inventions'][-1]['experiments'][-1]
 assert e['status']=='completed' and [r['passed'] for r in e['results']]==[True,False,True],[r['error'] for r in e['results']]
 good=candidates[0];rh=good['recipe_hash'];assert not s['readiness'][rh]['can_adopt']
 post('/review',{'recipe_hash':rh,'decision':'operational','reviewer':'test','evidence':'two goldens','applicability':'scope'},422)
 post('/triz/'+sid+'/evaluate',{'experiment':e['id'],'candidate':good['id'],'reviewer':'tester','evidence':'全明細保持と副作用を確認'})
 post('/review',{'recipe_hash':rh,'decision':'operational','reviewer':'tester','evidence':'independent tests','applicability':'CSV grouped sum'})
 assert client.get(base+'/library/search').json()
 revised=post('/recipes',{'recipe':spec('guard'),'parent':rh,'reason':'constraint correction'})['recipes'][-1]
 assert revised['triz']['candidate']!=good['id'] and not state()['readiness'][revised['hash']]['can_adopt']
 assert client.get(base.replace('/p/','/q/')+'/triz').status_code==404
 # New input invalidates the frozen experiment and prior adoption eligibility.
 cid=state()['cases'][0]['id'];post('/cases/'+cid+'/comparison',{'keys':['vehicle_id'],'fields':['amount'],'evidence':'changed proof'})
 assert not state()['readiness'][rh]['can_adopt']
 post('/triz/'+sid+'/evaluate',{'experiment':e['id'],'candidate':good['id'],'reviewer':'tester','evidence':'old proof'},422)

def test_duplicate_candidates_do_not_count_as_inventions(api):
 _,_,state,post,wait,_,_=api
 post('/recipes',{'recipe':spec(),'reason':'existing'})
 sid=post('/triz',problem())['inventions'][-1]['id'];post('/triz/'+sid+'/generate',code=202);s=wait()
 assert s['inventions'][-1]['candidates'][0]['status']=='duplicate'
 assert len(s['recipes'])==3

def test_bounds_stale_revision_and_failed_history(api):
 client,base,state,post,wait,calls,root=api
 invalid=problem();invalid['max_seconds']=float('inf')
 # Direct validation also guards nonfinite floats.
 with pytest.raises(ValueError):new_session(state(),invalid)
 sid=post('/triz',problem())['inventions'][-1]['id']
 stale=state()['revision'];post('/triz/'+sid+'/generate',code=202);s=wait()
 r=client.post(base+'/triz',json={'revision':stale,'values':problem()});assert r.status_code==409
 post('/triz/'+sid+'/generate',code=422)
 ids=[c['id'] for c in s['inventions'][-1]['candidates']]
 post('/triz/'+sid+'/experiment',{'candidates':ids,'budget_seconds':1},422)
 post('/triz/'+sid+'/experiment',{'candidates':ids},202);s=wait()
 post('/triz/'+sid+'/generate',code=202);s=wait();assert '期待結果不一致' in calls[-1][1]['content']
 post('/triz/'+sid+'/generate',code=202);wait()
 post('/triz/'+sid+'/generate',code=422)

def test_unavailable_operations_and_missing_capability():
 item={'source_hash':'s','cases':[],'runs':[],'recipes':[],'corrections':[]}
 s=new_session(item,problem());s['rounds']=1;a=answer()
 for c in a['candidates']:c['missing']=['OCRが必要'];c['nodes']=[]
 assert save_proposals(item,s['id'],a)==0
 assert s['status']=='stopped' and not item['recipes']
 assert diagnosis(item)['kind']=='missing_information'

@pytest.mark.parametrize('op',['shell','python','http'])
def test_generated_code_is_not_an_allowed_operation(op):
 raw=answer()['candidates'][0];raw['nodes'][0]['op']=op
 with pytest.raises(ValueError):normalize_recipe(raw,{'sales':['vehicle_id','amount']})

def test_time_and_cancel_checks_preserve_unadopted_state(api,monkeypatch):
 client,base,state,post,wait,calls,root=api
 import app.procedure_learning_api as mod
 original=mod.execute_recipe
 def slow(*args,**kwargs):time.sleep(.12);return original(*args,**kwargs)
 monkeypatch.setattr(mod,'execute_recipe',slow)
 sid=post('/triz',problem())['inventions'][-1]['id'];post('/triz/'+sid+'/generate',code=202);s=wait();ids=[c['id'] for c in s['inventions'][-1]['candidates']]
 job=post('/triz/'+sid+'/experiment',{'candidates':ids},202)
 post('/jobs/'+job['job_id']+'/cancel');s=wait()
 assert s['inventions'][-1]['experiments'][-1]['status']=='cancelled'
 assert not any(r['can_adopt'] for r in s['readiness'].values())





@pytest.mark.parametrize('change',[{'keys':[{'field':'vehicle_id'}],'values':['amount']},{'keys':['vehicle_id'],'values':[{'sum':'amount'}]}])
def test_malformed_aggregate_stopped_before_execution(change):
 raw=answer()['candidates'][0];raw['nodes'][0]['params_json']=json.dumps(change)
 with pytest.raises(ValueError):normalize_recipe(raw,{'sales':['vehicle_id','amount']})

def test_cannot_reuse_an_experiment_for_changed_recipe_hash(api):
 _,_,state,post,wait,_,_=api
 sid=post('/triz',problem())['inventions'][-1]['id'];post('/triz/'+sid+'/generate',code=202);s=wait()
 c=s['inventions'][-1]['candidates'][0];recipe=next(r for r in s['recipes'] if r['hash']==c['recipe_hash'])
 e={'id':'fake','case_hash':cases_hash(s),'status':'completed','results':[{'candidate':c['id'],'recipe_hash':'different','passed':True,'review':{'evidence':'proof'}}]}
 s['inventions'][-1]['experiments'].append(e)
 assert not can_adopt(s,recipe)
