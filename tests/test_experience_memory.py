import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.experience_store import ExperienceStore,MemoryPolicyError
from app.experience_memory import ExperienceMemory,memory_scope,augment_local_prompt,ChromaIndex,CURRENT_MEMORY

BUDGET={'daily_calls':2,'daily_micro_usd':200000,'per_call_micro_usd':100000}

class Index:
    def __init__(self,ids):self.ids=ids
    def search(self,*args):return self.ids


def verified(store,project='p',kind='success',content='Use a deterministic cap calculation',applies=None):
    rid=store.add(project,kind,content,applies or {},{'test':'acceptance'})
    store.review(project,rid,'verified','operator','test passed',time.time()+3600)
    return rid


def test_candidates_expiry_revocation_and_scope(tmp_path):
    st=ExperienceStore(tmp_path/'db')
    rid=st.add('p','success','x',{}, {})
    assert not st.list('p',True)
    with pytest.raises(ValueError):st.review('other',rid,'verified','me','proof',time.time()+10)
    with pytest.raises(ValueError):st.review('p',rid,'verified','me','',time.time()+10)
    with pytest.raises(ValueError):st.review('p',rid,'verified','me','proof',float('nan'))
    st.review('p',rid,'verified','me','proof',time.time()+10)
    assert len(st.list('p',True))==1
    st.review('p',rid,'revoked','me','wrong',0)
    assert not st.list('p',True)


@pytest.mark.asyncio
async def test_retrieval_authority_and_shadow(tmp_path):
    m=ExperienceMemory(tmp_path,{})
    good=verified(m.store)
    other=verified(m.store,'other')
    stale=verified(m.store)
    m.store.review('p',stale,'revoked','operator','bad',0)
    mismatch=verified(m.store,applies={'input_version':'old'})
    raw=verified(m.store,kind='external')
    m.index=Index([other,stale,mismatch,raw,good])
    with memory_scope(m,'p','new',mode='enforce'):
        answer=await augment_local_prompt('calculate')
    assert good in answer
    assert all(x not in answer for x in [other,stale,mismatch,raw])
    with memory_scope(m,'p','new',mode='shadow'):
        assert await augment_local_prompt('calculate')=='calculate'
    assert CURRENT_MEMORY.get() is None


@pytest.mark.asyncio
async def test_cache_requires_review_and_full_request_identity(tmp_path):
    m=ExperienceMemory(tmp_path,{'budget':dict(BUDGET,daily_calls=10,daily_micro_usd=1000000)})
    calls=[]
    async def send():calls.append(1);return {'answer':'ok','usage':{'input_tokens':2}}
    with memory_scope(m,'p','v1',mode='enforce',external_allowed=True) as scope:
        await m.external(scope,'https://example.test',{'model':'m','input':'x'},send)
        rid=m.store.list('p')[0]['id']
        m.store.review('p',rid,'verified','tester','independent verification',time.time()+100)
        await m.external(scope,'https://example.test',{'model':'m','input':'x'},send)
        assert len(calls)==1
        await m.external(scope,'https://example.test',{'model':'m2','input':'x'},send)
        await m.external(scope,'https://example.test',{'model':'m','input':'y'},send)
    with memory_scope(m,'p','v2',mode='enforce',external_allowed=True) as scope:
        await m.external(scope,'https://example.test',{'model':'m','input':'x'},send)
    assert len(calls)==4
    with memory_scope(m,'p','v1',mode='enforce',external_allowed=False) as scope:
        await m.external(scope,'https://example.test',{'model':'m','input':'x'},send)
        with pytest.raises(MemoryPolicyError):await m.external(scope,'https://example.test',{'model':'m','input':'not-cached'},send)
    assert len(calls)==4


@pytest.mark.asyncio
async def test_unverified_and_expired_external_not_reused(tmp_path):
    m=ExperienceMemory(tmp_path,{'budget':dict(BUDGET,daily_calls=5,daily_micro_usd=500000)})
    calls=[]
    async def send():calls.append(1);return {'text':'x'}
    with memory_scope(m,'p','v',mode='enforce',external_allowed=True) as scope:
        await m.external(scope,'url',{},send)
        await m.external(scope,'url',{},send)
        row=m.store.list('p')[0]
        m.store.review('p',row['id'],'verified','t','proof',time.time()+60)
        with m.store.connect() as db:db.execute('UPDATE experiences SET expires=0 WHERE id=?',(row['id'],))
        await m.external(scope,'url',{},send)
    assert len(calls)==3


def test_budget_atomic_across_connections(tmp_path):
    path=tmp_path/'db';ExperienceStore(path)
    def reserve(i):
        try:return ExperienceStore(path).reserve('p',str(i),BUDGET)
        except MemoryPolicyError:return None
    with ThreadPoolExecutor(max_workers=8) as ex:
        rows=list(ex.map(reserve,range(8)))
    assert sum(x is not None for x in rows)==2


def test_duplicate_pending_and_budget_not_refunded(tmp_path):
    st=ExperienceStore(tmp_path/'db')
    rid=st.reserve('p','same',BUDGET)
    with pytest.raises(MemoryPolicyError):st.reserve('p','same',BUDGET)
    st.finish(rid,'unknown')
    st.reserve('p','new',BUDGET)
    with pytest.raises(MemoryPolicyError):st.reserve('p','third',BUDGET)


@pytest.mark.asyncio
async def test_timeout_keeps_budget(tmp_path):
    m=ExperienceMemory(tmp_path,{'budget':dict(BUDGET,daily_calls=1)})
    async def fail():raise TimeoutError('unknown remote outcome')
    with memory_scope(m,'p','v',external_allowed=True) as scope:
        with pytest.raises(TimeoutError):await m.external(scope,'url',{},fail)
        with pytest.raises(MemoryPolicyError):await m.external(scope,'url',{},fail)
    assert m.store.stats('p')['requests'][0]['state']=='unknown'


@pytest.mark.asyncio
async def test_provider_http_integration(tmp_path,monkeypatch):
    from app import external_ai
    calls=[]
    async def fake(url,**kwargs):calls.append(kwargs);return {'text':'answer'}
    monkeypatch.setattr(external_ai,'_post_json_uncached',fake)
    m=ExperienceMemory(tmp_path,{'budget':BUDGET})
    with memory_scope(m,'p','v',mode='enforce',external_allowed=True):
        await external_ai._post_json('url',headers={'Authorization':'secret'},payload={'model':'m'})
        row=m.store.list('p')[0]
        assert 'secret' not in json.dumps(row)
        m.store.review('p',row['id'],'verified','t','proof',time.time()+100)
        await external_ai._post_json('url',headers={'Authorization':'secret'},payload={'model':'m'})
    assert len(calls)==1


def test_local_vector_index_persistence_and_filter(tmp_path,monkeypatch):
    class LocalEmbedding:
        def embed_documents(self,texts):return [self.embed_query(t) for t in texts]
        def embed_query(self,t):return [float('cap' in t),float('quote' in t),0.1]
    config={'embedding_model':'deterministic-test-v1'}
    index=ChromaIndex(tmp_path,config,embeddings=LocalEmbedding())
    m=ExperienceMemory(tmp_path,config,index=index)
    rid=verified(m.store,content='cap calculation')
    other=verified(m.store,'other',content='cap calculation')
    m.reindex('p');m.reindex('other')
    assert index.search('p','cap',3)==[rid]
    reopened=ChromaIndex(tmp_path,config,embeddings=LocalEmbedding())
    assert reopened.search('p','cap',3)==[rid]
    m.store.review('p',rid,'revoked','me','changed',0)
    with memory_scope(m,'p','v') as scope:assert m.retrieve(scope,'cap')==[]

@pytest.mark.asyncio
async def test_local_orchestrator_receives_experiences_without_bypassing_llm(tmp_path):
    from app.project_manager import ProjectOrchestrator
    class LLM:
        def __init__(self):self.messages=[]
        async def complete_json(self,messages,schema):self.messages=messages;return '{"ok":true}'
    llm=LLM()
    manager=ProjectOrchestrator(None,llm,None,None,None)
    service=ExperienceMemory(tmp_path,{})
    rid=verified(service.store)
    service.index=Index([rid])
    with memory_scope(service,'p','v',mode='enforce'):
        result=await manager._local_complete('system','question',{'type':'object'})
    assert result=='{"ok":true}'
    assert rid in llm.messages[-1]['content']
    assert llm.messages[0]['content']=='system'


def test_capture_is_candidate_not_auto_verified(tmp_path):
    from types import SimpleNamespace
    from app.experience_memory import capture_attempt
    service=ExperienceMemory(tmp_path,{})
    attempt=SimpleNamespace(task={'title':'task'},aid='attempt-1')
    with memory_scope(service,'p','v'):
        capture_attempt(attempt,'completed',{'passed':True})
    assert service.store.list('p')[0]['status']=='candidate'
    assert not service.store.list('p',True)

@pytest.mark.asyncio
async def test_retrieval_failure_falls_back_locally(tmp_path):
    class Broken:
        def search(self,*args):raise RuntimeError('index unavailable')
    m=ExperienceMemory(tmp_path,{},index=Broken());verified(m.store)
    with memory_scope(m,'p','v',mode='enforce'):
        assert await augment_local_prompt('question')=='question'
    with m.store.connect() as db:
        assert db.execute('SELECT mode FROM retrievals').fetchone()[0]=='retrieval_error:RuntimeError'


@pytest.mark.asyncio
async def test_oversized_output_blocked_before_spend(tmp_path):
    m=ExperienceMemory(tmp_path,{'budget':BUDGET})
    async def send():raise AssertionError('must not send')
    with memory_scope(m,'p','v',external_allowed=True) as scope:
        with pytest.raises(MemoryPolicyError):await m.external(scope,'url',{'max_tokens':10000},send)
    assert m.store.stats('p')['requests']==[]


@pytest.mark.asyncio
async def test_import_success_cases_api_and_rag(tmp_path):
    from fastapi.testclient import TestClient
    import app.web as web_module

    db_path = tmp_path / 'memory' / 'conversations.db'
    from app.memory.short_term import ShortTermMemory
    mem = ShortTermMemory(db_path)
    p_off = mem.create_project('proj_off')['id']
    p_shadow = mem.create_project('proj_shadow')['id']
    p_enforce = mem.create_project('proj_enforce')['id']

    # Prepare config file for experience_memory using generated project ids
    config_data = {
        'projects': {
            p_off: 'off',
            p_shadow: 'shadow',
            p_enforce: 'enforce',
        },
        'embedding_model': 'dummy-model',
    }
    cfg_path = tmp_path / 'memory' / 'experience_memory.json'
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(config_data), encoding='utf-8')

    # Override web module constants/globals for testing
    orig_memory = web_module.memory
    orig_db_path = web_module.DB_PATH
    web_module.memory = mem
    web_module.DB_PATH = db_path

    client = TestClient(web_module.app)

    valid_item_1 = {
        'kind': 'success',
        'content': '【事例】架空の製造ライン効率化【状況】ライン停止が頻発していた。【施策】センサー点検を日次化し、異常値を検知した。【成果】停止時間が半減した。',
        'evidence': {'source': 'synthetic-test-1'},
    }
    valid_item_2 = {
        'kind': 'success',
        'content': '【事例】架空の在庫適正化【状況】過剰在庫が発生していた。【施策】発注リードタイムを見直した。【成果】在庫回転率が20%向上した。',
        'evidence': {'source': 'synthetic-test-2'},
    }
    invalid_item = {
        'kind': 'success',
        'content': '',  # empty content -> conversion error
        'evidence': {'source': 'synthetic-test-invalid'},
    }

    try:
        # (f) mode='off' のプロジェクトは EXPERIENCE_OFF で 409
        resp_off = client.post(
            f'/api/projects/{p_off}/experience/import-success-cases',
            json={
                'items': [valid_item_1],
                'actor': 'operator',
            },
        )
        assert resp_off.status_code == 409
        assert resp_off.json()['detail']['code'] == 'EXPERIENCE_OFF'

        # (g) mode='shadow' または 'enforce' なら登録され、registered 件数が返る
        resp_shadow = client.post(
            f'/api/projects/{p_shadow}/experience/import-success-cases',
            json={
                'items': [valid_item_1],
                'actor': 'operator',
            },
        )
        assert resp_shadow.status_code == 200
        data = resp_shadow.json()
        assert data['registered'] == 1
        assert data['skipped_duplicate'] == 0
        assert data['failed'] == []

        # (h) 同一内容 (ハッシュ一致) の再送は skipped_duplicate に数えられ二重登録しない
        resp_dup = client.post(
            f'/api/projects/{p_shadow}/experience/import-success-cases',
            json={
                'items': [valid_item_1],
                'actor': 'operator',
            },
        )
        assert resp_dup.status_code == 200
        data_dup = resp_dup.json()
        assert data_dup['registered'] == 0
        assert data_dup['skipped_duplicate'] == 1
        assert data_dup['failed'] == []

        # (i) 変換に失敗した項目は failed に理由付きで返り、他の項目には影響しない
        resp_mixed = client.post(
            f'/api/projects/{p_enforce}/experience/import-success-cases',
            json={
                'items': [valid_item_1, invalid_item, valid_item_2],
                'actor': 'operator',
            },
        )
        assert resp_mixed.status_code == 200
        data_mixed = resp_mixed.json()
        assert data_mixed['registered'] == 2
        assert data_mixed['skipped_duplicate'] == 0
        assert len(data_mixed['failed']) == 1
        assert data_mixed['failed'][0]['index'] == 1
        assert 'error' in data_mixed['failed'][0]

        # (j) 登録された行が既存の経験RAG参照系 (reindex や プロンプト補強) から見えること
        from app.experience_memory import configured_memory
        exp_mem, mode = configured_memory(db_path, p_enforce)
        verified_rows = exp_mem.store.list(p_enforce, verified_only=True)
        assert len(verified_rows) == 2
        exp_mem.index = Index([r['id'] for r in verified_rows])
        with memory_scope(exp_mem, p_enforce, 'v1', mode='enforce'):
            augmented = await augment_local_prompt('ライン停止の対策')
            assert '過去の経験' in augmented
            assert any(r['content'] in augmented for r in verified_rows)
    finally:
        web_module.memory = orig_memory
        web_module.DB_PATH = orig_db_path
