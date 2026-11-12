"""G5-b1 計画方式の変更(操作部)のテスト。疑似PJのみ・実在値なし。

Node の擬似DOM。Node が無い環境では描画試験をスキップする。
スキップは成功ではなく、静的な検査と変異の代替検査で補う。
"""
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SETTINGS_JS = (ROOT / "app/static/planning_guard_settings.js").read_text(encoding="utf-8")
PANEL_JS = (ROOT / "app/static/planning_guard_panel.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app/static/index.html").read_text(encoding="utf-8")

DANGEROUS = ("innerHTML", "insertAdjacentHTML", "outerHTML", "document.write",
             "eval", "new Function")


def test_static_no_dangerous_and_methods():
    for token in DANGEROUS:
        assert token not in SETTINGS_JS, token
    for token in ("console.log", "console.info", "console.debug",
                  "console.warn", "console.error"):
        assert token not in SETTINGS_JS, token
    for token in ("localStorage", "sessionStorage", "indexedDB", "IndexedDB"):
        assert token not in SETTINGS_JS, token
    assert "planningGuardSettingsSection" in SETTINGS_JS
    assert "計画方式の変更" in SETTINGS_JS
    assert "現在の設定を確認できないため、変更できません" in SETTINGS_JS
    assert "独立評価として数えられません" in SETTINGS_JS
    assert "既存の送信許可(承認済みの送付内容)はすべて取り消されます" in SETTINGS_JS
    compact = SETTINGS_JS.replace(" ", "").replace("\n", "").replace("\t", "")
    assert "'PUT'" in SETTINGS_JS or '"PUT"' in SETTINGS_JS
    assert "'GET'" in SETTINGS_JS or '"GET"' in SETTINGS_JS
    assert "POST" not in SETTINGS_JS
    assert "DELETE" not in SETTINGS_JS
    assert "createElement('form')" not in SETTINGS_JS
    assert 'createElement("form")' not in SETTINGS_JS
    assert "<form" not in SETTINGS_JS
    assert "http://" not in SETTINGS_JS
    assert "https://" not in SETTINGS_JS
    for frag in ("createElement('button')",
                 "createElement('select')", "createElement('input')"):
        assert frag in SETTINGS_JS, frag
    for frag in ('createElement("button")', 'createElement("select")',
                 'createElement("input")', 'createElement("form")'):
        assert frag not in SETTINGS_JS, frag
    for word in ("この計画方式では、旧来の4経路(計画評価・別AI再調整・実現手段の検討・調査)の外部送信が止まります",
                 "目標達成ゲートの計画評価も止まります",
                 "実行の開始には、独立評価の合格が必要になります",
                 "外部へ送る内容は毎回あなたの承認が必要です",
                 "旧来の外部送信の経路が再開される場合があります"):
        assert word in SETTINGS_JS, word
    for word in ("変更しました", "取り消した送信許可",
                 "実行中は変更できません。先に一時停止してください",
                 "選べない提供元です", "生成AIの提供元を選んでください",
                 "計画方式が正しくありません", "変更できない項目です",
                 "変更できませんでした",
                 "変更できたか確認できませんでした。画面を更新して現在の設定を確認してください"):
        assert word in SETTINGS_JS, word


def test_internal_words_not_shown_raw():
    for match in re.finditer(r"textContent\s*=\s*('([^']*)'|\"([^\"]*)\")", SETTINGS_JS):
        literal = match.group(2) if match.group(2) is not None else match.group(3)
        for word in ("external_guarded", "local_only",
                     "unknown_provider", "provider_required", "invalid_method",
                     "unknown_field", "unknown_code", "feature_disabled"):
            if word in (literal or ""):
                raise AssertionError((word, literal))


def test_index_tag_one_line_and_order_kept():
    panel_lines = [line for line in INDEX_HTML.splitlines()
                   if "planning_guard_panel.js" in line]
    assert len(panel_lines) == 1, panel_lines
    added = [line for line in INDEX_HTML.splitlines()
             if "planning_guard_settings.js" in line]
    assert len(added) == 1, added
    assert "v=20261031-g5b1" in added[0]
    for token in DANGEROUS:
        assert token not in added[0], token
    panel_tag = '<script src="/static/planning_guard_panel.js?v=20261030-g5a"></script>'
    settings_tag = '<script src="/static/planning_guard_settings.js?v=20261031-g5b1"></script>'
    assert INDEX_HTML.count(panel_tag) == 1
    assert INDEX_HTML.count(settings_tag) == 1
    # G5-aタグの直後に1タグだけ追加(間に他のscriptなし)。
    assert panel_tag + settings_tag in INDEX_HTML
    order = ["presenters.js", "project_mission.js", "local_models.js",
             "agent_examples.js", "procedure_learning.js", "triz_invention.js",
             "triz_general.js", "review_queue.js", "workflow_readiness.js",
             "artifact_shelf.js", "goal_review.js", "plan_repair_cards.js",
             "plan_review_loop.js", "resolution_cards.js", "project_reset.js",
             "capability_gap.js", "domain_pack.js", "goal_state.js",
             "watch_panel.js", "ledger_panel.js", "night_run_panel.js",
             "shadow_panel.js", "contact_channel_panel.js",
             "planning_guard_panel.js", "planning_guard_settings.js",
             "project_delete.js", "project_management.js", "ocr_review.js",
             "experience_import.js"]
    positions = []
    for name in order:
        matches = [i for i, line in enumerate(INDEX_HTML.splitlines()) if name in line]
        assert matches, name
        assert len(matches) == 1, (name, matches)
        positions.append(matches[0])
    assert positions == sorted(positions), positions


def test_g5a_panel_unchanged():
    assert "planningGuardPanelSection" in PANEL_JS
    assert "計画方式・外部AI送信" in PANEL_JS
    assert "planningGuardSettingsSection" not in PANEL_JS
    assert "reloadPlanningGuardPanel" in PANEL_JS


def test_js_api_paths_and_functions_used():
    import app.web as web
    routes = {str(getattr(r, "path", "")) for r in web.app.routes}
    assert "/api/projects/{project_id}/planning-settings" in routes
    assert "/static/planning_guard_settings.js" in routes
    for path in ("/api/projects/'+encodeURIComponent(pid)+'/planning-settings",):
        assert path in SETTINGS_JS, path
        assert SETTINGS_JS.count(path) >= 2, path
    for name in ("howText", "putErrorText", "unknownErrorText",
                 "isValidPayload", "sameProvider", "confirmKind",
                 "needsCheck", "fetchJson", "apiGet", "apiPut",
                 "nextTicket", "isCurrentTicket"):
        assert SETTINGS_JS.count(name) >= 2, name
    assert "let epoch=0" in SETTINGS_JS
    compact = SETTINGS_JS.replace(" ", "")
    assert "catch(e){" in compact
    assert "isCurrentTicket(ticket))return" in compact
    assert "projectSelect" in SETTINGS_JS
    assert "'planning-settings'" not in SETTINGS_JS
    assert "/planning-settings" in SETTINGS_JS


def _probe_source(cases):
    probe = [
        "const fs=require('fs');",
        "const vm=require('vm');",
        "const src=fs.readFileSync(" + json.dumps(str(ROOT / "app/static/planning_guard_settings.js")) + ",'utf8');",
        ("function mkEl(tag){"
         "const e={tag,children:[],attrs:{},handlers:{},value:'',disabled:false,"
         "textContent:'',placeholder:'',type:'checkbox',className:'',id:'',checked:false,"
         "append(...xs){xs.forEach(x=>this.children.push(x));},"
         "appendChild(x){this.children.push(x);return x;},"
         "replaceChildren(...xs){this.children=[];if(xs.length)this.append(...xs);},"
         "addEventListener(k,f){this.handlers[k]=f;},"
         "setAttribute(k,v){this.attrs[k]=v;},"
         "querySelector(){return null;},"
         "querySelectorAll(){return [];}};"
         "return e;}"),
        ("const documentStub={_byId:{},readyState:'complete',hidden:false,"
         "createElement(t){return mkEl(t);},"
         "createTextNode(t){return {textContent:String(t),children:[]};},"
         "getElementById(id){return this._byId[id]||null;},"
         "addEventListener(){},"
         "querySelector(){return null;},"
         "querySelectorAll(){return [];}};"),
        "const mod={exports:{}};",
        ("vm.runInNewContext(src,{module:mod,document:documentStub,"
         "window:{addEventListener(){},reloadPlanningGuardPanel:function(){reloadCalls.push(1);}},"
         "reloadCalls:[],"
         "fetch:async()=>{throw Error('no-net')},"
         "setTimeout:function(){return 0;},clearTimeout:function(){},"
         "setInterval:function(){return 0;},clearInterval:function(){},"
         "location:{href:'http://localhost/'},"
         "console:console});"),
        "const m=mod.exports;",
        ("function texts(e){let out=[String(e.textContent||'')];"
         "for(const c of (e.children||[])){out=out.concat(texts(c));}return out;}"),
    ]
    probe.extend(cases)
    return "\n".join(probe)


def _require_node():
    if shutil.which("node") is None:
        pytest.skip("Node.js(node)が無いため画面の実行試験を実施できない(未検証。成功ではない)")


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


def _node_probe(cases):
    problems = _probe_declaration_problems(_probe_source(cases))
    assert not problems, problems
    _require_node()
    with tempfile.NamedTemporaryFile("w", suffix=".cjs", delete=False, encoding="utf-8") as tmp:
        tmp.write(_probe_source(cases))
        probe_path = tmp.name
    try:
        completed = subprocess.run(["node", probe_path], capture_output=True,
                                   text=True, timeout=30)
    finally:
        Path(probe_path).unlink(missing_ok=True)
    assert completed.returncode == 0, completed.stderr


def test_pure_functions_with_node():
    cases = [
        "if(m.howText('local_only')!=='ローカル計画方式（外部送信の可否は別設定）'){console.error('how');process.exit(1);}",
        "if(m.howText('external_guarded')!=='外部AIあり(承認・検査つき)'){console.error('how2');process.exit(1);}",
        "if(m.howText('nope')!=='確認できず'){console.error('how3');process.exit(1);}",
        "if(m.howText('')!=='確認できず'){console.error('how4');process.exit(1);}",
        "if(m.putErrorText(409,'x')!=='実行中は変更できません。先に一時停止してください'){console.error('c409');process.exit(1);}",
        "if(m.putErrorText(422,'unknown_provider')!=='選べない提供元です'){console.error('d1');process.exit(1);}",
        "if(m.putErrorText(422,'provider_required')!=='生成AIの提供元を選んでください'){console.error('d2');process.exit(1);}",
        "if(m.putErrorText(422,'invalid_method')!=='計画方式が正しくありません'){console.error('d3');process.exit(1);}",
        "if(m.putErrorText(422,'unknown_field')!=='変更できない項目です'){console.error('d4');process.exit(1);}",
        "if(m.putErrorText(422,'other_x')!=='変更できませんでした'){console.error('d5');process.exit(1);}",
        "if(m.putErrorText(422,'')!=='変更できませんでした'){console.error('d6');process.exit(1);}",
        "if(m.putErrorText(500,'x')!=='変更できたか確認できませんでした。画面を更新して現在の設定を確認してください'){console.error('net');process.exit(1);}",
        "if(m.putErrorText(0,'x')!=='変更できたか確認できませんでした。画面を更新して現在の設定を確認してください'){console.error('net2');process.exit(1);}",
        "if(m.unknownErrorText()!=='変更できたか確認できませんでした。画面を更新して現在の設定を確認してください'){console.error('unk');process.exit(1);}",
        "if(!m.isValidPayload({settings:{method:'local_only',generator_provider:'',reviewer_provider:''},known_providers:[]})){console.error('valid');process.exit(1);}",
        "if(m.isValidPayload({settings:{method:'nope',generator_provider:'',reviewer_provider:''},known_providers:[]})){console.error('invalid');process.exit(1);}",
        "if(m.isValidPayload(null)){console.error('null valid');process.exit(1);}",
        "if(m.isValidPayload({settings:{method:'local_only',generator_provider:'',reviewer_provider:''},known_providers:'x'})){console.error('known');process.exit(1);}",
        "if(m.isValidPayload({settings:{method:'local_only',generator_provider:'',reviewer_provider:''}})){console.error('known missing');process.exit(1);}",
        "if(m.isValidPayload({settings:{method:'local_only',generator_provider:1,reviewer_provider:''},known_providers:[]})){console.error('gen type');process.exit(1);}",
        "if(!m.sameProvider('a','a')){console.error('same');process.exit(1);}",
        "if(m.sameProvider('a','b')){console.error('diff');process.exit(1);}",
        "if(m.sameProvider('','')){console.error('empty same');process.exit(1);}",
        "if(m.sameProvider('',undefined)){console.error('empty undef');process.exit(1);}",
        "if(m.confirmKind('local_only','external_guarded')!=='to_external'){console.error('kind1');process.exit(1);}",
        "if(m.confirmKind('external_guarded','local_only')!=='to_local'){console.error('kind2');process.exit(1);}",
        "if(m.confirmKind('local_only','local_only')!=='providers'){console.error('kind3');process.exit(1);}",
        "if(!m.needsCheck('to_external')){console.error('chk1');process.exit(1);}",
        "if(!m.needsCheck('to_local')){console.error('chk2');process.exit(1);}",
        "if(m.needsCheck('providers')){console.error('chk3');process.exit(1);}",
        "{const a=m.nextTicket();const b=m.nextTicket();if(!m.isCurrentTicket(b)){console.error('ticket cur');process.exit(1);}if(m.isCurrentTicket(a)){console.error('ticket stale');process.exit(1);}}",
    ]
    _node_probe(cases)


def test_node_probe_sources_are_well_formed():
    problems = _probe_declaration_problems(_probe_source(["const a=1;\nconst a=2;"]))
    assert problems
    problems = _probe_declaration_problems(_probe_source(["if(true){"]))
    assert problems
    for name, cases in (("pure", ["const a=1;"]),):
        assert _probe_declaration_problems(_probe_source(cases)) == [], (name,)


def _full_mount_source(scenario_js):
    return "\n".join([
        "const fs=require('fs');",
        "const vm=require('vm');",
        "const src=fs.readFileSync(" + json.dumps(str(ROOT / "app/static/planning_guard_settings.js")) + ",'utf8');",
        "let fetchCalls=[];",
        "let scenario=" + scenario_js + ";",
        ("function mkEl(tag){"
         "const e={tag,children:[],attrs:{},handlers:{},value:'',disabled:false,"
         "textContent:'',placeholder:'',type:'',className:'',id:'',checked:false,"
         "append(...xs){xs.forEach(x=>this.children.push(x));},"
         "appendChild(x){this.children.push(x);return x;},"
         "replaceChildren(...xs){this.children=[];if(xs.length)this.append(...xs);},"
         "addEventListener(k,f){this.handlers[k]=f;},"
         "click(){if(this.handlers.click)this.handlers.click();},"
         "change(){if(this.handlers.change)this.handlers.change();},"
         "setAttribute(k,v){this.attrs[k]=v;},"
         "querySelector(){return null;},"
         "querySelectorAll(){return [];}};"
         "return e;}"),
        ("const ids={};"
         "const panel=mkEl('section');"
         "ids.planningGuardPanelSection=panel;"
         "const sel=mkEl('select');sel.value=scenario.startPid;ids.projectSelect=sel;"
         "const documentStub={_byId:ids,readyState:'complete',hidden:false,"
         "createElement(t){return mkEl(t);},"
         "createTextNode(t){const n=mkEl('span');n.textContent=String(t);return n;},"
         "getElementById(id){return this._byId[id]||null;},"
         "addEventListener(){},"
         "querySelector(){return null;},querySelectorAll(){return [];}};"),
        "const mod={exports:{}};",
        "const sandbox={module:mod,document:documentStub,window:{addEventListener(){}},location:{href:'http://localhost/'},console:console};",
        "sandbox.scenario=scenario;",
        "sandbox.fetch=async function(url,options){return scenario.onFetch(url,options||{});};",
        ("sandbox.setTimeout=function(f){f();return 0;};"
         "sandbox.clearTimeout=function(){};"
         "sandbox.setInterval=function(){return 0;};"
         "sandbox.clearInterval=function(){};"),
        "sandbox.window.reloadPlanningGuardPanel=function(){sandbox.reloaded=(sandbox.reloaded||0)+1;};",
        "vm.createContext(sandbox);",
        "vm.runInContext(src,sandbox);",
        ("function texts(e){let out=[String(e.textContent||'')];"
         "for(const c of (e.children||[])){out=out.concat(texts(c));}return out;}"),
        ("function byId(id){"
         "function walk(n){if(n.id===id)return n;for(const c of (n.children||[])){const f=walk(c);if(f)return f;}return null;}"
         "return walk(panel);}"),
        "let done=async()=>{};",
        "done=(async()=>{",
        "await new Promise(r=>setImmediate(r));",
        "await new Promise(r=>setImmediate(r));",
        "await new Promise(r=>setImmediate(r));",
        "await new Promise(r=>setImmediate(r));",
        "await scenario.steps({panel,texts,byId,sel,documentStub,sandbox});",
        "})();",
        ("done.then(()=>process.exit(0)).catch(e=>{console.error(e&&e.stack||String(e));process.exit(1);});"),
    ])


def _node_mount(scenario_js):
    problems = _probe_declaration_problems(_full_mount_source(scenario_js))
    assert not problems, problems
    if shutil.which("node") is None:
        pytest.skip("Node.js(node)が無いため画面の実行試験を実施できない(未検証。成功ではない)")
    with tempfile.NamedTemporaryFile("w", suffix=".cjs", delete=False, encoding="utf-8") as tmp:
        tmp.write(_full_mount_source(scenario_js))
        probe_path = tmp.name
    try:
        completed = subprocess.run(["node", probe_path], capture_output=True,
                                   text=True, timeout=30)
    finally:
        Path(probe_path).unlink(missing_ok=True)
    assert completed.returncode == 0, completed.stderr + completed.stdout


def test_settings_panel_static_route_serves_js():
    from fastapi.testclient import TestClient
    import app.web as web
    client = TestClient(web.app, raise_server_exceptions=False)
    response = client.get("/static/planning_guard_settings.js")
    assert response.status_code == 200
    assert "planningGuardSettingsSection" in response.text
    assert response.headers.get("content-type", "").startswith("text/javascript")


def test_mount_fetch_failure_hides_controls_with_node():
    _node_mount(r"""({
      startPid: 'pj1',
      onFetch: async (url, options) => {
        fetchCalls.push([url, (options||{}).method || 'GET']);
        return {ok: false, status: 500, json: async () => ({detail: 'x'})};
      },
      steps: async ({panel, texts, byId}) => {
        const seen = texts(panel).join('\n');
        if (!seen.includes('現在の設定を確認できないため、変更できません')) { throw Error('fail message missing: ' + seen); }
        if (byId('planningGuardSettingsMethod') || byId('planningGuardSettingsGenerator') || byId('planningGuardSettingsChange')) {
          throw Error('controls must be hidden on fetch failure');
        }
      }
    })""")


def test_mount_same_provider_warning_and_guard_with_node():
    _node_mount(r"""({
      startPid: 'pj1',
      onFetch: async (url, options) => {
        fetchCalls.push([url, (options||{}).method || 'GET']);
        if ((options||{}).method === 'GET' || !(options||{}).method) {
          return {ok: true, status: 200, json: async () => ({settings: {method: 'local_only', generator_provider: 'kp', reviewer_provider: 'kp'}, known_providers: ['kp', 'kq']})};
        }
        throw Error('PUT must not be called');
      },
      steps: async ({panel, texts, byId}) => {
        const seen = texts(panel).join('\n');
        if (!seen.includes('独立評価として数えられません')) { throw Error('same provider warning missing'); }
        const gen = byId('planningGuardSettingsGenerator');
        const rev = byId('planningGuardSettingsReviewer');
        const method = byId('planningGuardSettingsMethod');
        if (!gen || !rev || !method) { throw Error('selects missing'); }
        const opts = gen.children.filter(c => c.tag === 'option').map(c => String(c.value));
        if (opts.join(',') !== ',kp,kq') { throw Error('providers must come from known_providers only: ' + opts.join(',')); }
        method.value = 'external_guarded';
        gen.value = '';
        gen.change();
        const change = byId('planningGuardSettingsChange');
        if (!change.disabled) { throw Error('external_guarded without generator must disable change'); }
        const need = texts(byId('planningGuardSettingsNeedGen')).join('\n');
        if (!need.includes('生成AIの提供元を選んでください')) { throw Error('generator required note missing'); }
      }
    })""")


def test_mount_two_step_confirm_to_external_with_node():
    _node_mount(r"""({
      startPid: 'pj1',
      onFetch: async (url, options) => {
        fetchCalls.push([url, (options||{}).method || 'GET']);
        if ((options||{}).method === 'PUT') {
          const body = JSON.parse(options.body);
          if (body.method !== 'external_guarded' || body.generator_provider !== 'kp' || body.reviewer_provider !== 'kq') {
            return {ok: false, status: 422, json: async () => ({detail: 'unknown_field'})};
          }
          return {ok: true, status: 200, json: async () => ({settings: body, revoked: ['plan_generation', 'plan_review'], independent_review: true})};
        }
        return {ok: true, status: 200, json: async () => ({settings: {method: 'local_only', generator_provider: '', reviewer_provider: ''}, known_providers: ['kp', 'kq']})};
      },
      steps: async ({panel, texts, byId, sandbox}) => {
        const method = byId('planningGuardSettingsMethod');
        const gen = byId('planningGuardSettingsGenerator');
        method.value = 'external_guarded'; method.change();
        gen.value = 'kp'; gen.change();
        const rev = byId('planningGuardSettingsReviewer');
        rev.value = 'kq'; rev.change();
        byId('planningGuardSettingsChange').click();
        await new Promise(r => setImmediate(r));
        const putsBefore = fetchCalls.filter(c => c[1] === 'PUT').length;
        if (putsBefore !== 0) { throw Error('PUT must not be called before confirm'); }
        let seen = texts(panel).join('\n');
        for (const w of ['旧来の4経路', '目標達成ゲートの計画評価も止まります', '独立評価の合格が必要', '毎回あなたの承認が必要です']) {
          if (!seen.includes(w)) { throw Error('confirm text missing: ' + w); }
        }
        const commit = byId('planningGuardSettingsCommit');
        const check = byId('planningGuardSettingsCheck');
        if (!commit || !check) { throw Error('confirm controls missing'); }
        if (!commit.disabled) { throw Error('commit must require check'); }
        commit.click();
        await new Promise(r => setImmediate(r));
        if (fetchCalls.filter(c => c[1] === 'PUT').length !== 0) { throw Error('PUT must not bypass check'); }
        check.checked = true; check.change();
        if (commit.disabled) { throw Error('commit must enable after check'); }
        commit.click();
        await new Promise(r => setImmediate(r));
        await new Promise(r => setImmediate(r));
        if (fetchCalls.filter(c => c[1] === 'PUT').length !== 1) { throw Error('PUT must be called once'); }
        seen = texts(panel).join('\n');
        if (!seen.includes('変更しました')) { throw Error('success missing: ' + seen); }
        if (!seen.includes('2件')) { throw Error('revoked count missing: ' + seen); }
        if ((sandbox.reloaded || 0) !== 1) { throw Error('guard panel reload missing'); }
        byId('planningGuardSettingsCancel');
      }
    })""")


def test_mount_cancel_and_to_local_with_node():
    _node_mount(r"""({
      startPid: 'pj1',
      onFetch: async (url, options) => {
        fetchCalls.push([url, (options||{}).method || 'GET']);
        if ((options||{}).method === 'PUT') {
          return {ok: true, status: 200, json: async () => ({settings: JSON.parse(options.body), revoked: []})};
        }
        return {ok: true, status: 200, json: async () => ({settings: {method: 'external_guarded', generator_provider: 'kp', reviewer_provider: 'kq'}, known_providers: ['kp', 'kq']})};
      },
      steps: async ({panel, texts, byId}) => {
        byId('planningGuardSettingsChange').click();
        await new Promise(r => setImmediate(r));
        if (fetchCalls.filter(c => c[1] === 'PUT').length !== 0) { throw Error('PUT before confirm'); }
        byId('planningGuardSettingsCancel').click();
        await new Promise(r => setImmediate(r));
        if (fetchCalls.filter(c => c[1] === 'PUT').length !== 0) { throw Error('PUT after cancel'); }
        const method = byId('planningGuardSettingsMethod');
        method.value = 'local_only'; method.change();
        byId('planningGuardSettingsChange').click();
        await new Promise(r => setImmediate(r));
        const seen = texts(panel).join('\n');
        if (!seen.includes('旧来の外部送信の経路が再開される場合があります')) { throw Error('to_local note missing'); }
        const commit = byId('planningGuardSettingsCommit');
        if (!commit.disabled) { throw Error('to_local must require check'); }
        byId('planningGuardSettingsCheck').checked = true;
        byId('planningGuardSettingsCheck').change();
        commit.click();
        await new Promise(r => setImmediate(r));
        await new Promise(r => setImmediate(r));
        if (fetchCalls.filter(c => c[1] === 'PUT').length !== 1) { throw Error('PUT once'); }
      }
    })""")


def test_mount_error_texts_with_node():
    for code in ['unknown_provider', 'provider_required', 'invalid_method', 'unknown_field', 'other_x']:
        _node_mount(r"""({
          startPid: 'pj1',
          code: """ + json.dumps(code) + r""",
          onFetch: async function(url, options) {
            if ((options||{}).method === 'PUT') {
              return {ok: false, status: 422, json: async () => ({detail: this.code})};
            }
            return {ok: true, status: 200, json: async () => ({settings: {method: 'local_only', generator_provider: 'kp', reviewer_provider: ''}, known_providers: ['kp']})};
          },
          steps: async ({panel, texts, byId}) => {
            byId('planningGuardSettingsChange').click();
            await new Promise(r => setImmediate(r));
            byId('planningGuardSettingsCommit').click();
            await new Promise(r => setImmediate(r));
            await new Promise(r => setImmediate(r));
            const seen = texts(panel).join('\n');
            if (seen.includes('unknown_provider') || seen.includes('provider_required') || seen.includes('invalid_method') || seen.includes('unknown_field')) {
              throw Error('raw code shown');
            }
            if (seen.includes('変更しました')) { throw Error('failure shown as success'); }
          }
        })""")
    _node_mount(r"""({
      startPid: 'pj1',
      onFetch: async (url, options) => {
        if ((options||{}).method === 'PUT') {
          return {ok: false, status: 409, json: async () => ({detail: 'x'})};
        }
        return {ok: true, status: 200, json: async () => ({settings: {method: 'local_only', generator_provider: 'kp', reviewer_provider: ''}, known_providers: ['kp']})};
      },
      steps: async ({panel, texts, byId}) => {
        byId('planningGuardSettingsChange').click();
        await new Promise(r => setImmediate(r));
        byId('planningGuardSettingsCommit').click();
        await new Promise(r => setImmediate(r));
        await new Promise(r => setImmediate(r));
        const seen = texts(panel).join('\n');
        if (!seen.includes('実行中は変更できません。先に一時停止してください')) { throw Error('409 text missing'); }
        if (seen.includes('変更しました')) { throw Error('failure as success'); }
      }
    })""")
    _node_mount(r"""({
      startPid: 'pj1',
      onFetch: async (url, options) => {
        if ((options||{}).method === 'PUT') { throw Error('net down'); }
        return {ok: true, status: 200, json: async () => ({settings: {method: 'local_only', generator_provider: 'kp', reviewer_provider: ''}, known_providers: ['kp']})};
      },
      steps: async ({panel, texts, byId}) => {
        byId('planningGuardSettingsChange').click();
        await new Promise(r => setImmediate(r));
        byId('planningGuardSettingsCommit').click();
        await new Promise(r => setImmediate(r));
        await new Promise(r => setImmediate(r));
        const seen = texts(panel).join('\n');
        if (!seen.includes('変更できたか確認できませんでした')) { throw Error('unknown failure text missing'); }
        if (seen.includes('変更しました')) { throw Error('failure as success'); }
      }
    })""")


def test_mount_double_submit_is_single_put_with_node():
    _node_mount(r"""({
      startPid: 'pj1',
      onFetch: async (url, options) => {
        fetchCalls.push([url, (options||{}).method || 'GET']);
        if ((options||{}).method === 'PUT') {
          await new Promise(r => setTimeout(r, 5));
          return {ok: true, status: 200, json: async () => ({settings: JSON.parse(options.body), revoked: []})};
        }
        return {ok: true, status: 200, json: async () => ({settings: {method: 'local_only', generator_provider: 'kp', reviewer_provider: ''}, known_providers: ['kp']})};
      },
      steps: async ({panel, texts, byId}) => {
        byId('planningGuardSettingsChange').click();
        await new Promise(r => setImmediate(r));
        const commit = byId('planningGuardSettingsCommit');
        commit.click(); commit.click(); commit.click();
        await new Promise(r => setImmediate(r));
        await new Promise(r => setImmediate(r));
        await new Promise(r => setImmediate(r));
        if (fetchCalls.filter(c => c[1] === 'PUT').length !== 1) { throw Error('double submit'); }
      }
    })""")


def test_mount_pj_switch_drops_stale_response_with_node():
    _node_mount(r"""({
      startPid: 'pjA',
      onFetch: async function(url, options) {
        const pid = decodeURIComponent(url.split('/')[3]);
        if ((options||{}).method === 'PUT') {
          return {ok: true, status: 200, json: async () => ({settings: {method: 'local_only', generator_provider: pid + '-gen', reviewer_provider: ''}, revoked: []})};
        }
        if (pid === 'pjA') {
          return new Promise(res => { this.hold = () => res({ok: true, status: 200, json: async () => ({settings: {method: 'local_only', generator_provider: 'pjA-gen', reviewer_provider: ''}, known_providers: []})}); });
        }
        return {ok: true, status: 200, json: async () => ({settings: {method: 'local_only', generator_provider: 'pjB-gen', reviewer_provider: ''}, known_providers: []})};
      },
      steps: async ({panel, texts, byId, sel, sandbox}) => {
        await new Promise(r => setImmediate(r));
        sel.value = 'pjB';
        sel.handlers.change();
        await new Promise(r => setImmediate(r));
        await new Promise(r => setImmediate(r));
        sandbox.scenario.hold();
        await new Promise(r => setImmediate(r));
        await new Promise(r => setImmediate(r));
        const seen = texts(panel).join('\n');
        if (seen.includes('pjA-gen')) { throw Error('stale shown'); }
      }
    })""")


def test_mount_pj_switch_drops_stale_save_with_node():
    _node_mount(r"""({
      startPid: 'pjA',
      onFetch: function(url, options) {
        const pid = decodeURIComponent(url.split('/')[3]);
        if ((options||{}).method === 'PUT') {
          return new Promise(res => { this.holdPut = (v) => res(v); });
        }
        return {ok: true, status: 200, json: async () => ({settings: {method: 'local_only', generator_provider: pid + '-gen', reviewer_provider: ''}, known_providers: [pid + '-gen']})};
      },
      steps: async ({panel, texts, byId, sel, sandbox}) => {
        await new Promise(r => setImmediate(r));
        byId('planningGuardSettingsChange').click();
        await new Promise(r => setImmediate(r));
        byId('planningGuardSettingsCommit').click();
        await new Promise(r => setImmediate(r));
        sel.value = 'pjB';
        sel.handlers.change();
        await new Promise(r => setImmediate(r));
        await new Promise(r => setImmediate(r));
        sandbox.scenario.holdPut({ok: true, status: 200, json: async () => ({settings: {method: 'local_only', generator_provider: 'pjA-gen', reviewer_provider: ''}, revoked: []})});
        await new Promise(r => setImmediate(r));
        await new Promise(r => setImmediate(r));
        const seen = texts(panel).join('\n');
        if (seen.includes('変更しました')) { throw Error('stale save shown as success'); }
        if (seen.includes('pjA-gen')) { throw Error('stale save data shown'); }
      }
    })""")


def test_mount_injection_is_text_with_node():
    _node_mount(r"""({
      startPid: 'pj1',
      onFetch: async (url, options) => {
        if ((options||{}).method === 'PUT') {
          return {ok: true, status: 200, json: async () => ({settings: JSON.parse(options.body), revoked: 'x'})};
        }
        return {ok: true, status: 200, json: async () => ({settings: {method: 'local_only', generator_provider: '<img src=x onerror=alert(1)>', reviewer_provider: ''}, known_providers: ['<img src=x onerror=alert(1)>']})};
      },
      steps: async ({panel, texts, byId}) => {
        const seen = texts(panel).join('\n');
        if (!seen.includes('<img src=x onerror=alert(1)>')) { throw Error('injection text missing'); }
        function hasImg(n) { if (n.tag === 'img') return true; for (const c of (n.children || [])) { if (hasImg(c)) return true; } return false; }
        if (hasImg(panel)) { throw Error('injection element'); }
        byId('planningGuardSettingsChange').click();
        await new Promise(r => setImmediate(r));
        byId('planningGuardSettingsCommit').click();
        await new Promise(r => setImmediate(r));
        await new Promise(r => setImmediate(r));
        const seen2 = texts(panel).join('\n');
        if (!seen2.includes('変更できたか確認できませんでした')) { throw Error('malformed response must be unknown'); }
        if (seen2.includes('変更しました')) { throw Error('malformed response shown as success'); }
      }
    })""")


def _mutated_source(mutator):
    source = SETTINGS_JS
    mutated = mutator(source)
    assert mutated != source
    return mutated


def test_mutation_check_required_is_guarded():
    mutated = _mutated_source(lambda s: s.replace("commitBtn.disabled=true;", "commitBtn.disabled=false;"))
    assert "commitBtn.disabled=true;" in SETTINGS_JS
    assert "commitBtn.disabled=true;" not in mutated
    mutated = _mutated_source(lambda s: s.replace("checkBox.checked", "true"))
    assert "checkBox.checked" in SETTINGS_JS
    assert mutated.count("true") > SETTINGS_JS.count("true")


def test_mutation_failure_not_success_is_guarded():
    mutated = _mutated_source(lambda s: s.replace(
        "変更できたか確認できませんでした。画面を更新して現在の設定を確認してください",
        "変更しました"))
    assert "変更できたか確認できませんでした" in SETTINGS_JS
    assert "変更できたか確認できませんでした" not in mutated


def test_mutation_single_put_is_guarded():
    assert "if(saving)return;" in SETTINGS_JS.replace(" ", "")
    mutated = _mutated_source(lambda s: s.replace("if(saving)return;", "if(false)return;"))
    assert "if(false)return;" in mutated


def test_mutation_stale_drop_is_guarded():
    assert "isCurrentTicket(ticket))return" in SETTINGS_JS.replace(" ", "")
    mutated = _mutated_source(lambda s: s.replace(
        "if(!isCurrentTicket(ticket))return;", "if(false)return;"))
    assert "if(false)return;" in mutated


def test_change_scope_only_expected_files():
    import app.web as web
    routes = {str(getattr(r, "path", "")) for r in web.app.routes}
    assert "/static/planning_guard_settings.js" in routes
    assert "/static/planning_guard_panel.js" in routes
    before = (ROOT / "app/static/planning_guard_panel.js").read_text(encoding="utf-8")
    assert "planningGuardSettingsSection" not in before
    for name in ("project_mission.js", "goal_state.js", "watch_panel.js",
                 "ledger_panel.js", "night_run_panel.js", "shadow_panel.js",
                 "workflow_readiness.js", "contact_channel_panel.js"):
        text = (ROOT / "app/static" / name).read_text(encoding="utf-8")
        assert "planningGuardSettingsSection" not in text, name
        assert "planning_guard_settings" not in text, name
    web_text = (ROOT / "app/web.py").read_text(encoding="utf-8")
    assert web_text.count("@app.get('/static/planning_guard_settings.js')") == 1
    assert web_text.count("@app.get('/static/planning_guard_panel.js')") == 1
