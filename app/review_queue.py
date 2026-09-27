"""Read-only projection. Never creates stores or authorizes adoption."""
import json
import sqlite3
from pathlib import Path
from fastapi import HTTPException, Query


def read_rows(path, sql, args):
    if not path.exists():
        return []
    # mode=ro prevents reads from silently creating a database or schema.
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=3) as db:
        return db.execute(sql, args).fetchall()


def project_queue(root, project, offset=0, limit=30):
    root = Path(root)
    examples = read_rows(root/'agent_examples/examples.sqlite3',
        'SELECT id,data FROM examples WHERE project=? ORDER BY rowid DESC LIMIT ? OFFSET ?',
        (project, limit+1, offset))
    items = []
    for eid, raw in examples[:limit]:
        original = json.loads(raw)
        rows = read_rows(root/'procedure_learning/learning.sqlite3',
            'SELECT data FROM learning WHERE project=? AND example=?', (project, eid))
        learning = json.loads(rows[0][0]) if rows else {}
        def add(kind, title, count, reason):
            if count:
                items.append({'example_id':eid, 'filename':original.get('filename',''),
                    'kind':kind, 'title':title, 'count':count, 'reason':reason,
                    'example_revision':original['revision'],
                    'learning_revision':learning.get('revision')})
        if not original.get('extraction'):
            add('prepare','原文の整理',1,'取り込んだ作業記録の工程・課題を確認してください。')
        add('review','原文の確認課題',sum(x.get('status')=='open' for x in original.get('issues',[])),
            '元画面で判断内容と根拠を登録してください。')
        semantics=learning.get('semantics',[])
        add('review','抽出した工程',int(bool(semantics and semantics[-1].get('status')=='needs_review')),
            '最新の抽出候補について原文・判断条件を確認してください。')
        recipes=learning.get('recipes',[])
        add('revalidate','手順の再検証',sum(x.get('status')=='needs_revalidation' for x in recipes),
            '入力・条件が変わっています。元条件と別条件で再検証してください。')
        add('prepare','手順候補の検証',sum(x.get('status')=='draft' for x in recipes)+sum(x.get('status')=='draft' for x in original.get('procedures',[])),
            '候補は未採用です。元画面で試験結果と採用条件を確認してください。')
        candidates=[c for s in learning.get('general_inventions',[]) for c in s.get('candidates',[])]
        add('review','分野別TRIZの内容確認',sum(c.get('status')=='artifact_checked' for c in candidates),
            '機械検査済みという保存状態です。採用可否は最新の入力・試験・成果物を元画面で再確認します。')
        add('revalidate','分野別TRIZの再検証',sum(c.get('status')=='needs_revalidation' for c in candidates),
            '条件または試験が変わっています。過去のレビューだけでは採用できません。')
        add('blocked','分野別TRIZの未接続・不合格',sum(c.get('status') in {'plan_only','trial_failed'} for c in candidates),
            '必要能力または試験結果を確認してください。一覧から実行や採用は行いません。')
    return {'project':project,'items':items,'offset':offset,'limit':limit,
        'next_offset':offset+limit if len(examples)>limit else None,
        'scope':'実行例・手順学習・分野別TRIZ（保存状態の一覧）',
        'notice':'業務タスク全体の承認待ち一覧ではありません。件数は表示中の実行例内の確認対象数です。採用可否の再判定は元画面で行います。'}


def install(app, env):
    @app.get('/api/projects/{pid}/review-queue')
    def queue(pid: str, offset: int=Query(0, ge=0), limit: int=Query(30, ge=1, le=50)):
        env.require_project(pid)
        try:
            return project_queue(env.DATA_DIR, pid, offset, limit)
        except (sqlite3.Error, ValueError, KeyError, TypeError):
            # Do not expose private file paths, SQL, or raw records.
            raise HTTPException(503, '確認一覧を読み取れません。再読込し、続く場合は保存状態を確認してください。')
