"""Versioned procedural memory and evidence-based readiness."""
import copy
import json
import sqlite3
import time
import uuid
from pathlib import Path
from app.agent_examples import Conflict
from app.learning_tables import fingerprint,validate_recipe

class LearningStore:
    def __init__(self,root):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True);self.path=self.root/'learning.sqlite3'
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS learning(project TEXT,example TEXT,revision INTEGER,data TEXT,PRIMARY KEY(project,example))')
            db.execute('CREATE TABLE IF NOT EXISTS history(id INTEGER PRIMARY KEY,project TEXT,example TEXT,action TEXT,detail TEXT,created REAL)')
    def connect(self):
        db=sqlite3.connect(self.path,timeout=20);db.row_factory=sqlite3.Row;return db
    def get(self,project,example,source_hash=None):
        with self.connect() as db:
            row=db.execute('SELECT data FROM learning WHERE project=? AND example=?',(project,example)).fetchone()
            if row:return json.loads(row[0])
            if source_hash is None:raise KeyError(example)
            value={'project':project,'example':example,'revision':0,'source_hash':source_hash,'semantics':[],'recipes':[],'cases':[],'runs':[],'corrections':[],'capabilities':[],'jobs':[]}
            db.execute('INSERT OR IGNORE INTO learning VALUES(?,?,?,?)',(project,example,0,json.dumps(value)))
        return self.get(project,example)
    def list(self,project):
        with self.connect() as db:return [json.loads(r[0]) for r in db.execute('SELECT data FROM learning WHERE project=?',(project,))]
    def change(self,project,example,revision,action,fn):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');row=db.execute('SELECT data,revision FROM learning WHERE project=? AND example=?',(project,example)).fetchone()
            if not row:raise KeyError(example)
            if revision!=row['revision']:raise Conflict('内容が更新されています。再読込してください')
            item=json.loads(row['data']);before=fingerprint(item);fn(item);item['revision']+=1
            db.execute('UPDATE learning SET revision=?,data=? WHERE project=? AND example=?',(item['revision'],json.dumps(item,ensure_ascii=False),project,example))
            db.execute('INSERT INTO history(project,example,action,detail,created) VALUES(?,?,?,?,?)',(project,example,action,json.dumps({'before':before,'after':fingerprint(item),'revision':item['revision']}),time.time()))
            return item
    def update(self,project,example,action,fn):
        for _ in range(10):
            current=self.get(project,example)
            try:return self.change(project,example,current['revision'],action,fn)
            except Conflict:continue
        raise Conflict('更新が競合しています')


def case_signature(case):
    return fingerprint({'assets':[{k:v for k,v in a.items() if k in {'id','hash','mapping','sheet','header_row','dataset','kind'}} for a in case['assets']],'compare':case.get('compare',{})})


def input_signature(case):
    return fingerprint(sorted([{'hash':a['hash'],'sheet':a.get('sheet'),'header_row':a.get('header_row'), 'mapping':{k:v for k,v in a.get('mapping',{}).items() if k in {'columns','constants','numeric','scales'}},'dataset':a['dataset']} for a in case['assets'] if a['kind']=='input'],key=lambda x:x['dataset']))


def add_recipe(item,recipe,reason,parent=None,scope='general'):
    validate_recipe(recipe)
    if scope not in {'general','case_only'} or not str(reason).strip():raise ValueError('修正理由と適用範囲が必要です')
    previous=next((r for r in item['recipes'] if r['hash']==parent),None)
    if parent and not previous:raise ValueError('元の手順版がありません')
    version=len(item['recipes'])+1
    entry={'version':version,'hash':fingerprint({'recipe':recipe,'source':item['source_hash'],'version':version}), 'recipe':copy.deepcopy(recipe),'reason':reason[:3000],'parent':parent,'scope':scope,'status':'draft','created':time.time(),'reviews':[]}
    if previous and previous.get('triz'):
        from app.triz_invention import session
        origin=previous['triz'];invention=session(item,origin['session'])
        candidate=next(c for c in invention['candidates'] if c['id']==origin['candidate'])
        if len(invention['candidates'])>=12:raise ValueError('発明候補の上限です。新しい根拠で課題を定義してください')
        revised=copy.deepcopy(candidate);revised.update(id=uuid.uuid4().hex,recipe_hash=entry['hash'],status='proposed',title=candidate['title']+'（修正）',hypothesis=str(reason),missing=[])
        invention['candidates'].append(revised)
        entry['triz']={'session':invention['id'],'candidate':revised['id']}
    item['recipes'].append(entry)
    if previous:
        item['corrections'].append({'id':uuid.uuid4().hex,'before':previous['hash'],'after':entry['hash'],'before_recipe':previous['recipe'],'after_recipe':recipe,'reason':reason,'scope':scope,'created':time.time()})
        # A new candidate does not replace the adopted version until it passes review.
    return entry


def readiness(item,recipe):
    cases={c['id']:c for c in item['cases']}
    valid=[]
    for r in item['runs']:
        c=cases.get(r['case_id'])
        if c and r['recipe_hash']==recipe['hash'] and r['status']=='completed' and r.get('comparison',{}).get('passed') and r['case_signature']==case_signature(c):valid.append((r,c))
    baseline={input_signature(c) for r,c in valid if c['purpose']=='reproduction'}
    transfer={input_signature(c) for r,c in valid if c['purpose']=='transfer'}
    state='application_verified' if baseline and any(t not in baseline for t in transfer) else 'reproduction_verified' if baseline else 'extracted'
    current_failures=[]
    for c in item['cases']:
        attempts=[r for r in item['runs'] if r['recipe_hash']==recipe['hash'] and r['case_id']==c['id'] and r['case_signature']==case_signature(c)]
        if attempts and attempts[-1]['status'] in {'failed','needs_review'}:current_failures.append(c['id'])
    running=any(r['recipe_hash']==recipe['hash'] and r['status'] in {'pending','running'} for r in item['runs'])
    all_cases_passed=bool(cases) and all(any(c['id']==cid for r,c in valid) for cid in cases)
    from app.triz_invention import can_adopt as triz_can_adopt
    invention_ready=triz_can_adopt(item,recipe)
    return {'invention_ready':invention_ready,'all_cases_passed':all_cases_passed,'stage':state,'reproduction_cases':len(baseline),'transfer_cases':len(transfer-baseline),'current_failures':current_failures,'can_adopt':state=='application_verified' and not current_failures and not running and all_cases_passed and recipe['scope']=='general' and invention_ready}


def adopt(item,recipe_hash,decision,reviewer,evidence,applicability):
    if decision not in {'operational','revoked'} or not reviewer.strip() or not evidence.strip():raise ValueError('判断者と証拠が必要です')
    recipe=next((r for r in item['recipes'] if r['hash']==recipe_hash),None)
    if not recipe:raise ValueError('手順版がありません')
    if decision=='operational':
        if not readiness(item,recipe)['can_adopt']:raise ValueError('異なる入力での再現・応用検証と回帰確認が必要です')
        if not str(applicability).strip():raise ValueError('確認済みの適用範囲を記載してください')
        if recipe.get('capability_gaps'):raise ValueError('未対応能力が残っています')
        # Every active test case must have a current successful result for this version.
        for case in item['cases']:
            if not any(r['recipe_hash']==recipe_hash and r['case_id']==case['id'] and r['case_signature']==case_signature(case) and r['status']=='completed' and r.get('comparison',{}).get('passed') for r in item['runs']):raise ValueError('全登録ケースでこの版の回帰検証が必要です')
    if decision=='operational':
        for previous in item['recipes']:
            if previous['hash']!=recipe_hash and previous['status']=='operational':previous['status']='superseded'
    recipe['status']=decision;recipe['applicability']=applicability
    recipe['reviews'].append({'decision':decision,'reviewer':reviewer,'evidence':evidence,'created':time.time()})


def grounded_steps(source,answer):
    if not isinstance(answer,dict) or not isinstance(answer.get('steps'),list) or not answer['steps']:raise ValueError('工程候補がありません')
    result=[]
    for step in answer['steps']:
        quote=step.get('quote','')
        if not quote.strip() or quote not in source:raise ValueError('原文に一致しない引用が含まれます')
        step={k:step.get(k,'') for k in ('purpose','input','operation','output','condition','exception','quote')}
        step['line']=source[:source.index(quote)].count('\n')+1;step['status']='inferred';result.append(step)
    return result

SEMANTIC_SCHEMA={'type':'object','properties':{'steps':{'type':'array','maxItems':16,'items':{'type':'object','properties':{k:{'type':'string'} for k in ('purpose','input','operation','output','condition','exception','quote')},'required':['purpose','input','operation','output','condition','exception','quote'],'additionalProperties':False}}},'required':['steps'],'additionalProperties':False}
COMPILE_SCHEMA={'type':'object','properties':{'inputs':{'type':'array','items':{'type':'string'}},'nodes':{'type':'array','maxItems':40,'items':{'type':'object','properties':{'id':{'type':'string'},'op':{'type':'string'},'inputs':{'type':'array','items':{'type':'string'}},'params_json':{'type':'string'},'source_steps':{'type':'array','items':{'type':'integer'}}},'required':['id','op','inputs','params_json','source_steps'],'additionalProperties':False}},'gaps':{'type':'array','items':{'type':'string'}}},'required':['inputs','nodes','gaps'],'additionalProperties':False}
