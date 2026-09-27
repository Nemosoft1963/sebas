"""Persistent, project-scoped detailed plans, checkpoints and review evidence."""
import json
import time
import uuid
from app.upgrade_store import UpgradeStore, canonical, digest, utcnow
from app.recovery_policy import RecoveryStopped


class DetailStore(UpgradeStore):
    def __init__(self, path):
        super().__init__(path)
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS detailed_plans(
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, task_id TEXT NOT NULL,
                episode TEXT NOT NULL, revision INTEGER NOT NULL, signature TEXT NOT NULL,
                payload TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(project_id,task_id,episode,revision))''')
            db.execute('''CREATE TABLE IF NOT EXISTS detailed_steps(
                plan_id TEXT NOT NULL, step_id TEXT NOT NULL, state TEXT NOT NULL,
                output TEXT, output_hash TEXT, artifact_path TEXT, error TEXT NOT NULL,
                repairs INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
                PRIMARY KEY(plan_id,step_id), FOREIGN KEY(plan_id) REFERENCES detailed_plans(id))''')
            db.execute('''CREATE TABLE IF NOT EXISTS detailed_reviews(
                id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, candidate_hash TEXT NOT NULL,
                decision TEXT NOT NULL, notes TEXT NOT NULL, created_at TEXT NOT NULL,
                FOREIGN KEY(plan_id) REFERENCES detailed_plans(id))''')
            db.execute('''CREATE TABLE IF NOT EXISTS detailed_budgets(
                project_id TEXT NOT NULL, episode TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(project_id,episode))''')

    def latest_plan(self, pid, tid, episode=None):
        with self.connect() as db:
            sql='SELECT * FROM detailed_plans WHERE project_id=? AND task_id=?'
            args=[pid,tid]
            if episode is not None:sql+=' AND episode=?';args.append(episode)
            row=db.execute(sql+' ORDER BY created_at DESC,revision DESC LIMIT 1',args).fetchone()
        if not row:return None
        result=dict(row);result['payload']=json.loads(result['payload'])
        result['steps']=self.steps(pid,result['id'])
        return result

    def require_plan(self, pid, plan_id):
        with self.connect() as db:
            row=db.execute('SELECT * FROM detailed_plans WHERE id=? AND project_id=?',(plan_id,pid)).fetchone()
        if not row:raise ValueError('Detailed plan not found in project')
        return dict(row)

    def save_plan(self,pid,tid,episode,signature,payload):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rev=db.execute('SELECT COALESCE(MAX(revision),0)+1 FROM detailed_plans WHERE project_id=? AND task_id=? AND episode=?',(pid,tid,episode)).fetchone()[0]
            if rev>2:raise RecoveryStopped('詳細計画の再生成上限です。予算を自動解除しません')
            plan_id=uuid.uuid4().hex
            db.execute('INSERT INTO detailed_plans VALUES(?,?,?,?,?,?,?,?)',(plan_id,pid,tid,episode,rev,signature,canonical(payload),utcnow()))
            for step in payload['steps']:
                db.execute('INSERT INTO detailed_steps VALUES(?,?,?,?,?,?,?,?,?)',(plan_id,step['id'],'pending',None,None,None,'',0,utcnow()))
        return self.latest_plan(pid,tid,episode)

    def steps(self,pid,plan_id):
        self.require_plan(pid,plan_id)
        with self.connect() as db:rows=db.execute('SELECT * FROM detailed_steps WHERE plan_id=? ORDER BY step_id',(plan_id,)).fetchall()
        return [{**dict(r),'output':json.loads(r['output']) if r['output'] else None} for r in rows]

    def checkpoint(self,pid,plan_id,sid,state,output=None,path=None,error=''):
        self.require_plan(pid,plan_id)
        if state not in {'pending','running','completed','needs_review','failed','blocked'}:raise ValueError('Invalid step state')
        text=canonical(output) if output is not None else None
        with self.connect() as db:
            n=db.execute('UPDATE detailed_steps SET state=?,output=?,output_hash=?,artifact_path=?,error=?,updated_at=? WHERE plan_id=? AND step_id=?',
                         (state,text,digest(text) if text else None,path,error[:8000],utcnow(),plan_id,sid)).rowcount
            if n!=1:raise ValueError('Unknown step')

    def invalidate(self,pid,plan_id,sid,reason):
        self.require_plan(pid,plan_id)
        with self.connect() as db:
            db.execute("UPDATE detailed_steps SET state='pending',output=NULL,output_hash=NULL,artifact_path=NULL,error=?,updated_at=? WHERE plan_id=? AND step_id>=?",(reason,utcnow(),plan_id,sid))
            # Reviews remain immutable history; D06 binds the exact checkpoint identities.

    def repair(self,pid,plan_id,sid):
        self.require_plan(pid,plan_id)
        with self.connect() as db:
            if db.execute('UPDATE detailed_steps SET repairs=repairs+1 WHERE plan_id=? AND step_id=? AND repairs<1',(plan_id,sid)).rowcount!=1:
                raise RecoveryStopped('同一工程の修正上限: '+sid)

    def review(self,pid,plan_id,candidate_hash,decision,notes):
        self.require_plan(pid,plan_id)
        if decision not in {'approved','rejected'} or not notes.strip():raise ValueError('確認結果と根拠を記入してください')
        with self.connect() as db:
            row=db.execute("SELECT output_hash,state FROM detailed_steps WHERE plan_id=? AND step_id='D06'",(plan_id,)).fetchone()
            if not row or row['state']!='needs_review' or row['output_hash']!=candidate_hash:raise ValueError('確認対象が変更されました。再読込してください')
            db.execute('INSERT INTO detailed_reviews VALUES(?,?,?,?,?,?)',(uuid.uuid4().hex,plan_id,candidate_hash,decision,notes[:8000],utcnow()))

    def approved(self,pid,plan_id,candidate_hash):
        self.require_plan(pid,plan_id)
        with self.connect() as db:
            row=db.execute('SELECT decision FROM detailed_reviews WHERE plan_id=? AND candidate_hash=? ORDER BY created_at DESC LIMIT 1',(plan_id,candidate_hash)).fetchone()
        return bool(row and row['decision']=='approved')


class DetailBudget:
    """12 actual requests: planning 2, execution 8, repair 2. Active time persists."""
    mode='enforce'
    def __init__(self,store,pid,episode):
        self.store,self.pid,self.episode=store,pid,episode
        self.phase='planning';self.step='plan';self.llm_limit=12
        self.clock=time.monotonic()
        with store.connect() as db:
            db.execute('INSERT OR IGNORE INTO detailed_budgets VALUES(?,?,?)',(pid,episode,canonical({'planning':0,'execution':0,'repair':0,'tool':0,'seconds':0,'steps':{}})))
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            d=json.loads(db.execute('SELECT payload FROM detailed_budgets WHERE project_id=? AND episode=?',(pid,episode)).fetchone()[0])
            if d.get('active_started'):
                d['seconds']+=min(900,max(0,time.time()-d['active_started']))
            d['active_started']=time.time()
            db.execute('UPDATE detailed_budgets SET payload=? WHERE project_id=? AND episode=?',(canonical(d),pid,episode))
    def data(self):
        with self.store.connect() as db:
            return json.loads(db.execute('SELECT payload FROM detailed_budgets WHERE project_id=? AND episode=?',(self.pid,self.episode)).fetchone()[0])
    @property
    def llm_calls(self):
        d=self.data();return sum(d[k] for k in ('planning','execution','repair'))
    def remaining(self):return max(0,900-self.data()['seconds']-(time.monotonic()-self.clock))
    def consume(self,kind):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            d=json.loads(db.execute('SELECT payload FROM detailed_budgets WHERE project_id=? AND episode=?',(self.pid,self.episode)).fetchone()[0])
            if d['seconds']+time.monotonic()-self.clock>=900:raise RecoveryStopped('親タスクの実行時間予算に達しました')
            key=self.phase if kind=='llm' else 'tool'
            limit={'planning':2,'execution':8,'repair':2,'tool':24}[key]
            if d[key]>=limit:raise RecoveryStopped('親タスクの共有予算に達しました: '+key)
            sk=self.step+':'+key
            if kind=='llm' and d['steps'].get(sk,0)>=({'planning':2,'execution':4,'repair':2}[key]):
                raise RecoveryStopped('工程の実要求上限です: '+self.step)
            d[key]+=1;d['steps'][sk]=d['steps'].get(sk,0)+1
            db.execute('UPDATE detailed_budgets SET payload=? WHERE project_id=? AND episode=?',(canonical(d),self.pid,self.episode))
    def close(self):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            d=json.loads(db.execute('SELECT payload FROM detailed_budgets WHERE project_id=? AND episode=?',(self.pid,self.episode)).fetchone()[0])
            d['seconds']+=time.monotonic()-self.clock;self.clock=time.monotonic();d['active_started']=None
            db.execute('UPDATE detailed_budgets SET payload=? WHERE project_id=? AND episode=?',(canonical(d),self.pid,self.episode))
