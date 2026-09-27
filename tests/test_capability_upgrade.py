import json
import sqlite3
import pytest
from app.capability_registry import CapabilityRegistry
from app.claim_evidence import verify_claim, gates
from app.document_contracts import validate_extension
from app.failure_taxonomy import classify
from app.recovery_policy import RecoveryBudget, RecoveryStopped
from app.source_retrieval import build_units, select_units, presentation_manifest, render_units, extract_pdf_pages
from app.upgrade_store import UpgradeStore
from app.upgrade_runtime import UpgradeStop


def source(pid='p', **kwargs):
    return dict(project_id=pid,id='s',source='upload',content='前置き。'*4000+'対象経費と補助率の説明。'*50, original_data=b'',**kwargs)


def test_tail_selection_and_actual_payload_manifest():
    _,units=build_units('p',source())
    selected=select_units(units,'対象経費 補助率')
    assert any(u['locator']['start']>10000 for u in selected)
    assert all(x['state']=='sent' for x in presentation_manifest(selected,[{'content':render_units(selected)}]))
    compact=render_units(selected)[:100]
    assert any(x['state']=='truncated' for x in presentation_manifest(selected,[{'content':compact}]))
    assert all(x['state']=='omitted' for x in presentation_manifest(selected,[]))


def test_capture_is_not_raw_pdf_and_memo_not_evidence():
    s=source();s.update(source='web',original_data=b'# capture')
    v,u=build_units('p',s)
    assert not v['original_available'] and not v['raw_pdf_available']
    with pytest.raises(ValueError,match='original_not_available'):extract_pdf_pages('p',s,[1])
    s['source']='memo';assert build_units('p',s)==(None,[])


def test_project_scoped_units_and_audit(tmp_path):
    db=tmp_path/'audit.db';store=UpgradeStore(db)
    v,u=build_units('p',source());store.save_source('p',v,u)
    assert store.read_units('p',v['version_id'],[u[-1]['unit_id']])['units']==[u[-1]]
    with pytest.raises(ValueError):store.read_units('other',v['version_id'],[u[-1]['unit_id']])
    with pytest.raises(ValueError):store.read_units('p',v['version_id'],['../../secret'])
    aid=store.begin('p',{'id':'t'},1,'shadow',{})
    with pytest.raises(sqlite3.IntegrityError):store.record('claims','other',aid,{})
    UpgradeStore(db)
    assert len(store.latest('p'))==1


def test_registry_states_are_not_model_claims():
    checks=CapabilityRegistry().check(['missing','source.extract_table','source.hash_original'],[])
    assert [c['state'] for c in checks]==['unknown','unavailable','unknown']
    assert checks[0]['reason_code']=='unregistered'


@pytest.mark.parametrize('status,category',[(401,'authentication_error'),(503,'connection_error')])
def test_http_failure_observations(status,category):
    assert category in [x['category'] for x in classify([{'http_status':status}],'PDFが読めない')]


def test_opposite_meaning_and_unknown_version_cannot_pass():
    text='この費用は対象外です。'*8
    _,units=build_units('p',dict(project_id='p',id='s',source='upload',content=text))
    c=dict(kind='fact',text='この費用は対象です。'*8,quote=text,unit_id=units[0]['unit_id'])
    v=verify_claim(c,units,allow_historical=True)
    assert v['citation_match']=='exact' and v['support_state']=='needs_review'
    c['text']=text;v=verify_claim(c,units)
    assert gates([v],True,True)['content_gate']=='needs_review'


def test_hypothesis_requires_assumptions_and_no_fake_execution():
    c=dict(kind='hypothesis',text='仮説として担当者が原本を確認する案を示す。'*4,assumptions='担当者を配置する',verification_method='原本照合')
    assert verify_claim(c,[])['support_state']=='supported'
    c['text']+='取得しました';assert verify_claim(c,[])['support_state']=='contradicted'
    c['kind']='actual_result';assert verify_claim(c,[])['action_evidence_state']=='missing'


@pytest.mark.parametrize('change',[{'version':2},{'schema':'bad'},{'document_type':'generic'},{'allow_historical':'yes'},{'as_of':'bad'},{'permissions':['shell']}])
def test_new_contract_versions_fail_closed(change):
    ext=dict(schema='local-cowork-document/v1',version=1,document_type='case_analysis');ext.update(change)
    with pytest.raises(ValueError):validate_extension(ext)


def test_recovery_limits_and_same_failure():
    b=RecoveryBudget()
    for _ in range(3):b.consume('llm')
    with pytest.raises(RecoveryStopped):b.consume('llm')
    for _ in range(6):b.consume('tool')
    with pytest.raises(RecoveryStopped):b.consume('tool')
    b.recover('generation_defect','source1','regenerate')
    with pytest.raises(RecoveryStopped):b.recover('generation_defect','source1','regenerate')
    with pytest.raises(RecoveryStopped):RecoveryBudget().recover('authentication_error',{},'retry')


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['off','shadow','enforce'])
async def test_extension_never_falls_back_and_review_preserves_file(tmp_path,mode):
    from app.core import Ollama
    from app.memory.short_term import ShortTermMemory
    from app.project_manager import ProjectOrchestrator
    from app.structured_planning import compile_task,compile_plan,contract_of
    from app.workspace_files import WorkspaceSandbox
    from app.recovery_policy import CURRENT_ATTEMPT
    memory=ShortTermMemory(tmp_path/'memory.db')
    project=memory.create_project('検証',workspace_path='projects/check');pid=project['id']
    memory.save_mission(pid,'事例の分析','','',False,[])
    task=compile_task(1,'事例の分析',dict(title='事例分析',scope='事例を分析する',headings=['概要','構成','事例','手順','担当','確認'],depends_on=[]),[])
    plan=compile_plan(['事例の分析'],[task]);mission=memory.replace_plan(pid,plan['summary'],plan['tasks'])
    task=next(t for t in mission['tasks'] if t['task_key']=='SC01')
    store=UpgradeStore(memory.path)
    store.set_extension(pid,task,mission['plan_version'],dict(schema='local-cowork-document/v1',version=1,document_type='case_analysis',required_capabilities=['workspace.write_text']))
    (tmp_path/'capability_upgrade.json').write_text(json.dumps({'default':mode}))
    workspace=WorkspaceSandbox(tmp_path/'workspace')
    class Local(Ollama):
        def __init__(self): self.calls=0;self.model='local'
        async def complete_json(self,messages,schema):
            self.calls+=1
            CURRENT_ATTEMPT.get().before_send({'model':'local','messages':messages})
            return json.dumps({k:dict(kind='hypothesis',text='仮説として担当者が原本を確認して条件を整理する案を示す。'*6,unit_id='',quote='',assumptions='担当者を配置する',verification_method='原本照合を行う') for k in schema['properties']},ensure_ascii=False)
    local=Local()
    async def no_external(*a,**kw):raise AssertionError('external forbidden')
    manager=ProjectOrchestrator(memory,local,lambda p:('',[]),no_external,lambda:[],workspace=workspace)
    async def no_legacy(*a,**kw):raise AssertionError('fallback forbidden')
    manager._execute_legacy_task=no_legacy
    _,_,output=workspace.resolve_file(project['workspace_path'],pid,contract_of(task)['outputs'][0]['path'])
    output.parent.mkdir(parents=True,exist_ok=True);output.write_text('existing artifact',encoding='utf-8')
    with pytest.raises(UpgradeStop):await manager._execute_task(pid,task['id'])
    assert output.read_text(encoding='utf-8')=='existing artifact'
    count=len(contract_of(task)['outputs'][0]['required_headings']);size=max(2,(count+2)//3)
    assert local.calls==((count+size-1)//size if mode=='enforce' else 0)
    if mode=='enforce':assert store.latest(pid)[0]['state']=='needs_review'

@pytest.mark.asyncio
async def test_actual_http_retries_record_compacted_payload_and_shared_budget(monkeypatch):
    import httpx
    from app.core import Ollama
    from app.recovery_policy import CURRENT_ATTEMPT
    original_client=httpx.AsyncClient
    sent=[]
    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200,json={'message':{'content':''},'done_reason':'length'})
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original_client(transport=httpx.MockTransport(handler),**kw))
    class Probe:
        mode='enforce'
        budget=RecoveryBudget()
        payloads=[]
        def before_send(self,payload):
            self.budget.consume('llm');self.payloads.append(payload);return str(len(self.payloads))
        def response(self,*a,**kw):pass
    probe=Probe();llm=Ollama('http://localhost','test');llm._capabilities_model='test'
    token=CURRENT_ATTEMPT.set(probe)
    try:
        with pytest.raises(RuntimeError,match='no complete structured'):
            await llm.complete_json([{'role':'user','content':'a'*40000}],{'type':'object'})
        assert len(sent)==2 and len(probe.payloads)==2
        assert len(sent[1]['messages'][0]['content'])<len(sent[0]['messages'][0]['content'])
        with pytest.raises(RecoveryStopped):
            await llm.complete_json([{'role':'user','content':'b'}],{'type':'object'})
        assert len(sent)==3
    finally:CURRENT_ATTEMPT.reset(token)

@pytest.mark.parametrize('kind',['case_analysis','web_evidence_report','system_structure_proposal'])
def test_requirement_review_never_overrides_failed_claim(kind):
    from app.claim_evidence import document_gates
    v={'support_state':'contradicted','citation_match':'mismatch','version_state':'unknown'}
    state=document_gates([v],'## 内容\n本文',{'required_headings':['内容'],'minimum_characters':1},kind)
    assert state['content_gate']=='failed'


def test_diagram_prose_does_not_satisfy_diagram_requirement():
    from app.claim_evidence import document_gates
    v={'support_state':'supported','citation_match':'absent','version_state':'not_applicable'}
    out={'required_headings':['システム構成図'],'minimum_characters':1}
    prose='## システム構成図\n図を設計する予定です。'
    assert document_gates([v],prose,out,'case_analysis')['format_gate']=='failed'
    diagram=prose+'\n```mermaid\nflowchart LR\nA[受付] --> B[確認]\n```'
    state=document_gates([v],diagram,out,'case_analysis')
    assert state['format_gate']=='passed' and state['content_gate']=='needs_review'


def test_untagged_visible_text_is_not_confirmed_omission(tmp_path):
    from app.upgrade_runtime import Attempt
    _,units=build_units('p',source())
    a=Attempt.__new__(Attempt);a.guard=lambda:None;a.mode='shadow';a.calls=0;a.observations=[];a.selected=units[:1]
    a.store=UpgradeStore(tmp_path/'audit.db');a.pid='p';a.aid=a.store.begin('p',{'id':'t'},1,'shadow',{})
    a.before_send({'messages':[{'content':units[0]['content']}]})
    assert not a.observations
    assert a.store.records('presentation_manifests','p',a.aid)[0]['units'][0]['state']=='visibility_unknown'
    a.before_send({'messages':[{'content':'different text'}]})
    assert a.observations[0]['evidence_refs']


def test_timeout_diagnosis_uses_exception_not_empty_model_report(tmp_path):
    import httpx
    from types import SimpleNamespace
    from app.upgrade_runtime import Attempt
    a=Attempt.__new__(Attempt);a.observations=[];a.pid='p';a.task={'id':'t'}
    a.store=UpgradeStore(tmp_path/'audit.db');a.aid=a.store.begin('p',a.task,1,'enforce',{})
    a.manager=SimpleNamespace(memory=SimpleNamespace(add_event=lambda *args,**kwargs:None))
    failures=a.record_failure(httpx.ReadTimeout(''))
    assert failures[0]['category']=='connection_error'
    assert failures[0]['reason_code']=='request_timeout'



def test_topic_sources_rank_above_generic_system_manual():
    units=[]
    for sid,text in [('a','構成 手順 システム '*100),('z','ものづくり補助金 公募要領 '*40)]:
        _,part=build_units('p',dict(project_id='p',id=sid,source='upload',content=text));units+=part
    selected=select_units(units,'ものづくり補助金 公募要領 構成 手順 システム',max_chars=1400)
    assert selected[0]['source_doc_id']=='z'


def test_backend_diagram_and_invalid_connection():
    from app.document_contracts import render_diagram,generation_schema
    schema=generation_schema(['概要','システム構成図'])
    assert 'diagram_nodes' not in schema['properties']['section_1']['required']
    assert 'diagram_nodes' in schema['properties']['section_2']['required']
    claim={'diagram_nodes':['入力','確認'], 'diagram_edges':[{'from':0,'to':1}]}
    assert 'N0 --> N1' in render_diagram(claim)
    claim['diagram_edges'][0]['to']=99
    with pytest.raises(ValueError):render_diagram(claim)


def test_conditional_adoption_is_not_a_completed_result():
    c=dict(kind='hypothesis',text='架空事例の提案として、審査で採択されれば実施へ進む計画を示す。'*3,assumptions='仮定の事例',verification_method='原本要件と照合する')
    assert verify_claim(c,[])['support_state']=='supported'
    c['kind']='plan';c['text']+='原本を保存しました。'
    assert verify_claim(c,[])['support_state']=='contradicted'

@pytest.mark.parametrize('mode,extension,expected', [('off',False,'legacy'),('shadow',False,'legacy'),('enforce',False,'legacy'),('shadow',True,'blocked'),('enforce',True,'document')])
def test_route_status_distinguishes_config_and_contract(tmp_path, mode, extension, expected):
    from app.upgrade_runtime import task_route_status
    from types import SimpleNamespace
    (tmp_path/'capability_upgrade.json').write_text(json.dumps({'default':mode}))
    ext={'schema':'local-cowork-document/v1','version':1,'document_type':'case_analysis'}
    store=SimpleNamespace(extension=lambda *args:ext if extension else None)
    result=task_route_status(store,tmp_path/'db','p',{'id':'t'},1)
    assert result['route']==expected
    assert result['mode']==mode


def test_route_status_stale_contract_is_blocked(tmp_path):
    from app.upgrade_runtime import task_route_status
    from types import SimpleNamespace
    def stale(*args): raise ValueError('unsupported_contract')
    result=task_route_status(SimpleNamespace(extension=stale),tmp_path/'db','p',{'id':'t'},1)
    assert result['route']=='blocked' and result['reason']=='unsupported_contract'


def test_persistent_budget_survives_attempt_and_connection(tmp_path):
    from app.recovery_policy import PersistentRecoveryBudget
    path=tmp_path/'audit.db'
    first=PersistentRecoveryBudget(UpgradeStore(path),'p','same-input')
    first.consume('llm');first.consume('llm')
    second=PersistentRecoveryBudget(UpgradeStore(path),'p','same-input')
    second.consume('llm')
    with pytest.raises(RecoveryStopped):first.consume('llm')
    with pytest.raises(RecoveryStopped):second.consume('llm')
    PersistentRecoveryBudget(UpgradeStore(path),'other','same-input').consume('llm')
    PersistentRecoveryBudget(UpgradeStore(path),'p','changed-input').consume('llm')


def test_backend_quote_binding_and_scope():
    from app.atomic_documents import evidence_catalog,bind_claim
    text='原本に記載された条件を担当者が確認し、適用できるかどうか判断する必要があります。'
    _,units=build_units('p',dict(project_id='p',id='s',source='upload',content=text))
    catalog=evidence_catalog(units,'p')
    raw=dict(kind='fact',text='',evidence_ref='E1')
    claim,evidence=bind_claim(raw,catalog,{units[0]['unit_id']})
    assert claim['text']==text and claim['quote']==text
    assert evidence['end']-evidence['start']==len(text)
    with pytest.raises(ValueError):bind_claim(raw,catalog,set())
    with pytest.raises(ValueError):bind_claim({**raw,'text':'invented'},catalog,{units[0]['unit_id']})
    with pytest.raises(ValueError):evidence_catalog(units,'other')


def test_task_scoped_mode(tmp_path):
    from app.upgrade_runtime import configured_mode
    (tmp_path/'capability_upgrade.json').write_text(json.dumps({'default':'off','projects':{'p':'shadow'},'tasks':{'p':{'t':'enforce'}}}))
    assert configured_mode(tmp_path/'db','p','t')=='enforce'
    assert configured_mode(tmp_path/'db','p','other')=='shadow'

@pytest.mark.asyncio
async def test_atomic_partial_repair_and_shared_retry_budget(tmp_path):
    from app.core import Ollama
    from app.memory.short_term import ShortTermMemory
    from app.project_manager import ProjectOrchestrator
    from app.structured_planning import compile_task,compile_plan
    from app.workspace_files import WorkspaceSandbox
    from app.recovery_policy import CURRENT_ATTEMPT
    memory=ShortTermMemory(tmp_path/'memory.db')
    project=memory.create_project('検証',workspace_path='projects/check');pid=project['id']
    memory.save_mission(pid,'事例の分析','','',False,[])
    task=compile_task(1,'事例の分析',dict(title='事例分析',scope='事例を分析する',headings=['概要','構成','事例','手順','担当','確認'],depends_on=[]),[])
    plan=compile_plan(['事例の分析'],[task]);mission=memory.replace_plan(pid,plan['summary'],plan['tasks'])
    task=next(t for t in mission['tasks'] if t['task_key']=='SC01')
    store=UpgradeStore(memory.path)
    store.set_extension(pid,task,mission['plan_version'],dict(schema='local-cowork-document/v1',version=1,document_type='case_analysis',claim_format='atomic-v1',required_capabilities=['workspace.write_text']))
    (tmp_path/'capability_upgrade.json').write_text(json.dumps({'default':'enforce'}))
    class Local(Ollama):
        def __init__(self):self.batches=[];self.model='local'
        async def complete_json(self,messages,schema):
            CURRENT_ATTEMPT.get().before_send({'model':'local','messages':messages})
            self.batches.append(list(schema['properties']))
            claim=dict(kind='hypothesis',text='仮説として担当者が原本を確認して適用条件と例外を整理する業務を担当する案を示す。',evidence_ref='',event_id='',assumptions='担当者を配置する',verification_method='原本照合')
            data={k:{'claims':[dict(claim),dict(claim)]} for k in schema['properties']}
            if len(self.batches)==1:data[next(iter(data))]['claims'][0]['text']='bad'
            return json.dumps(data,ensure_ascii=False)
    local=Local()
    async def no_external(*a,**kw):raise AssertionError('external forbidden')
    manager=ProjectOrchestrator(memory,local,lambda p:('',[]),no_external,lambda:[],workspace=WorkspaceSandbox(tmp_path/'workspace'))
    with pytest.raises(UpgradeStop,match='確認待ち'):await manager._execute_task(pid,task['id'])
    assert len(local.batches)==3 and local.batches[-1]==['section_1']
    aid=store.latest(pid)[0]['attempt_id']
    records=store.records('claims',pid,aid)
    calls={r['inference_call_id'] for r in store.records('presentation_manifests',pid,aid)}
    assert records and all(r['inference_call_id'] in calls for r in records)
    with pytest.raises(UpgradeStop,match='共有呼出し予算'):await manager._execute_task(pid,task['id'])
    assert len(local.batches)==3
