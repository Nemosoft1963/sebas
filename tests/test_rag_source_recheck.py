import hashlib,json
from fastapi.testclient import TestClient
from app.memory.short_term import ShortTermMemory
from app.experience_memory import ExperienceMemory, configured_memory, current_index_identity
import app.web as web

def test_candidate_requires_registered_matching_original(tmp_path, monkeypatch):
    db=tmp_path/'memory'/'conversations.db'
    mem=ShortTermMemory(db)
    pid=mem.create_project('source-check')['id']
    (db.parent/'experience_memory.json').write_text(json.dumps({'projects':{pid:'enforce'},'embedding_model':'test-model'}),encoding='utf-8')
    monkeypatch.setattr(web,'memory',mem)
    monkeypatch.setattr(web,'DB_PATH',db)
    def stub(self,project,rid):
        identity=current_index_identity(self.config)
        self.store.set_index_state(project,rid,'indexed','',identity)
        return {'id':rid,'indexed':True}
    monkeypatch.setattr(ExperienceMemory,'reindex_verified_case',stub)
    raw=b'registered original bytes'
    h=hashlib.sha256(raw).hexdigest()
    item={'kind':'success','content':'【事例】点検手順の検証【状況】停止が頻発【施策】日次点検を行う【成果】停止時間が半減',
          'applicability':{'input_version':'v1'},
          'evidence':{'source_ref':'source.md','source_hash':h,'fetched_at':'2026-10-03T00:00:00Z',
                      'extraction_method':'manual','prohibitions':[]}}
    client=TestClient(web.app)
    assert client.post(f'/api/projects/{pid}/experience/import-success-cases',json={'items':[item],'actor':'collector'}).status_code==200
    service,_=configured_memory(db,pid)
    rid=service.store.list_candidates(pid)[0]['id']
    url=f'/api/projects/{pid}/experience/candidates/{rid}/approve'
    review={'reviewer':'human','reason':'原本照合'}
    assert client.get(f'/api/projects/{pid}/experience/candidates').json()['candidates'][0]['approvable'] is False
    assert client.post(url,json=review).status_code==409
    mem.add_context_file(pid,'source.md','wrong',5,data=b'wrong')
    assert client.post(url,json=review).status_code==409
    item_id=mem.list_context_files(pid)[0]['id']
    with mem._connect() as conn:
        conn.execute('UPDATE project_context_files SET original_data=?,content=? WHERE id=?',(raw,raw.decode(),item_id))
    assert client.get(f'/api/projects/{pid}/experience/candidates').json()['candidates'][0]['approvable'] is True
    response=client.post(url,json=review)
    assert response.status_code==200,response.text
    assert service.store.get(pid,rid)['status']=='verified'
