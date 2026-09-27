"""Domain-neutral invention sessions and fixed, independent artifact trials."""
import copy,json,math,time,uuid
from app.learning_tables import fingerprint
from app.triz_common import validate_problem,reasoning_context
from app.triz_invention import PRINCIPLES,obj,string,strings
from app.triz_adapters import DOMAINS,CAPABILITIES,gaps,sha,ADAPTER_VERSION

STEP=obj({'capability':string(),'instruction':string(),'verification':string()})
CANDIDATE=obj({**{k:string() for k in ('title','hypothesis','why','constraint_check','risk','stop','rollback','experiment_plan')},'principles':{'type':'array','minItems':1,'maxItems':4,'items':{'type':'string','enum':list(PRINCIPLES)}},'required_capabilities':strings(),'unknowns':strings(),'steps':{'type':'array','minItems':1,'maxItems':4,'items':STEP}})
SCHEMA=obj({'analysis':obj({k:string() for k in ('contradiction','ideal','resources','unknowns')}),'candidates':{'type':'array','minItems':2,'maxItems':3,'items':CANDIDATE}})

def find(item,sid):
 s=next((s for s in item.get('general_inventions',[]) if s['id']==sid),None)
 if not s:raise ValueError('分野横断の発明課題がありません')
 return s

def candidate(s,cid):
 c=next((c for c in s['candidates'] if c['id']==cid),None)
 if not c:raise ValueError('候補がありません')
 return c

def signature(s):return fingerprint({'adapter_version':ADAPTER_VERSION,**{k:s[k] for k in ('domain','problem','criteria','cases')}})

def create(item,v):
 domain=v.get('domain')
 if domain not in DOMAINS:raise ValueError('分野を選択してください')
 problem=validate_problem(v)
 human=str(v.get('human_checks','')).strip()
 if not human or len(human)>3000:raise ValueError('人が確認する内容・合格基準を3000文字以内で指定してください')
 limit=float(v.get('max_seconds',300))
 if not math.isfinite(limit) or not 1<=limit<=900:raise ValueError('時間基準は1〜900秒です')
 metrics=v.get('metrics',[])
 if not isinstance(metrics,list) or len(metrics)>6:raise ValueError('測定項目は6件以内です')
 names=set()
 for m in metrics:
  if not isinstance(m,dict) or not isinstance(m.get('name'),str) or not m['name'].strip() or m['name'] in names or m.get('comparison') not in {'le','ge'}:raise ValueError('測定名・上限/下限を指定してください')
  m['limit']=float(m['limit'])
  if not math.isfinite(m['limit']):raise ValueError('測定基準は有限数です')
  names.add(m['name'])
 s={'id':uuid.uuid4().hex,'domain':domain,'problem':problem,'criteria':{'human_checks':human,'max_seconds':limit,'metrics':metrics,'runtime_test_required':domain=='software'},'cases':[],'candidates':[],'experiments':[],'rounds':0,'status':'defined','created':time.time()}
 item.setdefault('general_inventions',[]).append(s);return s

def add_case(s,v):
 if len(s['cases'])>=4:raise ValueError('ケースは最大4件です')
 if v.get('purpose') not in {'reproduction','transfer'} or not str(v.get('name','')).strip():raise ValueError('ケース名と元条件/別条件を指定してください')
 resources=v.get('resources',[])
 if not isinstance(resources,list) or not 1<=len(resources)<=6:raise ValueError('資料を1〜6件登録してください')
 rows=[]
 for r in resources:
  if not isinstance(r,dict) or not isinstance(r.get('text'),str) or not r['text'].strip() or len(r['text'])>24000:raise ValueError('各資料は空でない24000文字以内のテキストです')
  name=str(r.get('name','source.txt')).replace('\\','/').split('/')[-1][:120]
  rows.append({'id':uuid.uuid4().hex,'name':name,'text':r['text'],'hash':sha(r['text'])})
 if sum(len(r['text']) for r in rows)>36000:raise ValueError('ケース全体の資料は36000文字以内です')
 e=v.get('expectations',{});expected={}
 for k in ('required','forbidden','headings'):
  values=e.get(k,[])
  if not isinstance(values,list) or len(values)>12 or any(not isinstance(x,str) or not x.strip() or len(x)>400 for x in values):raise ValueError('照合条件は各12件、各400文字以内です')
  expected[k]=values
 for k,default in [('min_chars',20),('max_chars',16000),('min_citations',1 if s['domain'] in {'research','operations'} else 0)]:expected[k]=int(e.get(k,default))
 if not 1<=expected['min_chars']<=expected['max_chars']<=80000 or not 0<=expected['min_citations']<=12:raise ValueError('文字数・引用件数が不正です')
 if s['domain'] in {'research','operations'}:expected['min_citations']=max(1,expected['min_citations'])
 evidence=str(e.get('evidence','')).strip()
 if not evidence or len(evidence)>2000:raise ValueError('独立した合格基準の根拠が必要です')
 expected['evidence']=evidence
 row={'id':uuid.uuid4().hex,'name':str(v['name'])[:160],'purpose':v['purpose'],'resources':rows,'expectations':expected,'created':time.time()}
 s['cases'].append(row)
 for c in s['candidates']:
  if c['status']=='adopted':c['status']='needs_revalidation'
 return row

def inputs_hash(c):return fingerprint(sorted(r['hash'] for r in c['resources']))

def prompt(item,s):
 history=[{'domain':x['domain'],'problem':x['problem'],'candidates':[{'hypothesis':c['hypothesis'],'steps':c['steps'],'missing':c['missing']} for c in x['candidates'][-3:]],'experiments':[{'status':e['status'],'results':[{'candidate':r['candidate'],'passed':r['passed'],'errors':[v.get('error','') for v in r['cases']]} for r in e.get('results',[])]} for e in x['experiments'][-2:]]} for x in item.get('general_inventions',[])[-3:]]
 base=reasoning_context(s['problem'],PRINCIPLES,{'human_checks':s['criteria']['human_checks'],'max_seconds':s['criteria']['max_seconds']},history)
 base.update(domain=s['domain'],capabilities=CAPABILITIES,available_inputs=[{'name':r['name'],'characters':len(r['text']),'excerpt':r['text'][:1500],'excerpt_is_partial':len(r['text'])>1500,'full_source_available_at_trial':True} for c in s['cases'] for r in c['resources']],instructions='表処理に限定せず、選択分野の問題に対して異なる2〜3案を発明する。stepsには能力IDと具体的な処理指示、検証方法を1〜4工程で記載する。能力が未接続でも案と実験計画を保存する。required_capabilitiesには実行に必要な能力を漏れなく列挙する。未知の条件はunknownsへ列挙。既知の実行可能なアダプターで作れる成果物と、実環境に適用する未実施工程を混同しない。独立期待結果の値は与えていない。原本の全文は実行時に読み取れる。抜粋に示した原文情報や、実行時に読める具体値を未確定条件扱いしない。unknownsは実行を妨げる未定義の判断条件だけにする。人の確認事項はexperiment_planへ記載し、自動処理のrequired_capabilitiesとは分ける。説明は日本語。')
 return base

def add_candidates(s,answer):
 rows=answer.get('candidates',[])
 if not isinstance(rows,list) or not 2<=len(rows)<=3:raise ValueError('候補は2〜3件必要です')
 s['analysis']=answer['analysis'];seen={c['structure_hash'] for c in s['candidates']}
 for raw in rows:
  c=copy.deepcopy(raw)
  for k in ('title','hypothesis','why','constraint_check','risk','stop','rollback','experiment_plan'):
   if not isinstance(c.get(k),str) or not c[k].strip() or len(c[k])>3000:raise ValueError('仮説・検証・停止復旧条件を記載してください')
  if not c.get('principles') or any(p not in PRINCIPLES for p in c['principles']):raise ValueError('発明原理が不正です')
  if not isinstance(c.get('steps'),list) or not 1<=len(c['steps'])<=4:raise ValueError('工程は1〜4件です')
  for step in c['steps']:
   if any(not isinstance(step.get(k),str) or not step[k].strip() or len(step[k])>2500 for k in ('capability','instruction','verification')):raise ValueError('能力・指示・検証方法が必要です')
  if any(not isinstance(c.get(k),list) or len(c[k])>12 or any(not isinstance(x,str) for x in c[k]) for k in ('required_capabilities','unknowns')):raise ValueError('必要能力・不明条件の形式が不正です')
  missing=gaps(s['domain'],c['steps'])+['未確定条件: '+u for u in c['unknowns'] if u.strip()]
  for cap in c['required_capabilities']:
   if not CAPABILITIES.get(cap,{}).get('available'):missing.append('必要能力未接続: '+cap)
  structure=fingerprint([{'capability':x['capability'],'instruction':' '.join(x['instruction'].split())} for x in c['steps']])
  duplicate=structure in seen
  c.update(id=uuid.uuid4().hex,round=s['rounds'],missing=list(dict.fromkeys(missing)),structure_hash=structure,status='duplicate' if duplicate else 'plan_only' if missing else 'candidate',reviews=[],created=time.time())
  s['candidates'].append(c);seen.add(structure)
 s['status']='candidates' if any(c['status']=='candidate' for c in s['candidates']) else 'plan_only'

def eligibility(s,c):
 current=signature(s)
 exps=[e for e in s['experiments'] if e['signature']==current and any(r['candidate']==c['id'] for r in e.get('results',[]))]
 if not exps:return False
 e=exps[-1];r=next(r for r in e['results'] if r['candidate']==c['id'])
 original={inputs_hash(x) for x in s['cases'] if x['purpose']=='reproduction'};transfer={inputs_hash(x) for x in s['cases'] if x['purpose']=='transfer'}
 return e['status']=='completed' and r['passed'] and not c['missing'] and bool(original and transfer-original)

def adopted(s,c):
 latest=next((e for e in reversed(s['experiments']) if e['signature']==signature(s) and any(r['candidate']==c['id'] for r in e.get('results',[]))),None)
 return c['status']=='adopted' and eligibility(s,c) and latest is not None and any(r['decision']=='adopted' and r['signature']==signature(s) and r.get('experiment')==latest['id'] for r in c['reviews'])

def review(s,c,v):
 decision=v.get('decision');evidence=str(v.get('evidence','')).strip();reviewer=str(v.get('reviewer','')).strip()
 if decision not in {'adopted','revoked'} or not evidence or not reviewer:raise ValueError('判断・判断者・証拠が必要です')
 if decision=='adopted':
  if not eligibility(s,c):raise ValueError('現行条件で元入力・異なる別入力の全ケース合格が必要です')
  if not str(v.get('scope','')).strip():raise ValueError('確認した適用範囲が必要です')
  if s['criteria']['runtime_test_required'] and (v.get('runtime_tests_passed') is not True or not str(v.get('runtime_evidence','')).strip()):raise ValueError('構文検査だけでは採用できません。独立した実行テストの証拠が必要です')
  for m in s['criteria']['metrics']:
   value=float(v.get('measurements',{}).get(m['name'],float('nan')))
   if not math.isfinite(value) or (value>m['limit'] if m['comparison']=='le' else value<m['limit']):raise ValueError('測定基準を満たしていません: '+m['name'])
 c['status']=decision;c['scope']=str(v.get('scope',''))[:2000]
 c['reviews'].append({'decision':decision,'reviewer':reviewer[:200],'evidence':evidence[:4000],'scope':c['scope'],'signature':signature(s),'experiment':next((e['id'] for e in reversed(s['experiments']) if e['signature']==signature(s) and any(r['candidate']==c['id'] for r in e.get('results',[]))),None),'runtime_evidence':str(v.get('runtime_evidence',''))[:3000],'measurements':v.get('measurements',{}),'verification_kind':'human_attested_with_artifact_checks','created':time.time()})


def defined_library_display(session):
    """74件 defined は参照ライブラリ。検証済み・業務回復済みと表示しない。"""
    status = session.get('status') or 'defined'
    if status == 'defined':
        return {'status': 'defined', 'label': '定義済み', 'verified': False, 'business_recovered': False}
    return {'status': status, 'label': status, 'verified': status == 'adopted', 'business_recovered': False}


def diagnosis(item):
 failures=[];missing=[]
 for s in item.get('general_inventions',[]):
  for c in s['candidates'][-3:]:
   if c['missing']:missing.append({'session':s['id'],'candidate':c['title'],'requirements':c['missing']})
  for e in s['experiments'][-1:]:
   if e['signature']!=signature(s):continue
   for r in e['results']:
    if not r['passed']:failures.append({'session':s['id'],'candidate':r['candidate'],'error':r.get('error',''),'cases':[x.get('error','') for x in r['cases'] if not x['passed']]})
 return {'triggered':bool(failures),'failures':failures[-6:],'missing_capabilities':missing[-6:]}
