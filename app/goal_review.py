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


def load_review_policy(manager):
    path=Path(manager.memory.path).parent/'goal_review_policy.json'
    if not path.exists():
        return {}
    try:
        config=json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}
    return config if isinstance(config, dict) else {}


def enabled(manager,pid):
    config=load_review_policy(manager)
    if not config:
        return False
    return config.get('projects',{}).get(pid,config.get('required',False)) is True


def _policy_section(config, pid):
    projects=config.get('projects') or {}
    row=projects.get(pid)
    if isinstance(row, dict):
        return row
    return config


def review_pass_policy(manager, pid, provider_count=None):
    """最低合格数と必須provider。未設定時は全社成功が必要（既存テスト互換）。"""
    config=load_review_policy(manager)
    section=_policy_section(config, pid)
    raw_required=section.get('required_providers', config.get('required_providers') or [])
    if isinstance(raw_required, str):
        raw_required=[raw_required]
    required=[str(x).strip() for x in (raw_required or []) if str(x).strip()]
    raw_min=section.get('min_success_count', config.get('min_success_count'))
    if raw_min is None:
        min_count=provider_count
    else:
        try:
            min_count=int(raw_min)
        except (TypeError, ValueError):
            min_count=provider_count
    if provider_count is not None and min_count is not None:
        min_count=max(0, min(int(min_count), int(provider_count)))
    return {
        'min_success_count': min_count,
        'required_providers': list(dict.fromkeys(required)),
    }


def provider_review_outcomes(parsed, providers):
    by={x.get('provider'): x for x in parsed or []}
    rows=[]
    for provider in providers:
        row=by.get(provider) or {}
        status=row.get('status')
        if not row:
            outcome,label='not_run','未実施'
        elif status=='connection_error':
            outcome,label='connection_error','接続エラー'
        elif status=='pass' and not row.get('issues'):
            outcome,label='success','成功'
        elif status in {'conditional','fail','unverifiable'} or (status=='pass' and row.get('issues')):
            outcome,label='content_fail','計画内容の指摘'
        elif status=='unverified':
            response=row.get('response') or {}
            if response.get('ok'):
                outcome,label='failed','解析不能'
            elif response:
                outcome,label='connection_error' if response.get('outcome')=='connection_error' else 'failed','接続エラー' if response.get('outcome')=='connection_error' else '未取得'
            else:
                outcome,label='not_run','未実施'
        else:
            outcome,label='failed','失敗'
        item={
            'id': provider, 'provider': provider, 'outcome': outcome, 'label': label,
            'status': status or '',
        }
        if status=='connection_error' or outcome=='connection_error':
            conn=row.get('connection_error') if isinstance(row.get('connection_error'), dict) else {}
            response=row.get('response') if isinstance(row.get('response'), dict) else {}
            item['error']=str(conn.get('error') or response.get('error') or '')[:500]
            item['status_code']=conn.get('status_code') or response.get('status_code')
            item['category']=conn.get('category') or response.get('error_category') or 'connection_error'
        rows.append(item)
    return rows


def evaluate_external_review_status(parsed, providers, policy):
    selected=list(dict.fromkeys(providers or []))
    required=list(dict.fromkeys(policy.get('required_providers') or []))
    needed=policy.get('min_success_count')
    if needed is None:
        needed=len(selected)
    try:
        needed=int(needed)
    except (TypeError, ValueError):
        needed=len(selected)
    if selected:
        needed=max(0, min(needed, len(selected)))
    else:
        needed=max(0, needed)
    outcomes={row['id']: row['outcome'] for row in provider_review_outcomes(parsed, selected)}
    success_count=sum(1 for provider in selected if outcomes.get(provider)=='success')
    has_content=any(outcomes.get(provider)=='content_fail' for provider in selected)
    required_connection=[]
    for provider in required:
        outcome=outcomes.get(provider)
        if provider not in selected or outcome in {'connection_error','not_run','failed'}:
            required_connection.append(provider)
    required_connection=list(dict.fromkeys(required_connection))
    if required_connection:
        names='、'.join(required_connection)
        return {
            'status': 'connection_failed',
            'stop_kind': 'connection_settings',
            'stop_reason': (
                f'必須の外部AI（{names}）の接続に失敗したため停止しました。'
                '計画内容の欠陥ではなく接続設定の問題です。APIキー・認証・レート制限を確認してください。'
            ),
            'success_count': success_count,
            'required_count': needed,
        }
    required_unmet=[p for p in required if outcomes.get(p)!='success']
    if success_count>=needed and not required_unmet:
        return {
            'status': 'passed', 'stop_kind': '', 'stop_reason': '',
            'success_count': success_count, 'required_count': needed,
        }
    by={x.get('provider'): x for x in parsed or []}
    all_unverified_no_ok = bool(selected) and all(
        (by.get(provider) or {}).get('status') in {'unverified', 'connection_error'}
        and not ((by.get(provider) or {}).get('response') or {}).get('ok')
        for provider in selected
    )
    only_connection_shortfall = (
        not has_content
        and any(outcomes.get(provider)=='connection_error' for provider in selected)
        and not any(
            (by.get(provider) or {}).get('status')=='unverified'
            and ((by.get(provider) or {}).get('response') or {}).get('ok')
            for provider in selected
        )
    )
    if (all_unverified_no_ok or only_connection_shortfall) and not has_content:
        return {
            'status': 'awaiting_external', 'stop_kind': 'connection_settings',
            'stop_reason': '外部AIの接続に失敗したため検証を完了できません。計画内容への指摘ではありません。',
            'success_count': success_count, 'required_count': needed,
        }
    return {
        'status': 'not_passed', 'stop_kind': 'plan', 'stop_reason': '',
        'success_count': success_count, 'required_count': needed,
    }


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
    if row and row.get('status')=='passed':
        return
    if row and row.get('status')=='connection_failed':
        raise ValueError(row.get('stop_reason') or '必須の外部AIの接続に失敗したため実行を開始できません。計画内容の問題ではなく接続設定の問題です。')
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
    purpose_catalog={
        'input_readiness':'Verify registered sources, missing inputs, constraints, and validation methods.',
        'multi_channel_workflow_design':'Design the execution workflow for forms, landing pages, social support, and lead capture.',
        'execution_monitoring_evidence':'Use monitor progress, errors, publication records, and lead counts as evidence.',
        'manual_social_posting_guard':'Prevent automatic social posting and require a human-controlled posting screen.',
        'external_approval_boundaries':'Require separate human approval for publication, copy approval, posting, and prospect contact.',
        'evidence_based_completion':'Judge completion using URLs, responses, approvals, and other execution evidence.',
        'campaign_execution_sequence':'Execute publication, posting-kit preparation, approval, manual posting, response sync, and lead evaluation in order.',
        'failure_recovery_policy':'Classify failures, stop for authentication or approval, and safely retry temporary failures.',
        'market_and_customer_definition':'Define markets, priority sectors, ideal customers, and customer problems.',
        'service_package_design':'Design multiple consulting service packages.',
        'commercial_terms':'Define scope, duration, prerequisites, price proposals, and exclusions.',
        'sales_assets':'Create introduction, proposal, interview, proof-of-concept, and quotation materials.',
        'sales_plan':'Create a sales plan with channels, activity, KPIs, and financial outlook.',
        'prospect_prioritization':'Evaluate prospects and define priorities and proposal hypotheses.',
        'outreach_content':'Create approved outreach copy, meeting scripts, and follow-up copy.',
        'pipeline_tracking':'Create structured tracking for meetings, requirements, next actions, and losses.',
        'delivery_process':'Define the post-order process through requirements, proof-of-concept, acceptance, education, and support.',
        'execution_status':'Report executed, approval-pending, not-started, failed, and next-action states.',
        'approved_customer_engagement':'Execute only approved customer engagement and register external evidence.',
        'final_goal_verification':'Verify every criterion against artifacts and execution evidence; any unmet criterion prevents PASS.',
        'criterion_delivery':'Create and verify the artifact required by its criterion.',
    }
    tasks=[]
    for i,t in enumerate(snapshot['tasks'],1):
        c=contract_of(t) or {}
        kind=c.get('execution_kind','document_or_legacy')
        role=('preparation' if c.get('role')=='preparation' else
              'final_verification' if c.get('final_verification') else
              'external_action' if c.get('action_requirements') else 'execution')
        tasks.append({'step':i,'type':kind,'role':role,
                      'parents':[next((j for j,x in enumerate(snapshot['tasks'],1) if x['task_key']==p),0) for p in t.get('depends_on',[])],
                      'outputs':[Path(x.get('path','')).suffix for x in c.get('outputs',[])],
                      'criterion_ids':[x for x in c.get('criterion_ids',[]) if isinstance(x,str) and __import__('re').fullmatch(r'SC[0-9]{2}',x)],
                      'public_purpose_code':c.get('public_purpose_code','unspecified'),
                      'public_purpose_summary':purpose_catalog.get(c.get('public_purpose_code'),'Purpose summary unavailable.'),
                      'artifact_category':c.get('artifact_category','unspecified'),
                      'responsible_role':c.get('responsible_role','unspecified'),
                      'source_reference_count':len(c.get('source_refs',[])),
                      'completion_evidence':c.get('completion_evidence','unspecified'),
                      'failure_policy':c.get('failure_policy','unspecified'),
                      'input_count':len(c.get('inputs',[])) + (len(c.get('source_refs',[])) if c.get('role')=='preparation' else 0),
                      'required_heading_count':sum(len(x.get('required_headings',[])) for x in c.get('outputs',[]) if isinstance(x,dict)),
                      'action_kinds':[x.get('kind') for x in c.get('action_requirements',[]) if isinstance(x,dict) and x.get('kind')],
                      'approval_required':bool(c.get('approval_required') or c.get('action_requirements')) and not c.get('final_verification'),
                      'evidence_required':bool(c.get('evidence_required')) or any(bool(x.get('evidence_required')) for x in c.get('action_requirements',[]) if isinstance(x,dict)),
                      'human_confirmation_required':bool(c.get('human_confirmation_required')),
                      'semantic_review_required':bool(c.get('semantic_review_required')),
                      'exit_check_count':len(c.get('exit_checks',[])),
                      'estimated_days':c.get('estimated_days'),
                      'period_months':[m for m in c.get('months',[]) if isinstance(m,str) and __import__('re').fullmatch(r'20\d{2}-(0[1-9]|1[0-2])',m)],'criteria_count':len(c.get('criterion_ids',[]))})
    used={x['public_purpose_code'] for x in tasks}
    packet={'goal_category':'controlled_business_execution','tasks':tasks,
            'purpose_catalog':{key:value for key,value in purpose_catalog.items() if key in used},
            'limitations':['文書生成と実処理は別。機械検査は人間の意味確認を代替しない。']}
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


async def review_plan(manager,pid,signature,public_summary,safe_to_send,tid=None,idempotency_key=None,providers_override=None):
    if not safe_to_send or not 20<=len(public_summary.strip())<=12000:raise ValueError('秘密・個人情報を含まない公開用の目標・達成条件・工程説明を確認してください')
    detail=selected_detail(manager,pid,tid);snapshot,current=plan_snapshot(manager,pid,detail)
    if current!=signature:raise ValueError('計画が変わりました。再読込してください')
    mission=manager.memory.get_mission(pid)
    allowed_providers=list(dict.fromkeys(mission.get('external_providers',[])))
    providers=(list(dict.fromkeys(providers_override)) if providers_override is not None else allowed_providers)
    if providers_override is not None:
        if not providers or not set(providers) <= set(allowed_providers):
            raise ValueError('検証AIはプロジェクトで許可済みのAIから選択してください')
        policy_config=load_review_policy(manager)
        section=_policy_section(policy_config,pid)
        required=section.get('required_providers',policy_config.get('required_providers') or [])
        if isinstance(required,str):required=[required]
        if not set(required or []) <= set(providers):
            raise ValueError('必須の検証AIを外せません')
        raw_min=section.get('min_success_count',policy_config.get('min_success_count'))
        try:minimum=int(raw_min) if raw_min is not None else None
        except (TypeError,ValueError):minimum=None
        if minimum is not None and len(providers)<minimum:
            raise ValueError('検証AIの数が最低合格数を下回ります')
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
        result={'status':'waiting_budget','packet':packet,'reviews':retained,'pending_providers':pending,'required_calls':len(pending),'resume_at':queue['next_at'],'job_id':job['id'],'called_providers':[],'reused_providers':[r.get('provider') for r in retained]}
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
            from app.external_ai import connection_error_from_payload
            conn=connection_error_from_payload(r)
            verdict={'provider':provider,'status':'unverified','issues':[]}
            if conn:
                verdict.update(status='connection_error',connection_error=conn,issues=[])
            elif r.get('ok'):
                try:
                    from app.structured_planning import decode_object
                    body=decode_object(r.get('review',''))
                    if body.get('verdict') in {'pass','conditional','fail','unverifiable'} and isinstance(body.get('issues'),list):
                        verdict.update(status=body['verdict'],issues=body['issues'])
                except (ValueError,TypeError):pass
            verdict['response']=r;parsed.append(verdict)
        policy=review_pass_policy(manager,pid,len(providers))
        judged=evaluate_external_review_status(parsed,providers,policy)
        if plan_snapshot(manager,pid,selected_detail(manager,pid,tid))[1]!=signature:
            judged={'status':'not_passed','stop_kind':'plan','stop_reason':'','success_count':0,'required_count':policy.get('min_success_count')}
        connection_errors=[{
            'provider':x['provider'],
            'status_code':(x.get('connection_error') or {}).get('status_code') or (x.get('response') or {}).get('status_code'),
            'category':(x.get('connection_error') or {}).get('category') or (x.get('response') or {}).get('error_category') or 'connection_error',
            'error':((x.get('connection_error') or {}).get('error') or (x.get('response') or {}).get('error') or '')[:2000],
        } for x in parsed if x.get('status')=='connection_error']
        result={'status':judged['status'],'reviews':parsed,'packet':packet,'finished':time.time(),
                'called_providers':list(pending),'reused_providers':[r.get('provider') for r in retained],
                'pending_providers':[x['provider'] for x in parsed if x['status'] in {'unverified','connection_error'}],
                'job_id':job['id'] if job else '','provider_outcomes':provider_review_outcomes(parsed,providers),
                'connection_errors':connection_errors,'pass_policy':policy,
                'success_count':judged.get('success_count',0),'required_count':judged.get('required_count'),
                'stop_kind':judged.get('stop_kind') or '','stop_reason':judged.get('stop_reason') or ''}
    except BaseException as exc:
        store.put(pid,'plan',signature,{'status':'unverified','error':type(exc).__name__,'packet':packet,'reviews':retained})
        if job:finish_job(store,pid,job['id'],'failed',blocking_error=type(exc).__name__)
        raise
    store.put(pid,'plan',signature,result)
    queued=store.get(pid,'plan_queue',signature)
    if queued and queued.get('status')!='cancelled' and result['status']!='waiting_budget':
        queued['status']='finished';store.put(pid,'plan_queue',signature,queued)
    job_status='succeeded' if result['status']=='passed' else ('needs_attention' if result['status']=='connection_failed' else 'failed')
    if job:finish_job(store,pid,job['id'],job_status,last_completed_stage='external_review',
                      blocking_error=result.get('stop_reason') or '')
    save_orchestration(store,pid,last_completed_stage='external_review',plan_signature=signature,
                       resume_from='plan_approval' if result['status']=='passed' else 'external_review',
                       blocking_error=result.get('stop_reason') or '',
                       stop_kind=result.get('stop_kind') or '')
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
    # P1-A: 撤回時は正本で即時不採用とし索引から削除する。検索結果の再照合は維持される。
    try:
        from app.experience_memory import configured_memory
        setting=configured_memory(manager.memory.path,pid)
        if setting:
            setting[0].remove_from_index(pid,row['experience_id'],reviewer,notes)
    except Exception:
        pass
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


def all_reviews_history(manager, pid):
    """履歴表示専用: legacy review を含めた全レビュー・指摘の閲覧用履歴を取得する。"""
    store = ReviewStore(manager.memory.path)
    mission = manager.memory.get_mission(pid)
    legacy = [dict(x, origin='legacy_mission', source_plan_version=None) for x in mission.get('plan_reviews', []) if isinstance(x, dict)]
    legacy_store = [payload for _, payload in store.list(pid, 'legacy_feedback')]
    plan_reviews = [{'signature': sig, **payload} for sig, payload in store.list(pid, 'plan')]
    user_feedbacks = [{'signature': sig, **payload} for sig, payload in store.list(pid, 'feedback')]
    return {
        'mission_legacy_reviews': legacy,
        'legacy_feedback_store': legacy_store,
        'plan_reviews': plan_reviews,
        'user_feedbacks': user_feedbacks,
    }


def execution_gate(manager,pid):
    try:
        from app.plan_repair_loop import approval_gate
        approval_gate(manager, pid)
        require_review(manager,pid)
        # Stage 2-A (追加のみ): missing_capability が残る計画は実行承認不可。
        # 既存のゲートを弱めない (不可にしかしない)。
        try:
            from app.capability_gap import blocks_execution_approval as _gap_gate
            _gap = _gap_gate(manager, pid)
            if isinstance(_gap, dict) and _gap.get("blocked"):
                return {"blocked": True,
                        "reason": _gap.get("reason") or "不足機能の提案が残っているため実行承認できません"}
        except Exception:
            pass
        return {'blocked':False,'reason':''}
    except ValueError as exc:
        text=str(exc)
        if '修復ループ' in text:
            return {'blocked':True,'reason':text}
        if '追加開発' in text or '業務事実' in text:
            return {'blocked':True,'reason':text}
        if '接続設定' in text or '接続に失敗' in text:
            return {'blocked':True,'reason':text,'stop_kind':'connection_settings'}
        return {'blocked':True,'reason':'計画の人間承認は記録できます。外部AIによる現行版の検証が未完了または未合格のため、実行開始はできません。「目標検証・人間確認・RAG」で指摘対応と再検証を行ってください。'}


def require_human_final_confirmation_ready(gate_result: dict) -> None:
    """Reject human final confirmation until every criterion is independently PASS."""
    from app.completion_gate import criteria_ready_for_human_confirmation

    if not criteria_ready_for_human_confirmation(gate_result):
        raise ValueError("COMPLETION_GATE_FAILED: 全達成条件がPASSした後にだけ最終確認できます")
