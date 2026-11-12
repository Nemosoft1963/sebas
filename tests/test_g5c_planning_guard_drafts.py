"""G5-c 外部AIの下書きの参照・作り直し試験。疑似PJのみ・実在値なし。

Node の擬似DOM。Node が無い環境では描画試験をスキップする。
スキップは成功ではなく、静的な検査で補う。
"""
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DRAFTS_JS = (ROOT / "app/static/planning_guard_drafts.js").read_text(encoding="utf-8")
JOBS_JS = (ROOT / "app/static/planning_guard_jobs.js").read_text(encoding="utf-8")
PACKETS_JS = (ROOT / "app/static/planning_guard_packets.js").read_text(encoding="utf-8")
PANEL_JS = (ROOT / "app/static/planning_guard_panel.js").read_text(encoding="utf-8")
SETTINGS_JS = (ROOT / "app/static/planning_guard_settings.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
WEB_TEXT = (ROOT / "app/web.py").read_text(encoding="utf-8")

DANGEROUS = ("innerHTML", "insertAdjacentHTML", "outerHTML", "document.write",
             "eval", "new Function")


def test_static_no_dangerous_and_methods():
    for token in DANGEROUS:
        assert token not in DRAFTS_JS, token
    for token in ("console.log", "console.info", "console.debug",
                  "console.warn", "console.error"):
        assert token not in DRAFTS_JS, token
    for token in ("localStorage", "sessionStorage", "indexedDB", "IndexedDB"):
        assert token not in DRAFTS_JS, token
    assert "planningGuardDraftsSection" in DRAFTS_JS
    assert "外部AIの下書き" in DRAFTS_JS
    assert "外部AIの下書きです。未検証で、事実の根拠にはなりません" in DRAFTS_JS
    assert "下書きを読む" in DRAFTS_JS
    assert "この下書きを参考に、ローカルで計画を作り直す" in DRAFTS_JS
    assert "計画を作り直す" in DRAFTS_JS
    assert "やめる" in DRAFTS_JS
    assert "いまの計画(版 " in DRAFTS_JS
    assert "置き換わります。この画面には、元に戻す操作はありません。" in DRAFTS_JS
    assert "外部の下書きは参考情報です。そのままは採用されず、ローカルで作り直されます。" in DRAFTS_JS
    assert "承認済みの送付内容は無効" in DRAFTS_JS
    assert "独立評価をやり直す" in DRAFTS_JS
    assert "いまの計画が置き換わることを理解したうえで、作り直します" in DRAFTS_JS
    assert "実行者名" in DRAFTS_JS
    assert "取得できませんでした" in DRAFTS_JS
    assert "この機能は管理者の設定で無効です" in DRAFTS_JS
    assert "可否を確認できません" in DRAFTS_JS
    assert "反映できる下書きはありません" in DRAFTS_JS
    assert "下書きの形式が確認できません" in DRAFTS_JS
    assert "いまの計画を確認できませんでした" in DRAFTS_JS
    assert "実行中は計画を作り直せません" in DRAFTS_JS
    assert "先に一時停止してください" in DRAFTS_JS
    assert "作り直せたか確認できませんでした。計画の画面で確認してください。確認する前に、もう一度は押さないでください" in DRAFTS_JS
    assert "計画を作り直しました(版 " in DRAFTS_JS
    assert "次は、独立評価と計画の承認です。この画面ではどちらも行いません。" in DRAFTS_JS
    assert "ローカルのAIが利用できないため、作り直せません" in DRAFTS_JS
    assert "この下書きが見つかりません" in DRAFTS_JS
    assert "この下書きは使えない状態です" in DRAFTS_JS
    assert "送信のあとで計画が変わったため、この下書きは使えません" in DRAFTS_JS
    assert "下書きの内容がありません" in DRAFTS_JS
    assert "作り直せませんでした" in DRAFTS_JS
    assert "この計画では、外部の下書きを使った作り直しはできません" in DRAFTS_JS
    assert "作り直しに失敗しました" in DRAFTS_JS
    assert "現在のプロジェクトが変わったため作り直しませんでした" in DRAFTS_JS
    assert "証拠のない主張(実施済み等)" in DRAFTS_JS
    assert "ここにある『実施済み』『達成』を事実として扱わないでください" in DRAFTS_JS
    assert "ほか" in DRAFTS_JS
    assert "以降は省略" in DRAFTS_JS
    assert "'GET'" in DRAFTS_JS or '"GET"' in DRAFTS_JS
    assert "'POST'" in DRAFTS_JS or '"POST"' in DRAFTS_JS
    assert "PUT" not in DRAFTS_JS
    assert "DELETE" not in DRAFTS_JS
    assert "PATCH" not in DRAFTS_JS
    assert "createElement('form')" not in DRAFTS_JS
    assert 'createElement("form")' not in DRAFTS_JS
    assert "<form" not in DRAFTS_JS
    assert "http://" not in DRAFTS_JS
    assert "https://" not in DRAFTS_JS
    assert "/external-plan-jobs" in DRAFTS_JS
    assert "/planning-job-capabilities" in DRAFTS_JS
    assert "/mission/plan/generate" in DRAFTS_JS
    assert "isValidJob" in DRAFTS_JS
    assert "isValidJobs" in DRAFTS_JS
    assert "isValidCapabilities" in DRAFTS_JS
    assert "isValidMission" in DRAFTS_JS
    assert "isValidDraft" in DRAFTS_JS
    assert "textContent" in DRAFTS_JS
    # POST本文に余計な項目を入れない
    assert "approver" not in DRAFTS_JS, "approver must not be in POST body"


def test_internal_words_not_shown_raw():
    for match in re.finditer(r"textContent\s*=\s*('([^']*)'|\"([^\"]*)\")", DRAFTS_JS):
        literal = match.group(2) if match.group(2) is not None else match.group(3)
        for word in ("feature_disabled", "job_not_found", "job_not_usable",
                     "plan_changed", "draft_missing", "invalid_job_id",
                     "unsupported_plan_path", "external_draft", "unverified_claims",
                     "request_template"):
            if word in (literal or ""):
                raise AssertionError((word, literal))
    # generateErrorText の分岐は固定日本語を返し、コード語を画面に出さない
    assert "generateErrorText" in DRAFTS_JS
    # 画面に直接出す文字列にコード語が混ざっていない
    for word in ("invalid_job_id", "job_not_usable", "plan_changed",
                 "draft_missing", "unsupported_plan_path", "feature_disabled",
                 "job_not_found"):
        # JS内の条件分岐の右辺(比較対象)としては許すが、addTextの直書きは禁止
        for match in re.finditer(r"addText\([^,]+,\s*'([^']*)'\)", DRAFTS_JS):
            if word in match.group(1):
                raise AssertionError((word, match.group(1)))
        for match in re.finditer(r'addText\([^,]+,\s*"([^"]*)"\)', DRAFTS_JS):
            if word in match.group(1):
                raise AssertionError((word, match.group(1)))


def test_index_tag_one_line_and_order_kept():
    jobs_lines = [line for line in INDEX_HTML.splitlines()
                  if "planning_guard_jobs.js" in line]
    assert len(jobs_lines) == 1, jobs_lines
    added = [line for line in INDEX_HTML.splitlines()
             if "planning_guard_drafts.js" in line]
    assert len(added) == 1, added
    assert "v=20261107-g5c" in added[0]
    for token in DANGEROUS:
        assert token not in added[0], token
    jobs_tag = '<script src="/static/planning_guard_jobs.js?v=20261106-g5b2b"></script>'
    drafts_tag = '<script src="/static/planning_guard_drafts.js?v=20261107-g5c"></script>'
    assert INDEX_HTML.count(jobs_tag) == 1
    assert INDEX_HTML.count(drafts_tag) == 1
    flat = INDEX_HTML.replace("\r", "").replace("\n", "")
    assert jobs_tag + drafts_tag in flat


def test_web_route_and_scope():
    import app.web as web
    routes = {str(getattr(r, "path", "")) for r in web.app.routes}
    assert "/static/planning_guard_drafts.js" in routes
    assert "/static/planning_guard_jobs.js" in routes
    assert "/api/projects/{project_id}/external-plan-jobs" in routes
    assert "/api/projects/{project_id}/planning-job-capabilities" in routes
    assert "/api/projects/{project_id}/mission/plan/generate" in routes
    assert WEB_TEXT.count("@app.get('/static/planning_guard_drafts.js')") == 1
    assert WEB_TEXT.count("async def planning_guard_drafts_js") == 1
    assert WEB_TEXT.count("@app.get('/static/planning_guard_jobs.js')") == 1
    assert "planningGuardDraftsSection" not in JOBS_JS
    assert "planningGuardDraftsSection" not in PANEL_JS
    assert "planningGuardDraftsSection" not in SETTINGS_JS
    assert "planningGuardDraftsSection" not in PACKETS_JS
    assert "planning_guard_drafts" not in JOBS_JS
    assert "planning_guard_drafts" not in PANEL_JS
    assert "planning_guard_drafts" not in SETTINGS_JS
    assert "planning_guard_drafts" not in PACKETS_JS
    for name in ("project_mission.js", "goal_state.js", "watch_panel.js",
                 "ledger_panel.js", "night_run_panel.js", "shadow_panel.js",
                 "workflow_readiness.js", "contact_channel_panel.js"):
        text = (ROOT / "app/static" / name).read_text(encoding="utf-8")
        assert "planningGuardDraftsSection" not in text, name
        assert "planning_guard_drafts" not in text, name
    for path in sorted((ROOT / "app").glob("*.py")):
        if path.name == "web.py":
            continue
        text = path.read_text(encoding="utf-8")
        assert "planning_guard_drafts" not in text, path.name
        assert "planningGuardDraftsSection" not in text, path.name


def test_drafts_static_route_serves_js():
    from fastapi.testclient import TestClient
    import app.web as web
    client = TestClient(web.app, raise_server_exceptions=False)
    response = client.get("/static/planning_guard_drafts.js")
    assert response.status_code == 200
    assert "planningGuardDraftsSection" in response.text
    assert response.headers.get("content-type", "").startswith("text/javascript")


def test_change_scope_only_expected_files():
    assert (ROOT / "app/static/planning_guard_drafts.js").is_file()
    assert (ROOT / "tests/test_g5c_planning_guard_drafts.py").is_file()
    assert WEB_TEXT.count("@app.get('/static/planning_guard_drafts.js')") == 1
    assert WEB_TEXT.count("async def planning_guard_drafts_js") == 1
    drafts_lines = [line for line in INDEX_HTML.splitlines()
                    if "planning_guard_drafts.js" in line]
    assert len(drafts_lines) == 1


def test_mutation_guards_have_regression_tests():
    assert "if(!name||!(checkInput&&checkInput.checked))return;" in DRAFTS_JS
    assert "if(generateBusy)return;" in DRAFTS_JS
    assert "if(!pid||currentPid()!==pid)" in DRAFTS_JS
    assert "if(!isCurrentTicket(ticket)||currentPid()!==pid)return;" in DRAFTS_JS
    assert "if(!isValidJobs(d))throw Error('bad');" in DRAFTS_JS
    assert "if(!isValidDraft(job.external_draft))return;" in DRAFTS_JS or \
        "if(!isValidDraft(draft))" in DRAFTS_JS
    assert "status==='external_generated'" in DRAFTS_JS
    assert "plan_generation!==true" in DRAFTS_JS
    assert "mission.status==='running'" in DRAFTS_JS
    assert "unknownLocked[job.id]=true" in DRAFTS_JS
    assert "JSON.stringify({external_job_id:jobId})" in DRAFTS_JS
    assert "nextTicket" in DRAFTS_JS
    assert "isCurrentTicket" in DRAFTS_JS


def _probe_declaration_problems(source):
    code = []
    depths = []
    stack = []
    problems = []
    quote = None
    i = 0
    while i < len(source):
        ch = source[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "'\"`":
            quote = ch
            i += 1
            continue
        if ch in "([{":
            stack.append(ch)
        elif ch in ")]}":
            expected = {")": "(", "]": "[", "}": "{"}[ch]
            if not stack or stack[-1] != expected:
                problems.append("括弧の対応が不正: 位置" + str(i) + "の'" + ch + "'")
            else:
                stack.pop()
        code.append(ch)
        depths.append(len(stack))
        i += 1
    if quote:
        problems.append("閉じられていない文字列リテラルがあります")
    if stack:
        problems.append("閉じられていない括弧があります: " + "".join(stack))
    text = "".join(code)
    counts = {}
    for match in re.finditer(r"(?<![\w$.])(?:const|let|var|function)\s+([A-Za-z_$][\w$]*)", text):
        if depths[match.start(1)] != 0:
            continue
        counts[match.group(1)] = counts.get(match.group(1), 0) + 1
    for name, count in sorted(counts.items()):
        if count > 1:
            problems.append("トップレベルの宣言が重複: " + name + "(" + str(count) + "回)")
    return problems


def _require_node():
    if shutil.which("node") is None:
        pytest.skip("Node.js(node)が無いため画面の実行試験を実施できない(未検証。成功ではない)")


def _full_mount_source(scenario_js, source_path=None):
    src_expr = json.dumps(str(ROOT / "app/static/planning_guard_drafts.js"))
    if source_path is not None:
        src_expr = json.dumps(str(source_path))
    return "\n".join([
        "const fs=require('fs');",
        "const vm=require('vm');",
        "const src=fs.readFileSync(" + src_expr + ",'utf8');",
        "let fetchCalls=[];",
        "let scenario=" + scenario_js + ";",
        "let installedSection=null;",
        "let panelSection=null;",
        "let jobsSection=null;",
        ("function mkEl(tag){"
         "const e={tag,children:[],attrs:{},handlers:{},value:'',disabled:false,"
         "textContent:'',placeholder:'',type:'',className:'',id:'',checked:false,"
         "append(...xs){xs.forEach(x=>this.children.push(x));},"
         "appendChild(x){this.children.push(x);return x;},"
         "replaceChildren(...xs){this.children=[];if(xs.length)this.append(...xs);},"
         "addEventListener(k,f){this.handlers[k]=f;},"
         "click(){if(this.handlers.click)this.handlers.click();},"
         "change(){if(this.handlers.change)this.handlers.change();},"
         "input(){if(this.handlers.input)this.handlers.input();},"
         "setAttribute(k,v){this.attrs[k]=v;},"
         "querySelector(){return null;},"
         "querySelectorAll(){return [];}};"
         "return e;}"),
        ("const ids={};"
         "const panel=mkEl('section');panel.id='planningGuardPanelSection';"
         "ids.planningGuardPanelSection=panel;"
         "const jobs=mkEl('section');jobs.id='planningGuardJobsSection';"
         "panel.children.push(jobs);ids.planningGuardJobsSection=jobs;"
         "const sel=mkEl('select');sel.value=scenario.startPid;ids.projectSelect=sel;"
         "const documentStub={_byId:ids,readyState:'complete',hidden:false,"
         "createElement(t){return mkEl(t);},"
         "createTextNode(t){const n=mkEl('span');n.textContent=String(t);return n;},"
         "getElementById(id){if(id==='planningGuardDraftsSection')return installedSection;return this._byId[id]||null;},"
         "addEventListener(){},"
         "querySelector(){return null;},querySelectorAll(){return [];}};"),
        "const mod={exports:{}};",
        ("const timeouts=[];"
         "let timerSeq=1;"
         "sandbox_gen_timeout=function(f,ms){const id=timerSeq++;timeouts.push({id:id,fn:f,ms:ms});return id;};"
         "sandbox_clear_timeout=function(id){for(let i=0;i<timeouts.length;i++){if(timeouts[i].id===id)timeouts.splice(i,1);}};"),
        ("const sandbox={module:mod,document:documentStub,window:{addEventListener(){}},"
         "location:{href:'http://localhost/'},console:console};"),
        "sandbox.scenario=scenario;",
        "sandbox.fetch=async function(url,options){const r=await scenario.onFetch(url,options||{});"
        "if(r&&r.tag==='__hold'){return new Promise(res=>{r.release=v=>res(v);scenario._releases.push(r);});}"
        "return r;};",
        ("sandbox.setTimeout=sandbox_gen_timeout;"
         "sandbox.clearTimeout=sandbox_clear_timeout;"
         "sandbox.setInterval=function(){return 0;};"
         "sandbox.clearInterval=function(){};"),
        "vm.createContext(sandbox);",
        "vm.runInContext(src,sandbox);",
        ("function texts(e){let out=[String(e.textContent||'')];"
         "for(const c of (e.children||[])){out=out.concat(texts(c));}return out;}"),
        ("function byId(id){"
         "function walk(n){if(n.id===id)return n;for(const c of (n.children||[])){const f=walk(c);if(f)return f;}return null;}"
         "const root=installedSection||panel;"
         "return walk(root);}"),
        "let done=async()=>{};",
        "done=(async()=>{",
        "await new Promise(r=>setImmediate(r));",
        "await new Promise(r=>setImmediate(r));",
        "await new Promise(r=>setImmediate(r));",
        "await new Promise(r=>setImmediate(r));",
        "installedSection=null;",
        ("function walkFind(n,id){if(n.id===id)return n;for(const c of (n.children||[])){const f=walkFind(c,id);if(f)return f;}return null;}"
         "installedSection=walkFind(panel,'planningGuardDraftsSection');"
         "if(!installedSection){throw Error('drafts section was not mounted under the panel');}"),
        "await scenario.steps({panel,texts,byId,sel,documentStub,sandbox,timeouts});",
        "})();",
        ("done.then(()=>process.exit(0)).catch(e=>{console.error(e&&e.stack||String(e));process.exit(1);});"),
    ])


def _node_mount(scenario_js, source_path=None):
    problems = _probe_declaration_problems(_full_mount_source(scenario_js, source_path))
    assert not problems, problems
    if shutil.which("node") is None:
        pytest.skip("Node.js(node)が無いため画面の実行試験を実施できない(未検証。成功ではない)")
    with tempfile.NamedTemporaryFile("w", suffix=".cjs", delete=False, encoding="utf-8") as tmp:
        tmp.write(_full_mount_source(scenario_js, source_path))
        probe_path = tmp.name
    try:
        completed = subprocess.run(["node", probe_path], capture_output=True,
                                   text=True, timeout=30)
    finally:
        Path(probe_path).unlink(missing_ok=True)
    assert completed.returncode == 0, completed.stderr + completed.stdout


def _node_mount_result(scenario_js, source_text):
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as src:
        src.write(source_text)
        src_path = src.name
    try:
        problems = _probe_declaration_problems(_full_mount_source(scenario_js, src_path))
        assert not problems, problems
        with tempfile.NamedTemporaryFile("w", suffix=".cjs", delete=False, encoding="utf-8") as tmp:
            tmp.write(_full_mount_source(scenario_js, src_path))
            probe_path = tmp.name
        try:
            completed = subprocess.run(["node", probe_path], capture_output=True,
                                       text=True, timeout=30)
        finally:
            Path(probe_path).unlink(missing_ok=True)
        return completed
    finally:
        Path(src_path).unlink(missing_ok=True)


def _draft_job(job_id="j" * 32, status="external_generated", provider="kp",
               draft=None, claims=None):
    base_draft = {"tasks": [{"title": "工程1", "description": "作業する",
                             "outputs": ["result/a.md"], "depends_on": []}],
                  "required_outputs": ["result/a.md"],
                  "unverified_facts": ["未確認の条件"],
                  "external_operations_pending": ["承認待ち操作"]}
    job = {"id": job_id, "pid": "pj1", "packet_id": "pkt1", "provider": provider,
           "purpose": "plan_generation", "plan_signature": "sig1",
           "status": status, "created_at": 1700000001, "updated_at": 1700000002,
           "external_draft": base_draft if draft is None else draft,
           "unverified_claims": ["実施済みと主張"] if claims is None else claims}
    return json.dumps(job)


def _base_scenario(jobs_js="[]", caps_js=None, mission_js=None,
                   generate_js=None, steps_js="",
                   jobs_fail=None, caps_fail=None, mission_fail=None,
                   generate_mode="ok", generate_status=200,
                   generate_detail="", jobs_bad=False, caps_bad=False,
                   mission_bad=False, mission_running=False):
    if caps_js is None:
        caps_js = json.dumps({"plan_generation": True, "plan_review": True})
    if mission_js is None:
        mission_js = json.dumps({"plan_version": 3, "status": "planning",
                                 "tasks": []})
    if generate_js is None:
        generate_js = json.dumps({"plan_version": 4, "status": "planning",
                                  "tasks": []})
    return ("""({
      startPid: 'pj1',
      _releases: [],
      calls: [],
      jobs: """ + jobs_js + """,
      capsFail: """ + json.dumps(bool(caps_fail)) + """,
      capsBad: """ + json.dumps(bool(caps_bad)) + """,
      jobsFail: """ + json.dumps(bool(jobs_fail)) + """,
      jobsBad: """ + json.dumps(bool(jobs_bad)) + """,
      missionFail: """ + json.dumps(bool(mission_fail)) + """,
      missionBad: """ + json.dumps(bool(mission_bad)) + """,
      missionRunning: """ + json.dumps(bool(mission_running)) + """,
      genMode: """ + json.dumps(generate_mode) + """,
      genStatus: """ + json.dumps(generate_status) + """,
      genDetail: """ + json.dumps(generate_detail) + """,
      onFetch: async function(url, options) {
        const method = (options||{}).method || 'GET';
        this.calls.push([method, url, (options&&options.body)||'']);
        const ok = (b) => ({ok: true, status: 200, json: async () => b});
        const fail = (status, detail) => ({ok: false, status: status, json: async () => ({detail: detail})});
        if (url.indexOf('/planning-job-capabilities') >= 0) {
          if (this.capsFail) { throw Error('net down'); }
          if (this.capsBad) { return ok({plan_generation: 'yes'}); }
          return ok(""" + caps_js + """);
        }
        if (url.indexOf('/mission/plan/generate') >= 0) {
          if (this.genHold) { return {tag: '__hold'}; }
          if (this.genMode === 'fail') { throw Error('net down'); }
          if (this.genBad) { return ok({plan_version: 'x'}); }
          if (this.genStatus !== 200) { return fail(this.genStatus, this.genDetail); }
          return ok(""" + generate_js + """);
        }
        if (url.indexOf('/external-plan-jobs') >= 0) {
          if (this.jobsFail) { throw Error('net down'); }
          if (this.jobsBad) { return ok({jobs: [{id: 1}]}); }
          if (this.jobsDisabled) { return fail(403, 'feature_disabled'); }
          return ok({jobs: this.jobs});
        }
        if (url.indexOf('/mission') >= 0) {
          if (this.missionFail) { throw Error('net down'); }
          if (this.missionBad) { return ok({plan_version: 'x'}); }
          if (this.missionRunning) { return ok({plan_version: 3, status: 'running', tasks: []}); }
          if (this.missionHold) { return {tag: '__hold'}; }
          return ok(""" + mission_js + """);
        }
        return fail(500, 'unexpected:' + url);
      },
      steps: async ({panel, texts, byId, sel, sandbox, timeouts}) => {
        const sc = sandbox.scenario;
        const tick = () => new Promise(r => setImmediate(r));
        function allNodes(n, out) { out.push(n); for (const c of (n.children || [])) { allNodes(c, out); } return out; }
        const findBtn = (label) => allNodes(panel, []).filter(x => x.tag === 'button' && x.textContent === label);
        const countCalls = (needle, m) => sc.calls.filter(c => c[0] === (m || 'POST') && c[1].indexOf(needle) >= 0).length;
        const seenText = () => texts(panel).join('||');
""" + steps_js + """
      }
    })""")


def test_node_pure_functions():
    problems = _probe_declaration_problems(DRAFTS_JS)
    assert not problems, problems
    _require_node()
    cases = [
        "if(m.shortJobId('abcdefghij')!=='abcdefgh'){console.error('short');process.exit(1);}",
        "if(m.shortJobId('')!=='確認できず'){console.error('short2');process.exit(1);}",
        "if(m.truncateText('abc',5)!=='abc'){console.error('tr1');process.exit(1);}",
        "if(!m.truncateText('abcdef',5).includes('以降は省略')){console.error('tr2');process.exit(1);}",
        "if(!m.isValidJobs({jobs:[]})){console.error('jobs ok');process.exit(1);}",
        "if(m.isValidJobs({jobs:[{id:1}]})){console.error('jobs bad');process.exit(1);}",
        "if(!m.isValidCapabilities({plan_generation:true,plan_review:false})){console.error('caps ok');process.exit(1);}",
        "if(m.isValidCapabilities({plan_generation:'yes',plan_review:true})){console.error('caps bad');process.exit(1);}",
        "if(!m.isValidMission({plan_version:1,status:'planning'})){console.error('mission ok');process.exit(1);}",
        "if(m.isValidMission({plan_version:'1',status:'planning'})){console.error('mission bad');process.exit(1);}",
        "if(!m.isValidDraft({tasks:[{title:'t',description:'d'}]})){console.error('draft ok');process.exit(1);}",
        "if(m.isValidDraft({tasks:[{title:1,description:'d'}]})){console.error('draft bad');process.exit(1);}",
        "if(m.generateErrorText(403,'feature_disabled')!=='この機能は管理者の設定で無効です'){console.error('e403');process.exit(1);}",
        "if(m.generateErrorText(409,'x')!=='実行中は計画を作り直せません。先に一時停止してください'){console.error('e409');process.exit(1);}",
        "if(m.generateErrorText(422,'job_not_usable')!=='この下書きは使えない状態です'){console.error('e422a');process.exit(1);}",
        "if(m.generateErrorText(422,'plan_changed')!=='送信のあとで計画が変わったため、この下書きは使えません'){console.error('e422b');process.exit(1);}",
        "if(m.generateErrorText(422,'zzz')!=='作り直せませんでした'){console.error('e422c');process.exit(1);}",
        "{const a=m.nextTicket();const b=m.nextTicket();if(!m.isCurrentTicket(b)){console.error('tick cur');process.exit(1);}if(m.isCurrentTicket(a)){console.error('tick stale');process.exit(1);}}",
    ]
    probe = "\n".join([
        "const fs=require('fs');",
        "const vm=require('vm');",
        "const src=fs.readFileSync(" + json.dumps(str(ROOT / "app/static/planning_guard_drafts.js")) + ",'utf8');",
        "const documentStub={_byId:{},readyState:'complete',hidden:false,createElement(){return null;},createTextNode(){return null;},getElementById(){return null;},addEventListener(){},querySelector(){return null;},querySelectorAll(){return [];}};",
        "const mod={exports:{}};",
        "vm.runInNewContext(src,{module:mod,document:documentStub,window:{addEventListener(){}},fetch:async()=>{throw Error('no-net');},setTimeout:function(){return 0;},clearTimeout:function(){},setInterval:function(){return 0;},clearInterval:function(){},location:{href:'http://localhost/'},console:console});",
        "const m=mod.exports;",
    ] + cases)
    with tempfile.NamedTemporaryFile("w", suffix=".cjs", delete=False, encoding="utf-8") as tmp:
        tmp.write(probe)
        probe_path = tmp.name
    try:
        completed = subprocess.run(["node", probe_path], capture_output=True,
                                   text=True, timeout=30)
    finally:
        Path(probe_path).unlink(missing_ok=True)
    assert completed.returncode == 0, completed.stderr + completed.stdout


def test_node_capability_and_list_guards():
    _node_mount(_base_scenario(
        jobs_js="[" + _draft_job() + "]",
        caps_js=json.dumps({"plan_generation": False, "plan_review": True}),
        steps_js="""
        await tick(); await tick(); await tick(); await tick(); await tick();
        if (!seenText().includes('この機能は管理者の設定で無効です')) { throw Error('disabled missing'); }
        findBtn('下書きを読む')[0].click();
        await tick(); await tick();
        if (!seenText().includes('この機能は管理者の設定で無効です')) { throw Error('disabled detail missing'); }
        if (findBtn('この下書きを参考に、ローカルで計画を作り直す').filter(b => !b.disabled).length !== 0) { throw Error('reflect must be absent/disabled'); }
    """))
    _node_mount(_base_scenario(
        jobs_js="[" + _draft_job() + "]",
        caps_fail=True,
        steps_js="""
        await tick(); await tick(); await tick(); await tick(); await tick();
        if (!seenText().includes('可否を確認できません')) { throw Error('caps unknown missing'); }
        findBtn('下書きを読む')[0].click();
        await tick(); await tick();
        if (findBtn('この下書きを参考に、ローカルで計画を作り直す').filter(b => !b.disabled).length !== 0) { throw Error('reflect must be blocked when caps unknown'); }
    """))
    _node_mount(_base_scenario(
        jobs_js="[]",
        jobs_fail=True,
        steps_js="""
        await tick(); await tick(); await tick(); await tick(); await tick();
        if (!seenText().includes('取得できませんでした')) { throw Error('list failed missing'); }
        if (seenText().includes('反映できる下書きはありません')) { throw Error('failed must not show empty'); }
    """))
    _node_mount(_base_scenario(
        jobs_js="[" + _draft_job(status="sending") + "," + _draft_job(job_id="q" * 32) + "]",
        steps_js="""
        await tick(); await tick(); await tick(); await tick(); await tick();
        const seen = seenText();
        if (seen.includes('ID：sending')) { throw Error('non-generated shown'); }
        if (!findBtn('下書きを読む') || findBtn('下書きを読む').length !== 1) { throw Error('only generated shown'); }
    """))
    _node_mount(_base_scenario(
        jobs_js="[]",
        steps_js="""
        await tick(); await tick(); await tick(); await tick(); await tick();
        if (!seenText().includes('反映できる下書きはありません')) { throw Error('empty missing'); }
    """))


def test_node_draft_display_and_truncation():
    long_desc = "説" * 1200
    big_tasks = [{"title": "工程%02d" % i, "description": "作業%02d" % i,
                  "outputs": [], "depends_on": []} for i in range(51)]
    big_draft = {"tasks": big_tasks, "required_outputs": [],
                 "unverified_facts": [], "external_operations_pending": []}
    pending_draft = {"tasks": [{"title": "工程1", "description": "作業する",
                                "outputs": [], "depends_on": []}],
                     "required_outputs": [], "unverified_facts": [],
                     "external_operations_pending": [{"k": "値は文字列", "n": 123},
                                                     "文字列の予定"]}
    _node_mount(_base_scenario(
        jobs_js="[" + _draft_job(draft=None) + "]",
        steps_js="""
        await tick(); await tick(); await tick(); await tick(); await tick();
        findBtn('下書きを読む')[0].click();
        await tick(); await tick();
        const seen = seenText();
        if (!seen.includes('工程1')) { throw Error('title missing'); }
        if (!seen.includes('作業する')) { throw Error('desc missing'); }
        if (!seen.includes('result/a.md')) { throw Error('outputs missing'); }
        if (!seen.includes('外部AIの下書きです。未検証で、事実の根拠にはなりません')) { throw Error('unverified note missing'); }
        if (!seen.includes('証拠のない主張(実施済み等)')) { throw Error('claims frame missing'); }
        if (!seen.includes('実施済みと主張')) { throw Error('claims text missing'); }
        if (!seen.includes('ここにある『実施済み』')) { throw Error('claims warning missing'); }
    """))
    _node_mount(_base_scenario(
        jobs_js="[" + _draft_job(draft={"tasks": [{"title": 1, "description": "x"}]}) + "]",
        steps_js="""
        await tick(); await tick(); await tick(); await tick(); await tick();
        findBtn('下書きを読む')[0].click();
        await tick(); await tick();
        if (!seenText().includes('下書きの形式が確認できません')) { throw Error('invalid draft missing'); }
        if (findBtn('この下書きを参考に、ローカルで計画を作り直す').filter(b => !b.disabled).length !== 0) { throw Error('invalid draft must not reflect'); }
    """))
    _node_mount(_base_scenario(
        jobs_js="[" + _draft_job(draft={"tasks": [{"title": "t", "description": json.loads(json.dumps(long_desc)),
                                                                               "outputs": [], "depends_on": []}]}) + "]",
        steps_js="""
        await tick(); await tick(); await tick(); await tick(); await tick();
        findBtn('下書きを読む')[0].click();
        await tick(); await tick();
        if (!seenText().includes('以降は省略')) { throw Error('truncation missing'); }
    """))
    _node_mount(_base_scenario(
        jobs_js="[" + _draft_job(draft=big_draft) + "]",
        steps_js="""
        await tick(); await tick(); await tick(); await tick(); await tick();
        findBtn('下書きを読む')[0].click();
        await tick(); await tick();
        if (!seenText().includes('ほか1件')) { throw Error('overflow missing'); }
    """))
    _node_mount(_base_scenario(
        jobs_js="[" + _draft_job(draft=pending_draft) + "]",
        steps_js="""
        await tick(); await tick(); await tick(); await tick(); await tick();
        findBtn('下書きを読む')[0].click();
        await tick(); await tick();
        const seen = seenText();
        if (!seen.includes('値は文字列')) { throw Error('pending string missing'); }
        if (!seen.includes('文字列の予定')) { throw Error('pending raw string missing'); }
        if (seen.includes('123')) { throw Error('non-string pending shown'); }
    """))


def test_node_reflect_requires_name_and_check():
    _node_mount(_base_scenario(
        jobs_js="[" + _draft_job() + "]",
        steps_js="""
        await tick(); await tick(); await tick(); await tick(); await tick();
        findBtn('下書きを読む')[0].click();
        await tick(); await tick();
        findBtn('この下書きを参考に、ローカルで計画を作り直す').filter(b => !b.disabled)[0].click();
        await tick(); await tick(); await tick();
        const seen = seenText();
        if (!seen.includes('置き換わります')) { throw Error('replace wording missing'); }
        if (!seen.includes('承認済みの送付内容は無効')) { throw Error('invalid wording missing'); }
        if (!seen.includes('独立評価をやり直す')) { throw Error('review wording missing'); }
        const btns = findBtn('計画を作り直す');
        if (btns.length !== 1) { throw Error('generate missing'); }
        const btn = btns[0];
        if (!btn.disabled) { throw Error('must require name+check'); }
        btn.click();
        await tick();
        if (countCalls('/mission/plan/generate') !== 0) { throw Error('posted without checks'); }
        byId('planningGuardDraftsExecutor').value = 'tester';
        byId('planningGuardDraftsExecutor').input();
        if (!btn.disabled) { throw Error('must still require check'); }
        btn.click();
        await tick();
        if (countCalls('/mission/plan/generate') !== 0) { throw Error('posted without check even via handler'); }
        byId('planningGuardDraftsCheck').checked = true;
        byId('planningGuardDraftsCheck').change();
        if (btn.disabled) { throw Error('should enable now'); }
    """))
    _node_mount(_base_scenario(
        jobs_js="[" + _draft_job() + "]",
        mission_fail=True,
        steps_js="""
        await tick(); await tick(); await tick(); await tick(); await tick();
        findBtn('下書きを読む')[0].click();
        await tick(); await tick();
        findBtn('この下書きを参考に、ローカルで計画を作り直す').filter(b => !b.disabled)[0].click();
        await tick(); await tick(); await tick();
        if (!seenText().includes('いまの計画を確認できませんでした')) { throw Error('mission fail missing'); }
        if (findBtn('計画を作り直す').length !== 0) { throw Error('must not proceed'); }
    """))
    _node_mount(_base_scenario(
        jobs_js="[" + _draft_job() + "]",
        mission_bad=True,
        steps_js="""
        await tick(); await tick(); await tick(); await tick(); await tick();
        findBtn('下書きを読む')[0].click();
        await tick(); await tick();
        findBtn('この下書きを参考に、ローカルで計画を作り直す').filter(b => !b.disabled)[0].click();
        await tick(); await tick(); await tick();
        if (!seenText().includes('いまの計画を確認できませんでした')) { throw Error('mission bad missing'); }
        if (findBtn('計画を作り直す').length !== 0) { throw Error('malformed mission must not proceed'); }
        if (seenText().includes('置き換わります')) { throw Error('confirm shown for malformed mission'); }
    """))
    _node_mount(_base_scenario(
        jobs_js="[" + _draft_job() + "]",
        mission_running=True,
        steps_js="""
        await tick(); await tick(); await tick(); await tick(); await tick();
        findBtn('下書きを読む')[0].click();
        await tick(); await tick();
        findBtn('この下書きを参考に、ローカルで計画を作り直す').filter(b => !b.disabled)[0].click();
        await tick(); await tick(); await tick();
        if (!seenText().includes('実行中は計画を作り直せません')) { throw Error('running missing'); }
        if (findBtn('計画を作り直す').length !== 0) { throw Error('must not proceed when running'); }
    """))


def test_node_generate_posts_once_with_minimal_body():
    _node_mount(_base_scenario(
        jobs_js="[" + _draft_job() + "]",
        steps_js="""
        await tick(); await tick(); await tick(); await tick(); await tick();
        findBtn('下書きを読む')[0].click();
        await tick(); await tick();
        findBtn('この下書きを参考に、ローカルで計画を作り直す').filter(b => !b.disabled)[0].click();
        await tick(); await tick(); await tick();
        byId('planningGuardDraftsExecutor').value = 'tester';
        byId('planningGuardDraftsExecutor').input();
        byId('planningGuardDraftsCheck').checked = true;
        byId('planningGuardDraftsCheck').change();
        findBtn('計画を作り直す')[0].click();
        await tick(); await tick(); await tick(); await tick();
        if (!seenText().includes('計画を作り直しました(版 3 → 4)')) { throw Error('success wording missing'); }
        const posts = sc.calls.filter(c => c[0] === 'POST' && c[1].indexOf('/mission/plan/generate') >= 0);
        if (posts.length !== 1) { throw Error('must post once'); }
        const body = JSON.parse(posts[0][2]);
        if (Object.keys(body).length !== 1 || !body.external_job_id) { throw Error('body must be external_job_id only: ' + posts[0][2]); }
    """))
    _node_mount(_base_scenario(
        jobs_js="[" + _draft_job() + "]",
        steps_js="""
        sc.genHold = true;
        await tick(); await tick(); await tick(); await tick(); await tick();
        findBtn('下書きを読む')[0].click();
        await tick(); await tick();
        findBtn('この下書きを参考に、ローカルで計画を作り直す').filter(b => !b.disabled)[0].click();
        await tick(); await tick(); await tick();
        byId('planningGuardDraftsExecutor').value = 'tester';
        byId('planningGuardDraftsExecutor').input();
        byId('planningGuardDraftsCheck').checked = true;
        byId('planningGuardDraftsCheck').change();
        const b = findBtn('計画を作り直す')[0];
        b.click(); b.click(); b.click();
        await tick();
        if (countCalls('/mission/plan/generate') !== 1) { throw Error('double submit'); }
    """))


def test_node_generate_error_words():
    cases = [
        (403, "feature_disabled", "この機能は管理者の設定で無効です"),
        (409, "busy", "実行中は計画を作り直せません。先に一時停止してください"),
        (503, "", "ローカルのAIが利用できないため、作り直せません"),
        (404, "job_not_found", "この下書きが見つかりません"),
        (422, "job_not_usable", "この下書きは使えない状態です"),
        (422, "plan_changed", "送信のあとで計画が変わったため、この下書きは使えません"),
        (422, "draft_missing", "下書きの内容がありません"),
        (422, "other_code", "作り直せませんでした"),
        (400, "unsupported_plan_path", "この計画では、外部の下書きを使った作り直しはできません"),
        (502, "", "作り直しに失敗しました"),
    ]
    for status, detail, word in cases:
        _node_mount(_base_scenario(
            jobs_js="[" + _draft_job() + "]",
            generate_status=status,
            generate_detail=detail,
            steps_js="""
        await tick(); await tick(); await tick(); await tick(); await tick();
        findBtn('下書きを読む')[0].click();
        await tick(); await tick();
        findBtn('この下書きを参考に、ローカルで計画を作り直す').filter(b => !b.disabled)[0].click();
        await tick(); await tick(); await tick();
        byId('planningGuardDraftsExecutor').value = 'tester';
        byId('planningGuardDraftsExecutor').input();
        byId('planningGuardDraftsCheck').checked = true;
        byId('planningGuardDraftsCheck').change();
        findBtn('計画を作り直す')[0].click();
        await tick(); await tick(); await tick();
        const seen = seenText();
        if (!seen.includes(%s)) { throw Error('word missing for %s: ' + seen); }
        if (seen.includes('計画を作り直しました')) { throw Error('must not show success'); }
        for (const w of ['feature_disabled','job_not_usable','plan_changed','draft_missing','invalid_job_id','unsupported_plan_path']) {
          if (seen.includes(w)) { throw Error('raw code shown: ' + w); }
        }
        """ % (json.dumps(word), status)))
    _node_mount(_base_scenario(
        jobs_js="[" + _draft_job() + "]",
        generate_mode="fail",
        steps_js="""
        await tick(); await tick(); await tick(); await tick(); await tick();
        findBtn('下書きを読む')[0].click();
        await tick(); await tick();
        findBtn('この下書きを参考に、ローカルで計画を作り直す').filter(b => !b.disabled)[0].click();
        await tick(); await tick(); await tick();
        byId('planningGuardDraftsExecutor').value = 'tester';
        byId('planningGuardDraftsExecutor').input();
        byId('planningGuardDraftsCheck').checked = true;
        byId('planningGuardDraftsCheck').change();
        findBtn('計画を作り直す')[0].click();
        await tick(); await tick(); await tick(); await tick();
        if (!seenText().includes('作り直せたか確認できませんでした')) { throw Error('unknown missing'); }
        if (seenText().includes('計画を作り直しました')) { throw Error('unknown must not show success'); }
    """))


def test_node_unknown_stays_locked_until_pj_switch():
    _node_mount(_base_scenario(
        jobs_js="[" + _draft_job() + "]",
        generate_mode="fail",
        steps_js="""
        await tick(); await tick(); await tick(); await tick(); await tick();
        findBtn('下書きを読む')[0].click();
        await tick(); await tick();
        findBtn('この下書きを参考に、ローカルで計画を作り直す').filter(b => !b.disabled)[0].click();
        await tick(); await tick(); await tick();
        byId('planningGuardDraftsExecutor').value = 'tester';
        byId('planningGuardDraftsExecutor').input();
        byId('planningGuardDraftsCheck').checked = true;
        byId('planningGuardDraftsCheck').change();
        findBtn('計画を作り直す')[0].click();
        await tick(); await tick(); await tick(); await tick();
        if (!seenText().includes('作り直せたか確認できませんでした')) { throw Error('unknown missing'); }
        findBtn('更新')[0].click();
        await tick(); await tick(); await tick(); await tick();
        findBtn('下書きを読む')[0].click();
        await tick(); await tick();
        const locked = findBtn('この下書きを参考に、ローカルで計画を作り直す');
        if (!locked.length || !locked.every(b => b.disabled)) { throw Error('must stay locked after reload'); }
        sel.value = 'pj2';
        sel.handlers.change();
        await tick(); await tick(); await tick(); await tick();
        findBtn('下書きを読む')[0].click();
        await tick(); await tick();
        const after = findBtn('この下書きを参考に、ローカルで計画を作り直す').filter(b => !b.disabled);
        if (!after.length) { throw Error('pj switch must unlock'); }
    """))
    _node_mount(_base_scenario(
        jobs_js="[" + _draft_job() + "]",
        steps_js="""
        sc.genBad = true;
        await tick(); await tick(); await tick(); await tick(); await tick();
        findBtn('下書きを読む')[0].click();
        await tick(); await tick();
        findBtn('この下書きを参考に、ローカルで計画を作り直す').filter(b => !b.disabled)[0].click();
        await tick(); await tick(); await tick();
        byId('planningGuardDraftsExecutor').value = 'tester';
        byId('planningGuardDraftsExecutor').input();
        byId('planningGuardDraftsCheck').checked = true;
        byId('planningGuardDraftsCheck').change();
        findBtn('計画を作り直す')[0].click();
        await tick(); await tick(); await tick(); await tick();
        if (!seenText().includes('作り直せたか確認できませんでした')) { throw Error('bad shape must be unknown'); }
        if (seenText().includes('計画を作り直しました')) { throw Error('bad shape must not succeed'); }
    """))


def test_node_pin_and_stale():
    _node_mount(_base_scenario(
        jobs_js="[" + _draft_job() + "]",
        steps_js="""
        await tick(); await tick(); await tick(); await tick(); await tick();
        findBtn('下書きを読む')[0].click();
        await tick(); await tick();
        findBtn('この下書きを参考に、ローカルで計画を作り直す').filter(b => !b.disabled)[0].click();
        await tick(); await tick(); await tick();
        byId('planningGuardDraftsExecutor').value = 'tester';
        byId('planningGuardDraftsExecutor').input();
        byId('planningGuardDraftsCheck').checked = true;
        byId('planningGuardDraftsCheck').change();
        sel.value = 'pj2';
        findBtn('計画を作り直す')[0].click();
        await tick();
        if (countCalls('/mission/plan/generate') !== 0) { throw Error('sent to another project'); }
        if (!seenText().includes('現在のプロジェクトが変わったため作り直しませんでした')) { throw Error('pin message missing'); }
    """))
    old_job = json.dumps({"id": "o" * 32, "pid": "pj1", "packet_id": "pkt1",
                          "provider": "OLD-PROVIDER", "purpose": "plan_generation",
                          "plan_signature": "sig1", "status": "external_generated",
                          "created_at": 1700000001, "updated_at": 1700000002,
                          "external_draft": {"tasks": [{"title": "旧工程", "description": "旧作業",
                                                        "outputs": [], "depends_on": []}]},
                          "unverified_claims": []})
    new_job = json.dumps({"id": "n" * 32, "pid": "pj1", "packet_id": "pkt1",
                          "provider": "NEW-PROVIDER", "purpose": "plan_generation",
                          "plan_signature": "sig1", "status": "external_generated",
                          "created_at": 1700000002, "updated_at": 1700000003,
                          "external_draft": {"tasks": [{"title": "新工程", "description": "新作業",
                                                        "outputs": [], "depends_on": []}]},
                          "unverified_claims": []})
    _node_mount(_base_scenario(
        jobs_js="[" + old_job + "]",
        steps_js="""
        await tick(); await tick(); await tick(); await tick(); await tick();
        if (!seenText().includes('OLD-PROVIDER')) { throw Error('old must show first'); }
        sc.jobs = [""" + new_job + """];
        sc.jobsHold = true;
        const origFetch = sc.onFetch;
        sc.onFetch = async function(url, options) {
          if (url.indexOf('/external-plan-jobs') >= 0 && this.jobsHold) {
            this.jobsHold = false;
            return {tag: '__hold'};
          }
          return origFetch.call(this, url, options);
        };
        findBtn('更新')[0].click();
        await tick();
        sel.value = 'pj2';
        sel.handlers.change();
        await tick(); await tick(); await tick(); await tick();
        const rel = sc._releases.shift();
        if (rel) { rel.release({ok: true, status: 200, json: async () => ({jobs: [""" + old_job + """]})}); }
        await tick(); await tick(); await tick();
        const seen = seenText();
        if (seen.includes('OLD-PROVIDER') && !seen.includes('NEW-PROVIDER')) { throw Error('stale list shown'); }
    """))


def test_node_injection_is_text_only():
    evil = json.dumps("<img src=x onerror=alert(1)>")
    evil_job = json.dumps({"id": json.loads(evil), "pid": "pj1",
                           "packet_id": "pkt1", "provider": json.loads(evil),
                           "purpose": "plan_generation", "plan_signature": "sig1",
                           "status": "external_generated",
                           "created_at": 1700000001, "updated_at": 1700000002,
                           "external_draft": {"tasks": [{"title": json.loads(evil),
                                                         "description": json.loads(evil),
                                                         "outputs": [], "depends_on": []}]},
                           "unverified_claims": [json.loads(evil)]})
    _node_mount(_base_scenario(
        jobs_js="[" + evil_job + "]",
        steps_js="""
        await tick(); await tick(); await tick(); await tick(); await tick();
        findBtn('下書きを読む')[0].click();
        await tick(); await tick();
        const seen = seenText();
        if (!seen.includes('<img src=x onerror=alert(1)>')) { throw Error('injection text missing'); }
        function hasImg(n) { if (n.tag === 'img') return true; for (const c of (n.children || [])) { if (hasImg(c)) return true; } return false; }
        if (hasImg(panel)) { throw Error('injection element'); }
    """))


# ---- 検証者(独立)が追加した操作の試験。ガードを外した変異で失敗することを確認済み ----
_OPEN_CONFIRM = """
        await tick(); await tick(); await tick(); await tick(); await tick();
        findBtn('下書きを読む')[0].click();
        await tick(); await tick();
        findBtn('この下書きを参考に、ローカルで計画を作り直す').filter(b => !b.disabled)[0].click();
        await tick(); await tick(); await tick();
"""


def test_node_handler_requires_name_even_when_checked():
    _node_mount(_base_scenario(
        jobs_js="[" + _draft_job() + "]",
        steps_js=_OPEN_CONFIRM + """
        byId('planningGuardDraftsCheck').checked = true;
        byId('planningGuardDraftsCheck').change();
        const btn = findBtn('計画を作り直す')[0];
        btn.click();
        await tick(); await tick();
        if (countCalls('/mission/plan/generate') !== 0) { throw Error('posted without executor name'); }
    """))


def test_node_malformed_job_list_is_unreadable():
    _node_mount(_base_scenario(
        jobs_js="[]", jobs_bad=True,
        steps_js="""
        await tick(); await tick(); await tick(); await tick(); await tick();
        const seen = seenText();
        if (!seen.includes('取得できませんでした')) { throw Error('malformed list must be unreadable'); }
        if (seen.includes('反映できる下書きはありません')) { throw Error('malformed list shown as empty'); }
    """))


def test_node_stale_job_list_is_dropped_after_pj_switch():
    old_job = json.dumps({"id": "o" * 32, "pid": "pj1", "packet_id": "pkt1",
                          "provider": "OLD-PROVIDER", "purpose": "plan_generation",
                          "plan_signature": "sig1", "status": "external_generated",
                          "created_at": 1700000001, "updated_at": 1700000002,
                          "external_draft": {"tasks": [{"title": "t", "description": "d",
                                                        "outputs": [], "depends_on": []}]},
                          "unverified_claims": []})
    _node_mount(_base_scenario(
        jobs_js="[" + old_job + "]",
        steps_js="""
        await tick(); await tick(); await tick(); await tick(); await tick();
        const origFetch = sc.onFetch;
        let held = false;
        sc.onFetch = async function(url, options) {
          if (!held && url.indexOf('/external-plan-jobs') >= 0) { held = true; return {tag: '__hold'}; }
          return origFetch.call(this, url, options);
        };
        sc.jobs = [];
        findBtn('更新')[0].click();
        await tick(); await tick(); await tick(); await tick();
        if (sc._releases.length !== 1) { throw Error('refresh request was not held: ' + sc._releases.length); }
        sel.value = 'pj2';
        sel.handlers.change();
        await tick(); await tick(); await tick(); await tick();
        sc._releases.shift().release({ok: true, status: 200, json: async () => ({jobs: [""" + old_job + """]})});
        await tick(); await tick(); await tick();
        if (seenText().includes('OLD-PROVIDER')) { throw Error('stale list shown after PJ switch'); }
    """))


def test_node_stale_generate_result_is_dropped_after_pj_switch():
    _node_mount(_base_scenario(
        jobs_js="[" + _draft_job() + "]",
        steps_js=_OPEN_CONFIRM + """
        sc.genHold = true;
        byId('planningGuardDraftsExecutor').value = 'tester';
        byId('planningGuardDraftsExecutor').input();
        byId('planningGuardDraftsCheck').checked = true;
        byId('planningGuardDraftsCheck').change();
        findBtn('計画を作り直す')[0].click();
        await tick(); await tick();
        if (sc._releases.length !== 1) { throw Error('generate request was not held'); }
        sc.genHold = false;
        sel.value = 'pj2';
        sel.handlers.change();
        await tick(); await tick(); await tick(); await tick();
        sc._releases.shift().release({ok: true, status: 200, json: async () => ({plan_version: 4, status: 'planning', tasks: []})});
        await tick(); await tick(); await tick();
        if (seenText().includes('計画を作り直しました')) { throw Error('stale generate result shown in another project'); }
    """))
