"""Task-scoped read/review/resume operations; no implicit execution or approval."""
import json
from types import SimpleNamespace
from app.detail_store import DetailStore
from app.detailed_planning import input_snapshot,episode_key
from app.step_executor import checkpoint_valid
from app.upgrade_store import canonical


def context(manager,pid,tid):
    mission=manager.memory.get_mission(pid)
    task=next((t for t in mission['tasks'] if t['id']==tid),None)
    if task is None:raise ValueError('このプロジェクトにタスクがありません')
    store=DetailStore(manager.memory.path)
    extension=store.extension(pid,task,mission['plan_version'])
    if not extension or extension.get('execution_strategy')!='two-stage-v1':raise ValueError('2段階計画の対象タスクではありません')
    return mission,task,store,extension


def view(manager,pid,tid):
    mission,task,store,ext=context(manager,pid,tid)
    plan=store.latest_plan(pid,tid,episode_key(task,mission))
    if not plan:return {'task_id':tid,'plan':None,'message':'実行直前に詳細計画を作成します'}
    signature=input_snapshot(manager.memory,pid,task,mission,ext,manager.workspace,manager.memory.get_project(pid))[1]
    probe=SimpleNamespace(project=manager.memory.get_project(pid),pid=pid)
    for step in plan['steps']:
        step['artifact_valid']=checkpoint_valid(manager,probe,step)
        if step['step_id']=='D06':step['approved']=store.approved(pid,plan['id'],step['output_hash']) if step['output_hash'] else False
    with store.connect() as db:
        row=db.execute('SELECT payload FROM detailed_budgets WHERE project_id=? AND episode=?',(pid,plan['episode'])).fetchone()
    return {'task_id':tid,'plan':plan,'stale':signature!=plan['signature'],'budget':json.loads(row[0]) if row else {}}


def idle(manager,pid,mission):
    worker=manager.workers.get(pid)
    if mission['status']=='running' or any(t['status']=='running' for t in mission['tasks']) or (worker and not worker.done()):raise ValueError('実行中は確認結果や計画を変更できません')


def record_review(manager,pid,tid,plan_id,candidate_hash,decision,notes,checks):
    mission,task,store,_=context(manager,pid,tid);idle(manager,pid,mission)
    current=view(manager,pid,tid);plan=current['plan']
    if not plan or plan['id']!=plan_id or current['stale'] or plan['payload'].get('replan_reason'):raise ValueError('計画・入力が変更されています。再実行後に確認してください')
    if task['status']!='needs_review':raise ValueError('タスクは内容確認待ちではありません')
    if any(not s['artifact_valid'] for s in plan['steps']):raise ValueError('中間成果が変更されています。再実行してください')
    if decision=='approved' and (len(checks)!=3 or not all(checks)):raise ValueError('意味・適用版・要求充足の3項目を確認してください')
    store.review(pid,plan_id,candidate_hash,decision,notes)
    manager.memory.add_event(pid,'detail_human_review','詳細成果の確認結果を記録しました',tid,canonical({'plan_id':plan_id,'candidate_hash':candidate_hash,'decision':decision,'notes':notes,'checks':checks}))
    return view(manager,pid,tid)


def prepare_resume(manager,pid,tid,replan_reason=''):
    mission,task,store,_=context(manager,pid,tid);idle(manager,pid,mission)
    if task['status']=='completed':raise ValueError('完了タスクをこの操作で再開できません')
    current=view(manager,pid,tid);plan=current['plan']
    if replan_reason:
        if not plan or plan['revision']>=2:raise ValueError('詳細計画の再生成上限です')
        payload=plan['payload'];payload['replan_reason']=replan_reason[:2000]
        with store.connect() as db:db.execute('UPDATE detailed_plans SET payload=? WHERE id=? AND project_id=?',(canonical(payload),plan['id'],pid))
    elif plan and not current['stale'] and task['status']=='needs_review':
        end=next(s for s in plan['steps'] if s['step_id']=='D06')
        if all(s['artifact_valid'] for s in plan['steps']) and not end['approved']:raise ValueError('確認結果の登録、または理由を付けた再計画が必要です')
    manager.memory.update_task(tid,'pending')
    dependents={task['task_key']}
    for t in mission['tasks']:
        if set(t.get('depends_on',[])) & dependents:
            dependents.add(t['task_key'])
            if t['status']=='blocked':manager.memory.update_task(t['id'],'pending')
    manager.memory.set_mission_status(pid,'paused','保存済み詳細計画から再開できます','detail_resume_ready')
    return manager.memory.get_mission(pid)
