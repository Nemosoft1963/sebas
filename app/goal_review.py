"""Version-bound external plan review and explicit human-approved local lessons."""
import asyncio
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from app.experience_store import ExperienceStore, fingerprint, canonical
from app.structured_planning import contract_of, verify_outputs

RUNNING_JOB = {'running', 'generating'}
ORCHESTRATION_STAGES = (
    'feedback_import', 'local_proposal', 'human_confirmation', 'apply',
    'public_packet', 'send_approval', 'external_review', 'plan_approval', 'execution_start',
)
_AFTER_APPLY = {
    'apply', 'public_packet', 'send_approval', 'external_review', 'plan_approval', 'execution_start',
}


class ReviewStore:
    def __init__(self,memory_path):
        self.path=Path(memory_path).parent/'goal_reviews.sqlite3'
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS reviews(project TEXT, kind TEXT, signature TEXT, payload TEXT, PRIMARY KEY(project,kind,signature))')
    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=15)
        try:
            with db:yield db
        finally:db.close()
    def get(self,pid,kind,signature):
        with self.connect() as db:r=db.execute('SELECT payload FROM reviews WHERE project=? AND kind=? AND signature=?',(pid,kind,signature)).fetchone()
        return json.loads(r[0]) if r else None
    def put(self,pid,kind,signature,payload):
        with self.connect() as db:db.execute('INSERT OR REPLACE INTO reviews VALUES(?,?,?,?)',(pid,kind,signature,canonical(payload)))
    def list(self,pid,kind):
        with self.connect() as db:
            rows=db.execute('SELECT signature,payload FROM reviews WHERE project=? AND kind=?',(pid,kind)).fetchall()
        return [(signature, json.loads(payload)) for signature, payload in rows]


def save_orchestration(store, pid, **fields):
    row = store.get(pid, 'orchestration', pid) or {}
    row.update(fields)
    row['updated_at'] = time.time()
    store.put(pid, 'orchestration', pid, row)
    return row


def load_orchestration(store, pid):
    return store.get(pid, 'orchestration', pid) or {}


def orchestration_view(manager, pid):
    store = ReviewStore(manager.memory.path)
    row = dict(load_orchestration(store, pid))
    _, signature = plan_snapshot(manager, pid)
    if row.get('plan_signature') and row.get('plan_signature') != signature and row.get('last_completed_stage') in _AFTER_APPLY:
        row['resume_from'] = 'public_packet'
        row['hash_changed'] = True
    return row


def begin_job(store, pid, kind, idempotency_key, extra=None):
    extra = extra or {}
    unique = extra.pop('unique', False)
    for _, row in store.list(pid, 'job'):
        if row.get('idempotency_key') != idempotency_key:
            continue
        if row.get('status') in RUNNING_JOB:
            raise ValueError('同じ処理を実行中です')
        if unique and row.get('status') == 'succeeded':
            raise ValueError('同じ処理は完了済みです')
    job_id = uuid.uuid4().hex
    row = {
        'id': job_id, 'kind': kind,
        'status': 'generating' if kind == 'propose' else 'running',
        'idempotency_key': idempotency_key, 'started': time.time(), 'updated_at': time.time(),
        'last_completed_stage': '', 'blocking_error': '', 'resume_from': extra.get('resume_from') or '',
    }
    row.update(extra)
    store.put(pid, 'job', job_id, row)
    return row


def finish_job(store, pid, job_id, status, **updates):
    row = store.get(pid, 'job', job_id) or {'id': job_id}
    row.update(updates)
    row['status'] = status
    row['updated_at'] = time.time()
    if status in {'failed', 'needs_attention'}:
        row['blocking_error'] = str(updates.get('blocking_error') or row.get('blocking_error') or '')[:1000]
    store.put(pid, 'job', job_id, row)
    return row


def active_jobs(store, pid):
    return [row for _, row in store.list(pid, 'job') if row.get('status') in RUNNING_JOB]


def development_blockers(manager, pid, detail=None):
    _, signature = plan_snapshot(manager, pid, detail)
    row = ReviewStore(manager.memory.path).get(pid, 'revision', signature) or {}
    return [b for b in row.get('blockers') or [] if isinstance(b, dict)
            and b.get('disposition') in {'development', 'business_fact', 'unresolved'}]


def enabled(manager,pid):
    path=Path(manager.memory.path).parent/'goal_review_policy.json'
    if not path.exists():return False
    config=json.loads(path.read_text(encoding='utf-8-sig'))
    return config.get('projects',{}).get(pid,config.get('required',False)) is True


def plan_snapshot(manager,pid,detail=None):
    mission=manager.memory.get_mission(pid)
    value={k:mission.get(k) for k in ['goal','success_criteria','constraints_text','plan_version']}
    value['instructions']=[x['message'] for x in mission.get('instruction_messages',[]) if x['kind']=='mission_instruction_user']
    value['review_policy_version']=2
    from app.vehicle_auto import REVISION
    value['vehicle_engine_revision']=REVISION if any((contract_of(t) or {}).get('execution_kind','').startswith('vehicle_') for t in mission['tasks']) else None
    value['external_providers']=mission.get('external_providers',[])
    value['sources']=[{'id':x['id'],'sha256':x.get('sha256'),'content_hash':fingerprint(x.get('content',''))} for x in manager.memory.list_context_files(pid,include_content=True) if x.get('source')!='memo']
    value['tasks']=[{k:t.get(k) for k in ['id','task_key','title','description','acceptance_criteria','depends_on']} for t in mission['tasks']]
    if detail is not None:value['detail']=detail
    return value,fingerprint(value)


def require_review(manager,pid,detail=None):
    if development_blockers(manager, pid, detail):
        raise ValueError('追加開発または業務事実の確認が必要な指摘があるため実行を開始できません')
    if not enabled(manager,pid):return
    _,signature=plan_snapshot(manager,pid,detail)
    row=ReviewStore(manager.memory.path).get(pid,'plan',signature)
    if not row or row.get('status')!='passed':
        raise ValueError('外部AIによる'+('詳細' if detail else '全体')+'計画の目標適合性検証が未完了または不合格です。「目標検証・RAG」で現行版を確認してください')


def detail_payloads(manager,pid):
    from app.detail_store import DetailStore
    from app.detailed_planning import episode_key
    mission=manager.memory.get_mission(pid);store=DetailStore(manager.memory.path);out=[]
    for task in mission['tasks']:
        row=store.latest_plan(pid,task['id'],episode_key(task,mission))
        if row:out.append({'task_id':task['id'],'plan_id':row['id'],'payload':row['payload'],'signature':row['signature']})
    return out


def selected_detail(manager,pid,tid):
    if not tid:return None
    found=next((x for x in detail_payloads(manager,pid) if x['task_id']==tid),None)
    if not found:raise ValueError('詳細計画がありません')
    from app.detail_service import view
    if view(manager,pid,tid).get('stale'):raise ValueError('詳細計画の入力が変更されています。詳細計画を再生成してください')
    return found['payload']


def public_structure(snapshot):
    # Whitelist structural facts only. No source text, filenames, original goals or names.
    tasks=[]
    for i,t in enumerate(snapshot['tasks'],1):
        c=contract_of(t) or {}
        tasks.append({'step':i,'type':c.get('execution_kind','document_or_legacy'),
                      'parents':[next((j for j,x in enumerate(snapshot['tasks'],1) if x['task_key']==p),0) for p in t.get('depends_on',[])],
                      'outputs':[Path(x.get('path','')).suffix for x in c.get('outputs',[])],
                      'period_months':[m for m in c.get('months',[]) if isinstance(m,str) and __import__('re').fullmatch(r'20\d{2}-(0[1-9]|1[0-2])',m)],'criteria_count':len(c.get('criterion_ids',[]))})
    packet={'tasks':tasks,'limitations':['文書生成と実処理は別。機械検査は人間の意味確認を代替しない。']}
    if any(x['type']=='vehicle_calculate' for x in tasks):
        packet['limitations'].append('対応するExcel・CSV・請求PDFを原本参照付き明細へ自動抽出する。未対応形式・曖昧な配賦・税区分混在は暫定Excelに表示し、確定合格にしない。')
    if snapshot.get('detail'):
        packet['detail']=[{'step':x.get('id'),'operation':x.get('operation'),'parents':x.get('depends_on'),'validation':x.get('validation')} for x in snapshot['detail'].get('steps',[])]
    return packet


def send_allowed(manager, pid):
    mission = manager.memory.get_mission(pid)
    providers = list(dict.fromkeys(mission.get('external_providers', [])))
    configured = {x['id'] for x in manager.provider_statuses() if x.get('configured')}
    return bool(mission.get('allow_external_ai') and providers and set(providers) <= configured and manager.plan_review_runner)


def record_send_approval(manager, pid, signature, public_summary, providers=None):
    store = ReviewStore(manager.memory.path)
    packet = {'public_goal_and_plan': public_summary, 'structure': public_structure(plan_snapshot(manager, pid)[0])}
    approval = {
        'approved': True, 'packet_hash': fingerprint(packet), 'public_summary': public_summary,
        'providers': list(providers or []), 'approved_at': time.time(), 'signature': signature,
    }
    store.put(pid, 'send_approval', signature, approval)
    save_orchestration(store, pid, last_completed_stage='send_approval', plan_signature=signature,
                       resume_from='external_review')
    return approval


def approved_packet(store, pid, signature, packet):
    approval = store.get(pid, 'send_approval', signature) or {}
    return bool(approval.get('approved') and approval.get('packet_hash') == fingerprint(packet))


async def review_plan(manager,pid,signature,public_summary,safe_to_send,tid=None,idempotency_key=None):
    if not safe_to_send or not 20<=len(public_summary.strip())<=12000:raise ValueError('秘密・個人情報を含まない公開用の目標・達成条件・工程説明を確認してください')
    detail=selected_detail(manager,pid,tid);snapshot,current=plan_snapshot(manager,pid,detail)
    if current!=signature:raise ValueError('計画が変わりました。再読込してください')
    mission=manager.memory.get_mission(pid)
    providers=list(dict.fromkeys(mission.get('external_providers',[])))
    configured={x['id'] for x in manager.provider_statuses() if x.get('configured')}
    if not mission.get('allow_external_ai') or not providers or not set(providers)<=configured or not manager.plan_review_runner:
        raise ValueError('選択した外部AIの許可・接続が揃っていません。未検証のまま実行できません')
    store=ReviewStore(manager.memory.path)
    previous=store.get(pid,'plan',signature)
    if previous and previous.get('status')=='running' and time.time()-previous.get('started',0)<240:raise ValueError('同じ計画を検証中です')
    packet={'public_goal_and_plan':public_summary,'structure':public_structure(snapshot)}
    # safe_to_send is the explicit send approval (step 6). Never send without it.
    record_send_approval(manager, pid, signature, public_summary, providers)
    job=None
    key=idempotency_key or fingerprint(['review_plan', pid, signature, packet])
    try:
        job=begin_job(store, pid, 'review_plan', key, extra={'plan_signature':signature,'task_id':tid})
    except ValueError:
        raise
    # Reuse only parsed API responses for the exact same reviewed packet and version.
    retained=[r for r in (previous or {}).get('reviews',[]) if r.get('status') in {'pass','conditional','fail','unverifiable'}] if (previous or {}).get('packet')==packet else []
    pending=[p for p in providers if not any(r.get('provider')==p for r in retained)]
    budget=review_budget(manager,pid)
    if pending and budget.get('managed') and budget.get('remaining_calls',0)<len(pending):
        queue={'status':'waiting_budget','signature':signature,'public_summary':public_summary,'providers':providers,
               'task_id':tid,'next_at':budget.get('reset_at',time.time()+3600),'expires':time.time()+7*86400,'attempts':0,
               'packet_hash':fingerprint(packet)}
        store.put(pid,'plan_queue',signature,queue)
        result={'status':'waiting_budget','packet':packet,'reviews':retained,'pending_providers':pending,'required_calls':len(pending),'resume_at':queue['next_at'],'job_id':job['id']}
        store.put(pid,'plan',signature,result)
        finish_job(store,pid,job['id'],'succeeded',last_completed_stage='external_review')
        save_orchestration(store,pid,last_completed_stage='external_review',plan_signature=signature,resume_from='external_review')
        return result
    store.put(pid,'plan',signature,{'status':'running','started':time.time(),'packet':packet,'reviews':retained,'job_id':job['id']})
    try:
        from contextlib import nullcontext
        from app.experience_memory import configured_memory,memory_scope
        setting=configured_memory(manager.memory.path,pid)
        scope=memory_scope(setting[0],pid,signature,mode=setting[1],external_allowed=True) if setting else nullcontext()
        with scope:
            responses=await asyncio.wait_for(manager.plan_review_runner('GOAL_GATE_V1\n'+canonical(packet),pending),timeout=180) if pending else []
        parsed=[]
        for provider in providers:
            saved=next((r for r in retained if r.get('provider')==provider),None)
            if saved:
                parsed.append(saved);continue
            r=next((x for x in responses if x.get('id')==provider),{})
            verdict={'provider':provider,'status':'unverified','issues':[]}
            if r.get('ok'):
                try:
                    from app.structured_planning import decode_object
                    body=decode_object(r.get('review',''))
                    if body.get('verdict') in {'pass','conditional','fail','unverifiable'} and isinstance(body.get('issues'),list):
                        verdict.update(status=body['verdict'],issues=body['issues'])
                except (ValueError,TypeError):pass
            verdict['response']=r;parsed.append(verdict)
        passed=all(x['status']=='pass' and not x['issues'] for x in parsed)
        if plan_snapshot(manager,pid,selected_detail(manager,pid,tid))[1]!=signature:passed=False
        result={'status':'passed' if passed else ('awaiting_external' if all(x['status']=='unverified' and not x.get('response',{}).get('ok') for x in parsed) else 'not_passed'),'reviews':parsed,'packet':packet,'finished':time.time(),'pending_providers':[x['provider'] for x in parsed if x['status']=='unverified'],'job_id':job['id'] if job else ''}
    except BaseException as exc:
        store.put(pid,'plan',signature,{'status':'unverified','error':type(exc).__name__,'packet':packet,'reviews':retained})
        if job:finish_job(store,pid,job['id'],'failed',blocking_error=type(exc).__name__)
        raise
    store.put(pid,'plan',signature,result)
    queued=store.get(pid,'plan_queue',signature)
    if queued and queued.get('status')!='cancelled' and result['status']!='waiting_budget':
        queued['status']='finished';store.put(pid,'plan_queue',signature,queued)
    if job:finish_job(store,pid,job['id'],'succeeded' if result['status']=='passed' else 'failed',last_completed_stage='external_review')
    save_orchestration(store,pid,last_completed_stage='external_review',plan_signature=signature,
                       resume_from='plan_approval' if result['status']=='passed' else 'external_review')
    if result['status']=='passed':
        revision=store.get(pid,'revision',signature) or {}
        if revision.get('lifecycle')=='revalidation_pending' or revision.get('status')=='applied':
            for action in revision.get('actions') or []:
                if action.get('disposition') in {'amend','rebuild_vehicle'}:
                    action['lifecycle']='validated'
            revision['lifecycle']='validated'
            store.put(pid,'revision',signature,revision)
    if result['status']=='passed':
        current=manager.memory.get_mission(pid)
        for task in current['tasks']:
            message='外部AIによる詳細計画' if tid else '外部AIによる全体計画'
            if (not tid or task['id']==tid) and task['status']=='needs_review' and message in task.get('error',''):
                manager.memory.update_task(task['id'],'pending',error='')
    manager.memory.add_event(pid,'goal_plan_review','外部AIによる目標適合性検証: '+result['status'],detail=canonical({'signature':signature,'task_id':tid,'status':result['status']}))
    return result


def execution_snapshot(manager,pid):
    mission=manager.memory.get_mission(pid);project=manager.memory.get_project(pid);artifacts=[];failures=[]
    if mission['status']!='completed':failures.append('全体目標が未達成です')
    if not mission['tasks']:failures.append('実行タスクがありません')
    def resolve(path):return manager.workspace.resolve_file(project.get('workspace_path',''),pid,path,must_exist=True)[2]
    for task in mission['tasks']:
        if task['status']!='completed':failures.append(task['task_key']+': 未完了です')
        c=contract_of(task)
        if not c:failures.append(task['task_key']+': 検証可能な成果物契約がありません');continue
        failures.extend(verify_outputs(task,resolve))
        for output in c.get('outputs',[]):
            try:artifacts.append({'path':output['path'],'hash':fingerprint(resolve(output['path']).read_bytes().hex())})
            except (ValueError,OSError):failures.append(output['path']+': 成果物がありません')
    if not artifacts:failures.append('検証できる成果物がありません')
    from app.vehicle_workflow import applicable,goal_failures
    if applicable(mission):failures.extend(goal_failures(manager,pid))
    inputs=[{'id':x['id'],'sha256':x.get('sha256'),'content_hash':fingerprint(x.get('content',''))} for x in manager.memory.list_context_files(pid,include_content=True) if x.get('source')!='memo']
    value={'plan':plan_snapshot(manager,pid)[1],'artifacts':artifacts,'sources':sorted(inputs,key=lambda x:x['id']),
           'workspace_path':project.get('workspace_path',''),'workspace_root':str(manager.workspace.root)}
    return value,fingerprint(value),list(dict.fromkeys(failures))


def evidence_valid(memory_path,pid,evidence):
    try:
        from app.memory.short_term import ShortTermMemory
        from app.workspace_files import WorkspaceSandbox
        row=ReviewStore(memory_path).get(pid,'result',evidence['human_result'])
        if not row or row['status']!='approved' or row.get('expires',0)<=time.time() or row.get('experience_id')!=evidence.get('experience_id'):return False
        proof=row['snapshot'];memory=ShortTermMemory(memory_path)
        project=memory.get_project(pid)
        if project.get('workspace_path','')!=proof['workspace_path']:return False
        workspace=WorkspaceSandbox(Path(proof['workspace_root']))
        for f in proof['artifacts']:
            path=workspace.resolve_file(proof['workspace_path'],pid,f['path'],must_exist=True)[2]
            if fingerprint(path.read_bytes().hex())!=f['hash']:return False
        current=[{'id':x['id'],'sha256':x.get('sha256'),'content_hash':fingerprint(x.get('content',''))} for x in memory.list_context_files(pid,include_content=True) if x.get('source')!='memo']
        # New independent sources are allowed; reviewed originals must remain identical.
        by_id={x['id']:x for x in current}
        return all(by_id.get(x['id'])==x for x in proof['sources'])
    except (ValueError,KeyError,TypeError,OSError):return False


async def approve_result(manager,pid,signature,reviewer,notes,lesson,conditions,checks,days=30):
    if not reviewer.strip() or not notes.strip() or len(lesson.strip())<20 or not conditions.strip() or checks!=[True,True,True] or not 1<=days<=366:
        raise ValueError('確認者・確認記録・再利用手順・適用条件と3項目の確認が必要です')
    require_review(manager,pid)
    snapshot,current,failures=execution_snapshot(manager,pid)
    if signature!=current or failures:raise ValueError('現行結果を承認できません: '+' / '.join(failures or ['結果が変わりました']))
    store=ReviewStore(manager.memory.path);prior=store.get(pid,'result',signature)
    if prior and prior.get('status')=='approved' and prior.get('expires',0)>time.time():return prior
    root=Path(manager.memory.path).parent/'experience_memory';experiences=ExperienceStore(root/'experience.sqlite3')
    rid=experiences.add(pid,'success',lesson+'\n適用条件: '+conditions,{}, {'human_result':signature})
    # Bind the authoritative row to this exact experience; an orphan cannot be retrieved.
    with experiences.connect() as db:db.execute('UPDATE experiences SET evidence=? WHERE id=? AND project=?',(canonical({'human_result':signature,'experience_id':rid}),rid,pid))
    experiences.review(pid,rid,'verified',reviewer,notes,time.time()+days*86400)
    row={'status':'approved','snapshot':snapshot,'experience_id':rid,'reviewer':reviewer,'notes':notes,'lesson':lesson,'conditions':conditions,'approved_at':time.time(),'expires':time.time()+days*86400,'index_status':'pending'}
    store.put(pid,'result',signature,row)
    from app.experience_memory import configured_memory
    setting=configured_memory(manager.memory.path,pid)
    try:
        if setting:
            await asyncio.to_thread(setting[0].reindex,pid);row['index_status']='indexed'
        else:row['index_status']='rag_disabled'
    except Exception as exc:row['index_status']='index_failed';row['index_error']=type(exc).__name__
    store.put(pid,'result',signature,row)
    from app.executable_recipes import register
    register(manager,pid,signature,row)
    manager.memory.add_event(pid,'human_result_rag_approved','人間の結果確認に基づくRAG登録',detail=canonical({'signature':signature,'experience_id':rid,'reviewer':reviewer,'index_status':row['index_status']}))
    return row


def revoke_result(manager,pid,signature,reviewer,notes):
    if not reviewer.strip() or not notes.strip():raise ValueError('取消者と理由が必要です')
    store=ReviewStore(manager.memory.path);row=store.get(pid,'result',signature)
    if not row:raise ValueError('確認記録がありません')
    row['status']='revoked';row['revoked_by']=reviewer;row['revocation_reason']=notes
    store.put(pid,'result',signature,row)
    recipe=store.get(pid,'executable_recipe',signature)
    if recipe:
        recipe=dict(recipe);recipe['status']='revoked'
        store.put(pid,'executable_recipe',signature,recipe)
    ExperienceStore(Path(manager.memory.path).parent/'experience_memory'/'experience.sqlite3').review(pid,row['experience_id'],'revoked',reviewer,notes,0)
    return row


def review_budget(manager,pid):
    from app.experience_memory import configured_memory
    setting=configured_memory(manager.memory.path,pid)
    if not setting:return {'managed':False}
    memory,mode=setting;budget=memory.config.get('budget',{})
    period=time.strftime('%Y-%m-%d',time.gmtime())
    with memory.store.connect() as db:
        calls,reserved=db.execute('SELECT COUNT(*),COALESCE(SUM(reserved_micro),0) FROM requests WHERE project=? AND period=?',(pid,period)).fetchone()
    call_limit=int(budget.get('daily_calls',0));money_limit=int(budget.get('daily_micro_usd',0));per_call=int(budget.get('per_call_micro_usd',0))
    remaining=max(0,min(call_limit-calls,(money_limit-reserved)//per_call)) if per_call>0 else 0
    return {'managed':True,'mode':mode,'period_utc':period,'used_calls':calls,'daily_calls':call_limit,'remaining_calls':remaining,
            'reset_at':(int(time.time())//86400+1)*86400}


def execution_gate(manager,pid):
    try:
        require_review(manager,pid)
        return {'blocked':False,'reason':''}
    except ValueError as exc:
        text=str(exc)
        if '追加開発' in text or '業務事実' in text:
            return {'blocked':True,'reason':text}
        return {'blocked':True,'reason':'計画の人間承認は記録できます。外部AIによる現行版の検証が未完了または未合格のため、実行開始はできません。「目標検証・人間確認・RAG」で指摘対応と再検証を行ってください。'}
