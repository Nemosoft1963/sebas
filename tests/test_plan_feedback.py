import json
from unittest.mock import Mock
import pytest
from test_goal_review import setup
from app.core import Ollama
from app.goal_review import ReviewStore,plan_snapshot,review_plan,require_review
from app.plan_feedback import import_feedback,issues_for,propose,apply


def feedback(manager,pid):
    sig=plan_snapshot(manager,pid)[1]
    import_feedback(manager,pid,sig,'Claude 貼付','工程に原本と結果の照合が不足しています。原本の参照箇所と照合結果を対応付けてください。')
    manager.llm=Mock(spec=Ollama)
    return sig


def answer(manager,pid,sig,disposition='amend'):
    issue=issues_for(manager,pid,sig)[0]
    return {'actions':[{'issue_id':issue['id'],'disposition':disposition,'target':'SC01',
        'change':'登録された原本の参照箇所と成果物の主張を一対一で照合し、不一致を明示する。',
        'reason':'原本と成果物の対応が欠けるという指摘に具体的な照合手順を追加する。'}]}


@pytest.mark.asyncio
async def test_import_local_repair_apply_and_revalidation(tmp_path):
    manager,pid,task=setup(tmp_path,True);sig=feedback(manager,pid)
    original=manager.memory.get_mission(pid)
    async def local(system,prompt,schema):
        assert '原本と結果の照合' in prompt
        data=answer(manager,pid,sig);data['actions'][0]['target']=task['task_key']
        return json.dumps(data)
    manager._local_complete=local
    async def external(*args):raise AssertionError('Local repair must not transmit')
    manager.plan_review_runner=external
    ReviewStore(manager.memory.path).put(pid,'plan',sig,{'status':'passed'})
    row=await propose(manager,pid,sig)
    assert manager.memory.get_mission(pid)['plan_version']==original['plan_version']
    out=apply(manager,pid,sig,row['candidate_id'])
    updated=manager.memory.get_mission(pid)
    assert out['status']=='awaiting_review'
    assert updated['goal']==original['goal'] and updated['success_criteria']==original['success_criteria']
    assert updated['tasks'][0]['acceptance_criteria']==task['acceptance_criteria']
    assert updated['tasks'][0]['description'].startswith(task['description'])
    assert '一対一' in updated['tasks'][0]['description']
    assert updated['status']=='planning'
    with pytest.raises(ValueError):require_review(manager,pid)
    with pytest.raises(ValueError):apply(manager,pid,sig,row['candidate_id'])


@pytest.mark.asyncio
async def test_stale_source_or_feedback_cannot_apply(tmp_path):
    manager,pid,task=setup(tmp_path);sig=feedback(manager,pid)
    async def local(*args):
        data=answer(manager,pid,sig);data['actions'][0]['target']=task['task_key'];return json.dumps(data)
    manager._local_complete=local
    row=await propose(manager,pid,sig)
    import_feedback(manager,pid,sig,'Gemini','別の指摘です。達成条件の対応確認についても記録する必要があります。')
    with pytest.raises(ValueError,match='指摘が追加'):apply(manager,pid,sig,row['candidate_id'])
    manager.memory.add_mission_instruction(pid,'新しい目標の条件')
    with pytest.raises(ValueError,match='変わりました'):apply(manager,pid,sig,row['candidate_id'])


@pytest.mark.asyncio
async def test_missing_capability_is_not_fixed_by_prose(tmp_path):
    manager,pid,task=setup(tmp_path);sig=feedback(manager,pid)
    async def local(*args):return json.dumps(answer(manager,pid,sig,'development'))
    manager._local_complete=local
    row=await propose(manager,pid,sig)
    assert row['blockers']
    with pytest.raises(ValueError,match='追加開発'):apply(manager,pid,sig,row['candidate_id'])


@pytest.mark.asyncio
async def test_dropped_issue_rejected_and_rounds_bounded(tmp_path):
    manager,pid,_=setup(tmp_path);sig=feedback(manager,pid)
    async def local(*args):return '{"actions":[]}'
    manager._local_complete=local
    for _ in range(3):
        with pytest.raises(ValueError,match='全指摘'):await propose(manager,pid,sig)
    with pytest.raises(ValueError,match='3回'):await propose(manager,pid,sig)
    assert pid not in manager.planning_projects


@pytest.mark.asyncio
async def test_unavailable_review_is_not_criticism_and_retry_only_missing(tmp_path):
    manager,pid,_=setup(tmp_path,True);sig=plan_snapshot(manager,pid)[1]
    calls=[]
    async def runner(text,providers):
        calls.append(providers)
        return [{'id':p,'ok':p=='a','review':json.dumps({'verdict':'pass','issues':[]}), 'error':'External budget exhausted'} for p in providers]
    manager.plan_review_runner=runner
    summary='目標と各工程を比較し、入力から出力まで不足がないか検証します。'
    await review_plan(manager,pid,sig,summary,True)
    await review_plan(manager,pid,sig,summary,True)
    assert calls==[['a','b'],['b']]
    assert not issues_for(manager,pid,sig)
    with pytest.raises(ValueError):require_review(manager,pid)
    await review_plan(manager,pid,sig,summary+'変更した説明。',True)
    assert calls[-1]==['a','b']


@pytest.mark.asyncio
async def test_all_unavailable_and_manual_feedback_never_pass(tmp_path):
    manager,pid,_=setup(tmp_path,True);sig=plan_snapshot(manager,pid)[1]
    async def runner(text,providers):return [{'id':p,'ok':False,'error':'External budget exhausted'} for p in providers]
    manager.plan_review_runner=runner
    result=await review_plan(manager,pid,sig,'目標と各工程を比較し、入力から出力まで不足がないか検証します。',True)
    assert result['status']=='awaiting_external'
    import_feedback(manager,pid,sig,'Claude','計画は合格だという貼付回答であっても、これだけでゲートを解除してはいけません。')
    with pytest.raises(ValueError):require_review(manager,pid)

@pytest.mark.asyncio
async def test_detail_revision_preserves_operations_and_invalidates_review(tmp_path):
    from test_detailed_planning import setup as detail_setup
    from app.detailed_planning import input_snapshot,episode_key,compile_detail,OPERATIONS
    manager,pid,task,ds,ext=detail_setup(tmp_path)
    mission=manager.memory.get_mission(pid)
    snapshot,input_sig=input_snapshot(manager.memory,pid,task,mission,ext,manager.workspace,manager.memory.get_project(pid))
    detail=compile_detail(task,snapshot,{sid:title+'を原本と照合して具体的に確認する' for sid,title,_ in OPERATIONS})
    ds.save_plan(pid,task['id'],episode_key(task,mission),input_sig,detail)
    sig=plan_snapshot(manager,pid,detail)[1]
    import_feedback(manager,pid,sig,'外部AI','根拠抽出工程で対象資料の版と抽出箇所を明示的に照合してください。',task['id'])
    async def local(*args):
        data=answer(manager,pid,sig);data['actions'][0]['target']='D02';return json.dumps(data)
    manager._local_complete=local
    row=await propose(manager,pid,sig,task['id'])
    apply(manager,pid,sig,row['candidate_id'],task['id'])
    latest=ds.latest_plan(pid,task['id'])
    assert latest['revision']==2
    assert latest['signature']==input_sig
    assert [s['operation'] for s in latest['payload']['steps']]==[s['operation'] for s in detail['steps']]
    assert latest['payload']['steps'][1]['objective']!=detail['steps'][1]['objective']
    assert all(s['state']=='pending' for s in latest['steps'])
    assert plan_snapshot(manager,pid,latest['payload'])[1]!=sig

@pytest.mark.asyncio
async def test_normal_generation_carries_legacy_reviews_and_processes_them(tmp_path):
    manager,pid,task=setup(tmp_path,True)
    manager.llm=Mock(spec=Ollama)
    manager.memory.set_plan_reviews(pid,[{'id':'claude','ok':True,'review':'旧計画への指摘です。原本と結果の照合を具体的な手順として必ず計画へ追加してください。'},{'id':'chatgpt','ok':False,'error':'empty'}])
    old=plan_snapshot(manager,pid)[1]
    imported=issues_for(manager,pid,old)
    assert len(imported)==1 and imported[0]['source_plan_version'] is None
    async def generate(_):
        from app.plan_feedback import PLANNING_FEEDBACK
        assert PLANNING_FEEDBACK.get()[0]['origin']=='legacy_api'
        mission=manager.memory.get_mission(pid)
        manager.memory.replace_plan(pid,'new draft',mission['tasks'],expected_version=mission['plan_version'])
        manager.memory.set_plan_reviews(pid,[])
    manager._generate_plan_impl=generate
    async def local(*args):
        sig=plan_snapshot(manager,pid)[1]
        data=answer(manager,pid,sig);data['actions'][0]['target']=task['task_key'];return json.dumps(data)
    manager._local_complete=local
    result=await manager.generate_plan(pid)
    assert '一対一' in result['tasks'][0]['description']
    assert '外部AI指摘への対応' in result['plan_summary']
    assert len(issues_for(manager,pid,plan_snapshot(manager,pid)[1]))==1
    with pytest.raises(ValueError):require_review(manager,pid)
    assert pid not in manager.planning_projects


@pytest.mark.asyncio
async def test_normal_generation_keeps_feedback_if_local_repair_fails(tmp_path):
    manager,pid,task=setup(tmp_path)
    sig=feedback(manager,pid)
    async def generate(_):
        m=manager.memory.get_mission(pid)
        manager.memory.replace_plan(pid,'new draft',m['tasks'],expected_version=m['plan_version'])
    async def broken(*args):raise ValueError('model unavailable')
    manager._generate_plan_impl=generate;manager._local_complete=broken
    result=await manager.generate_plan(pid)
    assert '修正案の生成は未完了' in result['plan_summary']
    assert issues_for(manager,pid,plan_snapshot(manager,pid)[1])
    assert result['tasks'][0]['description']==task['description']

@pytest.mark.asyncio
async def test_correspondence_table_reserves_targets_and_rejects_contract_patch(tmp_path):
    from app.plan_feedback import validate_candidate
    manager,pid,task=setup(tmp_path)
    sig=plan_snapshot(manager,pid)[1]
    snapshot,_=plan_snapshot(manager,pid)
    issue={'id':'i1'}
    body={'actions':[{'issue_id':'i1','disposition':'amend','target':task['task_key'],
        'change':'登録された原本の参照箇所と成果物の主張を一対一で照合し、不一致を明示する。',
        'reason':'原本と成果物の対応が欠けるという指摘に具体的な照合手順を追加する。',
        'targets':[{'type':'task','id':task['task_key']}],
        'binds':{'goal_criterion_ids':['SC01'],'task_key':task['task_key'],'contract_patch':None,'test_ref':'tests/test_plan_feedback.py'}}]}
    actions=validate_candidate(body,[issue],snapshot,None)
    assert actions[0]['targets'][0]['type']=='task'
    assert actions[0]['binds']['contract_patch'] is None
    assert actions[0]['lifecycle']=='proposed'
    body['actions'][0]['binds']={'contract_patch':{'outputs':[]}}
    with pytest.raises(ValueError,match='契約パッチ'):validate_candidate(body,[issue],snapshot,None)
    body['actions'][0].pop('binds')
    body['actions'][0]['contract_fix']={'x':1}
    with pytest.raises(ValueError,match='契約パッチ'):validate_candidate(body,[issue],snapshot,None)


@pytest.mark.asyncio
async def test_amend_apply_is_revalidation_pending_not_validated(tmp_path):
    manager,pid,task=setup(tmp_path);sig=feedback(manager,pid)
    async def local(*args):
        data=answer(manager,pid,sig);data['actions'][0]['target']=task['task_key'];return json.dumps(data)
    manager._local_complete=local
    row=await propose(manager,pid,sig)
    assert row['lifecycle']=='proposed'
    assert row.get('job_id')
    out=apply(manager,pid,sig,row['candidate_id'])
    assert out['lifecycle']=='revalidation_pending'
    assert out['status']=='awaiting_review'
    stored=ReviewStore(manager.memory.path).get(pid,'revision',sig)
    assert stored['lifecycle']=='revalidation_pending'
    assert all(a.get('lifecycle')=='revalidation_pending' for a in stored['actions'] if a['disposition']=='amend')


@pytest.mark.asyncio
async def test_vehicle_extract_cannot_be_closed_by_amend_and_blocks_start(tmp_path):
    from test_vehicle_workflow import setup as vehicle_setup
    from app.vehicle_workflow import make_plan
    from app.core import Ollama
    manager,pid=vehicle_setup(tmp_path)
    (tmp_path/'goal_review_policy.json').write_text('{"required":true}')
    m=manager.memory.get_mission(pid)
    plan=make_plan(m,['2026年1月から1月まで','車両ごとに月別集計'],[])
    manager.memory.replace_plan(pid,plan['summary'],plan['tasks'])
    sig=plan_snapshot(manager,pid)[1]
    import_feedback(manager,pid,sig,'検証','原本の読取失敗を0円にしないでください。抽出器の動作を直してください。')
    manager.llm=Mock(spec=Ollama)
    async def local(*args):
        issue=issues_for(manager,pid,sig)[0]
        return json.dumps({'actions':[{'issue_id':issue['id'],'disposition':'amend','target':'vehicle_extract',
            'change':'説明に読取失敗を0円にしない確認手順を追記し、セル位置を記録する。',
            'reason':'指摘された読取失敗の扱いを説明文で具体化する。'}]})
    manager._local_complete=local
    row=await propose(manager,pid,sig)
    assert row['blockers'] and any(a['disposition']=='development' for a in row['actions'])
    assert any(a.get('lifecycle')=='development_pending' for a in row['actions'])
    with pytest.raises(ValueError,match='追加開発'):apply(manager,pid,sig,row['candidate_id'])
    with pytest.raises(ValueError,match='追加開発'):require_review(manager,pid)
    with pytest.raises(ValueError,match='追加開発'):await manager.start(pid)


@pytest.mark.asyncio
async def test_happy_path_import_propose_apply_review_start_and_resume(tmp_path):
    manager,pid,task=setup(tmp_path,True);sig=feedback(manager,pid)
    async def local(*args):
        data=answer(manager,pid,sig);data['actions'][0]['target']=task['task_key'];return json.dumps(data)
    manager._local_complete=local
    row=await propose(manager,pid,sig)
    out=apply(manager,pid,sig,row['candidate_id'])
    new_sig=out['signature']
    assert new_sig!=sig
    with pytest.raises(ValueError):require_review(manager,pid)
    async def runner(text,providers):
        return [{'id':x,'ok':True,'review':json.dumps({'verdict':'pass','issues':[]})} for x in providers]
    manager.plan_review_runner=runner
    await review_plan(manager,pid,new_sig,'目標と各工程を比較し、入力から出力まで不足がないか検証します。',True)
    require_review(manager,pid)
    from app.goal_review import execution_gate
    assert execution_gate(manager,pid)['blocked'] is False
    manager.memory.set_mission_status(pid,'ready','外部検証合格後の実行待ち','approved')
    started=await manager.start(pid)
    assert started['status'] in {'running','ready','paused','completed'}
    await manager.pause(pid)
    from app.goal_review import begin_job, ReviewStore as Store
    from app.goal_review_queue import recover_interrupted
    from app.workflow_readiness import build_readiness
    store=Store(manager.memory.path)
    job=begin_job(store,pid,'review_plan','crash-key',extra={'resume_from':'external_review'})
    recover_interrupted(store)
    ready=build_readiness(manager,pid)
    crashed=store.get(pid,'job',job['id'])
    assert crashed['status']=='needs_attention'
    assert ready.get('resume_from') or crashed.get('resume_from')


@pytest.mark.asyncio
async def test_human_approval_does_not_bypass_external_execution_gate(tmp_path):
    from app.vehicle_workflow import make_plan
    manager,pid,_=setup(tmp_path,True)
    manager.memory.save_mission(pid,'車両別の損益をExcelで作成する','2026年1月から7月の車両ごとの損益','',False,[])
    mission=manager.memory.get_mission(pid)
    plan=make_plan(mission,manager._effective_planning_criteria(mission),[])
    manager.memory.replace_plan(pid,plan['summary'],plan['tasks'])
    approved=manager.approve(pid)
    assert approved['status']=='ready' and approved['execution_gate']['blocked']
    with pytest.raises(ValueError,match='外部AI'):await manager.start(pid)
    assert not manager.workers
    sig=plan_snapshot(manager,pid)[1]
    ReviewStore(manager.memory.path).put(pid,'plan',sig,{'status':'passed'})
    from app.goal_review import execution_gate
    assert not execution_gate(manager,pid)['blocked']
    manager.memory.add_mission_instruction(pid,'追加の原本照合条件')
    assert execution_gate(manager,pid)['blocked']
