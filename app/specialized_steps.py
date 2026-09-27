"""Checkpointed public collection/final verification using their dedicated executors."""
import asyncio
from app.detail_store import DetailStore,DetailBudget
from app.detailed_planning import input_snapshot,episode_key
from app.step_executor import checkpoint_valid,save_checkpoint,resolve_artifact
from app.structured_planning import contract_of,verify_outputs
from app.upgrade_store import canonical,digest


def specialized_plan(task,snapshot,web):
    titles=(['入力・公開検索条件確認','公式資料の収集','取得結果の検査','調査報告の保存','成果物の検証','親要件の確認'] if web else
            ['先行成果の確認','契約別検査','外部実行証拠の確認','最終報告の保存','成果物の検証','親要件の確認'])
    return {'schema':'local-cowork-specialized-detail/v1','parent_contract_hash':snapshot['contract_hash'],'input_snapshot':snapshot,
            'family':'public_web' if web else 'final_verification','requirements':['parent_contract'],
            'steps':[{'id':f'D{i:02d}','title':title,'objective':title+'を既存の専用実行器と保存済み証拠から実施する',
                      'operation':('public_web_' if web else 'final_')+str(i),'depends_on':[f'D{i-1:02d}'] if i>1 else [],
                      'validation':'実データと契約を照合する','output':f'D{i:02d}.json','requirements':['parent_contract']} for i,title in enumerate(titles,1)]}


async def execute_specialized(manager,attempt):
    from app.upgrade_runtime import UpgradeStop
    store=DetailStore(manager.memory.path);pid=attempt.pid;task=attempt.task;contract=contract_of(task)
    web=bool(contract.get('public_web_research',{}).get('required'));mission=manager.memory.get_mission(pid)
    snapshot,signature=input_snapshot(manager.memory,pid,task,mission,attempt.extension,manager.workspace,attempt.project)
    episode=episode_key(task,mission);budget=DetailBudget(store,pid,episode);budget.phase='execution';attempt.budget=budget
    original_guard=attempt.guard
    def current():
        m=manager.memory.get_mission(pid);t=next(t for t in m['tasks'] if t['id']==task['id'])
        ext=store.extension(pid,t,m['plan_version'])
        return input_snapshot(manager.memory,pid,t,m,ext,manager.workspace,attempt.project)
    def guard():
        original_guard()
        if current()[1]!=signature:raise UpgradeStop('入力・親契約が変更されたため詳細工程を停止しました')
    attempt.guard=guard
    plan=None;results={}
    try:
        if any(c['state']!='available' for c in attempt.checks):raise UpgradeStop('専用工程の必要能力を確認できません')
        plan=store.latest_plan(pid,task['id'],episode)
        if not plan or plan['signature']!=signature or plan['payload'].get('replan_reason'):
            plan=store.save_plan(pid,task['id'],episode,signature,specialized_plan(task,snapshot,web))
            manager.memory.add_event(pid,'detailed_plan_created','専用処理の詳細計画を保存しました',task['id'],canonical({'plan_id':plan['id'],'family':plan['payload']['family']}))
        from app.goal_review import require_review
        from app.upgrade_runtime import ReviewRequired
        try:require_review(manager,pid,plan['payload'])
        except ValueError as exc:raise ReviewRequired(str(exc)) from exc
        for spec in plan['payload']['steps']:
            try:require_review(manager,pid,plan['payload'])
            except ValueError as exc:raise ReviewRequired(str(exc)) from exc
            sid=spec['id'];guard();step=next(s for s in store.steps(pid,plan['id']) if s['step_id']==sid)
            valid=checkpoint_valid(manager,attempt,step)
            if valid and sid in {'D04','D05','D06'}:
                valid=all(resolve_artifact(manager,attempt,a['path']).exists() and digest(resolve_artifact(manager,attempt,a['path']).read_bytes())==a['sha256'] for a in step['output'].get('artifacts',[]))
            if valid:
                results[sid]=step['output'];continue
            if step['state']!='pending':store.invalidate(pid,plan['id'],sid,'未完了または成果の変更を検出')
            budget.step=sid;budget.consume('tool');store.checkpoint(pid,plan['id'],sid,'running')
            try:
                if sid=='D01':
                    if any(d['status'] not in {'completed','skipped'} or any(a.get('state')=='missing' for a in d['artifacts']) for d in snapshot['dependencies']):raise UpgradeStop('先行工程・成果が未完了です')
                    value={'input_snapshot':snapshot,'query':contract.get('public_web_research',{}).get('query')}
                elif sid=='D02':
                    if web:
                        ids=await asyncio.wait_for(manager._collect_public_web_evidence(attempt.project,pid,task,contract),timeout=max(0.1,min(180,budget.remaining())))
                        if not ids:raise UpgradeStop('公開資料を取得できませんでした')
                        # Only the collector's new source records are expected to change inputs.
                        new_snapshot,new_signature=current()
                        if {k:v for k,v in new_snapshot.items() if k!='sources'}!={k:v for k,v in snapshot.items() if k!='sources'}:raise UpgradeStop('収集中に親契約・先行成果が変更されました')
                        old_sources={s['id']:s['version'] for s in snapshot['sources']};new_sources={s['id']:s['version'] for s in new_snapshot['sources']}
                        if any(new_sources.get(k)!=v for k,v in old_sources.items()) or set(new_sources)-set(old_sources)-set(ids):raise UpgradeStop('収集以外の原本変更を検出しました')
                        snapshot,signature=new_snapshot,new_signature;plan['signature']=signature;plan['payload']['input_snapshot']=snapshot
                        with store.connect() as db:db.execute('UPDATE detailed_plans SET signature=?,payload=? WHERE id=? AND project_id=?',(signature,canonical(plan['payload']),plan['id'],pid))
                        value={'source_ids':sorted(ids)}
                    else:
                        errors=[]
                        for previous in mission['tasks']:
                            if previous['id']!=task['id'] and contract_of(previous):
                                errors+=verify_outputs(previous,lambda p:resolve_artifact(manager,attempt,p))
                                if previous['status'] not in {'completed','skipped'}:errors.append(previous['title']+' は未完了')
                        if errors:raise UpgradeStop('先行成果の検証不合格: '+'; '.join(errors))
                        value={'predecessors_verified':len(mission['tasks'])-1}
                elif sid=='D03':
                    if web:
                        ids=set(results['D02']['source_ids']);files=manager.memory.list_context_files(pid,include_content=True)
                        selected=[f for f in files if f['id'] in ids and f.get('source')=='web' and f.get('content')]
                        if len(selected)!=len(ids):raise UpgradeStop('取得済み原本が不足しています')
                        value={'verified_source_ids':sorted(ids)}
                    else:
                        if manager._task_requires_external_evidence(task) and not manager._task_external_evidence_satisfied(task,manager._external_execution_evidence(pid)):raise UpgradeStop('必要な外部実行証拠がありません')
                        value={'external_evidence_gate':'passed_or_not_required'}
                elif sid=='D04':
                    if web:
                        report=manager._execute_public_web_evidence_report(attempt.project,pid,task,manager.memory.list_context_files(pid,include_content=True),set(results['D02']['source_ids']))
                    else:report=manager._execute_structured_final_verification(attempt.project,pid,task)
                    value={'report':report,'artifacts':[{'path':o['path'],'sha256':digest(resolve_artifact(manager,attempt,o['path']).read_bytes())} for o in contract['outputs']]}
                else:
                    errors=verify_outputs(task,lambda p:resolve_artifact(manager,attempt,p))
                    if errors:raise UpgradeStop('成果物検証不合格: '+'; '.join(errors))
                    value={'verification':'passed','artifacts':results['D04']['artifacts']}
                save_checkpoint(manager,attempt,store,plan,sid,'completed',value);results[sid]=value
            except ReviewRequired:raise
            except asyncio.CancelledError:
                store.checkpoint(pid,plan['id'],sid,'pending',error='中断');raise
            except Exception as exc:
                store.checkpoint(pid,plan['id'],sid,'failed',error=str(exc))
                for downstream in plan['payload']['steps']:
                    if downstream['id']>sid:store.checkpoint(pid,plan['id'],downstream['id'],'blocked',error='先行工程 '+sid+' が未完了')
                attempt.store.record('recovery_attempts',pid,attempt.aid,{'plan_id':plan['id'],'step_id':sid,'root_error':str(exc)})
                raise UpgradeStop(sid+': '+str(exc)) from exc
        guard()
        return results['D04']['report']
    finally:budget.close()
