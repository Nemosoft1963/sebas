"""G5-b2b 外部AIへの送信(ジョブ作成・取消)の試験。疑似PJのみ・実在値なし。

Node の擬似DOM。Node が無い環境では描画試験をスキップする。
スキップは成功ではなく、静的な検査と変異の代替検査で補う。
"""
import json
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
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
        assert token not in JOBS_JS, token
    for token in ("console.log", "console.info", "console.debug",
                  "console.warn", "console.error"):
        assert token not in JOBS_JS, token
    for token in ("localStorage", "sessionStorage", "indexedDB", "IndexedDB"):
        assert token not in JOBS_JS, token
    assert "planningGuardJobsSection" in JOBS_JS
    assert "外部AIへの送信" in JOBS_JS
    assert "外部AIの下書きの採用は、この画面ではできません" in JOBS_JS
    assert "送る内容を確認する" in JOBS_JS
    assert "この内容を送信する" in JOBS_JS
    assert "やめる" in JOBS_JS
    assert "送信を開始しました。結果は下の一覧で確認してください" in JOBS_JS
    assert "この内容は、すでに送信されています(再送はしていません)" in JOBS_JS
    assert "取り消しました(すでに外部へ送られた内容は戻りません)" in JOBS_JS
    assert "確認できず" in JOBS_JS
    assert "取得できませんでした" in JOBS_JS
    assert "この機能は管理者の設定で無効です" in JOBS_JS
    assert "自動更新を止めました。更新ボタンで確認してください" in JOBS_JS
    assert "合格は実行の許可ではありません。人の承認と既存のゲートが別に必要です" in JOBS_JS
    assert "この用途は、この画面からは送れません" in JOBS_JS
    assert "'GET'" in JOBS_JS or '"GET"' in JOBS_JS
    assert "'POST'" in JOBS_JS or '"POST"' in JOBS_JS
    assert "PUT" not in JOBS_JS
    assert "DELETE" not in JOBS_JS
    assert "createElement('form')" not in JOBS_JS
    assert 'createElement("form")' not in JOBS_JS
    assert "<form" not in JOBS_JS
    assert "http://" not in JOBS_JS
    assert "https://" not in JOBS_JS
    for token in ("external_draft", "unverified_claims", "request_template"):
        assert token not in JOBS_JS, token
    for word in ("今日の送信回数の上限(3回)に達しました",
                 "計画方式が「外部AIあり」ではありません",
                 "設定された提供元と一致しません",
                 "この送付内容が見つかりません",
                 "用途が一致しません",
                 "計画が変わったため送れません",
                 "送信直前の再検査で、送れない内容が見つかりました",
                 "内容が変わっているため送れません",
                 "送信の許可がありません(承認が取り消された、または使用済みです)",
                 "確認に失敗したため送れません",
                 "送信できませんでした",
                 "送信できたか確認できませんでした。下の一覧を更新して確認してください。確認する前に、もう一度送らないでください",
                 "実行中は送信できません。先に一時停止してください",
                 "すでに送信が終わっているため、取り消せません",
                 "ジョブが見つかりません",
                 "取り消せたか確認できませんでした",
                 "現在のプロジェクトが変わったため送信しませんでした",
                 "現在のプロジェクトが変わったため取り消しませんでした",
                 "この内容は、外部のサービス",
                 "1回の承認で送れるのは1回だけです。1日に3回までです。",
                 "この内容を、この提供元へ、今、1回だけ送信することを承認します"):
        assert word in JOBS_JS, word
    assert "/external-plan-jobs" in JOBS_JS
    assert "/independent-review-jobs" in JOBS_JS
    assert "/plan-packets/approvals" in JOBS_JS
    assert "/job-key" in JOBS_JS
    assert "isValidApproval" in JOBS_JS
    assert "isValidApprovals" in JOBS_JS
    assert "isValidJob" in JOBS_JS
    assert "isValidJobs" in JOBS_JS
    assert "isValidPacketText" in JOBS_JS
    assert "isValidJobKey" in JOBS_JS


def test_internal_words_not_shown_raw():
    for match in re.finditer(r"textContent\s*=\s*('([^']*)'|\"([^\"]*)\")", JOBS_JS):
        literal = match.group(2) if match.group(2) is not None else match.group(3)
        for word in ("method_not_external", "provider_mismatch", "packet_not_found",
                     "purpose_mismatch", "idempotency_key_mismatch",
                     "quota_exceeded", "privacy_recheck_failed", "plan_changed",
                     "packet_tampered", "check_error", "permission_denied",
                     "feature_disabled", "job_not_found", "job_not_cancellable",
                     "purpose_unsupported", "packet_not_approved"):
            if word in (literal or ""):
                raise AssertionError((word, literal))


def test_index_tag_one_line_and_order_kept():
    packets_lines = [line for line in INDEX_HTML.splitlines()
                     if "planning_guard_packets.js" in line]
    assert len(packets_lines) == 1, packets_lines
    added = [line for line in INDEX_HTML.splitlines()
             if "planning_guard_jobs.js" in line]
    assert len(added) == 1, added
    assert "v=20261106-g5b2b" in added[0]
    for token in DANGEROUS:
        assert token not in added[0], token
    panel_tag = '<script src="/static/planning_guard_panel.js?v=20261030-g5a"></script>'
    settings_tag = '<script src="/static/planning_guard_settings.js?v=20261031-g5b1"></script>'
    packets_tag = '<script src="/static/planning_guard_packets.js?v=20261105-g5b2a"></script>'
    jobs_tag = '<script src="/static/planning_guard_jobs.js?v=20261106-g5b2b"></script>'
    assert INDEX_HTML.count(panel_tag) == 1
    assert INDEX_HTML.count(settings_tag) == 1
    assert INDEX_HTML.count(packets_tag) == 1
    assert INDEX_HTML.count(jobs_tag) == 1
    assert panel_tag + settings_tag + packets_tag + jobs_tag in INDEX_HTML.replace("\n", "")


def test_web_route_and_scope():
    import app.web as web
    routes = {str(getattr(r, "path", "")) for r in web.app.routes}
    assert "/static/planning_guard_jobs.js" in routes
    assert "/static/planning_guard_packets.js" in routes
    assert "/static/planning_guard_settings.js" in routes
    assert "/static/planning_guard_panel.js" in routes
    assert "/api/projects/{project_id}/plan-packets/approvals" in routes
    assert "/api/projects/{project_id}/plan-packets/audit" in routes
    assert "/api/projects/{project_id}/plan-packets/{packet_id}/job-key" in routes
    assert "/api/projects/{project_id}/planning-job-capabilities" in routes
    assert WEB_TEXT.count("@app.get('/static/planning_guard_jobs.js')") == 1
    assert WEB_TEXT.count("@app.get('/static/planning_guard_packets.js')") == 1
    assert WEB_TEXT.count("async def get_plan_packet_job_key") == 1
    assert WEB_TEXT.count("plan-packets/{packet_id}/job-key") >= 1
    assert WEB_TEXT.count("plan-packets/approvals") == 1
    assert WEB_TEXT.count("async def list_plan_packet_approvals") == 1
    assert "planningGuardJobsSection" not in PANEL_JS
    assert "planningGuardJobsSection" not in SETTINGS_JS
    assert "planningGuardJobsSection" not in PACKETS_JS
    assert "planning_guard_jobs" not in PANEL_JS
    assert "planning_guard_jobs" not in SETTINGS_JS
    assert "planning_guard_jobs" not in PACKETS_JS
    for name in ("project_mission.js", "goal_state.js", "watch_panel.js",
                 "ledger_panel.js", "night_run_panel.js", "shadow_panel.js",
                 "workflow_readiness.js", "contact_channel_panel.js"):
        text = (ROOT / "app/static" / name).read_text(encoding="utf-8")
        assert "planningGuardJobsSection" not in text, name
        assert "planning_guard_jobs" not in text, name
    for path in sorted((ROOT / "app").glob("*.py")):
        if path.name == "web.py":
            continue
        text = path.read_text(encoding="utf-8")
        assert "planning_guard_jobs" not in text, path.name
        assert "planningGuardJobsSection" not in text, path.name


def test_jobs_static_route_serves_js():
    from fastapi.testclient import TestClient
    import app.web as web
    client = TestClient(web.app, raise_server_exceptions=False)
    response = client.get("/static/planning_guard_jobs.js")
    assert response.status_code == 200
    assert "planningGuardJobsSection" in response.text
    assert response.headers.get("content-type", "").startswith("text/javascript")


def _server_client(tmp_path, monkeypatch):
    from app.memory.short_term import ShortTermMemory
    import app.web as web
    from fastapi.testclient import TestClient
    mem_path = tmp_path / "g5b2b-api.db"
    memory = ShortTermMemory(mem_path)
    pid = memory.create_project("G5B2B")["id"]
    memory.save_mission(pid, "目標G", "条件S", "制約C", False, [])
    client = TestClient(web.app, raise_server_exceptions=False)
    monkeypatch.setattr(web, "memory", memory)
    monkeypatch.setattr(web, "provider_statuses", lambda: [
        {"id": "fake-gen-a", "configured": True},
        {"id": "fake-rev-b", "configured": True},
        {"id": "fake-other-c", "configured": True},
    ])
    return client, memory, pid


def _enable_external(client, pid, generator="fake-gen-a", reviewer=""):
    res = client.put(f"/api/projects/{pid}/planning-settings",
                     json={"method": "external_guarded",
                           "generator_provider": generator,
                           "reviewer_provider": reviewer})
    assert res.status_code == 200, res.text


def _preview_approve(client, pid, goal="公開目標です", purpose="plan_generation",
                     provider="fake-gen-a", approver="tester"):
    res = client.post(f"/api/projects/{pid}/plan-packets/preview",
                      json={"purpose": purpose, "provider": provider,
                            "public_items": {"goal": goal},
                            "withheld_fields": []})
    assert res.status_code == 200, res.text
    packet_id = res.json()["id"]
    res = client.post(f"/api/projects/{pid}/plan-packets/{packet_id}/approve",
                      json={"approver": approver})
    assert res.status_code == 200, res.text
    return packet_id


def test_server_job_capabilities_follow_project_flags(tmp_path, monkeypatch):
    client, _memory, pid = _server_client(tmp_path, monkeypatch)
    monkeypatch.delenv("SEBAS_EXTERNAL_PLAN_JOBS_PROJECTS", raising=False)
    monkeypatch.delenv("SEBAS_INDEPENDENT_REVIEW_PROJECTS", raising=False)
    path = f"/api/projects/{pid}/planning-job-capabilities"
    assert client.get(path).json() == {"plan_generation": False, "plan_review": False}
    monkeypatch.setenv("SEBAS_EXTERNAL_PLAN_JOBS_PROJECTS", pid)
    assert client.get(path).json() == {"plan_generation": True, "plan_review": False}
    monkeypatch.setenv("SEBAS_INDEPENDENT_REVIEW_PROJECTS", pid)
    assert client.get(path).json() == {"plan_generation": True, "plan_review": True}


def test_server_jobkey_ok(tmp_path, monkeypatch):
    import app.external_plan_jobs as jobs
    import app.planning_packet as packet
    client, memory, pid = _server_client(tmp_path, monkeypatch)
    _enable_external(client, pid)
    packet_id = _preview_approve(client, pid)
    res = client.get(f"/api/projects/{pid}/plan-packets/{packet_id}/job-key")
    assert res.status_code == 200, res.text
    body = res.json()
    assert set(body.keys()) == {"packet_id", "purpose", "provider", "idempotency_key"}
    assert body["packet_id"] == packet_id
    assert body["purpose"] == "plan_generation"
    assert body["provider"] == "fake-gen-a"
    assert "text" not in body and "plan_signature" not in body
    expected = jobs.idempotency_key_of(
        pid, packet.plan_signature_of(memory.get_mission(pid)),
        client.get(f"/api/projects/{pid}/plan-packets/{packet_id}").json()["sha256"],
        "fake-gen-a", "plan_generation")
    assert body["idempotency_key"] == expected


def test_server_jobkey_not_approved(tmp_path, monkeypatch):
    client, _memory, pid = _server_client(tmp_path, monkeypatch)
    _enable_external(client, pid)
    res = client.post(f"/api/projects/{pid}/plan-packets/preview",
                      json={"purpose": "plan_generation", "provider": "fake-gen-a",
                            "public_items": {"goal": "下書きです"},
                            "withheld_fields": []})
    assert res.status_code == 200
    previewed_id = res.json()["id"]
    res = client.get(f"/api/projects/{pid}/plan-packets/{previewed_id}/job-key")
    assert res.status_code == 409
    assert res.json()["detail"] == "packet_not_approved"
    approved_id = _preview_approve(client, pid, goal="承認して取消す内容です")
    res = client.post(f"/api/projects/{pid}/plan-packets/{approved_id}/revoke",
                      json={"approver": "tester"})
    assert res.status_code == 200
    res = client.get(f"/api/projects/{pid}/plan-packets/{approved_id}/job-key")
    assert res.status_code == 409
    assert res.json()["detail"] == "packet_not_approved"


def test_server_jobkey_unsupported_purpose(tmp_path, monkeypatch):
    client, _memory, pid = _server_client(tmp_path, monkeypatch)
    _enable_external(client, pid)
    packet_id = _preview_approve(client, pid, goal="調査の内容です",
                                 purpose="research", provider="fake-gen-a")
    res = client.get(f"/api/projects/{pid}/plan-packets/{packet_id}/job-key")
    assert res.status_code == 422
    assert res.json()["detail"] == "purpose_unsupported"


def test_server_jobkey_missing(tmp_path, monkeypatch):
    client, _memory, pid = _server_client(tmp_path, monkeypatch)
    res = client.get(f"/api/projects/{pid}/plan-packets/no-such-id/job-key")
    assert res.status_code == 404
    assert res.json()["detail"] == "packet_not_found"


def test_server_jobkey_changes_with_plan(tmp_path, monkeypatch):
    client, memory, pid = _server_client(tmp_path, monkeypatch)
    _enable_external(client, pid)
    packet_id = _preview_approve(client, pid)
    before = client.get(f"/api/projects/{pid}/plan-packets/{packet_id}/job-key").json()["idempotency_key"]
    memory.save_mission(pid, "変わった後の目標", "条件S", "制約C", False, [])
    after = client.get(f"/api/projects/{pid}/plan-packets/{packet_id}/job-key").json()["idempotency_key"]
    assert before != after


def test_server_jobkey_readonly(tmp_path, monkeypatch):
    client, memory, pid = _server_client(tmp_path, monkeypatch)
    _enable_external(client, pid)
    packet_id = _preview_approve(client, pid)
    from app.goal_review import ReviewStore
    store = ReviewStore(memory.path)
    before_audit = store.list(pid, "packet_audit")
    before_packets = store.list(pid, "plan_packet")
    n_before = len(before_audit)
    res = client.get(f"/api/projects/{pid}/plan-packets/{packet_id}/job-key")
    assert res.status_code == 200
    res = client.get(f"/api/projects/{pid}/plan-packets/{packet_id}/job-key")
    assert res.status_code == 200
    after_audit = store.list(pid, "packet_audit")
    after_packets = store.list(pid, "plan_packet")
    assert len(after_audit) == n_before
    assert after_audit == before_audit
    assert after_packets == before_packets


def test_server_jobkey_read_failure_is_503(tmp_path, monkeypatch):
    import app.planning_packet as packet_mod
    client, _memory, pid = _server_client(tmp_path, monkeypatch)
    _enable_external(client, pid)
    packet_id = _preview_approve(client, pid)
    original = packet_mod.get_packet

    def _boom(store, project_id, signature):
        raise OSError("read failed")

    monkeypatch.setattr(packet_mod, "get_packet", _boom)
    try:
        res = client.get(f"/api/projects/{pid}/plan-packets/{packet_id}/job-key")
    finally:
        monkeypatch.setattr(packet_mod, "get_packet", original)
    assert res.status_code == 503
    assert res.json()["detail"] == "job_key_unavailable"


def test_server_jobkey_route_order(tmp_path, monkeypatch):
    import app.web as web
    paths = [str(getattr(r, "path", "")) for r in web.app.routes]
    assert "/api/projects/{project_id}/plan-packets/approvals" in paths
    assert "/api/projects/{project_id}/plan-packets/{packet_id}/job-key" in paths
    assert "/api/projects/{project_id}/plan-packets/{packet_id}" in paths
    get_routes = []
    for route in web.app.routes:
        path = str(getattr(route, "path", ""))
        methods = {m for m in (getattr(route, "methods", None) or [])}
        if "GET" in methods and path in (
                "/api/projects/{project_id}/plan-packets/approvals",
                "/api/projects/{project_id}/plan-packets/{packet_id}/job-key",
                "/api/projects/{project_id}/plan-packets/{packet_id}"):
            get_routes.append(path)
    assert get_routes.index(
        "/api/projects/{project_id}/plan-packets/approvals") < get_routes.index(
        "/api/projects/{project_id}/plan-packets/{packet_id}/job-key") < get_routes.index(
        "/api/projects/{project_id}/plan-packets/{packet_id}")
    client, _memory, pid = _server_client(tmp_path, monkeypatch)
    _enable_external(client, pid)
    _preview_approve(client, pid)
    res = client.get(f"/api/projects/{pid}/plan-packets/approvals")
    assert res.status_code == 200, res.text
    res = client.get(f"/api/projects/{pid}/plan-packets/audit")
    assert res.status_code == 200, res.text


def test_server_jobkey_enables_real_post(tmp_path, monkeypatch):
    import app.external_plan_jobs as jobs
    import app.planning_packet as packet
    import app.web as web
    client, memory, pid = _server_client(tmp_path, monkeypatch)
    _enable_external(client, pid)
    monkeypatch.setenv("SEBAS_EXTERNAL_PLAN_JOBS_PROJECTS", pid)
    packet_id = _preview_approve(client, pid)
    key_body = client.get(f"/api/projects/{pid}/plan-packets/{packet_id}/job-key").json()
    pkt = client.get(f"/api/projects/{pid}/plan-packets/{packet_id}").json()
    expected = jobs.idempotency_key_of(
        pid, packet.plan_signature_of(memory.get_mission(pid)),
        pkt["sha256"], "fake-gen-a", "plan_generation")
    assert key_body["idempotency_key"] == expected

    async def _fake(provider, prompt, system, max_tokens):
        assert prompt == pkt["text"]
        return {"text": '{"tasks": [{"title": "工程1", "description": "作業する", "outputs": [], "depends_on": []}], "required_outputs": [], "unverified_facts": [], "external_operations_pending": []}',
                "model": "fake-m", "usage": {}}

    monkeypatch.setattr(web, "_external_plan_runner", _fake)
    res = client.post(f"/api/projects/{pid}/external-plan-jobs",
                      json={"packet_id": packet_id, "provider": "fake-gen-a",
                            "idempotency_key": key_body["idempotency_key"],
                            "approver": "tester"})
    assert res.status_code == 202, res.text
    assert "text" not in res.text or pkt["text"] not in res.text


def test_server_jobkey_change_scope():
    assert WEB_TEXT.count("async def get_plan_packet_job_key") == 1
    assert WEB_TEXT.count("@app.get('/static/planning_guard_jobs.js')") == 1
    assert WEB_TEXT.count("plan-packets/{packet_id}/job-key") >= 1


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
    src_expr = json.dumps(str(ROOT / "app/static/planning_guard_jobs.js"))
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
        "let packetsSection=null;",
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
         "const packets=mkEl('section');packets.id='planningGuardPacketsSection';"
         "panel.children.push(packets);ids.planningGuardPacketsSection=packets;"
         "const sel=mkEl('select');sel.value=scenario.startPid;ids.projectSelect=sel;"
         "const documentStub={_byId:ids,readyState:'complete',hidden:false,"
         "createElement(t){return mkEl(t);},"
         "createTextNode(t){const n=mkEl('span');n.textContent=String(t);return n;},"
         "getElementById(id){if(id==='planningGuardJobsSection')return installedSection;return this._byId[id]||null;},"
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
         "installedSection=walkFind(panel,'planningGuardJobsSection');"
         "if(!installedSection){throw Error('jobs section was not mounted under the panel');}"),
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


def _job_json(job_id="j" * 32, purpose="plan_generation", provider="kp", status="sending"):
    return json.dumps({"id": job_id, "packet_id": "pkt1", "provider": provider,
                       "purpose": purpose, "status": status, "created_at": 1700000001,
                       "updated_at": 1700000002})


def _approval_row(packet_id="pkt1", purpose="plan_generation", provider="kp", active=True):
    return {"packet_id": packet_id, "purpose": purpose, "provider": provider,
            "sha256": "ab" * 32, "chars": 4, "approver": "t",
            "approved_at": 1700000000, "active": active,
            "inactive_reason": "" if active else "plan_changed"}


def _packet_text_json(packet_id="pkt1", text="abcd"):
    return json.dumps({"id": packet_id, "purpose": "plan_generation", "provider": "kp",
                       "plan_signature": "sig", "sha256": "ab" * 32, "chars": 4,
                       "included_fields": ["goal"],
                       "withheld_fields": ["success_criteria", "constraints"],
                       "withheld_count": 2, "privacy": {"ok": True, "codes": []},
                       "status": "approved", "created_at": 1700000000, "text": text})


def _base_scenario(approvals_js="[]", plan_js="[]", review_js="[]",
                   plan_status=200, review_status=200, plan_detail="feature_disabled",
                   review_detail="feature_disabled", packet_js=None,
                   key_js=None, create_js=None, cancel_js=None,
                   steps_js="", capabilities_js=None):
    return ("""({
      startPid: 'pj1',
      _releases: [],
      calls: [],
      approvals: """ + approvals_js + """,
      planJobs: """ + plan_js + """,
      reviewJobs: """ + review_js + """,
      onFetch: async function(url, options) {
        const method = (options||{}).method || 'GET';
        this.calls.push([method, url]);
        const ok = (b) => ({ok: true, status: 200, json: async () => b});
        const fail = (status, detail) => ({ok: false, status: status, json: async () => ({detail: detail})});
        if (url.indexOf('/planning-job-capabilities') >= 0) {
          return ok(""" + (capabilities_js or json.dumps({"plan_generation": True, "plan_review": True})) + """);
        }
        if (url.indexOf('/plan-packets/approvals') >= 0) {
          if (this.approvalsError) { throw Error('net down'); }
          if (this.approvalsBad) { return ok({approvals: [{packet_id: 'bad'}]}); }
          return ok({approvals: this.approvals});
        }
        if (url.indexOf('/job-key') >= 0) {
          if (this.jobKeyBad) { return ok({packet_id: 'pkt1'}); }
          return ok(""" + (key_js or json.dumps({"packet_id": "pkt1", "purpose": "plan_generation", "provider": "kp", "idempotency_key": "KEY-1"})) + """);
        }
        if (url.indexOf('/external-plan-jobs/') >= 0 && url.indexOf('/cancel') >= 0) {
          if (this.holdCancel) { await new Promise(r => { this.release = r; }); }
          if (this.cancelMode === '409') { return fail(409, 'job_not_cancellable'); }
          if (this.cancelMode === 'fail') { throw Error('net down'); }
          if (this.cancelBad) { return ok({id: 1}); }
          return ok(""" + (cancel_js or _job_json(status="cancelled")) + """);
        }
        if (url.indexOf('/independent-review-jobs/') >= 0 && url.indexOf('/cancel') >= 0) {
          if (this.cancelMode === '409') { return fail(409, 'job_not_cancellable'); }
          if (this.cancelMode === 'fail') { throw Error('net down'); }
          return ok(""" + (cancel_js or _job_json(purpose="plan_review", status="cancelled")) + """);
        }
        if (url.indexOf('/external-plan-jobs') >= 0 && method === 'POST') {
          if (this.createMode === '422quota') { return fail(422, 'quota_exceeded'); }
          if (this.createMode === '403') { return fail(403, 'feature_disabled'); }
          if (this.createMode === '409') { return fail(409, 'busy'); }
          if (this.createMode === 'fail') { throw Error('net down'); }
          if (this.createBad) { return ok({id: 1}); }
          if (this.createHold) { return {tag: '__hold'}; }
          if (this.createMode === '200') { return ok(""" + (create_js or _job_json()) + """); }
          return {ok: true, status: 202, json: async () => (""" + (create_js or _job_json()) + """)};
        }
        if (url.indexOf('/independent-review-jobs') >= 0 && method === 'POST') {
          if (this.createMode === '422quota') { return fail(422, 'quota_exceeded'); }
          if (this.createMode === '403') { return fail(403, 'feature_disabled'); }
          if (this.createMode === 'fail') { throw Error('net down'); }
          if (this.createHold) { return {tag: '__hold'}; }
          if (this.createMode === '200') { return ok(""" + (create_js or _job_json(purpose="plan_review")) + """); }
          return {ok: true, status: 202, json: async () => (""" + (create_js or _job_json(purpose="plan_review")) + """)};
        }
        if (url.indexOf('/external-plan-jobs') >= 0) {
          if (""" + str(plan_status) + """ !== 200) { return fail(""" + str(plan_status) + """, """ + json.dumps(plan_detail) + """); }
          if (this.planBad) { return ok({jobs: [{id: 1}]}); }
          return ok({jobs: this.planJobs});
        }
        if (url.indexOf('/independent-review-jobs') >= 0) {
          if (""" + str(review_status) + """ !== 200) { return fail(""" + str(review_status) + """, """ + json.dumps(review_detail) + """); }
          if (this.reviewBad) { return ok({jobs: [{id: 1}]}); }
          return ok({jobs: this.reviewJobs});
        }
        if (url.indexOf('/plan-packets/') >= 0) {
          if (this.holdPacket) { await new Promise(r => { this.release = r; }); }
          if (this.packetBad) { return ok({id: 'x'}); }
          return ok(""" + (packet_js or _packet_text_json()) + """);
        }
        return fail(500, 'unexpected:' + url);
      },
      steps: async ({panel, texts, byId, sel, sandbox, timeouts}) => {
        const sc = sandbox.scenario;
        const tick = () => new Promise(r => setImmediate(r));
        function allNodes(n, out) { out.push(n); for (const c of (n.children || [])) { allNodes(c, out); } return out; }
        const findBtn = (label) => allNodes(panel, []).filter(x => x.tag === 'button' && x.textContent === label);
        const countCalls = (needle, m) => sc.calls.filter(c => c[0] === (m || 'POST') && c[1].indexOf(needle) >= 0).length;
        const seenText = () => texts(panel).join('\\n');
""" + steps_js + """
      }
    })""")


def test_node_pure_functions():
    problems = _probe_declaration_problems(JOBS_JS)
    assert not problems, problems
    _require_node()
    cases = [
        "if(m.purposeText('plan_generation')!=='計画の作成'){console.error('p1');process.exit(1);}",
        "if(m.purposeText('research')!=='調査'){console.error('p5');process.exit(1);}",
        "if(m.purposeText('nope')!=='確認できず'){console.error('p6');process.exit(1);}",
        "if(m.planJobText('sending')!=='送信中'){console.error('j1');process.exit(1);}",
        "if(m.reviewJobText('review_passed')!=='合格'){console.error('j2');process.exit(1);}",
        "if(m.planJobText('zzz')!=='確認できず'){console.error('j3');process.exit(1);}",
        "if(m.jobStatusText('plan_generation','sending')!=='送信中'){console.error('j4');process.exit(1);}",
        "if(m.jobStatusText('plan_review','review_passed')!=='合格'){console.error('j5');process.exit(1);}",
        "if(m.jobStatusText('zzz','sending')!=='確認できず'){console.error('j6');process.exit(1);}",
        "if(m.sendErrorText(403,'feature_disabled')!=='この機能は管理者の設定で無効です'){console.error('s403');process.exit(1);}",
        "if(m.sendErrorText(409,'x')!=='実行中は送信できません。先に一時停止してください'){console.error('s409');process.exit(1);}",
        "if(m.sendErrorText(422,'quota_exceeded')!=='今日の送信回数の上限(3回)に達しました'){console.error('s422');process.exit(1);}",
        "if(m.sendErrorText(422,'permission_denied:stale')!=='送信の許可がありません(承認が取り消された、または使用済みです)'){console.error('sp');process.exit(1);}",
        "if(m.cancelErrorText(409,'job_not_cancellable')!=='すでに送信が終わっているため、取り消せません'){console.error('c409');process.exit(1);}",
        "{const a=m.nextTicket();const b=m.nextTicket();if(!m.isCurrentTicket(b)){console.error('tick cur');process.exit(1);}if(m.isCurrentTicket(a)){console.error('tick stale');process.exit(1);}}",
    ]
    probe = "\n".join([
        "const fs=require('fs');",
        "const vm=require('vm');",
        "const src=fs.readFileSync(" + json.dumps(str(ROOT / "app/static/planning_guard_jobs.js")) + ",'utf8');",
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


def test_node_validation_functions():
    problems = _probe_declaration_problems(JOBS_JS)
    assert not problems, problems
    _require_node()
    good_approval = json.dumps(_approval_row())
    good_job = json.dumps({"id": "j1", "purpose": "plan_generation", "provider": "kp",
                           "status": "sending", "created_at": 1700000001})
    cases = [
        "const goodAppr=" + good_approval + ";",
        "const goodJob=" + good_job + ";",
        "if(!m.isValidApproval(goodAppr)){console.error('good appr');process.exit(1);}",
        "if(m.isValidApproval(Object.assign({},goodAppr,{text:'x'}))){console.error('appr leak');process.exit(1);}",
        "if(!m.isValidApprovals({approvals:[goodAppr]})){console.error('approvals ok');process.exit(1);}",
        "if(m.isValidApprovals({approvals:[goodAppr,{packet_id:'x'}]})){console.error('approvals bad');process.exit(1);}",
        "if(!m.isValidJob(goodJob)){console.error('good job');process.exit(1);}",
        "if(m.isValidJob({id:1})){console.error('bad job');process.exit(1);}",
        "if(!m.isValidJobs({jobs:[goodJob]})){console.error('jobs ok');process.exit(1);}",
        "if(m.isValidJobs({jobs:[goodJob,{id:1}]})){console.error('jobs bad');process.exit(1);}",
        "if(!m.isValidJobKey({packet_id:'p',purpose:'plan_generation',provider:'kp',idempotency_key:'k'})){console.error('key ok');process.exit(1);}",
        "if(m.isValidJobKey({packet_id:'p',purpose:'plan_generation',provider:'kp',idempotency_key:'k',text:'x'})){console.error('key leak');process.exit(1);}",
    ]
    probe = "\n".join([
        "const fs=require('fs');",
        "const vm=require('vm');",
        "const src=fs.readFileSync(" + json.dumps(str(ROOT / "app/static/planning_guard_jobs.js")) + ",'utf8');",
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


def test_node_disabled_feature_hides_send():
    rows = json.dumps([_approval_row()])
    _node_mount(_base_scenario(
        approvals_js=rows, plan_js="[]", review_js="[]",
        plan_status=403, review_status=200, steps_js="""
        await tick(); await tick(); await tick(); await tick();
        const seen = seenText();
        if (!seen.includes('この機能は管理者の設定で無効です')) { throw Error('disabled missing: ' + seen); }
        if (!findBtn('送る内容を確認する').every(b => b.disabled)) { throw Error('send must be hidden or disabled'); }
    """))


def test_node_capability_flag_disables_send_even_when_job_list_is_readable():
    rows = json.dumps([_approval_row()])
    _node_mount(_base_scenario(
        approvals_js=rows, plan_js="[]", review_js="[]",
        capabilities_js=json.dumps({"plan_generation": False, "plan_review": False}),
        steps_js="""
        await tick(); await tick(); await tick(); await tick();
        if (!seenText().includes('この機能は管理者の設定で無効です')) { throw Error('flag disabled must be visible'); }
        const buttons = findBtn('送る内容を確認する');
        if (!buttons.length || !buttons.every(b => b.disabled)) { throw Error('flag disabled must block send control'); }
    """))


def test_node_jobs_fetch_failure_disables_send():
    rows = json.dumps([_approval_row()])
    _node_mount(_base_scenario(
        approvals_js=rows, plan_js="[]", review_js="[]",
        plan_status=500, review_status=200, plan_detail="boom",
        steps_js="""
        await tick(); await tick(); await tick(); await tick();
        const seen = seenText();
        if (!seen.includes('取得できませんでした')) { throw Error('fetch failure missing: ' + seen); }
        if (!findBtn('送る内容を確認する').every(b => b.disabled)) { throw Error('send must be disabled'); }
    """))


def test_node_send_requires_name_and_check():
    rows = json.dumps([_approval_row()])
    _node_mount(_base_scenario(
        approvals_js=rows, plan_js="[]", review_js="[]",
        steps_js="""
        await tick(); await tick(); await tick(); await tick();
        const openers = findBtn('送る内容を確認する');
        if (openers.length !== 1) { throw Error('opener missing'); }
        openers[0].click();
        await tick(); await tick();
        const seen = seenText();
        if (!seen.includes('abcd')) { throw Error('full text missing'); }
        const btns = findBtn('この内容を送信する');
        if (btns.length !== 1) { throw Error('send missing'); }
        const btn = btns[0];
        if (!btn.disabled) { throw Error('send must require name+check'); }
        btn.click();
        await tick();
        if (countCalls('/external-plan-jobs') !== 0) { throw Error('posted without checks'); }
        byId('planningGuardJobsApprover').value = 'tester';
        byId('planningGuardJobsApprover').input();
        if (!btn.disabled) { throw Error('send must still require check'); }
        btn.click();
        await tick();
        if (countCalls('/external-plan-jobs') !== 0) { throw Error('posted without check'); }
    """))


def test_node_send_mismatch_does_not_proceed():
    rows = json.dumps([_approval_row()])
    bad_packet = json.dumps({"id": "pkt1", "purpose": "plan_review", "provider": "other",
                             "plan_signature": "sig", "sha256": "ff" * 32, "chars": 9,
                             "included_fields": ["goal"], "withheld_fields": [],
                             "withheld_count": 0, "privacy": {"ok": True, "codes": []},
                             "status": "approved", "created_at": 1700000000, "text": "zz"})
    _node_mount(_base_scenario(
        approvals_js=rows, plan_js="[]", review_js="[]", packet_js=bad_packet,
        steps_js="""
        await tick(); await tick(); await tick(); await tick();
        findBtn('送る内容を確認する')[0].click();
        await tick(); await tick();
        const seen = seenText();
        if (!seen.includes('確認できず')) { throw Error('mismatch must be unknown'); }
        if (findBtn('この内容を送信する').length !== 0) { throw Error('must not proceed'); }
    """))


def test_node_send_flow_posts_key_then_create():
    rows = json.dumps([_approval_row()])
    _node_mount(_base_scenario(
        approvals_js=rows, plan_js="[]", review_js="[]",
        steps_js="""
        await tick(); await tick(); await tick(); await tick();
        findBtn('送る内容を確認する')[0].click();
        await tick(); await tick();
        byId('planningGuardJobsApprover').value = 'tester';
        byId('planningGuardJobsApprover').input();
        byId('planningGuardJobsCheck').checked = true;
        byId('planningGuardJobsCheck').change();
        findBtn('この内容を送信する')[0].click();
        await tick(); await tick(); await tick(); await tick();
        const seen = seenText();
        if (!seen.includes('送信を開始しました')) { throw Error('202 wording missing: ' + seen); }
        const calls = sc.calls.filter(c => c[1].indexOf('/external-plan-jobs') >= 0 && c[0] === 'POST');
        if (calls.length !== 1) { throw Error('create must post once'); }
    """))


def test_node_send_200_403_422_words():
    rows = json.dumps([_approval_row()])
    _node_mount(_base_scenario(
        approvals_js=rows, plan_js="[]", review_js="[]", create_js=_job_json(),
        steps_js="""
        sc.createMode = '200';
        await tick(); await tick(); await tick(); await tick();
        findBtn('送る内容を確認する')[0].click();
        await tick(); await tick();
        byId('planningGuardJobsApprover').value = 'tester';
        byId('planningGuardJobsApprover').input();
        byId('planningGuardJobsCheck').checked = true;
        byId('planningGuardJobsCheck').change();
        findBtn('この内容を送信する')[0].click();
        await tick(); await tick(); await tick(); await tick();
        if (!seenText().includes('すでに送信されています')) { throw Error('200 wording missing'); }
    """))
    _node_mount(_base_scenario(
        approvals_js=rows, plan_js="[]", review_js="[]",
        steps_js="""
        sc.createMode = '403';
        await tick(); await tick(); await tick(); await tick();
        findBtn('送る内容を確認する')[0].click();
        await tick(); await tick();
        byId('planningGuardJobsApprover').value = 'tester';
        byId('planningGuardJobsApprover').input();
        byId('planningGuardJobsCheck').checked = true;
        byId('planningGuardJobsCheck').change();
        findBtn('この内容を送信する')[0].click();
        await tick(); await tick(); await tick();
        const seen = seenText();
        if (!seen.includes('この機能は管理者の設定で無効です')) { throw Error('403 wording missing'); }
        if (seen.includes('feature_disabled')) { throw Error('raw code shown'); }
    """))
    _node_mount(_base_scenario(
        approvals_js=rows, plan_js="[]", review_js="[]",
        steps_js="""
        sc.createMode = '422quota';
        await tick(); await tick(); await tick(); await tick();
        findBtn('送る内容を確認する')[0].click();
        await tick(); await tick();
        byId('planningGuardJobsApprover').value = 'tester';
        byId('planningGuardJobsApprover').input();
        byId('planningGuardJobsCheck').checked = true;
        byId('planningGuardJobsCheck').change();
        findBtn('この内容を送信する')[0].click();
        await tick(); await tick(); await tick();
        const seen = seenText();
        if (!seen.includes('今日の送信回数の上限(3回)に達しました')) { throw Error('422 wording missing'); }
        if (seen.includes('quota_exceeded')) { throw Error('raw code shown'); }
    """))
    _node_mount(_base_scenario(
        approvals_js=rows, plan_js="[]", review_js="[]",
        steps_js="""
        sc.createMode = 'fail';
        await tick(); await tick(); await tick(); await tick();
        findBtn('送る内容を確認する')[0].click();
        await tick(); await tick();
        byId('planningGuardJobsApprover').value = 'tester';
        byId('planningGuardJobsApprover').input();
        byId('planningGuardJobsCheck').checked = true;
        byId('planningGuardJobsCheck').change();
        findBtn('この内容を送信する')[0].click();
        await tick(); await tick(); await tick(); await tick();
        const seen = seenText();
        if (!seen.includes('送信できたか確認できませんでした')) { throw Error('unknown wording missing'); }
        const rowBtn = findBtn('送る内容を確認する');
        if (!rowBtn.length || !rowBtn.every(b => b.disabled)) { throw Error('row must stay locked after unknown'); }
        if (!findBtn('この内容を送信する').every(b => b.disabled)) { throw Error('send button must not stay enabled'); }
    """))


def test_node_unknown_send_stays_locked_with_older_job_for_same_packet():
    rows = json.dumps([_approval_row()])
    old_job = json.dumps([{"id": "o" * 32, "packet_id": "pkt1",
                           "provider": "kp", "purpose": "plan_generation",
                           "idempotency_key": "KEY-OLD", "status": "failed",
                           "created_at": 1700000000}])
    _node_mount(_base_scenario(
        approvals_js=rows, plan_js=old_job, review_js="[]",
        steps_js="""
        sc.createMode = 'fail';
        await tick(); await tick(); await tick(); await tick();
        findBtn('送る内容を確認する')[0].click();
        await tick(); await tick();
        byId('planningGuardJobsApprover').value = 'tester';
        byId('planningGuardJobsApprover').input();
        byId('planningGuardJobsCheck').checked = true;
        byId('planningGuardJobsCheck').change();
        findBtn('この内容を送信する')[0].click();
        await tick(); await tick(); await tick(); await tick();
        const buttons = findBtn('送る内容を確認する');
        if (!buttons.length || !buttons.every(b => b.disabled)) { throw Error('older job must not unlock unknown send'); }
    """))


def test_node_double_submit_single_post():
    rows = json.dumps([_approval_row()])
    _node_mount(_base_scenario(
        approvals_js=rows, plan_js="[]", review_js="[]",
        steps_js="""
        sc.createHold = true;
        await tick(); await tick(); await tick(); await tick();
        findBtn('送る内容を確認する')[0].click();
        await tick(); await tick();
        byId('planningGuardJobsApprover').value = 'tester';
        byId('planningGuardJobsApprover').input();
        byId('planningGuardJobsCheck').checked = true;
        byId('planningGuardJobsCheck').change();
        const btn = findBtn('この内容を送信する')[0];
        btn.click(); btn.click(); btn.click();
        await tick();
        if (countCalls('/external-plan-jobs') !== 1) { throw Error('double submit'); }
    """))


def test_node_purpose_endpoints_and_unsupported():
    rows = json.dumps([_approval_row(packet_id="p1", purpose="plan_generation"),
                       _approval_row(packet_id="p2", purpose="plan_review", provider="kq"),
                       _approval_row(packet_id="p3", purpose="research"),
                       _approval_row(packet_id="p4", purpose="plan_generation", active=False)])
    _node_mount(_base_scenario(
        approvals_js=rows, plan_js="[]", review_js="[]",
        packet_js=json.dumps({"id": "p2", "purpose": "plan_review", "provider": "kq",
                              "plan_signature": "sig", "sha256": "ab" * 32, "chars": 4,
                              "included_fields": ["goal"], "withheld_fields": [],
                              "withheld_count": 0, "privacy": {"ok": True, "codes": []},
                              "status": "approved", "created_at": 1, "text": "revtext"}),
        key_js=json.dumps({"packet_id": "p2", "purpose": "plan_review",
                           "provider": "kq", "idempotency_key": "KEY-2"}),
        create_js=_job_json(purpose="plan_review"),
        steps_js="""
        await tick(); await tick(); await tick(); await tick();
        const openers = findBtn('送る内容を確認する');
        if (openers.length !== 2) { throw Error('only two sendable: ' + openers.length); }
        const seen = seenText();
        if (!seen.includes('この用途は、この画面からは送れません')) { throw Error('unsupported wording missing'); }
        if (seen.includes('p4')) { throw Error('inactive must not show'); }
        openers[1].click();
        await tick(); await tick();
        byId('planningGuardJobsApprover').value = 'tester';
        byId('planningGuardJobsApprover').input();
        byId('planningGuardJobsCheck').checked = true;
        byId('planningGuardJobsCheck').change();
        findBtn('この内容を送信する')[0].click();
        await tick(); await tick(); await tick(); await tick();
        const reviewCalls = sc.calls.filter(c => c[0] === 'POST' && c[1].indexOf('/independent-review-jobs') >= 0);
        const planCalls = sc.calls.filter(c => c[0] === 'POST' && c[1].indexOf('/external-plan-jobs') >= 0);
        if (reviewCalls.length !== 1) { throw Error('review endpoint missing'); }
        if (planCalls.length !== 0) { throw Error('wrong endpoint'); }
    """))


def test_node_jobs_list_words_and_cancel():
    plan_row = json.dumps({"id": "a" * 32, "packet_id": "pkt1", "provider": "kp",
                           "purpose": "plan_generation", "status": "sending",
                           "created_at": 1700000002, "updated_at": 1700000002})
    review_row = json.dumps({"id": "b" * 32, "packet_id": "pkt2", "provider": "kq",
                             "purpose": "plan_review", "status": "review_passed",
                             "created_at": 1700000001, "updated_at": 1700000002})
    leaked_plan = json.dumps(dict(json.loads(plan_row), external_draft={"tasks": []},
                                  unverified_claims=["実施済み"], request_template="TPL",
                                  codes=["x"]))
    _node_mount(_base_scenario(
        approvals_js="[]", plan_js="[" + leaked_plan + "]",
        review_js="[" + review_row + "]",
        steps_js="""
        await tick(); await tick(); await tick(); await tick();
        const seen = seenText();
        if (!seen.includes('送信中')) { throw Error('plan ja missing'); }
        if (!seen.includes('合格')) { throw Error('review ja missing'); }
        if (!seen.includes('合格は実行の許可ではありません')) { throw Error('pass note missing'); }
        if (seen.includes('実施済み') || seen.includes('TPL')) { throw Error('draft leaked'); }
        const btns = findBtn('取り消す');
        if (btns.length !== 1) { throw Error('only sending cancellable'); }
        btns[0].click();
        await tick(); await tick(); await tick(); await tick();
        if (!seenText().includes('取り消しました')) { throw Error('cancel wording missing'); }
    """))


def test_node_jobs_malformed_and_cancel_words():
    _node_mount(_base_scenario(
        approvals_js="[]", plan_js="[]", review_js="[]",
        steps_js="""
        sc.planBad = true;
        await tick(); await tick(); await tick(); await tick();
        findBtn('更新').forEach(b => b.click());
        await tick(); await tick(); await tick(); await tick();
        const seen = seenText();
        if (!seen.includes('確認できず') && !seen.includes('取得できませんでした')) { throw Error('malformed must be unknown: ' + seen); }
    """))
    _node_mount(_base_scenario(
        approvals_js="[]",
        plan_js="[" + _job_json() + "]",
        review_js="[]",
        steps_js="""
        sc.cancelMode = '409';
        await tick(); await tick(); await tick(); await tick();
        findBtn('取り消す')[0].click();
        await tick(); await tick();
        if (!seenText().includes('すでに送信が終わっているため、取り消せません')) { throw Error('409 wording missing'); }
    """))
    _node_mount(_base_scenario(
        approvals_js="[]",
        plan_js="[" + _job_json() + "]",
        review_js="[]",
        steps_js="""
        sc.cancelMode = 'fail';
        await tick(); await tick(); await tick(); await tick();
        findBtn('取り消す')[0].click();
        await tick(); await tick();
        if (!seenText().includes('取り消せたか確認できませんでした')) { throw Error('cancel unknown missing'); }
    """))


def test_node_auto_refresh_only_with_sending():
    _node_mount(_base_scenario(
        approvals_js="[]", plan_js="[" + _job_json() + "]", review_js="[]",
        steps_js="""
        await tick(); await tick(); await tick(); await tick();
        const n0 = timeouts.length;
        if (n0 === 0) { throw Error('auto must be scheduled'); }
        const before = countCalls('/external-plan-jobs', 'GET');
        const entry = timeouts[timeouts.length - 1];
        await entry.fn();
        await tick(); await tick(); await tick();
        const after = countCalls('/external-plan-jobs', 'GET');
        if (after <= before) { throw Error('auto must GET jobs'); }
        if (countCalls('/external-plan-jobs', 'POST') !== 0) { throw Error('auto must not POST'); }
    """))
    _node_mount(_base_scenario(
        approvals_js="[]",
        plan_js="[" + _job_json(status="cancelled") + "]",
        review_js="[]",
        steps_js="""
        await tick(); await tick(); await tick(); await tick();
        if (timeouts.length !== 0) { throw Error('no auto without sending'); }
    """))
    _node_mount(_base_scenario(
        approvals_js="[]", plan_js="[" + _job_json() + "]", review_js="[]",
        steps_js="""
        await tick(); await tick(); await tick(); await tick();
        if (timeouts.length === 0) { throw Error('auto must exist'); }
        sc.planJobs = [];
        sel.value = 'pj2';
        sel.handlers.change();
        await tick(); await tick();
        if (timeouts.length !== 0) { throw Error('pj switch must stop auto'); }
    """))


def test_node_pin_and_stale_responses():
    rows = json.dumps([_approval_row()])
    _node_mount(_base_scenario(
        approvals_js=rows, plan_js="[]", review_js="[]",
        steps_js="""
        await tick(); await tick(); await tick(); await tick();
        findBtn('送る内容を確認する')[0].click();
        await tick(); await tick();
        byId('planningGuardJobsApprover').value = 'tester';
        byId('planningGuardJobsApprover').input();
        byId('planningGuardJobsCheck').checked = true;
        byId('planningGuardJobsCheck').change();
        sel.value = 'pj2';
        findBtn('この内容を送信する')[0].click();
        await tick();
        if (countCalls('/external-plan-jobs') !== 0) { throw Error('must pin to shown pid'); }
        if (!seenText().includes('現在のプロジェクトが変わったため送信しませんでした')) { throw Error('pin message missing'); }
    """))
    evil = json.dumps("<img src=x onerror=alert(1)>")
    evil_packet = json.dumps({"id": "pkt1", "purpose": "plan_generation", "provider": "kp",
                              "plan_signature": "sig", "sha256": "ab" * 32, "chars": 6,
                              "included_fields": ["goal"], "withheld_fields": [],
                              "withheld_count": 0, "privacy": {"ok": True, "codes": []},
                              "status": "approved", "created_at": 1,
                              "text": json.loads(evil)})
    _node_mount(_base_scenario(
        approvals_js=json.dumps([dict(_approval_row(), provider=json.loads(evil),
                                      approver=json.loads(evil))]),
        plan_js="[]", review_js="[]", packet_js=evil_packet,
        steps_js="""
        await tick(); await tick(); await tick(); await tick();
        findBtn('送る内容を確認する')[0].click();
        await tick(); await tick();
        const seen = seenText();
        if (!seen.includes('<img src=x onerror=alert(1)>')) { throw Error('injection text missing'); }
        function hasImg(n) { if (n.tag === 'img') return true; for (const c of (n.children || [])) { if (hasImg(c)) return true; } return false; }
        if (hasImg(panel)) { throw Error('injection element'); }
    """))


def _mutation_static_or_node(name, guard, mutated, scenario_js):
    assert guard in JOBS_JS, name
    assert mutated != JOBS_JS
    assert guard not in mutated, name
    if shutil.which("node") is None:
        return
    completed = _node_mount_result(scenario_js, mutated)
    assert completed.returncode != 0, (name, completed.stderr + completed.stdout)


def test_change_scope_only_expected_files():
    assert (ROOT / "app/static/planning_guard_jobs.js").is_file()
    assert (ROOT / "tests/test_g5b2b_planning_guard_jobs.py").is_file()
    assert WEB_TEXT.count("@app.get('/static/planning_guard_jobs.js')") == 1
    assert WEB_TEXT.count("async def get_plan_packet_job_key") == 1
    assert WEB_TEXT.count("plan-packets/approvals") == 1
    assert WEB_TEXT.count("async def list_plan_packet_approvals") == 1
    jobs_lines = [line for line in INDEX_HTML.splitlines()
                  if "planning_guard_jobs.js" in line]
    assert len(jobs_lines) == 1


def test_mutation_guards_have_regression_tests():
    assert "if(!name||!(checkInput&&checkInput.checked))return;" in JOBS_JS
    assert "if(!!sendLocked[row.packet_id])return;" in JOBS_JS
    assert "if(!pid||currentPid()!==pid)" in JOBS_JS
    assert "if(!isCurrentTicket(ticket)||currentPid()!==pid)return;" in JOBS_JS
    assert "if(!isValidJobs(d))throw Error('bad');" in JOBS_JS
    assert "if(!isValidApprovals(d))throw Error('bad');" in JOBS_JS
    assert "if(!isValidPacketText(d))throw Error('bad');" in JOBS_JS
    assert "if(!isValidJobKey(d))throw Error('bad');" in JOBS_JS
    assert "if(!isValidJob(d))throw Error('bad');" in JOBS_JS
    assert 'packet.get("status") != "approved"' in WEB_TEXT
    assert "if purpose not in" in WEB_TEXT
    assert "purpose_unsupported" in WEB_TEXT
    assert "job_key_unavailable" in WEB_TEXT
    assert "sendLocked" in JOBS_JS
    assert "autoCount>=120" in JOBS_JS
    assert "clearAuto()" in JOBS_JS


# ---- 検証者(独立)が追加した操作の試験。ガードを外した変異で失敗することを確認済み ----
_SEND_STEPS = """
        await tick(); await tick(); await tick(); await tick();
        findBtn('送る内容を確認する')[0].click();
        await tick(); await tick();
        byId('planningGuardJobsApprover').value = 'tester';
        byId('planningGuardJobsApprover').input();
        byId('planningGuardJobsCheck').checked = true;
        byId('planningGuardJobsCheck').change();
"""


def test_node_cancel_is_pinned_to_shown_project():
    _node_mount(_base_scenario(
        approvals_js="[]", plan_js="[" + _job_json() + "]", review_js="[]",
        steps_js="""
        await tick(); await tick(); await tick(); await tick();
        sel.value = 'pj2';
        findBtn('取り消す')[0].click();
        await tick(); await tick();
        if (countCalls('/cancel') !== 0) { throw Error('cancel sent to another project'); }
        if (!seenText().includes('現在のプロジェクトが変わったため取り消しませんでした')) { throw Error('pin message missing'); }
    """))


def test_node_cancel_is_single_and_needs_cancelled_status():
    _node_mount(_base_scenario(
        approvals_js="[]", plan_js="[" + _job_json() + "]", review_js="[]",
        steps_js="""
        await tick(); await tick(); await tick(); await tick();
        sc.holdCancel = true;
        const b = findBtn('取り消す')[0];
        b.click(); b.click();
        await tick();
        if (countCalls('/cancel') !== 1) { throw Error('double click must cancel once'); }
        sc.release();
        await tick(); await tick(); await tick();
        if (countCalls('/cancel') !== 1) { throw Error('cancel sent more than once'); }
    """))
    _node_mount(_base_scenario(
        approvals_js="[]", plan_js="[" + _job_json() + "]", review_js="[]",
        cancel_js=_job_json(status="sending"),
        steps_js="""
        await tick(); await tick(); await tick(); await tick();
        findBtn('取り消す')[0].click();
        await tick(); await tick(); await tick(); await tick();
        const seen = seenText();
        if (seen.includes('取り消しました')) { throw Error('not-cancelled response shown as cancelled'); }
        if (!seen.includes('取り消せたか確認できませんでした')) { throw Error('unknown wording missing'); }
    """))


def test_node_only_sendable_purposes_can_be_cancelled():
    odd = json.dumps({"id": "c" * 32, "packet_id": "pkt9", "provider": "kp",
                      "purpose": "research", "status": "sending",
                      "created_at": 1700000003, "updated_at": 1700000003})
    _node_mount(_base_scenario(
        approvals_js="[]", plan_js="[" + odd + "]", review_js="[]",
        steps_js="""
        await tick(); await tick(); await tick(); await tick();
        if (findBtn('取り消す').length !== 0) { throw Error('unsupported purpose must not be cancellable'); }
    """))


def test_node_malformed_job_list_shows_nothing_of_it():
    _node_mount(_base_scenario(
        approvals_js="[]", plan_js="[]", review_js="[]",
        steps_js="""
        sc.planBad = true;
        await tick(); await tick(); await tick(); await tick();
        findBtn('更新').forEach(b => b.click());
        await tick(); await tick(); await tick(); await tick();
        const seen = seenText();
        if (!seen.includes('取得できませんでした')) { throw Error('malformed list must be unreadable'); }
        if (seen.includes('ID：1')) { throw Error('malformed job rendered'); }
    """))


def test_node_job_key_mismatch_sends_nothing():
    rows = json.dumps([_approval_row()])
    other = json.dumps({"packet_id": "pkt1", "purpose": "plan_generation",
                        "provider": "other-provider", "idempotency_key": "KEY-X"})
    _node_mount(_base_scenario(
        approvals_js=rows, plan_js="[]", review_js="[]", key_js=other,
        steps_js=_SEND_STEPS + """
        findBtn('この内容を送信する')[0].click();
        await tick(); await tick(); await tick(); await tick();
        if (countCalls('/external-plan-jobs') !== 0) { throw Error('sent with a mismatching key'); }
        if (!seenText().includes('何も送信していません')) { throw Error('nothing-sent wording missing'); }
    """))


def test_node_packet_text_mismatch_does_not_open_confirm():
    rows = json.dumps([_approval_row()])
    other = json.loads(_packet_text_json())
    other["sha256"] = "cd" * 32
    _node_mount(_base_scenario(
        approvals_js=rows, plan_js="[]", review_js="[]", packet_js=json.dumps(other),
        steps_js="""
        await tick(); await tick(); await tick(); await tick();
        findBtn('送る内容を確認する')[0].click();
        await tick(); await tick(); await tick();
        if (byId('planningGuardJobsText')) { throw Error('mismatching text shown for sending'); }
        if (byId('planningGuardJobsSend')) { throw Error('send controls shown for mismatching text'); }
    """))


def test_node_create_with_malformed_job_is_unknown_not_success():
    rows = json.dumps([_approval_row()])
    _node_mount(_base_scenario(
        approvals_js=rows, plan_js="[]", review_js="[]",
        steps_js="""
        sc.createBad = true;
""" + _SEND_STEPS + """
        findBtn('この内容を送信する')[0].click();
        await tick(); await tick(); await tick(); await tick();
        const seen = seenText();
        if (seen.includes('すでに送信されています') || seen.includes('送信を開始しました')) { throw Error('malformed response shown as success'); }
        if (!seen.includes('送信できたか確認できませんでした')) { throw Error('unknown wording missing'); }
    """))


def test_node_stale_packet_response_is_dropped_after_pj_switch():
    rows = json.dumps([_approval_row()])
    _node_mount(_base_scenario(
        approvals_js=rows, plan_js="[]", review_js="[]",
        steps_js="""
        await tick(); await tick(); await tick(); await tick();
        sc.holdPacket = true;
        findBtn('送る内容を確認する')[0].click();
        await tick();
        sc.approvals = [];
        sel.value = 'pj2';
        sel.handlers.change();
        await tick(); await tick();
        sc.release();
        await tick(); await tick(); await tick();
        if (byId('planningGuardJobsText')) { throw Error('stale packet text shown'); }
        if (byId('planningGuardJobsSend')) { throw Error('stale send controls shown'); }
    """))
