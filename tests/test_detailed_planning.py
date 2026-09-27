import asyncio
import json
from pathlib import Path
import pytest
from app.core import Ollama
from app.memory.short_term import ShortTermMemory
from app.project_manager import ProjectOrchestrator
from app.workspace_files import WorkspaceSandbox
from app.structured_planning import compile_task,compile_plan,contract_of
from app.upgrade_runtime import UpgradeStop,ReviewRequired
from app.upgrade_store import canonical,digest
from app.detail_store import DetailStore,DetailBudget
from app.detailed_planning import OPERATIONS,compile_detail,validate_detail,input_snapshot,episode_key
from app.detail_service import view,record_review,prepare_resume
from app.recovery_policy import CURRENT_ATTEMPT,RecoveryStopped


class Local(Ollama):
    def __init__(self):self.model='test-local';self.sent=[];self.fail_diagram=False;self.cancel_diagram=False
    async def complete_json(self,messages,schema):
        attempt=CURRENT_ATTEMPT.get();attempt.before_send({'model':self.model,'messages':messages})
        keys=list(schema['properties']);self.sent.append(keys)
        if keys[0]=='D01':return canonical({sid:title+'を対象資料と親の要件に照らして具体的に実施する' for sid,title,_ in OPERATIONS})
        if 'claims' in schema['properties'][keys[0]]['properties']:
            c={'kind':'hypothesis','text':'仮想の製造事業者が受付情報と設備記録を照合する構成を提案し、担当者が原本と照合して条件を整理する事例を検討する。',
               'evidence_ref':'','event_id':'','assumptions':'実在の採択実績ではなく架空の事業者を想定する','verification_method':'担当者が対象年度の原本と個別要件を照合する'}
            return canonical({k:{'claims':[c,c,c]} for k in keys})
        if self.cancel_diagram:raise asyncio.CancelledError()
        if self.fail_diagram:return canonical({k:{'diagram_nodes':['受付','検証'],'diagram_edges':[{'from':0,'to':99}]} for k in keys})
        return canonical({k:{'diagram_nodes':['受付','検証'],'diagram_edges':[{'from':0,'to':1}]} for k in keys})


def setup(tmp_path):
    memory=ShortTermMemory(tmp_path/'memory.db');project=memory.create_project('検証',workspace_path='projects/check');pid=project['id']
    memory.save_mission(pid,'事例の分析','','',False,[])
    text='担当者は資料の版を確認し、条件を整理してから事例を分析し、利用可能な根拠を明らかにします。'
    memory.add_context_file(pid,'source.md',text*15,len(text)*15)
    task=compile_task(1,'事例の分析',{'title':'事例分析','scope':'架空の製造業事例を分析する','headings':['概要','背景','事例','システム構成図','手順','検証'],'depends_on':[]},[])
    plan=compile_plan(['事例の分析'],[task]);mission=memory.replace_plan(pid,plan['summary'],plan['tasks']);task=next(t for t in mission['tasks'] if t['task_key']=='SC01')
    store=DetailStore(memory.path);ext={'schema':'local-cowork-document/v1','version':1,'document_type':'case_analysis','claim_format':'atomic-v1','execution_strategy':'two-stage-v1','required_capabilities':['source.read_excerpt','workspace.write_text']}
    store.set_extension(pid,task,mission['plan_version'],ext)
    (tmp_path/'capability_upgrade.json').write_text(json.dumps({'default':'enforce'}))
    local=Local()
    async def forbidden(*a,**kw):raise AssertionError('external forbidden')
    manager=ProjectOrchestrator(memory,local,lambda p:('',memory.list_context_files(pid,include_content=True)),forbidden,lambda:[],workspace=WorkspaceSandbox(tmp_path/'workspace'))
    for dep in mission['tasks']:
        if dep['task_key'] in task.get('depends_on',[]):
            for output in (contract_of(dep) or {}).get('outputs',[]):
                path=manager.workspace.resolve_file(project['workspace_path'],pid,output['path'])[2]
                path.parent.mkdir(parents=True,exist_ok=True);path.write_text('検証用の先行工程の成果です。対象資料を準備しました。',encoding='utf-8')
            memory.update_task(dep['id'],'completed',result='先行工程の準備完了')
    return manager,pid,task,store,ext


@pytest.mark.asyncio
async def test_sequential_review_resume_and_final_artifact(tmp_path):
    m,pid,t,store,ext=setup(tmp_path)
    assert not await m._run_one(pid,t,'test')
    assert next(x for x in m.memory.get_mission(pid)['tasks'] if x['id']==t['id'])['status']=='needs_review'
    info=view(m,pid,t['id']);plan=info['plan']
    assert [x['state'] for x in plan['steps']]==['completed']*5+['needs_review']
    assert len(m.llm.sent)==4
    before=[s['output_hash'] for s in plan['steps']]
    end=plan['steps'][-1]
    with pytest.raises(ValueError):prepare_resume(m,pid,t['id'])
    with pytest.raises(ValueError):record_review(m,pid,t['id'],plan['id'],'wrong','approved','確認',[True]*3)
    with pytest.raises(ValueError):record_review(m,pid,t['id'],plan['id'],end['output_hash'],'approved','確認',[False]*3)
    record_review(m,pid,t['id'],plan['id'],end['output_hash'],'approved','架空事例の意味、適用範囲、親要件を確認した',[True]*3)
    prepare_resume(m,pid,t['id'])
    # Legacy final artifact validation remains active; it must not trigger new generation.
    result=await m._run_one(pid,t,'test')
    assert len(m.llm.sent)==4
    assert [s['output_hash'] for s in view(m,pid,t['id'])['plan']['steps']]==before
    assert result, m.memory.get_mission(pid)['tasks']
    path=m.workspace.resolve_file(m.memory.get_project(pid)['workspace_path'],pid,contract_of(t)['outputs'][0]['path'])[2]
    assert path.exists() and 'flowchart LR' in path.read_text(encoding='utf-8')


@pytest.mark.asyncio
async def test_cancel_after_analysis_reuses_upstream(tmp_path):
    m,pid,t,store,_=setup(tmp_path);m.llm.cancel_diagram=True
    with pytest.raises(asyncio.CancelledError):await m._execute_task(pid,t['id'])
    plan=view(m,pid,t['id'])['plan'];hashes=[s['output_hash'] for s in plan['steps'][:3]]
    assert [s['state'] for s in plan['steps'][:3]]==['completed']*3
    m.llm.cancel_diagram=False
    with pytest.raises(ReviewRequired):await m._execute_task(pid,t['id'])
    latest=view(m,pid,t['id'])['plan']
    assert hashes==[s['output_hash'] for s in latest['steps'][:3]]
    assert sum(keys==['D01','D02','D03','D04','D05','D06'] for keys in m.llm.sent)==1
    assert len(m.llm.sent)==5


@pytest.mark.asyncio
async def test_invalid_diagram_does_not_repeat_analysis(tmp_path):
    m,pid,t,store,_=setup(tmp_path);m.llm.fail_diagram=True
    with pytest.raises(UpgradeStop):await m._execute_task(pid,t['id'])
    plan=view(m,pid,t['id'])['plan']
    assert [s['state'] for s in plan['steps']]==['completed']*3+['failed','blocked','blocked']
    count=len(m.llm.sent)
    with pytest.raises(UpgradeStop):await m._execute_task(pid,t['id'])
    assert len(m.llm.sent)==count


@pytest.mark.asyncio
async def test_changed_input_creates_revision_without_resetting_budget(tmp_path):
    m,pid,t,store,_=setup(tmp_path)
    with pytest.raises(ReviewRequired):await m._execute_task(pid,t['id'])
    before=view(m,pid,t['id']);used=before['budget']['execution']
    m.memory.add_context_file(pid,'extra.md','確認対象の年度と条件を整理する追加資料であり、適用条件は担当者が確認する必要があります。',60)
    assert view(m,pid,t['id'])['stale']
    with pytest.raises(ReviewRequired):await m._execute_task(pid,t['id'])
    after=view(m,pid,t['id'])
    assert after['plan']['revision']==2 and after['budget']['execution']>used and after['budget']['planning']==2


@pytest.mark.asyncio
async def test_tampered_artifact_rejects_review(tmp_path):
    m,pid,t,store,_=setup(tmp_path)
    await m._run_one(pid,t,'test')
    plan=view(m,pid,t['id'])['plan'];step=plan['steps'][3]
    p=m.workspace.resolve_file(m.memory.get_project(pid)['workspace_path'],pid,step['artifact_path'])[2];p.write_text('tampered')
    with pytest.raises(ValueError,match='中間成果'):record_review(m,pid,t['id'],plan['id'],plan['steps'][-1]['output_hash'],'approved','確認',[True]*3)
    assert not view(m,pid,t['id'])['plan']['steps'][3]['artifact_valid']
    with pytest.raises(ReviewRequired):await m._execute_task(pid,t['id'])
    after=view(m,pid,t['id'])['plan'];assert after['steps'][2]['output_hash']==plan['steps'][2]['output_hash']
    assert after['steps'][3]['artifact_path']!=step['artifact_path']


def test_parent_requirements_and_registered_operations(tmp_path):
    m,pid,t,store,ext=setup(tmp_path);mission=m.memory.get_mission(pid)
    snapshot,_=input_snapshot(m.memory,pid,t,mission,ext)
    plan=compile_detail(t,snapshot,{sid:title+'を原本と条件に照らして確認する' for sid,title,_ in OPERATIONS})
    plan['steps'][2]['requirements']=[]
    with pytest.raises(ValueError):validate_detail(plan,t)
    plan=compile_detail(t,snapshot,{sid:title+'を原本と条件に照らして確認する' for sid,title,_ in OPERATIONS})
    plan['steps'][0]['operation']='shell'
    with pytest.raises(ValueError):validate_detail(plan,t)


def test_budget_survives_revision_and_is_project_scoped(tmp_path):
    store=DetailStore(tmp_path/'db');b=DetailBudget(store,'p','e')
    b.consume('llm');b.consume('llm');b.close()
    c=DetailBudget(store,'p','e')
    with pytest.raises(RecoveryStopped):c.consume('llm')
    c.phase='execution';c.step='D03'
    for _ in range(4):c.consume('llm')
    with pytest.raises(RecoveryStopped):c.consume('llm')
    c.close()
    other=DetailBudget(store,'other','e');other.consume('llm');other.close()

@pytest.mark.asyncio
async def test_detail_api_project_scope_review_and_replan(tmp_path,monkeypatch):
    import app.web as web
    from fastapi import HTTPException
    m,pid,t,store,_=setup(tmp_path)
    monkeypatch.setattr(web,'memory',m.memory);monkeypatch.setattr(web,'orchestrator',m)
    await m._run_one(pid,t,'test')
    info=await web.get_detailed_task(pid,t['id']);plan=info['plan'];end=plan['steps'][-1]
    mission=await web.get_project_mission(pid)
    assert next(r for r in mission['task_routes'] if r['task_id']==t['id'])['route']=='detailed'
    other=m.memory.create_project('other')['id']
    with pytest.raises(HTTPException):await web.get_detailed_task(other,t['id'])
    payload=web.DetailedReviewPayload(plan_id=plan['id'],candidate_hash=end['output_hash'],decision='rejected',notes='根拠と要件の対応を修正してください')
    await web.review_detailed_task(pid,t['id'],payload)
    with pytest.raises(HTTPException):await web.resume_detailed_task(pid,t['id'],web.DetailedResumePayload())
    await web.resume_detailed_task(pid,t['id'],web.DetailedResumePayload(replan_reason='仮説の検証方法を具体化'))
    assert view(m,pid,t['id'])['plan']['payload']['replan_reason']
    before=view(m,pid,t['id'])['budget']['execution']
    with pytest.raises(ReviewRequired):await m._execute_task(pid,t['id'])
    after=view(m,pid,t['id'])
    assert after['plan']['revision']==2 and after['budget']['execution']>before
    assert not after['plan']['steps'][-1]['approved']


def test_active_time_survives_restart_but_not_review_wait(tmp_path):
    import time
    store=DetailStore(tmp_path/'db');b=DetailBudget(store,'p','e');b.close()
    data=b.data();data['seconds']=850;data['active_started']=time.time()-60
    with store.connect() as db:db.execute('UPDATE detailed_budgets SET payload=?',(canonical(data),))
    resumed=DetailBudget(store,'p','e')
    with pytest.raises(RecoveryStopped):resumed.consume('llm')
    resumed.close()


@pytest.mark.asyncio
async def test_changed_predecessor_artifact_invalidates_review(tmp_path):
    m,pid,t,store,_=setup(tmp_path)
    await m._run_one(pid,t,'test')
    before=view(m,pid,t['id']);plan=before['plan']
    dep=next(x for x in m.memory.get_mission(pid)['tasks'] if x['task_key'] in t['depends_on'])
    out=contract_of(dep)['outputs'][0]['path']
    path=m.workspace.resolve_file(m.memory.get_project(pid)['workspace_path'],pid,out)[2]
    path.write_text('先行成果を変更しました。',encoding='utf-8')
    assert view(m,pid,t['id'])['stale']
    with pytest.raises(ValueError,match='変更'):
        record_review(m,pid,t['id'],plan['id'],plan['steps'][-1]['output_hash'],'approved','確認',[True]*3)
