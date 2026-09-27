"""Local-only APIs for reports, versioned procedures and cancellable reproduction jobs."""
import asyncio
import hashlib
import json
import time
import uuid
from pathlib import Path
from fastapi import APIRouter,HTTPException,UploadFile,File,Form
from fastapi.responses import FileResponse
from pydantic import BaseModel,Field
from app.agent_examples import ExampleStore,Conflict,digest
from app.vehicle_profit import build_workbook

class ImportBody(BaseModel):
    filename:str=Field(max_length=180)
    source:str=Field(max_length=5*1024*1024)
    agent:str=Field(default='',max_length=120)

class ChangeBody(BaseModel):
    revision:int
    values:dict=Field(default_factory=dict)

class ArchiveBody(BaseModel):
    reason:str=Field(min_length=3)
    actor:str=Field(min_length=1)


def install(app,env):
    router=APIRouter(prefix='/api/projects/{pid}/agent-examples')
    jobs=set()
    def store():return ExampleStore(env.DATA_DIR/'agent_examples')
    def require(pid):env.require_project(pid)
    def execute(fn):
        try:return fn()
        except Conflict as e:raise HTTPException(409,str(e))
        except KeyError:raise HTTPException(404,'対象がありません')
        except (ValueError,TypeError,IndexError) as e:raise HTTPException(422,str(e))
    def asset_dir(pid,eid):
        store().get(pid,eid)
        return env.DATA_DIR/'agent_examples'/'assets'/hashlib.sha256(pid.encode()).hexdigest()/eid
    def run_dir(pid,eid,rid):return asset_dir(pid,eid)/'runs'/rid
    def attach_job(coro):
        task=asyncio.create_task(coro);jobs.add(task);task.add_done_callback(jobs.discard)
    def update_latest(pid,eid,action,fn):
        for _ in range(8):
            item=store().get(pid,eid)
            try:return store().change(pid,eid,item['revision'],action,fn)
            except Conflict:continue
        raise Conflict('更新が競合しています')

    @router.get('')
    async def listing(pid:str,q:str='',include_archived:bool=False):
        require(pid)
        return execute(lambda:[i for i in store().list(pid,include_archived=include_archived) if not q or q.lower() in (i['filename']+json.dumps(i.get('extraction',{}),ensure_ascii=False)).lower()])

    @router.post('')
    async def importing(pid:str,body:ImportBody):
        require(pid)
        return execute(lambda:store().import_text(pid,body.filename,body.source,body.agent))

    @router.post('/{eid}/archive')
    async def archive(pid:str,eid:str,body:ArchiveBody):
        require(pid)
        if not body.reason.strip() or len(body.reason.strip()) < 3:
            raise HTTPException(422, '理由は3文字以上必要です')
        if not body.actor.strip():
            raise HTTPException(422, '実行者が必要です')
        return execute(lambda:store().archive(pid,eid,body.reason,body.actor))

    @router.get('/template')
    async def template(pid:str):
        require(pid)
        return {'schema':'vehicle-profit-input-v1','rounding':'yen_half_up','basis_note':'架空テスト。給与は非課税、売上は税抜。金額単位は円。',
            'vehicles':[{'id':'sample-A-0001','label':'架空車両A'},{'id':'sample-B-0001','label':'架空車両B'}],'months':['2026-08'],
            'records':[{'id':'sale-1','company':'架空会社','month':'2026-08','category':'revenue','tax_basis':'exclusive','quality':'actual','amount':100000,'source_ref':'synthetic-fixture','allocations':[{'vehicle_id':'sample-A-0001','ratio':1}]},
            {'id':'salary-1','company':'架空会社','month':'2026-08','category':'payroll','tax_basis':'non_taxable','quality':'actual','amount':40000,'source_ref':'synthetic-fixture','allocations':[{'vehicle_id':'sample-A-0001','ratio':0.5},{'vehicle_id':'sample-B-0001','ratio':0.5}]}]}

    @router.get('/{eid}')
    async def detail(pid:str,eid:str):
        require(pid);return execute(lambda:store().get(pid,eid))

    @router.post('/{eid}/parse')
    async def parse(pid:str,eid:str,body:ChangeBody):
        require(pid);return execute(lambda:store().parse(pid,eid,body.revision))

    @router.post('/{eid}/assets')
    async def upload(pid:str,eid:str,file:UploadFile=File(...),role:str=Form('normalized_input'),revision:int=Form(...)):
        require(pid)
        item=execute(lambda:store().get(pid,eid))
        suffix=Path(file.filename or '').suffix.lower()
        if suffix not in {'.json','.csv','.xlsx','.pdf','.md','.txt'}:raise HTTPException(422,'添付形式が未対応です')
        raw=await file.read(20*1024*1024+1)
        if len(raw)>20*1024*1024:raise HTTPException(413,'添付上限は20MiBです')
        if role=='normalized_input':
            if suffix!='.json':raise HTTPException(422,'再現入力には入力テンプレートのJSONを指定してください。元資料は別の役割で関連付けできます')
            from app.vehicle_profit import calculate
            execute(lambda:calculate(json.loads(raw.decode('utf-8-sig'))))
        aid=uuid.uuid4().hex;folder=asset_dir(pid,eid);folder.mkdir(parents=True,exist_ok=True)
        target=folder/(aid+suffix);target.write_bytes(raw)
        from app.context_files import extract_context_file
        extracted=await asyncio.to_thread(extract_context_file,file.filename or ('asset'+suffix),raw)
        preview_text=extracted.content[:60000]
        binding={'preview':preview_text,'extraction_note':extracted.note,'capability':('text_extracted' if preview_text.strip() else 'needs_capability'), 'id':aid,'suffix':suffix,'name':Path((file.filename or '').replace('\\','/')).name,'hash':hashlib.sha256(raw).hexdigest(),'size':len(raw),'kind':'private_attachment'}
        return execute(lambda:store().bind(pid,eid,revision,role,binding))

    @router.post('/{eid}/issues/{issue}')
    async def issue(pid:str,eid:str,issue:str,body:ChangeBody):
        require(pid);v=body.values
        return execute(lambda:store().resolve_issue(pid,eid,body.revision,issue,v.get('answer',''),v.get('evidence','')))

    @router.post('/{eid}/procedures')
    async def procedure(pid:str,eid:str,body:ChangeBody):
        require(pid);return execute(lambda:store().procedure(pid,eid,body.revision,body.values.get('kind','vehicle_profit_v1')))

    @router.post('/{eid}/procedures/{version}/review')
    async def review(pid:str,eid:str,version:int,body:ChangeBody):
        require(pid);v=body.values
        return execute(lambda:store().review(pid,eid,body.revision,version,v.get('hash'),v.get('decision'),v.get('reviewer',''),v.get('evidence','')))

    async def reproduce(pid,eid,rid,data):
        def cancelled():
            return next(r for r in store().get(pid,eid)['runs'] if r['id']==rid).get('cancel_requested',False)
        def set_run(result):
            def update(item):
                run=next(r for r in item['runs'] if r['id']==rid)
                run.update(result)
                if run.get('cancel_requested'):run['status']='cancelled'
                procedure=next(p for p in item['procedures'] if p['hash']==run['procedure_hash'])
                if procedure['status']=='revoked':run['status']='needs_review';run['error']='実行中に手順条件が変更されました'
            return update
        try:
            update_latest(pid,eid,'run_start',set_run({'status':'running'}))
            result=await asyncio.to_thread(build_workbook,data,run_dir(pid,eid,rid),cancelled)
            if result.get('file'):
                f=Path(result.pop('file'));result['artifact']=str(f.relative_to(run_dir(pid,eid,rid)));result['artifact_hash']=hashlib.sha256(f.read_bytes()).hexdigest()
            result['finished']=time.time()
            update_latest(pid,eid,'run_finish',set_run(result))
        except Exception as exc:
            update_latest(pid,eid,'run_error',set_run({'status':'failed','error':str(exc)[:1000],'finished':time.time()}))

    @router.post('/{eid}/reproductions',status_code=202)
    async def reproduction(pid:str,eid:str,body:ChangeBody):
        require(pid);item=execute(lambda:store().get(pid,eid));v=body.values
        p=next((p for p in item['procedures'] if p['hash']==v.get('hash')),None)
        if not p or p['status']=='revoked':raise HTTPException(409,'有効な手順版を選択してください')
        if p['spec']['kind']!='vehicle_profit_v1':raise HTTPException(422,'この手順は人間の資料確認が必要です')
        b=item['bindings'].get('normalized_input')
        if not b:raise HTTPException(422,'正規化入力JSONを関連付けしてください')
        raw=(asset_dir(pid,eid)/(b['id']+b['suffix'])).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=b['hash']:raise HTTPException(409,'入力版が変わっています')
        data=execute(lambda:json.loads(raw.decode('utf-8-sig')));rid=uuid.uuid4().hex
        def update(current):
            if any(r['status'] in {'pending','running'} for r in current['runs']):raise Conflict('この実行例は実行中です')
            current['runs'].append({'id':rid,'procedure_hash':p['hash'],'input_hash':b['hash'],'status':'pending','created':time.time(),'cancel_requested':False})
        execute(lambda:store().change(pid,eid,body.revision,'run_requested',update))
        folder=run_dir(pid,eid,rid);folder.mkdir(parents=True,exist_ok=True)
        (folder/'input.json').write_bytes(raw)
        attach_job(reproduce(pid,eid,rid,data))
        return {'job_id':rid,'status':'pending'}

    @router.post('/{eid}/reproductions/{rid}/cancel')
    async def cancel(pid:str,eid:str,rid:str):
        require(pid)
        def update(item):
            r=next((r for r in item['runs'] if r['id']==rid),None)
            if not r:raise KeyError(rid)
            if r['status'] in {'running','pending'}:r['cancel_requested']=True
        return execute(lambda:update_latest(pid,eid,'cancel',update))

    @router.get('/{eid}/reproductions/{rid}/download')
    async def download(pid:str,eid:str,rid:str):
        require(pid);item=execute(lambda:store().get(pid,eid));r=next((r for r in item['runs'] if r['id']==rid),None)
        if not r or not r.get('artifact'):raise HTTPException(404,'成果物がありません')
        root=run_dir(pid,eid,rid).resolve();path=(root/r['artifact']).resolve()
        if root not in path.parents or hashlib.sha256(path.read_bytes()).hexdigest()!=r['artifact_hash']:raise HTTPException(409,'成果物が変更されています')
        return FileResponse(path,filename='vehicle-profit-'+rid[:8]+'.xlsx')

    @router.post('/{eid}/procedures/{version}/plan-preview')
    async def preview(pid:str,eid:str,version:int,body:ChangeBody):
        require(pid);item=execute(lambda:store().get(pid,eid));p=next((p for p in item['procedures'] if p['version']==version),None)
        if item['revision']!=body.revision or not p or p['hash']!=body.values.get('hash') or p['status']!='reusable':raise HTTPException(409,'検証・採用済みの手順版が必要です')
        mission=env.memory.get_mission(pid)
        return {'plan_version':mission['plan_version'],'procedure_hash':p['hash'],'instruction':f"採用済み手順 {eid} v{version} を参考に、今回の原本・対応表・配分条件を確認して計画を作成する。再現機能: 実行例・再利用タブ。元案件の金額・料率・承認は引き継がない。",'will_start':False}

    @router.post('/{eid}/procedures/{version}/apply')
    async def apply(pid:str,eid:str,version:int,body:ChangeBody):
        result=await preview(pid,eid,version,body)
        if result['plan_version']!=body.values.get('plan_version'):raise HTTPException(409,'計画版が変わりました')
        # Add only a reference instruction: existing plan generation and approval remain required.
        execute(lambda:env.memory.add_mission_instruction(pid,result['instruction'],expected_plan_version=result['plan_version']))
        return {'status':'instruction_added','will_start':False,'message':'追加指示へ登録しました。計画再作成・確認後に承認してください'}

    @router.post('/{eid}/procedures/{version}/publish-experience')
    async def publish(pid:str,eid:str,version:int,body:ChangeBody):
        await preview(pid,eid,version,body)
        from app.experience_store import ExperienceStore
        es=ExperienceStore(env.DB_PATH.parent/'experience_memory'/'experience.sqlite3')
        content='車両別損益では一意な車両ID、年月、費目別税区分、推計根拠、配分率を明示する。入力合計＝配分済＋未配分を照合し、Excel再計算後に独立計算と比較する。欠測をゼロに置換しない。'
        rid=es.add(pid,'success',content,{}, {'example_id':eid,'procedure_version':version,'procedure_hash':body.values['hash']})
        # Candidate review still required by experience memory; source contains no personal data.
        es.review(pid,rid,'verified','procedure-review',f'手順 {eid} v{version} の再現検証・採用記録',time.time()+90*86400)
        from app.experience_memory import configured_memory
        setting=configured_memory(env.DB_PATH,pid)
        if setting:await asyncio.to_thread(setting[0].reindex,pid)
        return {'experience_id':rid,'status':'verified','source_report_shared':False}

    async def recover():
        s=store()
        with s.connect() as db:rows=db.execute('SELECT project,id FROM examples').fetchall()
        for row in rows:
            if not any(r['status'] in {'pending','running'} for r in s.get(row['project'],row['id'])['runs']):continue
            def update(item):
                for r in item['runs']:
                    if r['status'] in {'pending','running'}:r.update(status='failed',error='サービス再起動で中断。入力版を確認して再実行してください')
            update_latest(row['project'],row['id'],'recover',update)

    from contextlib import asynccontextmanager
    original_lifespan=app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(application):
        async with original_lifespan(application) as state:
            await recover()
            try:
                yield state
            finally:
                if jobs:
                    await asyncio.gather(*jobs,return_exceptions=True)
    app.router.lifespan_context=lifespan
    app.include_router(router)
