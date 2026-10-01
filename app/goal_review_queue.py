"""Only resume explicitly approved, identical public review packets. No new send authority."""
import asyncio
import json
import time
from app.goal_review import ReviewStore,plan_snapshot,selected_detail,review_plan,finish_job,save_orchestration,approved_packet


def public_draft(snapshot):
    from app.structured_planning import contract_of
    tasks=snapshot.get('tasks',[])
    kinds={(contract_of(t) or {}).get('execution_kind') for t in tasks}
    if 'vehicle_calculate' in kinds:
        return ('車両ごとの月別損益Excelを登録原本から自動生成する計画です。売上から総支給給与、事業主社会保険、燃料、税込リース、対象その他費用を差し引きます。'
                '原本読取、明細正規化、車両・従業員・月の対応付け、重複防止、計算、Excel再計算と原本照合を行います。'
                '承認された不足0円方針があれば仮定ゼロを区別し、読取失敗や未配賦を確定合格にしません。'
                'TRIZ回復は原本に存在しない金額を作らず、許可された処理方法を比較します。未対応形式は開発課題とします。'
                '目標の網羅、工程の依存関係、検算の独立性、未実装能力、利用者への不要な転記負担を検証してください。')
    # Structural facts only: goal text, filenames and titles can contain private data.
    contracts=[contract_of(t) or {} for t in tasks]
    outputs=sum(len(c.get('outputs') or []) for c in contracts)
    dependencies=sum(len(t.get('depends_on') or []) for t in tasks)
    approvals=sum(1 for c in contracts if (c.get('approval_required') or c.get('action_requirements')) and not c.get('final_verification'))
    semantic_reviews=sum(1 for c in contracts if c.get('human_confirmation_required'))
    links=sum(len(c.get('criterion_ids') or []) for c in contracts)
    detail_steps=len((snapshot.get('detail') or {}).get('steps') or [])
    detail=f' 詳細計画は{detail_steps}工程です。' if detail_steps else ''
    return (f'登録済みの目標と達成条件を実行する汎用計画です。全体は{len(tasks)}工程、成果物契約は{outputs}件、'
            f'工程間依存は{dependencies}件、達成条件との対応は{links}件、外部操作の承認点は{approvals}工程、'
            f'人間による意味確認は{semantic_reviews}工程です。'
            f'{detail} 文書作成と実処理を区別し、公開・送信・顧客連絡・契約確定などの外部操作は人間承認後だけ実行します。'
            '各工程について、目標の網羅、入力と出力、依存関係、実行能力、検証可能な完了条件、承認境界、途中で目標から外れる危険を検証してください。'
            'この文章は構造情報だけから生成しており、秘密情報、個人情報、原本本文、ファイル名は含みません。必要なら送信前に公開可能な目的説明を追記してください。')


async def tick(manager):
    store=ReviewStore(manager.memory.path)
    with store.connect() as db:
        rows=[(r[0],r[1],json.loads(r[2])) for r in db.execute("SELECT project,signature,payload FROM reviews WHERE kind='plan_queue'")]
    for pid,signature,row in rows:
        if row.get('status')!='waiting_budget' or row.get('next_at',0)>time.time():continue
        try:
            mission=manager.memory.get_mission(pid)
            _,current=plan_snapshot(manager,pid,selected_detail(manager,pid,row.get('task_id')))
            if current!=signature or mission.get('external_providers')!=row['providers'] or not mission.get('allow_external_ai') or row['expires']<time.time():
                row['status']='stale';store.put(pid,'plan_queue',signature,row);continue
            if row.get('attempts',0)>=3:
                row['status']='needs_attention';store.put(pid,'plan_queue',signature,row);continue
            approval=store.get(pid,'send_approval',signature) or {}
            if not approval.get('approved') or (row.get('packet_hash') and approval.get('packet_hash')!=row.get('packet_hash')):
                row['status']='needs_attention';row['last_error']='send_approval_missing';store.put(pid,'plan_queue',signature,row);continue
            from app.goal_review import review_budget
            budget=review_budget(manager,pid)
            if budget.get('managed') and budget.get('remaining_calls',0)<len((store.get(pid,'plan',signature) or {}).get('pending_providers',row['providers'])):
                row['next_at']=budget.get('reset_at',time.time()+3600);store.put(pid,'plan_queue',signature,row);continue
            row['attempts']=row.get('attempts',0)+1;row['status']='running';store.put(pid,'plan_queue',signature,row)
            await review_plan(manager,pid,signature,row['public_summary'],True,row.get('task_id'))
        except Exception as exc:
            if (store.get(pid,'plan_queue',signature) or {}).get('status')=='cancelled':continue
            row.update(status='waiting_budget',next_at=time.time()+3600,last_error=type(exc).__name__)
            store.put(pid,'plan_queue',signature,row)


def recover_interrupted(store):
    # A crash during a request must not silently repeat a possibly completed paid call.
    with store.connect() as db:
        rows=list(db.execute("SELECT project,signature,payload FROM reviews WHERE kind='plan_queue'"))
    for pid,sig,payload in rows:
        row=json.loads(payload)
        if row.get('status')=='running':
            row['status']='needs_attention';store.put(pid,'plan_queue',sig,row)
    with store.connect() as db:
        plans=list(db.execute("SELECT project,signature,payload FROM reviews WHERE kind='plan'"))
    for pid,sig,payload in plans:
        row=json.loads(payload)
        if row.get('status')=='running':
            row['status']='needs_attention';row['error']='interrupted';store.put(pid,'plan',sig,row)
    with store.connect() as db:
        jobs=list(db.execute("SELECT project,signature,payload FROM reviews WHERE kind='job'"))
    for pid,job_id,payload in jobs:
        job=json.loads(payload)
        if job.get('status') in {'running','generating'}:
            finish_job(store,pid,job_id,'needs_attention',blocking_error='interrupted',resume_from='needs_attention')
            save_orchestration(store,pid,blocking_error='interrupted',resume_from='needs_attention')


async def loop(manager):
    recover_interrupted(ReviewStore(manager.memory.path))
    while True:
        try:await tick(manager)
        except Exception:pass
        await asyncio.sleep(30)
