import json
import pytest
from app.memory.short_term import ShortTermMemory
from app.project_manager import ProjectOrchestrator
from app.workspace_files import WorkspaceSandbox
from app.structured_planning import compile_task,compile_plan,contract_of
from app.planning_rollout import complete_criteria,namespace_tasks,extension_for
from app.detail_store import DetailStore
from app.detail_service import view
from app.upgrade_runtime import UpgradeStop


def test_unnumbered_outcomes_and_full_goal_are_not_lost():
    mission={'goal':'運送業向けAIシステムを構築販売する','success_criteria':'補助金の提案書\n経営計画書'}
    legacy=['計画を詳細にしてください','現行公募要領をWeb検索して収集する']
    items=complete_criteria(mission,legacy)
    assert '補助金の提案書' in items and '経営計画書' in items
    assert any('運送業向けAI' in x for x in items)
    assert items[0]==legacy[1] and legacy[0] not in items


class Researcher:
    def __init__(self):self.calls=0
    async def collect(self,query):
        self.calls+=1
        return {'query':query,'errors':[],'sources':[{'url':'https://www.chusho.meti.go.jp/rule.html','title':'公式公募要領','retrieved_at':'2026-09-17T00:00:00+00:00','content':'公式の公募条件は対象者と対象経費を原本で確認する必要があります。'*30,'sha256':'b'*64,'official':True}]}


class NeverLlm:
    async def complete(self,*a):raise AssertionError('Dedicated executors must not invent evidence using LLM')


def setup(tmp_path):
    memory=ShortTermMemory(tmp_path/'memory.db');p=memory.create_project('full',workspace_path='projects/full');pid=p['id']
    memory.save_mission(pid,'調査と検証','','',False,[])
    web=compile_task(1,'現行公募要領をWeb検索して収集する',{'title':'公式資料の調査','scope':'公募条件の根拠を整理する','headings':['取得資料','確認事項'],'depends_on':[]},[])
    plan=compile_plan(['現行公募要領をWeb検索して収集する'],[web]);tasks=namespace_tasks(plan['tasks'],1)
    mission=memory.replace_plan(pid,plan['summary'],tasks,task_extensions={t['task_key']:extension_for(t) for t in tasks})
    (tmp_path/'capability_upgrade.json').write_text(json.dumps({'projects':{pid:'enforce'},'planning_projects':{pid:'two-stage-v1'}}))
    ws=WorkspaceSandbox(tmp_path/'workspace');researcher=Researcher()
    manager=ProjectOrchestrator(memory,NeverLlm(),lambda _:('',memory.list_context_files(pid,include_content=True)),None,lambda:[],workspace=ws,public_web_researcher=researcher)
    prep=mission['tasks'][0]
    for out in contract_of(prep)['outputs']:
        path=ws.resolve_file(p['workspace_path'],pid,out['path'])[2];path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text('\n\n'.join('## '+h+'\n\n'+'原本を照合し、未確認事項を整理する準備資料です。'*30 for h in out['required_headings']),encoding='utf-8')
    memory.update_task(prep['id'],'completed',result='準備資料を確認済み')
    return manager,pid,mission,researcher


@pytest.mark.asyncio
async def test_web_checkpoint_resume_and_backend_final(tmp_path):
    m,pid,mission,researcher=setup(tmp_path);web=mission['tasks'][1];final=mission['tasks'][-1]
    report=await m._execute_task(pid,web['id'])
    assert 'Web原本' in report and researcher.calls==1
    detail=view(m,pid,web['id']);assert all(s['state']=='completed' for s in detail['plan']['steps'])
    await m._execute_task(pid,web['id']);assert researcher.calls==1
    m.memory.update_task(web['id'],'completed',result=report)
    result=await m._execute_task(pid,final['id'])
    assert 'バックエンド検証' in result
    assert all(s['state']=='completed' for s in view(m,pid,final['id'])['plan']['steps'])
    assert 'PASS' in result


@pytest.mark.asyncio
async def test_final_rejects_incomplete_predecessor(tmp_path):
    m,pid,mission,_=setup(tmp_path)
    with pytest.raises(UpgradeStop,match='先行'):await m._execute_task(pid,mission['tasks'][-1]['id'])


def test_all_extensions_registered_with_new_plan_and_old_artifacts_preserved(tmp_path):
    m,pid,mission,_=setup(tmp_path)
    before=mission['tasks'];oldpath=contract_of(before[0])['outputs'][0]['path'];old=m.workspace.resolve_file('projects/full',pid,oldpath)[2].read_bytes()
    tasks=namespace_tasks(before,2)
    newer=m.memory.replace_plan(pid,'再作成',tasks,task_extensions={t['task_key']:extension_for(t) for t in tasks})
    assert newer['plan_version']==2
    assert all(t['status']=='pending' for t in newer['tasks'])
    store=DetailStore(m.memory.path)
    assert all(store.extension(pid,t,2)['execution_strategy']=='two-stage-v1' for t in newer['tasks'])
    assert m.workspace.resolve_file('projects/full',pid,oldpath)[2].read_bytes()==old
    assert all('/plan_2/' in contract_of(t)['outputs'][0]['path'] for t in newer['tasks'])
    with pytest.raises(ValueError):m.memory.replace_plan(pid,'invalid',tasks,task_extensions={})
    assert m.memory.get_mission(pid)['plan_version']==2


def test_namespaced_plan_validation_rejects_path_escape():
    from app.structured_planning import validate_plan
    task=compile_task(1,'資料作成',{'title':'資料','scope':'根拠を整理する','headings':['概要','根拠'],'depends_on':[]},[])
    tasks=namespace_tasks(compile_plan(['資料作成'],[task])['tasks'],8)
    assert validate_plan({'tasks':tasks},{'SC01'})['passed']
    c=contract_of(tasks[0]);c['outputs'][0]['path']='result/plan_8/../outside.md';tasks[0]['acceptance_criteria']=json.dumps(c)
    assert not validate_plan({'tasks':tasks},{'SC01'})['passed']

@pytest.mark.asyncio
async def test_project_wide_generation_keeps_outcomes_and_serial_dependencies(tmp_path):
    memory=ShortTermMemory(tmp_path/'memory.db');p=memory.create_project('経営計画');pid=p['id']
    memory.save_mission(pid,'運送業向けAIを構築する\n　点呼支援システム\n　年休管理システム','補助金で使える提案書\nサンプル運輸の経営計画書','',False,[])
    memory.add_mission_instruction(pid,'現行公募要領をWeb検索して収集する')
    (tmp_path/'capability_upgrade.json').write_text(json.dumps({'projects':{pid:'enforce'},'planning_projects':{pid:'two-stage-v1'}}))
    manager=ProjectOrchestrator(memory,NeverLlm(),lambda _:('',[]),None,lambda:[],workspace=WorkspaceSandbox(tmp_path/'workspace'))
    mission=await manager.generate_plan(pid)
    assert len(mission['tasks'])==6
    assert mission['tasks'][0]['task_key']=='SC01' and mission['tasks'][0]['depends_on']==[]
    assert mission['tasks'][1]['task_key']=='SC00' and mission['tasks'][1]['depends_on']==['SC01']
    assert '経営計画' in mission['tasks'][3]['title']
    assert '提案書' in mission['tasks'][4]['title']
    assert 'SC03' in mission['tasks'][4]['depends_on']
    assert any('財務' in h for h in contract_of(mission['tasks'][3])['outputs'][0]['required_headings'])
    assert all(DetailStore(memory.path).extension(pid,t,mission['plan_version']) for t in mission['tasks'])
    with pytest.raises(ValueError):memory.replace_plan(pid,'stale',mission['tasks'],expected_version=0)
    assert memory.get_mission(pid)['plan_version']==1
