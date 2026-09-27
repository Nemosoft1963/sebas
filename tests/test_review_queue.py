import hashlib,json,sqlite3
from types import SimpleNamespace
from fastapi import FastAPI,HTTPException
from fastapi.testclient import TestClient
from app.agent_examples import ExampleStore
from app.procedure_learning import LearningStore
from app.review_queue import install,project_queue


def api(root):
 def require(pid):
  if pid not in {'p','q'}:raise HTTPException(404)
 app=FastAPI();install(app,SimpleNamespace(DATA_DIR=root,require_project=require));return TestClient(app)


def test_empty_read_does_not_create_storage(tmp_path):
 with api(tmp_path) as c:
  assert c.get('/api/projects/p/review-queue').json()['items']==[]
  assert not list(tmp_path.iterdir())
  assert c.get('/api/projects/unknown/review-queue').status_code==404
  assert c.get('/api/projects/p/review-queue?limit=51').status_code==422
  assert c.get('/api/projects/p/review-queue?offset=-1').status_code==422
  assert c.post('/api/projects/p/review-queue',json={'decision':'adopted'}).status_code==405


def test_project_boundary_no_private_content_or_writes(tmp_path):
 store=ExampleStore(tmp_path/'agent_examples');x=store.import_text('p','safe.md','private original');y=store.import_text('q','other.md','other private original')
 store.parse('p',x['id'],x['revision']);ls=LearningStore(tmp_path/'procedure_learning');item=ls.get('p',x['id'],x['hash'])
 def populate(i):
  i['semantics']=[{'status':'needs_review'},{'status':'reviewed'}]
  i['recipes']=[{'status':'draft'},{'status':'needs_revalidation'},{'status':'operational'}]
  i['general_inventions']=[{'candidates':[{'status':s,'text':'private candidate'} for s in ['artifact_checked','plan_only','trial_failed','adopted','revoked','needs_revalidation']]}]
 ls.change('p',x['id'],item['revision'],'fixture',populate)
 paths=list(tmp_path.rglob('*.sqlite3'));before={p:p.read_bytes() for p in paths}
 with api(tmp_path) as c:
  r=c.get('/api/projects/p/review-queue');assert r.status_code==200
  data=r.json();assert all(i['example_id']==x['id'] for i in data['items'])
  assert 'private' not in r.text and y['id'] not in r.text
  assert not any(i['title']=='抽出した工程' for i in data['items'])
  assert next(i for i in data['items'] if i['kind']=='blocked')['count']==2
  assert any(i['title']=='手順の再検証' for i in data['items'])
  assert c.get('/api/projects/q/review-queue').json()['items'][0]['example_id']==y['id']
 assert all(p.read_bytes()==before[p] for p in paths)
 # An example without learning does not create a learning row.
 assert len(ls.list('q'))==0


def test_pagination_and_resolved_source(tmp_path):
 s=ExampleStore(tmp_path/'agent_examples');a=s.import_text('p','one.md','one');b=s.import_text('p','two.md','two')
 a=s.parse('p',a['id'],a['revision'])
 for issue in a['issues']:a=s.resolve_issue('p',a['id'],a['revision'],issue['id'],'checked','proof')
 first=project_queue(tmp_path,'p',0,1);assert first['next_offset']==1 and first['items'][0]['example_id']==b['id']
 second=project_queue(tmp_path,'p',1,1);assert second['next_offset'] is None and second['items']==[]
 assert not (tmp_path/'procedure_learning').exists()


def test_corrupt_storage_is_error_not_empty_success(tmp_path):
 p=tmp_path/'agent_examples';p.mkdir();(p/'examples.sqlite3').write_bytes(b'not sqlite')
 with api(tmp_path) as c:
  r=c.get('/api/projects/p/review-queue');assert r.status_code==503
  assert str(tmp_path) not in r.text
