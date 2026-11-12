"""G5-a 計画方式・外部AI送信パネルのテスト。疑似PJのみ・実在値なし。"""
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PANEL_JS = (ROOT / "app/static/planning_guard_panel.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
MISSION_JS = (ROOT / "app/static/project_mission.js").read_text(encoding="utf-8")

DANGEROUS = ("innerHTML", "insertAdjacentHTML", "outerHTML", "document.write",
             "eval", "new Function")


def test_static_no_dangerous_and_get_only():
    for token in DANGEROUS:
        assert token not in PANEL_JS, token
    assert PANEL_JS.count("innerHTML") == 0
    for token in ("console.log", "console.info", "console.debug",
                  "console.warn", "console.error"):
        assert token not in PANEL_JS, token
    for token in ("localStorage", "sessionStorage", "indexedDB", "IndexedDB"):
        assert token not in PANEL_JS, token
    compact = PANEL_JS.replace(" ", "").replace("\n", "").replace("\t", "")
    assert "method:" not in compact
    assert "POST" not in PANEL_JS
    assert "PUT" not in PANEL_JS
    assert "DELETE" not in PANEL_JS
    for frag in ("createElement('button')", 'createElement("button")',
                 "createElement('input')", 'createElement("input")',
                 "createElement('form')", 'createElement("form")',
                 "<button", "<input", "<form"):
        assert frag not in PANEL_JS, frag
    assert "planningGuardPanelSection" in PANEL_JS
    assert "計画方式・外部AI送信" in PANEL_JS
    assert "http://" not in PANEL_JS
    assert "https://" not in PANEL_JS
    for word in ("計画方式：", "独立性：", "選べる提供元",
                 "この機能は管理者の設定で無効です",
                 "合格は実行の許可ではありません。人の承認と既存のゲートが別に必要です",
                 "ジョブなし", "取得できませんでした", "確認できず",
                 "開始できます", "開始できません"):
        assert word in PANEL_JS, word


def test_index_tag_one_line_and_order_kept():
    added = [line for line in INDEX_HTML.splitlines()
             if "planning_guard_panel.js" in line]
    assert len(added) == 1, added
    assert "v=20261030-g5a" in added[0]
    for token in DANGEROUS:
        assert token not in added[0], token
    order = ["presenters.js", "project_mission.js", "local_models.js",
             "agent_examples.js", "procedure_learning.js", "triz_invention.js",
             "triz_general.js", "review_queue.js", "workflow_readiness.js",
             "artifact_shelf.js", "goal_review.js", "plan_repair_cards.js",
             "plan_review_loop.js", "resolution_cards.js", "project_reset.js",
             "capability_gap.js", "domain_pack.js", "goal_state.js",
             "watch_panel.js", "ledger_panel.js", "night_run_panel.js",
             "shadow_panel.js", "contact_channel_panel.js",
             "planning_guard_panel.js",
             "project_delete.js", "project_management.js", "ocr_review.js",
             "experience_import.js"]
    positions = []
    for name in order:
        matches = [i for i, line in enumerate(INDEX_HTML.splitlines()) if name in line]
        assert matches, name
        positions.append(matches[0])
    assert positions == sorted(positions), positions
    assert MISSION_JS.count("innerHTML") == 28


def test_internal_words_not_shown_raw():
    for match in re.finditer(r"textContent\s*=\s*('([^']*)'|\"([^\"]*)\")", PANEL_JS):
        literal = match.group(2) if match.group(2) is not None else match.group(3)
        for word in ("external_guarded", "local_only",
                     "independent_review_missing", "independent_review_failed",
                     "independent_review_unknown",
                     "independent_review_check_error", "feature_disabled"):
            if word in (literal or ""):
                raise AssertionError((word, literal))


def test_js_api_paths_and_functions_used():
    import app.web as web
    routes = {str(getattr(r, "path", "")) for r in web.app.routes}
    assert "/api/projects/{project_id}/planning-settings" in routes
    assert "/api/projects/{project_id}/workflow-readiness" in routes
    assert "/api/projects/{project_id}/external-plan-jobs" in routes
    assert "/api/projects/{project_id}/independent-review-jobs" in routes
    assert "/api/projects/{project_id}/plan-packets/audit" in routes
    for path in ("/api/projects/'+encodeURIComponent(pid)+'/planning-settings",
                 "/api/projects/'+encodeURIComponent(pid)+'/workflow-readiness",
                 "/api/projects/'+encodeURIComponent(pid)+'/external-plan-jobs",
                 "/api/projects/'+encodeURIComponent(pid)+'/independent-review-jobs",
                 "/api/projects/'+encodeURIComponent(pid)+'/plan-packets/audit"):
        assert path in PANEL_JS, path
    for name in ("howText", "aloneText", "reasonText", "planJobText",
                 "reviewText", "renderHow", "renderReadiness",
                 "renderPlanJobs", "renderReviewJobs", "renderAudit",
                 "apiHow", "apiReadiness", "apiPlanJobs", "apiReviewJobs",
                 "apiAudit", "nextTicket", "isCurrentTicket", "shouldFetch"):
        assert PANEL_JS.count(name) >= 2, name
    assert "let epoch=0" in PANEL_JS
    compact = PANEL_JS.replace(" ", "")
    assert "catch(e){" in compact
    assert "alive())return" in compact
    assert "hiddenNow" in PANEL_JS or "document.hidden" in PANEL_JS
    assert "projectSelect" in PANEL_JS
    assert "visibilitychange" in PANEL_JS
    assert "setInterval" in PANEL_JS


def _probe_source(cases):
    probe = [
        "const fs=require('fs');",
        "const vm=require('vm');",
        "const src=fs.readFileSync(" + json.dumps(str(ROOT / "app/static/planning_guard_panel.js")) + ",'utf8');",
        ("function mkEl(tag){"
         "const e={tag,children:[],attrs:{},handlers:{},value:'',disabled:false,"
         "textContent:'',placeholder:'',type:'',className:'',id:'',"
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
         "window:{addEventListener(){},confirm:function(){return true;}},"
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
                problems.append(f"括弧の対応が不正: 位置{i}の'{ch}'")
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
            problems.append(f"トップレベルの宣言が重複: {name}({count}回)")
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


def test_panel_pure_functions_with_node():
    cases = [
        "if(m.howText('local_only')!=='ローカル計画方式（外部送信の可否は別設定）'){console.error('how');process.exit(1);}",
        "if(m.howText('external_guarded')!=='外部AIあり(承認・検査つき)'){console.error('how2');process.exit(1);}",
        "if(m.howText('nope')!=='確認できず'){console.error('how3');process.exit(1);}",
        "if(m.howText('')!=='確認できず'){console.error('how4');process.exit(1);}",
        "if(m.aloneText(true)!=='独立評価として数えられる'){console.error('alone');process.exit(1);}",
        "if(m.aloneText(false)!=='独立と数えられない'){console.error('alone2');process.exit(1);}",
        "if(m.aloneText(null)!=='確認できず'){console.error('alone3');process.exit(1);}",
        "if(m.reasonText('xx independent_review_missing yy')!=='独立評価がまだありません'){console.error('reason');process.exit(1);}",
        "if(m.reasonText('independent_review_failed')!=='独立評価が不合格です'){console.error('reason2');process.exit(1);}",
        "if(m.reasonText('independent_review_unknown')!=='独立評価の結果が不明です(合格ではありません)'){console.error('reason3');process.exit(1);}",
        "if(m.reasonText('independent_review_check_error')!=='方式または評価の確認に失敗しました(安全のため止めています)'){console.error('reason4');process.exit(1);}",
        "if(m.categoryText('missing_goal')!=='目標の不足'){console.error('category');process.exit(1);}",
        "if(m.purposeText('plan_review')!=='計画の評価'){console.error('purpose');process.exit(1);}",
        "if(m.reasonText('unknown_code')!=='確認できず'){console.error('reason unknown');process.exit(1);}",
        "if(m.planJobText('sending')!=='送信中'){console.error('plan');process.exit(1);}",
        "if(m.planJobText('nope')!=='確認できず'){console.error('plan2');process.exit(1);}",
        "if(m.reviewText('review_passed')!=='合格'){console.error('rev');process.exit(1);}",
        "if(m.reviewText('review_failed')!=='不合格'){console.error('rev2');process.exit(1);}",
        "if(m.reviewText('unknown_verdict')!=='判定不能'){console.error('rev3');process.exit(1);}",
        "if(m.reviewText('not_independent')!=='独立と認められず'){console.error('rev4');process.exit(1);}",
        "if(m.reviewText('rejected_response')!=='応答を拒否'){console.error('rev5');process.exit(1);}",
        "if(m.reviewText('connection_failed')!=='接続失敗'){console.error('rev6');process.exit(1);}",
        "if(m.reviewText('failed')!=='失敗'){console.error('rev7');process.exit(1);}",
        "if(m.reviewText('outcome_unknown')!=='結果不明'){console.error('rev8');process.exit(1);}",
        "if(m.reviewText('sending')!=='送信中'){console.error('rev9');process.exit(1);}",
        "if(m.reviewText('cancelled')!=='取消'){console.error('rev10');process.exit(1);}",
        "if(m.reviewText('nope')!=='確認できず'){console.error('rev11');process.exit(1);}",
        "if(m.PASS_NOTE!=='合格は実行の許可ではありません。人の承認と既存のゲートが別に必要です'){console.error('note');process.exit(1);}",
    ]
    _node_probe(cases)


def _render_cases():
    return [
        "function mkHost(){return {children:[],textContent:'',append(x){this.children.push(x);},replaceChildren(...xs){this.children=[];xs.forEach(x=>this.children.push(x));}};}",
        "const BAD=['external_guarded','local_only','independent_review_missing','independent_review_failed','independent_review_unknown','independent_review_check_error','review_passed','feature_disabled'];",
        "function hasBad(s){return BAD.some(w=>s.includes(w));}",
        "{const h=mkHost();m.renderHow(h,{settings:{method:'local_only',generator_provider:'架空生成',reviewer_provider:'架空評価',independent_review:true},known_providers:['架空A']});const s=texts(h).join('\\n');if(!s.includes('ローカル計画方式（外部送信の可否は別設定）')){console.error('how local');process.exit(1);}if(!s.includes('独立評価として数えられる')){console.error('alone t');process.exit(1);}if(hasBad(s)){console.error('raw code in how');process.exit(1);}}",
        "{const h=mkHost();m.renderHow(h,{settings:{method:'external_guarded',generator_provider:'架空生成',reviewer_provider:'架空評価',independent_review:false},known_providers:[]});const s=texts(h).join('\\n');if(!s.includes('外部AIあり(承認・検査つき)')){console.error('how ext');process.exit(1);}if(!s.includes('独立と数えられない')){console.error('alone f');process.exit(1);}}",
        "{const h=mkHost();m.renderHow(h,{settings:{method:'nope'}});const s=texts(h).join('\\n');if(!s.includes('確認できず')){console.error('how unknown');process.exit(1);}if(hasBad(s)){console.error('raw in unknown');process.exit(1);}}",
        "{const h=mkHost();m.renderHow(h,null);const s=texts(h).join('\\n');if(!s.includes('確認できず')){console.error('how null');process.exit(1);}}",
        "{const h=mkHost();m.renderHow(h,{});const s=texts(h).join('\\n');if(!s.includes('確認できず')){console.error('how missing');process.exit(1);}}",
        "{const h=mkHost();m.renderReadiness(h,{allowed_actions:{start:{allowed:false,reason:'xx independent_review_missing yy'}}});const s=texts(h).join('\\n');if(!s.includes('独立評価がまだありません')){console.error('rsn missing');process.exit(1);}if(hasBad(s)){console.error('raw reason');process.exit(1);}}",
        "{const h=mkHost();m.renderReadiness(h,{allowed_actions:{start:{allowed:true}}});const s=texts(h).join('\\n');if(!s.includes('開始できます')){console.error('allowed true');process.exit(1);}}",
        "{const h=mkHost();m.renderReadiness(h,null);const s=texts(h).join('\\n');if(!s.includes('確認できず')){console.error('ready null');process.exit(1);}if(s.includes('開始できます')){console.error('ready null allows');process.exit(1);}}",
        "{const h=mkHost();m.renderReadiness(h,{});const s=texts(h).join('\\n');if(!s.includes('確認できず')){console.error('ready bad');process.exit(1);}if(s.includes('開始できます')){console.error('ready bad allows');process.exit(1);}}",
        "{const h=mkHost();m.renderPlanJobs(h,{jobs:[{id:'abcdef1234567890',provider:'架空P',status:'sending',created_at:1700000000,model:'架空M',text:'見せない本文',response:'見せない',body:'見せない',packet_text:'見せない'}]});const s=texts(h).join('\\n');if(!s.includes('送信中')){console.error('plan st');process.exit(1);}if(s.includes('見せない本文')){console.error('plan leak');process.exit(1);}}",
        "{const h=mkHost();m.renderPlanJobs(h,{jobs:[{id:'x',provider:'p',status:'nope',created_at:1,model:'m'}]});const s=texts(h).join('\\n');if(!s.includes('確認できず')){console.error('plan unknown');process.exit(1);}}",
        "{const h=mkHost();m.renderReviewJobs(h,{jobs:[{id:'x',provider:'p',status:'review_passed',findings:[{category:'note',summary:'架空指摘'}],model:'架空M',reviewed_at:1700000000,text:'見せない',response:'見せない'}]});const s=texts(h).join('\\n');if(!s.includes('合格')||!s.includes('補足')){console.error('rev pass');process.exit(1);}if(!s.includes('合格は実行の許可ではありません')){console.error('pass note');process.exit(1);}if(s.includes('見せない')){console.error('rev leak');process.exit(1);}}",
        "{const h=mkHost();m.renderReviewJobs(h,{jobs:[{id:'x',status:'nope',findings:[],model:'m',reviewed_at:1}]});const s=texts(h).join('\\n');if(!s.includes('確認できず')){console.error('rev unknown');process.exit(1);}if(s.includes('合格')&&!s.includes('確認できず')){console.error('unknown as pass');process.exit(1);}}",
        "{const h=mkHost();m.renderPlanJobs(h,{jobs:[]});const s=texts(h).join('\\n');if(!s.includes('ジョブなし')){console.error('empty plan');process.exit(1);}}",
        "{const h=mkHost();m.renderPlanJobs(h,{disabled:true});const s=texts(h).join('\\n');if(!s.includes('この機能は管理者の設定で無効です')){console.error('disabled plan');process.exit(1);}if(s.includes('ジョブなし')){console.error('disabled as empty');process.exit(1);}}",
        "{const h=mkHost();m.renderPlanJobs(h,null);const s=texts(h).join('\\n');if(!s.includes('取得できませんでした')){console.error('fail plan');process.exit(1);}if(s.includes('ジョブなし')){console.error('fail as empty');process.exit(1);}}",
        "{const h=mkHost();m.renderReviewJobs(h,{jobs:[]});const s=texts(h).join('\\n');if(!s.includes('ジョブなし')){console.error('empty rev');process.exit(1);}}",
        "{const h=mkHost();m.renderAudit(h,{audit:[{at:1700000000,action:'approved',provider:'架空P',purpose:'架空用途',text:'見せない'},{at:1700000001,action:'unknown_code_x',provider:'架空P',purpose:'架空用途'}]});const s=texts(h).join('\\n');if(s.includes('見せない')){console.error('audit leak');process.exit(1);}if(!s.includes('承認')){console.error('audit row');process.exit(1);}if(s.includes('approved')||s.includes('unknown_code_x')){console.error('audit raw code');process.exit(1);}if(!s.includes('確認できず')){console.error('audit unknown action');process.exit(1);}}",
        "{const h=mkHost();m.renderAudit(h,{audit:[]});const s=texts(h).join('\\n');if(!s.includes('記録なし')){console.error('audit empty');process.exit(1);}}",
        "{const h=mkHost();m.renderAudit(h,null);const s=texts(h).join('\\n');if(!s.includes('取得できませんでした')){console.error('audit fail');process.exit(1);}}",
        "{const h=mkHost();const evil='<img src=x onerror=alert(1)>';m.renderReviewJobs(h,{jobs:[{id:'x',provider:evil,status:'review_failed',findings:[{category:evil,summary:evil}],model:evil,reviewed_at:1}]});const s=texts(h).join('\\n');if(!s.includes(evil)){console.error('inject text');process.exit(1);}function hasEl(e){if(e.tag==='img')return true;for(const c of (e.children||[])){if(hasEl(c))return true;}return false;}if(hasEl(h)){console.error('inject el');process.exit(1);}}",
        "if(m.shouldFetch(true,false)!==false){console.error('hidden fetch');process.exit(1);}",
        "if(m.shouldFetch(false,true)!==false){console.error('busy fetch');process.exit(1);}",
        "if(m.shouldFetch(false,false)!==true){console.error('fetch ok');process.exit(1);}",
        "{const a=m.nextTicket();const b=m.nextTicket();if(!m.isCurrentTicket(b)){console.error('ticket cur');process.exit(1);}if(m.isCurrentTicket(a)){console.error('ticket stale');process.exit(1);}}",
    ]


def test_panel_renders_all_states():
    _node_probe(_render_cases())


def test_node_probe_sources_are_well_formed():
    for name, cases in (("pure", [
        "if(m.howText('local_only')!=='x'){process.exit(0);}",
    ]), ("render", _render_cases())):
        problems = _probe_declaration_problems(_probe_source(cases))
        assert problems == [], (name, problems)
    assert _probe_declaration_problems(_probe_source(["const a=1;\nconst a=2;"]))
    assert _probe_declaration_problems(_probe_source(["if(true){"]))


def test_mutation_403_disabled_still_disabled():
    assert "この機能は管理者の設定で無効です" in PANEL_JS
    assert "if(isDisabled(e))" in PANEL_JS.replace(" ", "")
    assert "e.message==='feature_disabled'" in PANEL_JS.replace(" ", "")
    mutated = PANEL_JS.replace("この機能は管理者の設定で無効です", "ジョブなし")
    assert "この機能は管理者の設定で無効です" not in mutated
    assert mutated.count("ジョブなし") > PANEL_JS.count("ジョブなし")
    assert PANEL_JS.count("この機能は管理者の設定で無効です") >= 5


def test_mutation_allowed_true_fail_closed():
    assert "実行の可否：確認できず" in PANEL_JS
    assert "開始できます" in PANEL_JS
    assert "typeof start.allowed" in PANEL_JS
    mutated = PANEL_JS.replace("実行の可否：確認できず", "実行の可否：開始できます")
    assert mutated != PANEL_JS
    assert PANEL_JS.count("実行の可否：確認できず") >= 3


def test_mutation_pass_note_kept():
    assert "合格は実行の許可ではありません。人の承認と既存のゲートが別に必要です" in PANEL_JS
    assert "if(item.status==='review_passed')" in PANEL_JS.replace(" ", "")
    mutated = PANEL_JS.replace("合格は実行の許可ではありません。人の承認と既存のゲートが別に必要です", "")
    assert "合格は実行の許可ではありません" not in mutated


def test_mutation_allowlist_kept():
    assert "AUDIT_KEYS" in PANEL_JS
    assert "['at','action','provider','purpose']" in PANEL_JS.replace(" ", "")
    for key in ("id", "provider", "status", "created_at", "model"):
        assert key in PANEL_JS
    assert "packet_text" not in PANEL_JS
    assert ".text" not in PANEL_JS.replace("render", "") or "textContent" in PANEL_JS
    mutated_keys = "for(const k in item)"
    assert mutated_keys not in PANEL_JS


def test_mutation_unknown_status_not_pass():
    assert "return '確認できず'" in PANEL_JS
    assert "reviewText" in PANEL_JS
    assert "planJobText" in PANEL_JS
    mutated = PANEL_JS.replace("return '確認できず'", "return '合格'")
    assert mutated != PANEL_JS
    assert PANEL_JS.count("return '確認できず'") >= 4


def test_change_scope_only_three_files():
    assert (ROOT / "app/static/planning_guard_panel.js").is_file()
    assert (ROOT / "tests/test_g5a_planning_guard_panel.py").is_file()
    web_text = (ROOT / "app/web.py").read_text(encoding="utf-8")
    assert web_text.count("@app.get('/static/planning_guard_panel.js')") == 1
    assert "planningGuardPanelSection" not in web_text
    for name in ("project_mission.js", "goal_state.js", "watch_panel.js",
                 "ledger_panel.js", "night_run_panel.js", "shadow_panel.js",
                 "workflow_readiness.js", "contact_channel_panel.js"):
        text = (ROOT / "app/static" / name).read_text(encoding="utf-8")
        assert "planningGuardPanelSection" not in text, name
        assert "planning_guard_panel" not in text, name


def test_pj_switch_while_busy_does_not_show_stale_data():
    """取得中にPJを切り替えたとき、前PJの応答を表示せず、新PJを取得し直す(Nodeの擬似環境)。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js(node)が無いため画面の実行試験を実施できない(未検証。成功ではない)")
    script = r"""
const fs=require('fs');
const src=fs.readFileSync(process.argv[2],'utf8');
function el(tag){return {tag,children:[],textContent:'',id:'',parentNode:null,
 append(...c){for(const x of c){x.parentNode=this;this.children.push(x);}},
 replaceChildren(...c){this.children=[];this.append(...c);},
 insertBefore(n){n.parentNode=this;this.children.push(n);},
 addEventListener(){}, get nextSibling(){return null;}};}
function texts(n){let out=[];if(n.textContent)out.push(n.textContent);for(const c of n.children||[])out=out.concat(texts(c));return out;}
const ids={}; const anchor=el('div'); anchor.id='contactChannelPanelSection'; anchor.parentNode=el('div');
anchor.parentNode.insertBefore=function(node){node.parentNode=this;ids.planningGuardPanelSection=node;};
const sel=el('select'); sel.value='pjA'; ids.projectSelect=sel; ids.contactChannelPanelSection=anchor;
global.document={hidden:false,readyState:'complete',
 getElementById(i){ return ids[i]||null; },
 createElement:el, addEventListener(){}};
global.window={};
global.fetch=function(url){
 const parts=url.split('/'); const pj=decodeURIComponent(parts[3]);
 fetched.push(pj);
 return new Promise(res=>{
  const body = url.endsWith('planning-settings') ? {settings:{method:'local_only',generator_provider:pj+'-gen',reviewer_provider:'',independent_review:false},known_providers:[]}
   : url.endsWith('workflow-readiness') ? {allowed_actions:{start:{allowed:false,reason:'x'}}}
   : url.endsWith('audit') ? {audit:[]} : {jobs:[]};
  const done=()=>res({ok:true,status:200,json:async()=>body});
  if(pj==='pjA') pendingA.push(done); else done();
 });
};
const fetched=[]; const pendingA=[];
global.setInterval=()=>0; global.setTimeout=()=>0;
eval(src);
(async()=>{
 await new Promise(r=>setImmediate(r));
 sel.value='pjB';
 const p=window.reloadPlanningGuardPanel();
 await p;
 for(const f of pendingA) f();
 await new Promise(r=>setTimeout=== undefined?r():setImmediate(r));
 await new Promise(r=>setImmediate(r));
 await new Promise(r=>setImmediate(r));
 const box=ids.planningGuardPanelSection;
 const shown=texts(box).join('\n');
 if(!fetched.includes('pjB')){console.error('new project not fetched while busy');process.exit(1);}
 if(shown.includes('pjA-gen')){console.error('stale data of previous project shown');process.exit(1);}
 if(!shown.includes('pjB-gen')){console.error('new project data not shown');process.exit(1);}
 console.log('ok');process.exit(0);
})();
"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "probe.cjs"
        path.write_text(script, encoding="utf-8")
        completed = subprocess.run(
            [node, str(path), str(ROOT / "app/static/planning_guard_panel.js")],
            capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr or completed.stdout

def test_panel_static_route_serves_js():
    from fastapi.testclient import TestClient
    import app.web as web
    client = TestClient(web.app, raise_server_exceptions=False)
    response = client.get("/static/planning_guard_panel.js")
    assert response.status_code == 200
    assert "planningGuardPanelSection" in response.text
    assert response.headers.get("content-type", "").startswith("text/javascript")
