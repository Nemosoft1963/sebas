import json,time
from pathlib import Path
from types import SimpleNamespace
import pytest
from app.memory.short_term import ShortTermMemory
from app.workspace_files import WorkspaceSandbox
from app.project_manager import ProjectOrchestrator
from app.structured_planning import compile_task,contract_of
from app.goal_review import *
from app.experience_memory import ExperienceMemory


def setup(tmp_path,required=False):
    memory=ShortTermMemory(tmp_path/'conversations.db');p=memory.create_project('private',workspace_path='projects/private');pid=p['id']
    memory.save_mission(pid,'機密会社の作業結果を整理する','根拠を示す','',True,['a','b'])
    task=compile_task(1,'根拠を示す',{'title':'機密の文書','scope':'根拠を示す','headings':['概要','確認'],'depends_on':[]},[])
    memory.replace_plan(pid,'test',[task])
    task=memory.get_mission(pid)['tasks'][0];ws=WorkspaceSandbox(tmp_path/'workspace')
    manager=ProjectOrchestrator(memory,None,lambda p:('',[]),None,lambda:[{'id':'a','configured':True},{'id':'b','configured':True}],workspace=ws)
    if required:(tmp_path/'goal_review_policy.json').write_text('{"required":true}')
    return manager,pid,task


def complete(manager,pid,task):
    out=contract_of(task)['outputs'][0];path=manager.workspace.resolve_file('projects/private',pid,out['path'])[2];path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text('\n'.join('## '+h+'\n'+'原本と結果を確認したテスト文書。'*50 for h in out['required_headings']),encoding='utf-8')
    manager.memory.update_task(task['id'],'completed',result='tested')
    manager.memory.set_final_report(pid,'done',status='completed')
    return path


@pytest.mark.asyncio
async def test_all_reviewers_must_pass_and_raw_private_goal_not_sent(tmp_path):
    manager,pid,task=setup(tmp_path,True)
    with pytest.raises(ValueError):require_review(manager,pid)
    sent=[]
    async def runner(text,providers):
        sent.append(text)
        return [{'id':x,'ok':True,'review':json.dumps({'verdict':'pass','issues':[]})} for x in providers]
    manager.plan_review_runner=runner
    sig=plan_snapshot(manager,pid)[1]
    await review_plan(manager,pid,sig,'一般的な文書作成の目標・根拠・工程・出力を確認するための公開説明です。',True)
    require_review(manager,pid)
    assert '機密会社' not in sent[0] and '機密の文書' not in sent[0]
    manager.memory.add_mission_instruction(pid,'新しい条件')
    with pytest.raises(ValueError):require_review(manager,pid)


@pytest.mark.asyncio
@pytest.mark.parametrize('verdict',['conditional','fail','unverifiable','bad'])
async def test_nonpass_or_malformed_review_cannot_approve(tmp_path,verdict):
    manager,pid,_=setup(tmp_path,True)
    async def runner(*args):return [{'id':'a','ok':True,'review':json.dumps({'verdict':verdict,'issues':[]})},{'id':'b','ok':False}]
    manager.plan_review_runner=runner
    result=await review_plan(manager,pid,plan_snapshot(manager,pid)[1],'目標と各工程を比較し、入力から出力まで不足がないか検証します。',True)
    assert result['status']=='not_passed'
    with pytest.raises(ValueError):require_review(manager,pid)


@pytest.mark.asyncio
async def test_human_result_requires_completed_unchanged_output_and_explicit_checks(tmp_path):
    manager,pid,task=setup(tmp_path)
    sig=execution_snapshot(manager,pid)[1]
    with pytest.raises(ValueError):await approve_result(manager,pid,sig,'person','checked','この手順を同一条件で再利用するために確認した内容です','同じ帳票形式のみ',[True]*3)
    path=complete(manager,pid,task);sig=execution_snapshot(manager,pid)[1]
    with pytest.raises(ValueError):await approve_result(manager,pid,sig,'person','checked','この手順を同一条件で再利用するために確認した内容です','同じ帳票形式のみ',[True,False,True])
    row=await approve_result(manager,pid,sig,'person','原本と成果物を照合','この手順を同一条件で再利用するために確認した内容です','同じ帳票形式のみ',[True]*3)
    assert row['index_status']=='rag_disabled'
    evidence={'human_result':sig,'experience_id':row['experience_id']}
    assert evidence_valid(manager.memory.path,pid,evidence)
    assert not evidence_valid(manager.memory.path,'other',evidence)
    path.write_text('changed',encoding='utf-8')
    assert not evidence_valid(manager.memory.path,pid,evidence)


@pytest.mark.asyncio
async def test_rag_retrieval_rechecks_revocation_even_with_stale_vector_index(tmp_path):
    manager,pid,task=setup(tmp_path);complete(manager,pid,task);sig=execution_snapshot(manager,pid)[1]
    row=await approve_result(manager,pid,sig,'person','確認済み','この手順を同一条件で再利用するために確認した内容です','同じ条件のみ',[True]*3)
    class Index:
        def search(self,*args):return [row['experience_id']]
    rag=ExperienceMemory(tmp_path/'experience_memory',{},Index());scope={'project':pid,'mode':'enforce'}
    assert len(rag.retrieve(scope,'手順'))==1
    revoke_result(manager,pid,sig,'person','手順に問題を発見')
    assert rag.retrieve(scope,'手順')==[]


@pytest.mark.asyncio
async def test_safe_to_send_false_rejects_and_packet_has_no_source_text(tmp_path):
    manager,pid,task=setup(tmp_path,True)
    sent=[]
    async def runner(text,providers):
        sent.append(text)
        return [{'id':x,'ok':True,'review':json.dumps({'verdict':'pass','issues':[]})} for x in providers]
    manager.plan_review_runner=runner
    sig=plan_snapshot(manager,pid)[1]
    with pytest.raises(ValueError,match='公開用'):await review_plan(manager,pid,sig,'一般的な文書作成の目標・根拠・工程・出力を確認するための公開説明です。',False)
    assert sent==[]
    await review_plan(manager,pid,sig,'一般的な文書作成の目標・根拠・工程・出力を確認するための公開説明です。',True)
    assert sent and '機密会社' not in sent[0] and '機密の文書' not in sent[0]


@pytest.mark.asyncio
async def test_old_signature_passed_review_is_rejected_after_plan_change(tmp_path):
    manager,pid,_=setup(tmp_path,True)
    old=plan_snapshot(manager,pid)[1]
    ReviewStore(manager.memory.path).put(pid,'plan',old,{'status':'passed'})
    require_review(manager,pid)
    manager.memory.add_mission_instruction(pid,'新しい照合条件')
    with pytest.raises(ValueError):require_review(manager,pid)


@pytest.mark.asyncio
async def test_external_send_crash_is_needs_attention(tmp_path):
    from app.goal_review_queue import recover_interrupted
    manager,pid,_=setup(tmp_path,True)
    sig=plan_snapshot(manager,pid)[1]
    store=ReviewStore(manager.memory.path)
    store.put(pid,'plan',sig,{'status':'running','started':time.time(),'packet':{},'reviews':[]})
    store.put(pid,'plan_queue',sig,{'status':'running','public_summary':'x'*20,'providers':['a'],'attempts':1})
    store.put(pid,'job','j1',{'id':'j1','kind':'review_plan','status':'running','idempotency_key':'k','started':time.time()})
    recover_interrupted(store)
    assert store.get(pid,'plan',sig)['status']=='needs_attention'
    assert store.get(pid,'plan_queue',sig)['status']=='needs_attention'
    assert store.get(pid,'job','j1')['status']=='needs_attention'


@pytest.mark.asyncio
async def test_send_disabled_without_external_ai_local_repair_available(tmp_path):
    from app.goal_review import send_allowed
    from app.workflow_readiness import build_readiness
    from app.plan_feedback import import_feedback
    manager,pid,task=setup(tmp_path,True)
    manager.memory.save_mission(pid,'機密会社の作業結果を整理する','根拠を示す','',False,[])
    assert send_allowed(manager,pid) is False
    ready=build_readiness(manager,pid)
    assert ready['send_allowed'] is False
    assert ready['allowed_actions']['external_review']['allowed'] is False
    sig=plan_snapshot(manager,pid)[1]
    imported=import_feedback(manager,pid,sig,'Claude 貼付','工程に原本と結果の照合が不足しています。原本の参照箇所と照合結果を対応付けてください。')
    assert imported['issues']
    assert ready['allowed_actions']['import_feedback']['allowed'] is True or build_readiness(manager,pid)['allowed_actions']['import_feedback']['allowed'] is True


@pytest.mark.asyncio
async def test_reload_restores_active_job_and_resume_from(tmp_path):
    from app.goal_review import begin_job
    from app.workflow_readiness import build_readiness
    manager,pid,_=setup(tmp_path,True)
    store=ReviewStore(manager.memory.path)
    job=begin_job(store,pid,'propose','reload-key',extra={'resume_from':'local_proposal'})
    ready=build_readiness(manager,pid)
    assert ready['active_job']
    assert ready['active_job']['id']==job['id']
    assert ready['resume_from']=='local_proposal'
    assert ready['active_job']['kind']=='propose'


@pytest.mark.asyncio
async def test_duplicate_running_job_is_conflict(tmp_path):
    from app.goal_review import begin_job
    manager,pid,_=setup(tmp_path)
    store=ReviewStore(manager.memory.path)
    begin_job(store,pid,'propose','same-key')
    with pytest.raises(ValueError,match='同じ処理'):begin_job(store,pid,'propose','same-key')


@pytest.mark.asyncio
async def test_new_plan_cannot_reuse_review_of_other_detail(tmp_path):
    manager,pid,task=setup(tmp_path,True)
    one={'steps':[{'id':'D01','operation':'read'}]};two={'steps':[{'id':'D01','operation':'write'}]}
    sig=plan_snapshot(manager,pid,one)[1]
    ReviewStore(manager.memory.path).put(pid,'plan',sig,{'status':'passed'})
    require_review(manager,pid,one)
    with pytest.raises(ValueError):require_review(manager,pid,two)
    with pytest.raises(ValueError):require_review(manager,pid)
