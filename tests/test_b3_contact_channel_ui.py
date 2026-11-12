"""B3 画面『発信・問い合わせ設定』のテスト (疑似PJのみ・静的検査中心)。

疑似PJのみ。実在PJのID・件数・版・語を使わない。外部通信なし。
Node の偽DOMで描画し、既存の ledger パネルの回帰と新しい経路の両方を検査する。
"""
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PANEL_JS = (ROOT / "app/static/contact_channel_panel.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
MISSION_JS = (ROOT / "app/static/project_mission.js").read_text(encoding="utf-8")
LEDGER_JS = (ROOT / "app/static/ledger_panel.js").read_text(encoding="utf-8")

DANGEROUS = ("innerHTML", "insertAdjacentHTML", "outerHTML", "document.write")
FORBIDDEN_WORDS = ("stale", "no_binding", "unreadable", "awaiting_approval",
                   "fictional", "internal_demo")


def test_no_new_dangerous_html_and_script_order_kept():
    for token in DANGEROUS:
        assert token not in PANEL_JS, token
    assert PANEL_JS.count("innerHTML") == 0
    added = [line for line in INDEX_HTML.splitlines()
             if "contact_channel_panel.js" in line]
    assert added, "contact_channel_panel.js の読込タグがありません"
    for line in added:
        for token in DANGEROUS:
            assert token not in line, token
    assert MISSION_JS.count("innerHTML") == 28
    assert "contact_channel_panel.js" in INDEX_HTML
    assert "contactChannelPanelSection" in PANEL_JS
    assert "発信・問い合わせ設定" in PANEL_JS
    # 既存の読込順序を壊さない(追記のみ)。
    order = ["presenters.js", "project_mission.js", "local_models.js",
             "agent_examples.js", "procedure_learning.js", "triz_invention.js",
             "triz_general.js", "review_queue.js", "workflow_readiness.js",
             "artifact_shelf.js", "goal_review.js", "plan_repair_cards.js",
             "plan_review_loop.js", "resolution_cards.js", "project_reset.js",
             "capability_gap.js", "domain_pack.js", "goal_state.js",
             "watch_panel.js", "ledger_panel.js", "night_run_panel.js",
             "shadow_panel.js", "contact_channel_panel.js",
             "project_delete.js", "project_management.js", "ocr_review.js",
             "experience_import.js"]
    positions = []
    for name in order:
        matches = [i for i, line in enumerate(INDEX_HTML.splitlines()) if name in line]
        assert matches, name
        positions.append(matches[0])
    assert positions == sorted(positions), positions
    # 既存パネルは残る。
    assert "ledgerPanelSection" in LEDGER_JS
    assert "nightRunPanelSection" in (ROOT / "app/static/night_run_panel.js").read_text(encoding="utf-8")
    for word in ("運用区分", "架空試験", "実運用", "未設定(厳しい側=公開用途として扱う)",
                 "架空・公開不可", "問い合わせ方法", "使用SNS", "事前確認" if False else "不足",
                 "公開前の版差分", "ここにはパスワードやAPIキーを入れないでください",
                 "次の行動", "未設定", "読取不能"):
        assert word in PANEL_JS, word


def test_internal_words_never_shown_raw():
    body = PANEL_JS
    for word in FORBIDDEN_WORDS:
        # JS内部の識別子・API名としては許すが、画面文言(textContent)としては出さない。
        # DOM全体の走査は Node 側で行う。ここでは textContent の引数だけを見る。
        for match in re.finditer(r"textContent\s*=\s*('([^']*)'|\"([^\"]*)\")", body):
            literal = match.group(2) if match.group(2) is not None else match.group(3)
            assert word not in (literal or ""), (word, literal)


def test_static_route_serves_panel_js(tmp_path, monkeypatch):
    import app.web as web
    from fastapi.testclient import TestClient
    from app.memory.short_term import ShortTermMemory

    mem_dir = tmp_path / "架空data" / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    mem = mem_dir / "conversations.db"
    memory = ShortTermMemory(mem)
    memory.create_project("架空B3画面PJ", "")
    monkeypatch.setattr(web, "memory", memory)
    client = TestClient(web.app, raise_server_exceptions=False)
    resp = client.get("/static/contact_channel_panel.js")
    assert resp.status_code == 200
    assert "contactChannelPanelSection" in resp.text
    for token in DANGEROUS:
        assert token not in resp.text, token


def test_js_api_paths_exist_and_functions_used():
    import app.web as web
    routes = {str(getattr(r, "path", "")) for r in web.app.routes}
    assert "/api/projects/{project_id}/contact-registry/operating-mode" in routes
    assert "/api/projects/{project_id}/contact-routes" in routes
    assert "/api/projects/{project_id}/channel-selections" in routes
    assert "/api/projects/{project_id}/public-identities" in routes
    assert "/api/projects/{project_id}/contact-registry/preflight" in routes
    assert "/api/projects/{project_id}/premarketing/campaigns/{campaign_id}/publication-binding" in routes
    assert "/static/contact_channel_panel.js" in routes
    for path in ("/api/projects/'+encodeURIComponent(pid)+'/contact-registry/operating-mode",
                 "/api/projects/'+encodeURIComponent(pid)+'/contact-routes",
                 "/api/projects/'+encodeURIComponent(pid)+'/channel-selections",
                 "/api/projects/'+encodeURIComponent(pid)+'/public-identities",
                 "/api/projects/'+encodeURIComponent(pid)+'/contact-registry/preflight",
                 "publication-binding"):
        assert path in PANEL_JS, path
    for name in ("renderOperatingMode", "renderContactRoutes",
                 "renderChannelSelections", "renderPreflight",
                 "renderPublicationBinding", "modeText", "sourceText",
                 "contactKindText", "bindingText", "apiOperatingMode",
                 "apiSaveOperatingMode", "apiContactRoutes", "apiSaveContactRoute",
                 "apiChannelSelections", "apiPublicIdentities", "apiPreflight",
                 "apiPublicationBinding", "apiCampaigns"):
        assert PANEL_JS.count(name) >= 2, name
    assert "contactChannelPanelSection" in PANEL_JS
    # スコープ: ハンドラが参照する変数は同じ関数内で宣言されている。
    assert "const safeCtx" not in PANEL_JS
    assert "let epoch=0" in PANEL_JS
    compact = PANEL_JS.replace(" ", "")
    assert "let epoch=0" in compact.replace("\n", "").replace("\t", "") or "letepoch=0" in compact
    compact = PANEL_JS.replace(" ", "")
    assert "catch(e){" in compact
    assert "isCurrent())return" in compact
    assert "disabled=true" in compact
    assert "disabled=false" in compact
    # PJ切替で前の表示・入力が残らない(読込のたびに空に戻す)。
    assert "contactChannelModeActor" in PANEL_JS
    assert "node.value=''" in compact or 'node.value=""' in compact


def _probe_source(cases):
    """Node に渡すJS全体(前置き + 各試験の本体)を組み立てる。実行はしない。"""
    probe = [
        "const fs=require('fs');",
        "const vm=require('vm');",
        "const src=fs.readFileSync(" + json.dumps(str(ROOT / "app/static/contact_channel_panel.js")) + ",'utf8');",
        ("const els={};"
         "function mkEl(tag){"
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
         "localStorage:{getItem:function(){return null;},setItem:function(){},removeItem:function(){}},"
         "console:console});"),
        "const m=mod.exports;",
        ("function texts(e){let out=[String(e.textContent||'')];"
         "for(const c of (e.children||[])){out=out.concat(texts(c));}return out;}"),
    ]
    probe.extend(cases)
    return "\n".join(probe)


def _require_node():
    """既存の画面試験と同じく、Node が無いときは理由つきでスキップする(成功にはしない)。"""
    if shutil.which("node") is None:
        pytest.skip("Node.js(node)が無いため画面の実行試験を実施できない(未検証。成功ではない)")


def _probe_declaration_problems(source):
    """Node に渡すJSの静的検査(実行しない)。

    - 同じ有効範囲(トップレベル)での const/let/var/function の同名重複
    - 括弧 () [] {} の対応、閉じられていない文字列
    文字列リテラルの中身は数えない。
    """
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


def _pure_cases():
    cases_mode = [["demo_test", "架空試験"], ["production", "実運用"],
                  ["", "未設定(厳しい側=公開用途として扱う)"], ["nope", "未設定(厳しい側=公開用途として扱う)"]]
    cases_source = [["fictional", "架空・公開不可"], ["registered", "登録済み(未確認)"],
                    ["verified", "確認済み"], ["retired", "失効"], ["", "確認できず"]]
    cases_binding = [["ok", "公開できる状態"],
                     ["stale", "登録が変わったため再プレビュー・再承認が必要"],
                     ["no_binding", "承認の記録が無いため再プレビュー・再承認が必要"],
                     ["blocked", "公開できない登録があるため公開を止めている"],
                     ["unreadable", "照合を読めないため公開を止めている"],
                     ["", "確認できず"]]
    return [
        "const casesMode=" + json.dumps(cases_mode, ensure_ascii=False) + ";",
        "const casesSource=" + json.dumps(cases_source, ensure_ascii=False) + ";",
        "const casesBinding=" + json.dumps(cases_binding, ensure_ascii=False) + ";",
        "for(const c of casesMode){if(m.modeText(c[0])!==c[1]){console.error('mode '+c[0]);process.exit(1);}}",
        "for(const c of casesSource){if(m.sourceText(c[0])!==c[1]){console.error('source '+c[0]);process.exit(1);}}",
        "for(const c of casesBinding){if(m.bindingText(c[0])!==c[1]){console.error('binding '+c[0]);process.exit(1);}}",
        "if(m.contactKindText('form')!=='Webフォーム'){console.error('kind');process.exit(1);}",
    ]


def test_panel_pure_functions_with_node():
    _node_probe(_pure_cases())


def _render_cases():
    """描画試験の本体。各検査は独立したブロックに閉じ、宣言の名前を共有しない。"""
    return [
        "function mkHost(){return {children:[],textContent:'',append(x){this.children.push(x);},replaceChildren(...xs){this.children=[];xs.forEach(x=>this.children.push(x));}};}",
        "const INTERNAL=['stale','no_binding','unreadable','awaiting_approval','fictional','internal_demo'];",
        "function leaked(seen){return INTERNAL.filter(w=>seen.includes(w));}",
        # 運用区分の3状態と読取不能。
        "for(const mode of [{current:{mode:'demo_test',actor:'a',reason:'r'},status:'ok'},{current:{mode:'production'},status:'ok'},{current:null,status:'unset'},{current:null,status:'unreadable'}]){",
        "const h=mkHost();m.renderOperatingMode(h,mode);const seen=texts(h).join('\\n');",
        "if(leaked(seen).length){console.error('internal words in mode view: '+leaked(seen));process.exit(1);}",
        "if(mode.current&&mode.current.mode==='demo_test'&&!seen.includes('架空・公開不可')){console.error('demo note missing');process.exit(1);}",
        "if(mode.status==='unset'&&!seen.includes('未設定(厳しい側=公開用途として扱う)')){console.error('unset label missing');process.exit(1);}",
        "if(mode.status==='unreadable'&&!seen.includes('読取不能')){console.error('unreadable label missing');process.exit(1);}",
        "}",
        # 問い合わせ方法の各出所。
        "for(const source of ['fictional','registered','verified','retired']){",
        "const h=mkHost();m.renderContactRoutes(h,{current:[{type:'email',public_value:'a@example.invalid',source_kind:source,expires_at:null}]});const seen=texts(h).join('\\n');",
        "if(leaked(seen).length){console.error('internal words in route view');process.exit(1);}",
        "if(source==='fictional'&&!seen.includes('架空・公開不可')){console.error('fictional label missing');process.exit(1);}",
        "}",
        "{const h=mkHost();m.renderContactRoutes(h,{current:[]});const seen=texts(h).join('\\n');",
        "if(!seen.includes('未設定')||!seen.includes('次の行動')){console.error('empty routes view');process.exit(1);}}",
        # 使用SNSとID未登録・秘密情報の注意。
        "{const h=mkHost();m.renderChannelSelections(h,{current:[{platform:'X',enabled:true}]},[]);const seen=texts(h).join('\\n');",
        "if(!seen.includes('X：有効')||!seen.includes('IDが未設定')){console.error('channel view');process.exit(1);}",
        "if(!seen.includes('ここにはパスワードやAPIキーを入れないでください')){console.error('secret note missing');process.exit(1);}}",
        # preflight の不足と止まる操作。
        "{const h=mkHost();m.renderPreflight(h,{missing_input:[{field:'contact_route',input:'問い合わせ方法と実値',next_action:'問い合わせ先を登録してください'}],blocking_tasks:['customer_contact','publish']});const seen=texts(h).join('\\n');",
        "if(!seen.includes('不足：問い合わせ方法と実値')||!seen.includes('次の行動：問い合わせ先を登録してください')||!seen.includes('止まる操作：customer_contact / publish')){console.error('preflight view');process.exit(1);}}",
        # 版差分の各状態は内部語を出さない。
        "for(const status of ['ok','stale','no_binding','blocked','unreadable']){",
        "const h=mkHost();m.renderPublicationBinding(h,{binding_status:status,reasons:['理由']});const seen=texts(h).join('\\n');",
        "if(leaked(seen).length){console.error('internal words in binding view '+status);process.exit(1);}",
        "}",
    ]


def test_panel_renders_modes_routes_channels_preflight_binding():
    _node_probe(_render_cases())


def test_node_probe_sources_are_well_formed():
    """Node を使わない静的検査。生成されるJSの重複宣言・括弧の対応を確認する。"""
    for name, cases in (("pure", _pure_cases()), ("render", _render_cases())):
        problems = _probe_declaration_problems(_probe_source(cases))
        assert problems == [], (name, problems)
    # 検査器自身の確認: 元の不具合(前置きの mkEl と本体の const mkEl の重複)を検出できること。
    assert _probe_declaration_problems(_probe_source(["const mkEl=()=>({});"]))
    assert _probe_declaration_problems(_probe_source(["const a=1;\nconst a=2;"]))
    assert _probe_declaration_problems(_probe_source(["if(true){"]))
