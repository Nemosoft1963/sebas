"""Project-isolated imported reports. Imported instructions never acquire authority."""
import hashlib
import json
import re
import sqlite3
import time
import uuid
from pathlib import Path


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class Conflict(ValueError):
    pass


class ExampleStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'examples.sqlite3'
        with self.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS examples(
              id TEXT PRIMARY KEY, project TEXT NOT NULL, hash TEXT NOT NULL,
              revision INTEGER NOT NULL, data TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
              UNIQUE(project,hash));
              CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, project TEXT,
              example TEXT, action TEXT, detail TEXT, created REAL);''')
            try:
                db.execute("ALTER TABLE examples ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
            except sqlite3.OperationalError:
                pass

    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        return db

    def list(self, project, include_archived=False):
        with self.connect() as db:
            if include_archived:
                rows = db.execute('SELECT data, status FROM examples WHERE project=? ORDER BY rowid DESC', (project,)).fetchall()
            else:
                rows = db.execute("SELECT data, status FROM examples WHERE project=? AND status='active' ORDER BY rowid DESC", (project,)).fetchall()
            result = []
            for r in rows:
                item = {k:v for k,v in json.loads(r['data']).items() if k != 'source'}
                item['archive_status'] = r['status']
                result.append(item)
            return result

    def get(self, project, eid):
        with self.connect() as db:
            row = db.execute('SELECT data, status FROM examples WHERE project=? AND id=?', (project,eid)).fetchone()
        if not row:
            raise KeyError('このプロジェクトに実行例がありません')
        item = json.loads(row['data'])
        item['archive_status'] = row['status']
        return item

    def archive(self, project, eid, reason, actor):
        if not str(reason or '').strip() or len(str(reason or '').strip()) < 3:
            raise ValueError('理由は3文字以上必要です')
        if not str(actor or '').strip():
            raise ValueError('実行者が必要です')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data, status FROM examples WHERE project=? AND id=?', (project, eid)).fetchone()
            if not row:
                raise KeyError(eid)
            if row['status'] == 'archived':
                item = json.loads(row['data'])
                item['archive_status'] = 'archived'
                return item
            item = json.loads(row['data'])
            item['archive_status'] = 'archived'
            db.execute("UPDATE examples SET status='archived', data=? WHERE project=? AND id=?",
                       (json.dumps(item, ensure_ascii=False), project, eid))
            self.audit(db, item, 'archive', {'reason': str(reason).strip(), 'actor': str(actor).strip()})
            return item

    def import_text(self, project, filename, source, agent=''):
        if Path(filename).suffix.lower() not in {'.md','.txt','.json'}:
            raise ValueError('MD/TXT/JSONを指定してください')
        if not source.strip() or len(source.encode()) > 5*1024*1024 or '\x00' in source:
            raise ValueError('空の記録または上限5MiBを超える記録です')
        if filename.lower().endswith('.json'):
            json.loads(source)
        sha = hashlib.sha256(source.encode()).hexdigest()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM examples WHERE project=? AND hash=?',(project,sha)).fetchone()
            if row:
                return json.loads(row[0])
            item = dict(id=uuid.uuid4().hex, project=project, hash=sha, revision=1,
                filename=Path(filename.replace('\\','/')).name[:180], source=source,
                source_agent=agent[:120], status='needs_review', classification='restricted_local',
                external_send_allowed=False, created=time.time(), extraction={}, bindings={}, issues=[], procedures=[], runs=[])
            db.execute('INSERT INTO examples(id,project,hash,revision,data,status) VALUES(?,?,?,?,?,?)',
                       (item['id'],project,sha,1,json.dumps(item,ensure_ascii=False),'active'))
            self.audit(db,item,'import',{'hash':sha})
            return item

    @staticmethod
    def audit(db, item, action, detail):
        db.execute('INSERT INTO audit(project,example,action,detail,created) VALUES(?,?,?,?,?)',
            (item['project'],item['id'],action,json.dumps(detail,ensure_ascii=False),time.time()))

    def change(self, project, eid, revision, action, update):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT data,revision FROM examples WHERE project=? AND id=?',(project,eid)).fetchone()
            if not row:raise KeyError(eid)
            if row['revision'] != revision:raise Conflict('内容が更新されています。再読込してください')
            item=json.loads(row['data'])
            update(item)
            item['revision']+=1
            db.execute('UPDATE examples SET revision=?,data=? WHERE project=? AND id=?',
                (item['revision'],json.dumps(item,ensure_ascii=False),project,eid))
            self.audit(db,item,action,{'revision':item['revision']})
            return item

    def parse(self, project, eid, revision):
        def update(item):
            lines=item['source'].splitlines()
            sections=[]
            for n,line in enumerate(lines,1):
                if re.match(r'^#{1,6}\s',line):
                    sections.append({'line':n,'title':re.sub(r'^#+\s*','',line),'status':'reported'})
            chunks=[]
            for i,s in enumerate(sections):
                end=sections[i+1]['line']-1 if i+1<len(sections) else len(lines)
                chunks.append(dict(s,end_line=end,text='\n'.join(lines[s['line']-1:end])))
            item['extraction']={'method':'source_sections_v1','sections':chunks,'observed_checks':[],
                'reported_checks':[{'line':n,'text':line,'status':'reported'} for n,line in enumerate(lines,1) if re.search('エラー0|承認|完了|検証|成功',line)][:100]}
            issues=[]
            for section in chunks:
                if re.search('課題|未解決|注意|未確認',section['title']):
                    for offset,line in enumerate(section['text'].splitlines()):
                        if re.match(r'^\s*(?:[-*]|\d+[.)、]|\|)',line) and line.strip('| :-'):
                            issues.append(dict(id=uuid.uuid4().hex,text=line[:1000],line=section['line']+offset,status='open',answer='',evidence=''))
            def table_entries(section):
                result=[]
                for offset,line in enumerate(section['text'].splitlines()):
                    cells=[v.strip() for v in line.strip().strip('|').split('|')]
                    if line.lstrip().startswith('|') and cells and re.fullmatch(r'\d+',cells[0]):
                        result.append({'line':section['line']+offset,'text':line,'status':'reported'})
                    elif re.match(r'^\s*\d+[.)、]\s*',line):
                        result.append({'line':section['line']+offset,'text':line,'status':'reported'})
                return result
            item['extraction']['reported_steps']=[entry for section in chunks if '実行手順' in section['title'] or '作業計画' in section['title'] for entry in table_entries(section)]
            item['extraction']['reported_inputs']=[section for section in chunks if '入力' in section['title']]
            item['extraction']['reported_outputs']=[section for section in chunks if '出力' in section['title']]
            item['extraction']['reported_goal']=[section for section in chunks if '指示' in section['title']]
            item['issues']=issues or [dict(id=uuid.uuid4().hex,text='入力原本・成果物・計算条件・検証ログの確認',line=1,status='open',answer='',evidence='')]
            item['status']='needs_review'
            for p in item['procedures']:p['status']='revoked'
        return self.change(project,eid,revision,'parse',update)

    def procedure(self, project,eid,revision,kind):
        if kind not in {'vehicle_profit_v1','source_review_v1'}:raise ValueError('未対応の手順種別')
        def update(item):
            if not item['extraction']:raise ValueError('先に原文を解析してください')
            spec={'kind':kind,'input_schema':'vehicle-profit-input-v1' if kind=='vehicle_profit_v1' else 'source-review-v1',
                'operations':['validate_inputs','map_entities','reconcile','build_workbook','recalculate','independent_check'] if kind=='vehicle_profit_v1' else ['review_sources','document_requirements'],
                'source_hash':item['hash'],'binding_hash':digest(item['bindings']), 'issue_hash':digest(item['issues'])}
            item['procedures'].append(dict(version=len(item['procedures'])+1,hash=digest(spec),spec=spec,status='draft',reviews=[]))
        return self.change(project,eid,revision,'procedure',update)

    def resolve_issue(self,project,eid,revision,issue_id,answer,evidence):
        if not answer.strip() or not evidence.strip():raise ValueError('判断と根拠が必要です')
        def update(item):
            issue=next((i for i in item['issues'] if i['id']==issue_id),None)
            if not issue:raise KeyError(issue_id)
            issue.update(status='resolved',answer=answer[:4000],evidence=evidence[:4000])
            for p in item['procedures']:p['status']='revoked'
        return self.change(project,eid,revision,'resolve_issue',update)

    def bind(self,project,eid,revision,role,binding):
        if role not in {'vehicle_master','revenue','payroll','fuel','reference_totals','normalized_input','output'}:raise ValueError('資料役割が不正です')
        def update(item):
            item['bindings'][role]=binding
            for p in item['procedures']:p['status']='revoked'
        return self.change(project,eid,revision,'bind',update)

    def review(self,project,eid,revision,version,expected_hash,decision,reviewer,evidence):
        if decision not in {'reusable','revoked'} or not reviewer.strip() or not evidence.strip():raise ValueError('判断者と根拠が必要です')
        def update(item):
            p=next((p for p in item['procedures'] if p['version']==version),None)
            if not p or p['hash']!=expected_hash:raise Conflict('手順版が異なります')
            if decision=='reusable':
                if p['status']=='revoked' or any(i['status']!='resolved' for i in item['issues']):raise ValueError('未解決課題または失効済み手順があります')
                successes=[r for r in item['runs'] if r['procedure_hash']==expected_hash and r['status']=='completed']
                if not successes:raise ValueError('同じ手順版の再現検証合格が必要です')
            p['status']=decision
            p['reviews'].append(dict(decision=decision,reviewer=reviewer[:120],evidence=evidence[:4000],created=time.time()))
        return self.change(project,eid,revision,'review',update)
