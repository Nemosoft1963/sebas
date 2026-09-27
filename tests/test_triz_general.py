import asyncio,copy,json,time,uuid
from pathlib import Path
from types import SimpleNamespace
import pytest
from fastapi import FastAPI,HTTPException
from fastapi.testclient import TestClient
from app.agent_examples import ExampleStore
from app.procedure_learning_api import install
from app.triz_adapters import execute_step,validate_output,sha
from app.triz_general import create,add_case,add_candidates,eligibility,review


def problem(domain='documents'):
 return {'domain':domain,'goal':'説明の詳細を残して読みやすくする','improve':'読みやすさ','worsens':'詳細が欠落する','ideal':'必要な内容を短時間で把握できる','physical':'短く詳しく','constraints':'原文の事実を変えない','resources':'登録テキスト','evidence':'現行成果物が長く要点を把握しにくい','human_checks':'内容と副作用を原本照合','max_seconds':120}

def plans(domain='documents'):
 cap={'documents':'document.rewrite','research':'research.synthesize','software':'code.patch','workflow':'workflow.design','operations':'operations.diagnose','browser':'browser.interact'}[domain]
 return {'analysis':{k:'仮説の分析' for k in ('contradiction','ideal','resources','unknowns')},'candidates':[{'title':title,'hypothesis':title,'why':'情報を役割ごとに分離','constraint_check':'事実保持','risk':'欠落を確認','stop':'比較不合格で停止','rollback':'原本と旧版を保持','experiment_plan':'固定条件で元入力と別入力を比較','principles':['1'],'required_capabilities':[cap],'unknowns':[],'steps':[{'capability':cap,'instruction':title,'verification':'固定基準と人の確認'}]} for title in ('良い案','悪い案')]}

def resource(text='原本の事実を保持しながら構成だけを変える。詳細な案内を要約と本文に分離する。',name='input.md'):
 return {'id':'source1','name':name,'text':text,'hash':sha(text)}

@pytest.fixture
def api(tmp_path):
 root=tmp_path.parent/('g'+uuid.uuid4().hex[:5]);original=ExampleStore(root/'agent_examples').import_text('p','example.md','架空の問題記録');calls=[];settings={'domain':'documents','delay':0}
 class Local:
  url='http://127.0.0.1:11434'
  async def _request_once(self,messages,*args):
   calls.append(messages);await asyncio.sleep(settings['delay']);schema=args[-1]
   if 'candidates' in schema['properties']:answer=plans(settings['domain'])
   else:
    p=json.loads(messages[-1]['content']);sources=p['sources']
    if 'text' in schema['properties']:answer={'text':'# 要約\n\n'+('重要な事実と詳細を保持しました。内容は原本と照合してください。' if p['instruction']=='良い案' else '禁止表現')}
    elif 'edits' in schema['properties']:answer={'edits':[{'source_id':sources[0]['id'],'before':'return x','after':'return x + 1'}],'explanation':'戻り値を修正した候補。'}
    else:raise AssertionError('unsupported test response')
   return json.dumps(answer,ensure_ascii=False),{'done_reason':'stop'}
 def require(pid):
  if pid not in {'p','q'}:raise HTTPException(404)
 app=FastAPI();install(app,SimpleNamespace(DATA_DIR=root,DB_PATH=root/'memory'/'db',llm=Local(),require_project=require))
 with TestClient(app) as client:
  base='/api/projects/p/agent-examples/'+original['id']+'/learning'
  def state():
   r=client.get(base);assert r.status_code==200;return r.json()
  def post(path,values=None,code=200):
   r=client.post(base+path,json={'revision':state()['revision'],'values':values or {}});assert r.status_code==code,r.text;return r.json()
  def wait():
   for _ in range(300):
    s=state()
    if not any(j['status'] in {'pending','running'} for j in s['jobs']):return s
    time.sleep(.01)
   raise AssertionError('timeout')
  def setup(domain='documents',same=False):
   settings['domain']=domain;sid=post('/general',problem(domain))['general_inventions'][-1]['id']
   for i,purpose in enumerate(['reproduction','transfer']):
    text='def f(x):\n    return x\n' if domain=='software' else '原本の詳細と重要な事実を維持する。'
    text+='\n# '+str(0 if same else i)
    post('/general/'+sid+'/cases',{'name':purpose,'purpose':purpose,'resources':[{'name':'source.py' if domain=='software' else 'source.md','text':text}],'expectations':{'required':[] if domain=='software' else ['重要な事実'],'forbidden':['禁止表現'],'headings':[] if domain=='software' else ['要約'],'min_chars':10,'max_chars':2000,'evidence':'独立に定義した要件'}})
   post('/general/'+sid+'/generate',code=202);s=wait();assert s['jobs'][-1]['status']=='completed',s['jobs'][-1]
   return sid,s['general_inventions'][-1]['candidates']
  yield client,base,state,post,wait,setup,calls,settings,root

def test_document_invention_trial_review_reuse_and_scope(api):
 client,base,state,post,wait,setup,calls,settings,root=api
 sid,cs=setup();assert '独立に定義した要件' not in calls[0][-1]['content'] and 'expectations' not in calls[0][-1]['content']
 post('/general/'+sid+'/experiment',{'candidates':[c['id'] for c in cs]},202);s=wait()['general_inventions'][-1];e=s['experiments'][-1]
 assert e['status']=='completed' and [r['passed'] for r in e['results']]==[True,False],e
 post('/general/'+sid+'/review',{'candidate':cs[1]['id'],'decision':'adopted','reviewer':'test','evidence':'proof','scope':'fixture'},422)
 post('/general/'+sid+'/review',{'candidate':cs[0]['id'],'decision':'adopted','reviewer':'test','evidence':'原本意味保持を照合','scope':'架空文書'})
 rows=client.get(base+'/general-library').json();assert len(rows)==1
 post('/general-reuse',rows[0]);new=state()['general_inventions'][-1];assert not new['cases'] and not new['candidates'][0]['reviews']
 case=e['results'][0]['cases'][0];url=base+'/general/'+sid+'/experiments/'+e['id']+'/'+cs[0]['id']+'/'+case['case']+'/download'
 assert client.get(url).status_code==200 and client.get(url+'?name=../../secret').status_code==404
 assert client.get(base.replace('/p/','/q/')+'/general').status_code==404
 # Fixed signature loses eligibility when new independent case is added.
 post('/general/'+sid+'/cases',{'name':'new','purpose':'transfer','resources':[{'name':'new.txt','text':'new input'}],'expectations':{'evidence':'new contract'}})
 assert not client.get(base+'/general-library').json()

def test_browser_plan_survives_without_connector(api):
 client,base,state,post,wait,setup,calls,settings,root=api
 settings['domain']='browser';sid=post('/general',problem('browser'))['general_inventions'][-1]['id'];post('/general/'+sid+'/generate',code=202);s=wait()['general_inventions'][-1]
 assert s['status']=='plan_only' and all(c['missing'] for c in s['candidates'])
 assert client.get(base+'/general/'+sid+'/plan').json()['candidates'][0]['experiment_plan']
 post('/general/'+sid+'/experiment',{'candidates':[s['candidates'][0]['id']]},422)

def test_python_syntax_is_not_functional_test(api):
 client,base,state,post,wait,setup,*_=api
 sid,cs=setup('software');post('/general/'+sid+'/experiment',{'candidates':[cs[0]['id']]},202);s=wait()['general_inventions'][-1];assert s['experiments'][-1]['results'][0]['passed']
 values={'candidate':cs[0]['id'],'decision':'adopted','reviewer':'test','evidence':'構文確認','scope':'Python fixture'}
 post('/general/'+sid+'/review',values,422)
 values.update(runtime_tests_passed=True,runtime_evidence='独立した実行環境でf(1)==2とf(5)==6を確認した記録（テスト用申告）')
 post('/general/'+sid+'/review',values)
 assert state()['general_inventions'][-1]['candidates'][0]['reviews'][-1]['verification_kind']=='human_attested_with_artifact_checks'

def test_same_input_cannot_fake_transfer(api):
 _,_,state,post,wait,setup,*_=api
 sid,cs=setup(same=True);post('/general/'+sid+'/experiment',{'candidates':[cs[0]['id']]},202);wait()
 post('/general/'+sid+'/review',{'candidate':cs[0]['id'],'decision':'adopted','reviewer':'test','evidence':'proof','scope':'scope'},422)

@pytest.mark.asyncio
async def test_research_quotes_and_operations_evidence():
 r=resource()
 async def model(messages,schema):return {'claims':[{'kind':'fact','source_id':r['id'],'quote':r['text'],'text':''}]}
 result=await execute_step({'capability':'research.synthesize','instruction':'根拠を整理'},[r],'',model)
 assert result['citations'][0]['verdict']['citation_match']=='exact'
 async def bad(messages,schema):return {'claims':[{'kind':'fact','source_id':r['id'],'quote':'存在しない証拠','text':''}]}
 with pytest.raises(ValueError):await execute_step({'capability':'research.synthesize','instruction':'調査'},[r],'',bad)
 async def logs(messages,schema):return {'hypotheses':[{'source_id':r['id'],'quote':r['text'],'hypothesis':'原因の仮説','test':'隔離で試す','stop':'不一致','rollback':'旧設定保持'}]}
 result=await execute_step({'capability':'operations.diagnose','instruction':'診断'},[r],'',logs)
 assert '実システムへの操作・修復は未実施' in result['text']

@pytest.mark.asyncio
async def test_code_is_never_executed_and_exact_patch_required(tmp_path):
 marker=tmp_path/'must-not-exist';code='from pathlib import Path\nPath('+repr(str(marker))+').write_text("BAD")\ndef f(x):\n    return x\n';r=resource(code,'../source.py')
 async def model(messages,schema):return {'edits':[{'source_id':r['id'],'before':'return x','after':'return x + 1'}],'explanation':'change'}
 result=await execute_step({'capability':'code.patch','instruction':'修正'},[r],'',model)
 assert result['runtime_tests_pending'] and not marker.exists() and list(result['files'])==['code-source1.py']
 r=resource('def f():\n return x\n return x\n','source.py')
 with pytest.raises(ValueError):await execute_step({'capability':'code.patch','instruction':'修正'},[r],'',model)

@pytest.mark.asyncio
async def test_workflow_design_has_measurement_plan():
 async def model(messages,schema):return {'steps':[{'title':'分離','action':'例外を識別','verify':'全件照合','stop':'未分類','rollback':'旧手順'}],'measurement_plan':'処理時間と誤り率を比較する'}
 r=await execute_step({'capability':'workflow.design','instruction':'業務設計'},[resource()],'',model)
 assert '現場での適用・効果測定は未実施' in r['text'] and '処理時間' in r['text']

def test_cancel_and_round_limit(api):
 _,_,state,post,wait,setup,calls,settings,_=api
 sid,cs=setup();settings['delay']=.5
 j=post('/general/'+sid+'/experiment',{'candidates':[cs[0]['id']]},202)
 post('/jobs/'+j['job_id']+'/cancel');s=wait();assert s['general_inventions'][-1]['experiments'][-1]['status']=='cancelled'
 settings['delay']=0
 for _ in range(2):post('/general/'+sid+'/generate',code=202);wait()
 post('/general/'+sid+'/generate',code=422)

def test_tampered_artifact_cannot_be_adopted(api):
 _,_,state,post,wait,setup,_,_,root=api
 sid,cs=setup();post('/general/'+sid+'/experiment',{'candidates':[cs[0]['id']]},202);wait()
 file=next((root/'procedure_learning'/'general_artifacts').rglob('result.md'));file.write_text('tampered')
 post('/general/'+sid+'/review',{'candidate':cs[0]['id'],'decision':'adopted','reviewer':'test','evidence':'proof','scope':'scope'},422)

def test_manual_metrics_are_hard_gates():
 item={};s=create(item,{**problem('workflow'),'metrics':[{'name':'error_rate','comparison':'le','limit':1,'unit':'%'}]})
 for i,p in enumerate(('reproduction','transfer')):add_case(s,{'name':p,'purpose':p,'resources':[{'name':'input.txt','text':str(i)}],'expectations':{'evidence':'independent'}})
 add_candidates(s,plans('workflow'));c=s['candidates'][0]
 from app.triz_general import signature
 s['experiments'].append({'id':'e','signature':signature(s),'status':'completed','results':[{'candidate':c['id'],'passed':True}]})
 with pytest.raises(ValueError):review(s,c,{'decision':'adopted','reviewer':'test','evidence':'proof','scope':'scope','measurements':{'error_rate':2}})
 review(s,c,{'decision':'adopted','reviewer':'test','evidence':'measurement proof','scope':'scope','measurements':{'error_rate':.5}})

def test_code_contract_checks_candidate_code_not_explanation():
 result={'text':'指定通り fixed を追加したという説明','files':{'code.py':'def f():\n return 1\n'},'citations':[]}
 e={'required':['fixed'],'forbidden':[],'headings':[],'min_chars':1,'max_chars':1000,'min_citations':0}
 assert not validate_output(result,e,'software')['passed']
 result['files']['code.py']='def fixed():\n return 1\n'
 assert validate_output(result,e,'software')['passed']

def test_adopted_library_checks_artifact_hashes(api):
 client,base,state,post,wait,setup,_,_,root=api
 sid,cs=setup();post('/general/'+sid+'/experiment',{'candidates':[cs[0]['id']]},202);wait()
 post('/general/'+sid+'/review',{'candidate':cs[0]['id'],'decision':'adopted','reviewer':'tester','evidence':'fixture content checked','scope':'fixture'})
 row=client.get(base+'/general-library').json()[0]
 next((root/'procedure_learning'/'general_artifacts').rglob('result.md')).write_text('altered')
 assert client.get(base+'/general-library').json()==[]
 post('/general-reuse',row,422)

def test_rerunning_generation_requires_review_of_new_artifacts(api):
 client,base,state,post,wait,setup,*_=api
 sid,cs=setup();v={'candidate':cs[0]['id'],'decision':'adopted','reviewer':'tester','evidence':'meaning checked','scope':'fixture'}
 post('/general/'+sid+'/experiment',{'candidates':[cs[0]['id']]},202);wait();post('/general/'+sid+'/review',v)
 assert client.get(base+'/general-library').json()
 post('/general/'+sid+'/experiment',{'candidates':[cs[0]['id']]},202);wait()
 assert client.get(base+'/general-library').json()==[]
 post('/general/'+sid+'/review',v)
 assert client.get(base+'/general-library').json()
