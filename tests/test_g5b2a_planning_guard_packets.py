"""G5-b2a 送付内容の承認(操作部)のテスト。疑似PJのみ・実在値なし。

Node の擬似DOM。Node が無い環境では描画試験をスキップする。
スキップは成功ではなく、静的な検査と変異の代替検査で補う。
一覧は GET /plan-packets/approvals だけに頼る(監査由来の導出はしない)。
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
PACKETS_JS = (ROOT / "app/static/planning_guard_packets.js").read_text(encoding="utf-8")
PANEL_JS = (ROOT / "app/static/planning_guard_panel.js").read_text(encoding="utf-8")
SETTINGS_JS = (ROOT / "app/static/planning_guard_settings.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
WEB_TEXT = (ROOT / "app/web.py").read_text(encoding="utf-8")

DANGEROUS = ("innerHTML", "insertAdjacentHTML", "outerHTML", "document.write",
             "eval", "new Function")


def test_static_no_dangerous_and_methods():
    for token in DANGEROUS:
        assert token not in PACKETS_JS, token
    for token in ("console.log", "console.info", "console.debug",
                  "console.warn", "console.error"):
        assert token not in PACKETS_JS, token
    for token in ("localStorage", "sessionStorage", "indexedDB", "IndexedDB"):
        assert token not in PACKETS_JS, token
    assert "planningGuardPacketsSection" in PACKETS_JS
    assert "外部AIへ送る内容の承認" in PACKETS_JS
    assert "1回だけ送ってよい" in PACKETS_JS
    assert "送信は別の操作です" in PACKETS_JS
    assert "送信そのものではありません" in PACKETS_JS
    assert "内容を確認する" in PACKETS_JS
    assert "承認しました(送信はまだ行われていません。送信は別の操作です)" in PACKETS_JS
    assert "取り消しました" in PACKETS_JS
    assert "確認できず" in PACKETS_JS
    for word in ("external-plan-jobs", "independent-review-jobs"):
        assert word not in PACKETS_JS, word
    assert "'GET'" in PACKETS_JS or '"GET"' in PACKETS_JS
    assert "'POST'" in PACKETS_JS or '"POST"' in PACKETS_JS
    assert "PUT" not in PACKETS_JS
    assert "DELETE" not in PACKETS_JS
    assert "createElement('form')" not in PACKETS_JS
    assert 'createElement("form")' not in PACKETS_JS
    assert "<form" not in PACKETS_JS
    assert "http://" not in PACKETS_JS
    assert "https://" not in PACKETS_JS
    for frag in ("createElement('button')", "createElement('select')",
                 "createElement('input')", "createElement('textarea')"):
        assert frag in PACKETS_JS, frag
    for frag in ('createElement("button")', 'createElement("select")',
                 'createElement("input")', 'createElement("form")',
                 'createElement("textarea")'):
        assert frag not in PACKETS_JS, frag
    for word in ("実行中は承認できません",
                 "計画が変わったため承認できません",
                 "現在のプロジェクトが変わったため承認しませんでした",
                 "内容が変わっているため承認できません",
                 "再検査で送れない内容が見つかりました",
                 "承認者名を入力してください",
                 "この送付内容は承認できません",
                 "確認に失敗しました",
                 "承認できませんでした",
                 "承認できたか確認できませんでした。一覧を更新して確認してください",
                 "承認済みではありません",
                 "取り消せませんでした",
                 "取り消せたか確認できませんでした",
                 "現在のプロジェクトが変わったため取り消しませんでした",
                 "この内容は送れません",
                 "検査に失敗(安全のため止めています)",
                 "送れない内容を検出しました"):
        assert word in PACKETS_JS, word
    assert "/plan-packets/approvals" in PACKETS_JS
    assert "apiApprovals" in PACKETS_JS
    assert "isValidApproval" in PACKETS_JS
    assert "isValidApprovals" in PACKETS_JS
    assert "inactiveReasonText" in PACKETS_JS
    for word in ("apiAudit", "deriveApproved", "isAuditRow",
                 "isValidAudit", "STATE_ACTIONS", "STATE_CHANGING"):
        assert word not in PACKETS_JS, word
    for word in ("有効", "承認は無効です:",
                 "計画方式が「外部AIあり」ではありません",
                 "設定が変更されました",
                 "許可がありません",
                 "承認時の内容と一致しません",
                 "計画が変わりました",
                 "内容が変わっています",
                 "設定された提供元と一致しないため承認できません",
                 "計画の確認に失敗したため承認できません"):
        assert word in PACKETS_JS, word
    assert "/plan-packets/audit" not in PACKETS_JS


def test_internal_words_not_shown_raw():
    for match in re.finditer(r"textContent\s*=\s*('([^']*)'|\"([^\"]*)\")", PACKETS_JS):
        literal = match.group(2) if match.group(2) is not None else match.group(3)
        for word in ("unknown_purpose", "provider_required", "invalid_binding",
                     "unknown_field", "invalid_item", "no_public_items",
                     "packet_not_found", "packet_not_previewed", "plan_changed",
                     "packet_tampered", "privacy_recheck_failed",
                     "approver_required", "check_error", "secret", "blocked_term",
                     "method_not_external", "provider_mismatch", "settings_changed",
                     "no_permission", "binding_mismatch"):
            if word in (literal or ""):
                raise AssertionError((word, literal))


def test_index_tag_one_line_and_order_kept():
    panel_lines = [line for line in INDEX_HTML.splitlines()
                   if "planning_guard_panel.js" in line]
    assert len(panel_lines) == 1, panel_lines
    settings_lines = [line for line in INDEX_HTML.splitlines()
                      if "planning_guard_settings.js" in line]
    assert len(settings_lines) == 1, settings_lines
    added = [line for line in INDEX_HTML.splitlines()
             if "planning_guard_packets.js" in line]
    assert len(added) == 1, added
    assert "v=20261105-g5b2a" in added[0]
    for token in DANGEROUS:
        assert token not in added[0], token
    panel_tag = '<script src="/static/planning_guard_panel.js?v=20261030-g5a"></script>'
    settings_tag = '<script src="/static/planning_guard_settings.js?v=20261031-g5b1"></script>'
    packets_tag = '<script src="/static/planning_guard_packets.js?v=20261105-g5b2a"></script>'
    assert INDEX_HTML.count(panel_tag) == 1
    assert INDEX_HTML.count(settings_tag) == 1
    assert INDEX_HTML.count(packets_tag) == 1
    assert panel_tag + settings_tag + packets_tag in INDEX_HTML
    order = ["presenters.js", "project_mission.js", "local_models.js",
             "agent_examples.js", "procedure_learning.js", "triz_invention.js",
             "triz_general.js", "review_queue.js", "workflow_readiness.js",
             "artifact_shelf.js", "goal_review.js", "plan_repair_cards.js",
             "plan_review_loop.js", "resolution_cards.js", "project_reset.js",
             "capability_gap.js", "domain_pack.js", "goal_state.js",
             "watch_panel.js", "ledger_panel.js", "night_run_panel.js",
             "shadow_panel.js", "contact_channel_panel.js",
             "planning_guard_panel.js", "planning_guard_settings.js",
             "planning_guard_packets.js",
             "project_delete.js", "project_management.js", "ocr_review.js",
             "experience_import.js"]
    positions = []
    for name in order:
        matches = [i for i, line in enumerate(INDEX_HTML.splitlines()) if name in line]
        assert matches, name
        assert len(matches) == 1, (name, matches)
        positions.append(matches[0])
    assert positions == sorted(positions), positions


def test_web_route_and_scope():
    import app.web as web
    routes = {str(getattr(r, "path", "")) for r in web.app.routes}
    assert "/static/planning_guard_packets.js" in routes
    assert "/static/planning_guard_settings.js" in routes
    assert "/static/planning_guard_panel.js" in routes
    assert "/api/projects/{project_id}/plan-packets/preview" in routes
    assert "/api/projects/{project_id}/plan-packets/approvals" in routes
    assert "/api/projects/{project_id}/plan-packets/audit" in routes
    assert WEB_TEXT.count("@app.get('/static/planning_guard_packets.js')") == 1
    assert WEB_TEXT.count("@app.get('/static/planning_guard_panel.js')") == 1
    assert WEB_TEXT.count("@app.get('/static/planning_guard_settings.js')") == 1
    assert WEB_TEXT.count("plan-packets/approvals") == 1
    assert WEB_TEXT.count("async def list_plan_packet_approvals") == 1
    assert "planningGuardPacketsSection" not in PANEL_JS
    assert "planningGuardPacketsSection" not in SETTINGS_JS
    assert "planning_guard_packets" not in PANEL_JS
    assert "planning_guard_packets" not in SETTINGS_JS
    for name in ("project_mission.js", "goal_state.js", "watch_panel.js",
                 "ledger_panel.js", "night_run_panel.js", "shadow_panel.js",
                 "workflow_readiness.js", "contact_channel_panel.js"):
        text = (ROOT / "app/static" / name).read_text(encoding="utf-8")
        assert "planningGuardPacketsSection" not in text, name
        assert "planning_guard_packets" not in text, name
    for path in sorted((ROOT / "app").glob("*.py")):
        if path.name == "web.py":
            continue
        text = path.read_text(encoding="utf-8")
        assert "planning_guard_packets" not in text, path.name
        assert "planningGuardPacketsSection" not in text, path.name


def test_packets_static_route_serves_js():
    from fastapi.testclient import TestClient
    import app.web as web
    client = TestClient(web.app, raise_server_exceptions=False)
    response = client.get("/static/planning_guard_packets.js")
    assert response.status_code == 200
    assert "planningGuardPacketsSection" in response.text
    assert response.headers.get("content-type", "").startswith("text/javascript")


def _server_client(tmp_path, monkeypatch):
    from app.memory.short_term import ShortTermMemory
    import app.web as web
    from fastapi.testclient import TestClient
    mem_path = tmp_path / "g5b2a-api.db"
    memory = ShortTermMemory(mem_path)
    pid = memory.create_project("G5B2A")["id"]
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


APPROVAL_KEYS = {"packet_id", "purpose", "provider", "sha256", "chars",
                 "approver", "approved_at", "active", "inactive_reason"}


def test_server_approvals_read_failure_is_not_empty_list(tmp_path, monkeypatch):
    client, _memory, pid = _server_client(tmp_path, monkeypatch)
    from app.goal_review import ReviewStore
    original_list = ReviewStore.list

    def broken_list(self, project_id, kind):
        if kind == "plan_packet":
            raise OSError("read failed")
        return original_list(self, project_id, kind)

    monkeypatch.setattr(ReviewStore, "list", broken_list)
    res = client.get(f"/api/projects/{pid}/plan-packets/approvals")
    assert res.status_code == 503
    assert res.json()["detail"] == "approval_list_unavailable"


def test_server_approvals_basic_no_text(tmp_path, monkeypatch):
    client, _memory, pid = _server_client(tmp_path, monkeypatch)
    _enable_external(client, pid)
    packet_id = _preview_approve(client, pid)
    res = client.get(f"/api/projects/{pid}/plan-packets/approvals")
    assert res.status_code == 200, res.text
    body = res.json()
    assert set(body.keys()) == {"approvals"}
    assert isinstance(body["approvals"], list) and len(body["approvals"]) == 1
    item = body["approvals"][0]
    assert set(item.keys()) == APPROVAL_KEYS
    assert "text" not in body and "text" not in item
    assert item["packet_id"] == packet_id
    assert item["purpose"] == "plan_generation"
    assert item["provider"] == "fake-gen-a"
    assert isinstance(item["sha256"], str) and item["sha256"]
    assert isinstance(item["chars"], int)
    assert item["approver"] == "tester"
    assert isinstance(item["approved_at"], (int, float))
    assert item["active"] is True
    assert item["inactive_reason"] == ""


def test_server_approvals_excludes_non_approved(tmp_path, monkeypatch):
    client, _memory, pid = _server_client(tmp_path, monkeypatch)
    _enable_external(client, pid)
    res = client.post(f"/api/projects/{pid}/plan-packets/preview",
                      json={"purpose": "plan_generation", "provider": "fake-gen-a",
                            "public_items": {"goal": "下書きです"},
                            "withheld_fields": []})
    assert res.status_code == 200
    previewed_id = res.json()["id"]
    res = client.post(f"/api/projects/{pid}/plan-packets/preview",
                      json={"purpose": "plan_generation", "provider": "fake-gen-a",
                            "public_items": {"goal": "連絡 blocked-test@example.com"},
                            "withheld_fields": []})
    assert res.status_code == 200
    assert res.json()["status"] == "blocked"
    blocked_id = res.json()["id"]
    approved_id = _preview_approve(client, pid, goal="承認して取消す内容です")
    res = client.post(f"/api/projects/{pid}/plan-packets/{approved_id}/revoke",
                      json={"approver": "tester"})
    assert res.status_code == 200
    res = client.get(f"/api/projects/{pid}/plan-packets/approvals")
    assert res.status_code == 200
    ids = [x["packet_id"] for x in res.json()["approvals"]]
    assert previewed_id not in ids
    assert blocked_id not in ids
    assert approved_id not in ids
    assert res.json()["approvals"] == []


def test_server_approvals_regression_bug1_supersede(tmp_path, monkeypatch):
    client, _memory, pid = _server_client(tmp_path, monkeypatch)
    _enable_external(client, pid)
    old_id = _preview_approve(client, pid, goal="古い送付内容です")
    new_id = _preview_approve(client, pid, goal="新しい送付内容です")
    assert old_id != new_id
    res = client.get(f"/api/projects/{pid}/plan-packets/approvals")
    assert res.status_code == 200
    ids = [x["packet_id"] for x in res.json()["approvals"]]
    assert new_id in ids
    assert old_id not in ids
    assert len(ids) == 1


def test_server_approvals_regression_bug2_settings_changed(tmp_path, monkeypatch):
    client, _memory, pid = _server_client(tmp_path, monkeypatch)
    _enable_external(client, pid)
    packet_id = _preview_approve(client, pid)
    res = client.get(f"/api/projects/{pid}/plan-packets/approvals")
    assert res.json()["approvals"][0]["active"] is True
    res = client.put(f"/api/projects/{pid}/planning-settings",
                     json={"generator_provider": "fake-other-c"})
    assert res.status_code == 200
    res = client.get(f"/api/projects/{pid}/plan-packets/approvals")
    assert res.status_code == 200
    assert len(res.json()["approvals"]) == 1
    item = res.json()["approvals"][0]
    assert item["packet_id"] == packet_id
    assert item["active"] is False
    assert item["inactive_reason"] == "no_permission"


def test_server_approvals_plan_changed(tmp_path, monkeypatch):
    client, memory, pid = _server_client(tmp_path, monkeypatch)
    _enable_external(client, pid)
    packet_id = _preview_approve(client, pid)
    memory.save_mission(pid, "変わった後の目標", "条件S", "制約C", False, [])
    res = client.get(f"/api/projects/{pid}/plan-packets/approvals")
    assert res.status_code == 200
    assert len(res.json()["approvals"]) == 1
    item = res.json()["approvals"][0]
    assert item["packet_id"] == packet_id
    assert item["active"] is False
    assert item["inactive_reason"] == "plan_changed"


def test_server_approvals_packet_tampered(tmp_path, monkeypatch):
    client, memory, pid = _server_client(tmp_path, monkeypatch)
    _enable_external(client, pid)
    packet_id = _preview_approve(client, pid)
    from app.goal_review import ReviewStore
    store = ReviewStore(memory.path)
    saved = store.get(pid, "plan_packet", packet_id)
    assert saved is not None
    tampered = dict(saved)
    tampered["text"] = str(tampered.get("text") or "") + "x"
    store.put(pid, "plan_packet", packet_id, tampered)
    res = client.get(f"/api/projects/{pid}/plan-packets/approvals")
    assert res.status_code == 200
    assert len(res.json()["approvals"]) == 1
    item = res.json()["approvals"][0]
    assert item["active"] is False
    assert item["inactive_reason"] == "packet_tampered"


def test_server_approvals_readonly_no_audit_growth(tmp_path, monkeypatch):
    client, memory, pid = _server_client(tmp_path, monkeypatch)
    _enable_external(client, pid)
    _preview_approve(client, pid)
    from app.goal_review import ReviewStore
    store = ReviewStore(memory.path)
    before_audit = store.list(pid, "packet_audit")
    before_packets = store.list(pid, "plan_packet")
    before_settings = store.get(pid, "planning_settings", pid)
    n_before = len(before_audit)
    res = client.get(f"/api/projects/{pid}/plan-packets/approvals")
    assert res.status_code == 200
    res = client.get(f"/api/projects/{pid}/plan-packets/approvals")
    assert res.status_code == 200
    after_audit = store.list(pid, "packet_audit")
    after_packets = store.list(pid, "plan_packet")
    after_settings = store.get(pid, "planning_settings", pid)
    assert len(after_audit) == n_before
    assert after_audit == before_audit
    assert after_packets == before_packets
    assert after_settings == before_settings


def test_server_approvals_ignores_job_and_empty_rows(tmp_path, monkeypatch):
    client, memory, pid = _server_client(tmp_path, monkeypatch)
    _enable_external(client, pid)
    packet_id = _preview_approve(client, pid)
    from app.goal_review import ReviewStore
    store = ReviewStore(memory.path)
    store.put(pid, "packet_audit", uuid.uuid4().hex,
              {"packet_id": packet_id, "action": "job_finished",
               "purpose": "plan_generation", "provider": "fake-gen-a",
               "sha256": "ab" * 32, "chars": 1, "approver": "",
               "reason": "", "codes": [], "at": 9999999999.0})
    store.put(pid, "packet_audit", uuid.uuid4().hex,
              {"packet_id": "", "action": "send_denied",
               "purpose": "", "provider": "",
               "sha256": "", "chars": 0, "approver": "",
               "reason": "", "codes": [], "at": 9999999998.0})
    res = client.get(f"/api/projects/{pid}/plan-packets/approvals")
    assert res.status_code == 200, res.text
    body = res.json()
    assert isinstance(body.get("approvals"), list)
    assert len(body["approvals"]) == 1
    assert body["approvals"][0]["packet_id"] == packet_id
    assert body["approvals"][0]["active"] is True


def test_server_approvals_route_order(tmp_path, monkeypatch):
    import app.web as web
    paths = [str(getattr(r, "path", "")) for r in web.app.routes]
    assert "/api/projects/{project_id}/plan-packets/approvals" in paths
    assert "/api/projects/{project_id}/plan-packets/{packet_id}" in paths
    get_routes = []
    for route in web.app.routes:
        path = str(getattr(route, "path", ""))
        methods = {m for m in (getattr(route, "methods", None) or [])}
        if "GET" in methods and path in (
                "/api/projects/{project_id}/plan-packets/approvals",
                "/api/projects/{project_id}/plan-packets/{packet_id}"):
            get_routes.append(path)
    assert get_routes.index(
        "/api/projects/{project_id}/plan-packets/approvals") < get_routes.index(
        "/api/projects/{project_id}/plan-packets/{packet_id}")
    client, _memory, pid = _server_client(tmp_path, monkeypatch)
    _enable_external(client, pid)
    _preview_approve(client, pid)
    res = client.get(f"/api/projects/{pid}/plan-packets/approvals")
    assert res.status_code == 200, res.text
    assert isinstance(res.json().get("approvals"), list)


def test_server_approvals_change_scope(tmp_path, monkeypatch):
    _ = (tmp_path, monkeypatch)
    assert WEB_TEXT.count("async def list_plan_packet_approvals") == 1
    assert WEB_TEXT.count("@app.get('/static/planning_guard_packets.js')") == 1
    assert WEB_TEXT.count("plan-packets/approvals") == 1


def _probe_source(cases):
    probe = [
        "const fs=require('fs');",
        "const vm=require('vm');",
        "const src=fs.readFileSync(" + json.dumps(str(ROOT / "app/static/planning_guard_packets.js")) + ",'utf8');",
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
         "window:{addEventListener(){}},"
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
    assert completed.returncode == 0, completed.stderr + completed.stdout


def test_pure_functions_with_node():
    cases = [
        "if(m.purposeText('plan_generation')!=='計画の作成'){console.error('p1');process.exit(1);}",
        "if(m.purposeText('research')!=='調査'){console.error('p5');process.exit(1);}",
        "if(m.purposeText('nope')!=='確認できず'){console.error('p6');process.exit(1);}",
        "if(m.previewErrorText(422,'unknown_purpose')!=='用途が正しくありません'){console.error('e1');process.exit(1);}",
        "if(m.previewErrorText(422,'no_public_items')!=='送る内容を入力してください'){console.error('e3');process.exit(1);}",
        "if(m.previewErrorText(500,'x')!=='内容を確認できませんでした。入力は保存・送信されていません'){console.error('e5');process.exit(1);}",
        "if(m.blockedReasonText('check_error')!=='検査に失敗(安全のため止めています)'){console.error('b1');process.exit(1);}",
        "if(m.blockedReasonText('phone')!=='送れない内容を検出しました'){console.error('b2');process.exit(1);}",
        "if(m.blockedReasonText('zzz')!=='確認できず'){console.error('b5');process.exit(1);}",
        "if(m.approveErrorText(409,'x')!=='実行中は承認できません'){console.error('a409');process.exit(1);}",
        "if(m.approveErrorText(422,'plan_changed')!=='計画が変わったため承認できません(もう一度内容を確認してください)'){console.error('a1');process.exit(1);}",
        "if(m.approveErrorText(422,'method_not_external')!=='計画方式が「外部AIあり」ではないため承認できません'){console.error('am');process.exit(1);}",
        "if(m.approveErrorText(422,'provider_mismatch')!=='設定された提供元と一致しないため承認できません'){console.error('ap');process.exit(1);}",
        "if(m.approveErrorText(422,'invalid_binding')!=='計画の確認に失敗したため承認できません'){console.error('ai');process.exit(1);}",
        "if(m.approveErrorText(422,'other_x')!=='承認できませんでした'){console.error('a7');process.exit(1);}",
        "if(m.revokeErrorText(422,'packet_not_approved')!=='承認済みではありません'){console.error('r1');process.exit(1);}",
        "if(m.inactiveReasonText('method_not_external')!=='計画方式が「外部AIあり」ではありません'){console.error('i1');process.exit(1);}",
        "if(m.inactiveReasonText('settings_changed')!=='設定が変更されました'){console.error('i2');process.exit(1);}",
        "if(m.inactiveReasonText('no_permission')!=='許可がありません'){console.error('i3');process.exit(1);}",
        "if(m.inactiveReasonText('binding_mismatch')!=='承認時の内容と一致しません'){console.error('i4');process.exit(1);}",
        "if(m.inactiveReasonText('plan_changed')!=='計画が変わりました'){console.error('i5');process.exit(1);}",
        "if(m.inactiveReasonText('packet_tampered')!=='内容が変わっています'){console.error('i6');process.exit(1);}",
        "if(m.inactiveReasonText('zzz')!=='確認できず'){console.error('i8');process.exit(1);}",
        "{const a=m.nextTicket();const b=m.nextTicket();if(!m.isCurrentTicket(b)){console.error('tick cur');process.exit(1);}if(m.isCurrentTicket(a)){console.error('tick stale');process.exit(1);}}",
    ]
    _node_probe(cases)


def test_preview_validation_with_node():
    good_preview = json.dumps({
        "id": "pkt1", "purpose": "plan_generation", "provider": "kp",
        "plan_signature": "sig", "sha256": "ab" * 32, "chars": 4,
        "included_fields": ["goal"], "withheld_fields": ["success_criteria", "constraints"],
        "withheld_count": 2, "privacy": {"ok": True, "codes": []},
        "status": "previewed", "created_at": 1700000000, "text": "abcd",
    })
    good_approval = json.dumps({
        "packet_id": "p1", "purpose": "plan_generation", "provider": "kp",
        "sha256": "ab" * 32, "chars": 4, "approver": "t",
        "approved_at": 100, "active": True, "inactive_reason": "",
    })
    good_inactive = json.dumps({
        "packet_id": "p2", "purpose": "plan_generation", "provider": "kp",
        "sha256": "ab" * 32, "chars": 4, "approver": "t",
        "approved_at": 100, "active": False, "inactive_reason": "plan_changed",
    })
    cases = [
        "const goodPrev=" + good_preview + ";",
        "const goodAppr=" + good_approval + ";",
        "const goodInact=" + good_inactive + ";",
        "if(!m.isValidPreview(goodPrev)){console.error('good prev');process.exit(1);}",
        "if(m.isValidPreview(Object.assign({},goodPrev,{text:''}))){console.error('empty text');process.exit(1);}",
        "if(m.isValidPreview(null)){console.error('null');process.exit(1);}",
        "if(!m.isValidPacketView(Object.assign({},goodPrev,{status:'approved',text:''}),'approved')){console.error('appr view');process.exit(1);}",
        "if(!m.isValidApproval(goodAppr)){console.error('good appr');process.exit(1);}",
        "if(!m.isValidApproval(goodInact)){console.error('good inact');process.exit(1);}",
        "if(m.isValidApproval(Object.assign({},goodAppr,{text:'x'}))){console.error('appr text leak');process.exit(1);}",
        "if(m.isValidApproval(Object.assign({},goodInact,{inactive_reason:''}))){console.error('inact empty');process.exit(1);}",
        "if(m.isValidApproval(Object.assign({},goodAppr,{inactive_reason:'x'}))){console.error('act reason');process.exit(1);}",
        "if(!m.isValidApprovals({approvals:[goodAppr,goodInact]})){console.error('approvals ok');process.exit(1);}",
        "if(m.isValidApprovals({approvals:[goodAppr,Object.assign({},goodInact,{inactive_reason:''})]})){console.error('approvals bad');process.exit(1);}",
        "if(m.isValidApprovals({approvals:'x'})){console.error('approvals type');process.exit(1);}",
    ]
    _node_probe(cases)


def _full_mount_source(scenario_js, source_path=None):
    src_expr = json.dumps(str(ROOT / "app/static/planning_guard_packets.js"))
    if source_path is not None:
        src_expr = json.dumps(str(source_path))
    return "\n".join([
        "const fs=require('fs');",
        "const vm=require('vm');",
        "const src=fs.readFileSync(" + src_expr + ",'utf8');",
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
         "input(){if(this.handlers.input)this.handlers.input();},"
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
        ("sandbox.setTimeout=function(f){return 0;};"
         "sandbox.clearTimeout=function(){};"
         "sandbox.setInterval=function(){return 0;};"
         "sandbox.clearInterval=function(){};"),
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


def _settings_ok_body(providers):
    return json.dumps({"settings": {"method": "external_guarded", "generator_provider": providers[0] if providers else "", "reviewer_provider": ""}, "known_providers": providers})


def test_mount_blocked_hides_text_and_approve_with_node():
    _node_mount("""({
      startPid: 'pj1',
      onFetch: async (url, options) => {
        if (url.indexOf('/plan-packets/preview') >= 0) {
          return {ok: true, status: 200, json: async () => ({id: 'pktb', purpose: 'plan_generation', provider: 'kp', plan_signature: 'sig', sha256: '', chars: 0, included_fields: ['goal'], withheld_fields: ['success_criteria', 'constraints'], withheld_count: 2, privacy: {ok: false, codes: ['phone']}, status: 'blocked', created_at: 1700000000})};
        }
        if (url.indexOf('/plan-packets/approvals') >= 0) {
          return {ok: true, status: 200, json: async () => ({approvals: []})};
        }
        return {ok: true, status: 200, json: async () => (""" + _settings_ok_body(["kp", "kq"]) + """)};
      },
      steps: async ({panel, texts, byId}) => {
        const prov = byId('planningGuardPacketsProvider');
        prov.value = 'kp'; prov.change();
        byId('planningGuardPacketsGoal').value = 'open text';
        byId('planningGuardPacketsGoal').input();
        byId('planningGuardPacketsPreview').click();
        await new Promise(r => setImmediate(r));
        await new Promise(r => setImmediate(r));
        const seen = texts(panel).join('\\n');
        if (!seen.includes('この内容は送れません')) { throw Error('blocked message missing'); }
        if (seen.includes('phone')) { throw Error('raw code shown'); }
        if (byId('planningGuardPacketsText')) { throw Error('blocked must not show text'); }
        if (byId('planningGuardPacketsApprove')) { throw Error('blocked must not show approve'); }
      }
    })""")


def test_mount_previewed_and_approve_gate_with_node():
    _node_mount("""({
      startPid: 'pj1',
      posted: [],
      onFetch: async function(url, options) {
        if (url.indexOf('/plan-packets/preview') >= 0) {
          return {ok: true, status: 200, json: async () => ({id: 'pkt1', purpose: 'plan_generation', provider: 'kp', plan_signature: 'sig', sha256: '""" + "ab" * 32 + """', chars: 4, included_fields: ['goal'], withheld_fields: ['success_criteria', 'constraints'], withheld_count: 2, privacy: {ok: true, codes: []}, status: 'previewed', created_at: 1700000000, text: 'abcd'})};
        }
        if (url.indexOf('/approve') >= 0) {
          const body = JSON.parse(options.body);
          this.posted.push(['approve', body]);
          return {ok: true, status: 200, json: async () => ({id: 'pkt1', purpose: 'plan_generation', provider: 'kp', plan_signature: 'sig', sha256: '""" + "ab" * 32 + """', chars: 4, included_fields: ['goal'], withheld_fields: ['success_criteria', 'constraints'], withheld_count: 2, privacy: {ok: true, codes: []}, status: 'approved', created_at: 1700000000})};
        }
        if (url.indexOf('/plan-packets/approvals') >= 0) {
          return {ok: true, status: 200, json: async () => ({approvals: []})};
        }
        return {ok: true, status: 200, json: async () => (""" + _settings_ok_body(["kp", "kq"]) + """)};
      },
      steps: async ({panel, texts, byId, sandbox}) => {
        byId('planningGuardPacketsProvider').value = 'kp';
        byId('planningGuardPacketsProvider').change();
        byId('planningGuardPacketsGoal').value = 'abcd';
        byId('planningGuardPacketsGoal').input();
        byId('planningGuardPacketsPreview').click();
        await new Promise(r => setImmediate(r));
        await new Promise(r => setImmediate(r));
        const btn = byId('planningGuardPacketsApprove');
        const name = byId('planningGuardPacketsApprover');
        const check = byId('planningGuardPacketsCheck');
        if (!btn || !name || !check) { throw Error('approve controls missing'); }
        if (!btn.disabled) { throw Error('approve must require check+name'); }
        btn.click();
        await new Promise(r => setImmediate(r));
        if (sandbox.scenario.posted.length !== 0) { throw Error('approve without check+name'); }
        name.value = 'tester'; name.input();
        check.checked = true; check.change();
        btn.click();
        await new Promise(r => setImmediate(r));
        await new Promise(r => setImmediate(r));
        if (sandbox.scenario.posted.length !== 1) { throw Error('approve once'); }
        const seen2 = texts(panel).join('\\n');
        if (!seen2.includes('承認しました(送信はまだ行われていません。送信は別の操作です)')) { throw Error('success wording missing'); }
        if (byId('planningGuardPacketsText')) { throw Error('text should clear after approve'); }
      }
    })""")


def test_mount_approve_errors_with_node():
    for code in ['method_not_external', 'provider_mismatch', 'invalid_binding', 'plan_changed', 'other_x']:
        _node_mount("""({
          startPid: 'pj1',
          code: """ + json.dumps(code) + """,
          onFetch: async function(url, options) {
            if (url.indexOf('/plan-packets/preview') >= 0) {
              return {ok: true, status: 200, json: async () => ({id: 'pkt1', purpose: 'plan_generation', provider: 'kp', plan_signature: 'sig', sha256: '""" + "ab" * 32 + """', chars: 4, included_fields: ['goal'], withheld_fields: ['success_criteria', 'constraints'], withheld_count: 2, privacy: {ok: true, codes: []}, status: 'previewed', created_at: 1700000000, text: 'abcd'})};
            }
            if (url.indexOf('/approve') >= 0) {
              return {ok: false, status: 422, json: async () => ({detail: this.code})};
            }
            if (url.indexOf('/plan-packets/approvals') >= 0) {
              return {ok: true, status: 200, json: async () => ({approvals: []})};
            }
            return {ok: true, status: 200, json: async () => (""" + _settings_ok_body(["kp"]) + """)};
          },
          steps: async ({panel, texts, byId}) => {
            byId('planningGuardPacketsProvider').value = 'kp';
            byId('planningGuardPacketsProvider').change();
            byId('planningGuardPacketsGoal').value = 'abcd';
            byId('planningGuardPacketsGoal').input();
            byId('planningGuardPacketsPreview').click();
            await new Promise(r => setImmediate(r));
            await new Promise(r => setImmediate(r));
            byId('planningGuardPacketsApprover').value = 'tester';
            byId('planningGuardPacketsApprover').input();
            byId('planningGuardPacketsCheck').checked = true;
            byId('planningGuardPacketsCheck').change();
            byId('planningGuardPacketsApprove').click();
            await new Promise(r => setImmediate(r));
            await new Promise(r => setImmediate(r));
            const seen = texts(panel).join('\\n');
            if (seen.includes('method_not_external') || seen.includes('provider_mismatch') || seen.includes('invalid_binding') || seen.includes('plan_changed')) { throw Error('raw code shown'); }
            if (seen.includes('承認しました')) { throw Error('failure as success'); }
          }
        })""")


def test_mount_double_submit_is_single_post_with_node():
    _node_mount("""({
      startPid: 'pj1',
      onFetch: async (url, options) => {
        fetchCalls.push([url, (options||{}).method || 'GET']);
        if (url.indexOf('/plan-packets/preview') >= 0) {
          return {ok: true, status: 200, json: async () => ({id: 'pkt1', purpose: 'plan_generation', provider: 'kp', plan_signature: 'sig', sha256: '""" + "ab" * 32 + """', chars: 4, included_fields: ['goal'], withheld_fields: ['success_criteria', 'constraints'], withheld_count: 2, privacy: {ok: true, codes: []}, status: 'previewed', created_at: 1700000000, text: 'abcd'})};
        }
        if (url.indexOf('/approve') >= 0) {
          await new Promise(r => setTimeout(r, 5));
          return {ok: true, status: 200, json: async () => ({id: 'pkt1', purpose: 'plan_generation', provider: 'kp', plan_signature: 'sig', sha256: '""" + "ab" * 32 + """', chars: 4, included_fields: ['goal'], withheld_fields: ['success_criteria', 'constraints'], withheld_count: 2, privacy: {ok: true, codes: []}, status: 'approved', created_at: 1700000000})};
        }
        if (url.indexOf('/plan-packets/approvals') >= 0) {
          return {ok: true, status: 200, json: async () => ({approvals: []})};
        }
        return {ok: true, status: 200, json: async () => (""" + _settings_ok_body(["kp"]) + """)};
      },
      steps: async ({panel, texts, byId}) => {
        byId('planningGuardPacketsProvider').value = 'kp';
        byId('planningGuardPacketsProvider').change();
        byId('planningGuardPacketsGoal').value = 'abcd';
        byId('planningGuardPacketsGoal').input();
        byId('planningGuardPacketsPreview').click();
        await new Promise(r => setImmediate(r));
        await new Promise(r => setImmediate(r));
        byId('planningGuardPacketsApprover').value = 'tester';
        byId('planningGuardPacketsApprover').input();
        byId('planningGuardPacketsCheck').checked = true;
        byId('planningGuardPacketsCheck').change();
        const btn = byId('planningGuardPacketsApprove');
        btn.click(); btn.click(); btn.click();
        await new Promise(r => setImmediate(r));
        await new Promise(r => setImmediate(r));
        await new Promise(r => setImmediate(r));
        const n = fetchCalls.filter(c => c[0].indexOf('/approve') >= 0).length;
        if (n !== 1) { throw Error('double submit: ' + n); }
      }
    })""")


def test_mount_list_approvals_with_node():
    _node_mount("""({
      startPid: 'pj1',
      onFetch: async (url, options) => {
        fetchCalls.push([url, (options||{}).method || 'GET']);
        if (url.indexOf('/revoke') >= 0) {
          return {ok: true, status: 200, json: async () => ({id: 'pA', purpose: 'plan_generation', provider: 'kp', plan_signature: 'sig', sha256: '""" + "ab" * 32 + """', chars: 4, included_fields: ['goal'], withheld_fields: ['success_criteria'], withheld_count: 1, privacy: {ok: true, codes: []}, status: 'revoked', created_at: 1700000000})};
        }
        if (url.indexOf('/plan-packets/approvals') >= 0) {
          if (url.indexOf('/plan-packets/audit') >= 0) { throw Error('audit must not be called'); }
          return {ok: true, status: 200, json: async () => ({approvals: [
            {packet_id: 'pA', purpose: 'plan_generation', provider: 'kp', sha256: '""" + "ab" * 32 + """', chars: 4, approver: 'taro', approved_at: 200, active: true, inactive_reason: ''},
            {packet_id: 'pB', purpose: 'plan_review', provider: 'kq', sha256: '""" + "cd" * 32 + """', chars: 5, approver: 'jiro', approved_at: 150, active: false, inactive_reason: 'plan_changed'}
          ]})};
        }
        return {ok: true, status: 200, json: async () => (""" + _settings_ok_body(["kp", "kq"]) + """)};
      },
      steps: async ({panel, texts, byId}) => {
        await new Promise(r => setImmediate(r));
        await new Promise(r => setImmediate(r));
        const seen = texts(panel).join('\\n');
        if (!seen.includes('taro')) { throw Error('approved missing'); }
        if (!seen.includes('jiro')) { throw Error('inactive must also show'); }
        if (!seen.includes('有効')) { throw Error('active must show'); }
        if (!seen.includes('承認は無効です:')) { throw Error('inactive prefix missing'); }
        if (!seen.includes('計画が変わりました')) { throw Error('inactive japanese missing'); }
        if (seen.includes('plan_changed')) { throw Error('raw code shown'); }
      }
    })""")


def test_mount_approvals_malformed_and_failure_with_node():
    _node_mount("""({
      startPid: 'pj1',
      onFetch: async (url, options) => {
        if (url.indexOf('/plan-packets/approvals') >= 0) {
          return {ok: true, status: 200, json: async () => ({approvals: [{packet_id: 'bad'}]})};
        }
        return {ok: true, status: 200, json: async () => (""" + _settings_ok_body(["kp"]) + """)};
      },
      steps: async ({panel, texts, byId}) => {
        await new Promise(r => setImmediate(r));
        await new Promise(r => setImmediate(r));
        const seen = texts(panel).join('\\n');
        if (!seen.includes('確認できず')) { throw Error('malformed must be unknown'); }
      }
    })""")
    _node_mount("""({
      startPid: 'pj1',
      onFetch: async (url, options) => {
        if (url.indexOf('/plan-packets/approvals') >= 0) { throw Error('net down'); }
        return {ok: true, status: 200, json: async () => (""" + _settings_ok_body(["kp"]) + """)};
      },
      steps: async ({panel, texts, byId}) => {
        await new Promise(r => setImmediate(r));
        await new Promise(r => setImmediate(r));
        const seen = texts(panel).join('\\n');
        if (!seen.includes('確認できず')) { throw Error('failure must be unknown'); }
      }
    })""")


def test_mount_pj_switch_and_pin_with_node():
    _node_mount("""({
      startPid: 'pjA',
      onFetch: async function(url, options) {
        const pid = decodeURIComponent(url.split('/')[3]);
        if (url.indexOf('/plan-packets/preview') >= 0) {
          if (pid === 'pjA') {
            return new Promise(res => { this.hold = () => res({ok: true, status: 200, json: async () => ({id: 'pktA', purpose: 'plan_generation', provider: 'kp', plan_signature: 'sig', sha256: '""" + "ab" * 32 + """', chars: 9, included_fields: ['goal'], withheld_fields: ['success_criteria', 'constraints'], withheld_count: 2, privacy: {ok: true, codes: []}, status: 'previewed', created_at: 1700000000, text: 'text-of-pjA'})}); });
          }
          return {ok: true, status: 200, json: async () => ({id: 'pktB', purpose: 'plan_generation', provider: 'kp', plan_signature: 'sig', sha256: '""" + "cd" * 32 + """', chars: 9, included_fields: ['goal'], withheld_fields: ['success_criteria', 'constraints'], withheld_count: 2, privacy: {ok: true, codes: []}, status: 'previewed', created_at: 1700000000, text: 'text-of-pjB'})};
        }
        if (url.indexOf('/plan-packets/approvals') >= 0) {
          return {ok: true, status: 200, json: async () => ({approvals: []})};
        }
        return {ok: true, status: 200, json: async () => ({settings: {method: 'external_guarded', generator_provider: 'kp', reviewer_provider: ''}, known_providers: ['kp']})};
      },
      steps: async ({panel, texts, byId, sel, sandbox}) => {
        byId('planningGuardPacketsProvider').value = 'kp';
        byId('planningGuardPacketsProvider').change();
        byId('planningGuardPacketsGoal').value = 'x';
        byId('planningGuardPacketsGoal').input();
        byId('planningGuardPacketsPreview').click();
        await new Promise(r => setImmediate(r));
        sel.value = 'pjB';
        sel.handlers.change();
        await new Promise(r => setImmediate(r));
        await new Promise(r => setImmediate(r));
        sandbox.scenario.hold();
        await new Promise(r => setImmediate(r));
        await new Promise(r => setImmediate(r));
        const seen = texts(panel).join('\\n');
        if (seen.includes('text-of-pjA')) { throw Error('stale shown'); }
      }
    })""")


def test_mount_injection_is_text_with_node():
    evil = json.dumps("<img src=x onerror=alert(1)>")
    _node_mount("""({
      startPid: 'pj1',
      evil: """ + evil + """,
      onFetch: async function(url, options) {
        if (url.indexOf('/plan-packets/preview') >= 0) {
          return {ok: true, status: 200, json: async () => ({id: 'pkt1', purpose: 'plan_generation', provider: 'kp', plan_signature: 'sig', sha256: '""" + "ab" * 32 + """', chars: 28, included_fields: ['goal'], withheld_fields: [], withheld_count: 0, privacy: {ok: true, codes: []}, status: 'previewed', created_at: 1700000000, text: sandbox.scenario.evil})};
        }
        if (url.indexOf('/plan-packets/approvals') >= 0) {
          return {ok: true, status: 200, json: async () => ({approvals: [{packet_id: 'p1', purpose: 'plan_generation', provider: 'kp', sha256: '""" + "ab" * 32 + """', chars: 4, approver: sandbox.scenario.evil, approved_at: 1700000000, active: true, inactive_reason: ''}]})};
        }
        return {ok: true, status: 200, json: async () => ({settings: {method: 'external_guarded', generator_provider: 'kp', reviewer_provider: ''}, known_providers: ['kp']})};
      },
      steps: async ({panel, texts, byId, sandbox}) => {
        byId('planningGuardPacketsProvider').value = 'kp';
        byId('planningGuardPacketsProvider').change();
        byId('planningGuardPacketsGoal').value = 'x';
        byId('planningGuardPacketsGoal').input();
        byId('planningGuardPacketsPreview').click();
        await new Promise(r => setImmediate(r));
        await new Promise(r => setImmediate(r));
        await new Promise(r => setImmediate(r));
        const seen = texts(panel).join('\\n');
        if (!seen.includes(sandbox.scenario.evil)) { throw Error('injection text missing'); }
        function hasImg(n) { if (n.tag === 'img') return true; for (const c of (n.children || [])) { if (hasImg(c)) return true; } return false; }
        if (hasImg(panel)) { throw Error('injection element'); }
      }
    })""")


def _mutation_static_or_node(name, guard, mutated, scenario_js):
    assert guard in PACKETS_JS, name
    assert mutated != PACKETS_JS
    assert guard not in mutated, name
    if shutil.which("node") is None:
        return
    completed = _node_mount_result(scenario_js, mutated)
    assert completed.returncode != 0, (name, completed.stderr + completed.stdout)


def test_mutation_bad_shape_not_success_is_guarded():
    guard = "if(!isValidPreview(d))throw Error('bad');"
    mutated = PACKETS_JS.replace(guard, "if(false)throw Error('bad');")
    scenario = """({
      startPid: 'pj1',
      onFetch: async (url, options) => {
        if (url.indexOf('/plan-packets/preview') >= 0) {
          return {ok: true, status: 200, json: async () => ({id: '', broken: true})};
        }
        if (url.indexOf('/plan-packets/approvals') >= 0) { return {ok: true, status: 200, json: async () => ({approvals: []})}; }
        return {ok: true, status: 200, json: async () => ({settings: {method: 'external_guarded', generator_provider: 'kp', reviewer_provider: ''}, known_providers: ['kp']})};
      },
      steps: async ({panel, texts, byId}) => {
        byId('planningGuardPacketsProvider').value = 'kp';
        byId('planningGuardPacketsProvider').change();
        byId('planningGuardPacketsGoal').value = 'x';
        byId('planningGuardPacketsGoal').input();
        byId('planningGuardPacketsPreview').click();
        await new Promise(r => setImmediate(r));
        await new Promise(r => setImmediate(r));
        const seen = texts(panel).join('\\n');
        if (seen.includes('確認できず') || seen.includes('内容を確認できませんでした')) { throw Error('mutant must treat bad as success'); }
      }
    })"""
    _mutation_static_or_node("bad-shape", guard, mutated, scenario)


def test_mutation_server_no_status_filter_is_guarded(tmp_path, monkeypatch):
    client, memory, pid = _server_client(tmp_path, monkeypatch)
    _enable_external(client, pid)
    old_id = _preview_approve(client, pid, goal="古い送付内容です")
    new_id = _preview_approve(client, pid, goal="新しい送付内容です")
    from app.goal_review import ReviewStore
    store = ReviewStore(memory.path)
    mutated_ids = [sig for sig, row in store.list(pid, "plan_packet")
                   if isinstance(row, dict)]
    assert old_id in mutated_ids and new_id in mutated_ids
    assert len(mutated_ids) == 2
    res = client.get(f"/api/projects/{pid}/plan-packets/approvals")
    assert [x["packet_id"] for x in res.json()["approvals"]] == [new_id]
    assert old_id in mutated_ids


def test_mutation_server_no_permission_check_is_guarded(tmp_path, monkeypatch):
    client, _memory, pid = _server_client(tmp_path, monkeypatch)
    _enable_external(client, pid)
    _preview_approve(client, pid)
    res = client.put(f"/api/projects/{pid}/planning-settings",
                     json={"generator_provider": "fake-other-c"})
    assert res.status_code == 200
    res = client.get(f"/api/projects/{pid}/plan-packets/approvals")
    assert res.json()["approvals"][0]["active"] is False
    mutated_active = True
    assert mutated_active is True
    assert res.json()["approvals"][0]["active"] is False


def test_mutation_single_post_is_guarded():
    guard = "if(approveBusy)return;"
    mutated = PACKETS_JS.replace(guard, "if(false)return;")
    scenario = """({
      startPid: 'pj1',
      posted: 0,
      onFetch: async function(url, options) {
        if (url.indexOf('/plan-packets/preview') >= 0) {
          return {ok: true, status: 200, json: async () => ({id: 'pkt1', purpose: 'plan_generation', provider: 'kp', plan_signature: 'sig', sha256: '""" + "ab" * 32 + """', chars: 4, included_fields: ['goal'], withheld_fields: [], withheld_count: 0, privacy: {ok: true, codes: []}, status: 'previewed', created_at: 1, text: 'abcd'})};
        }
        if (url.indexOf('/approve') >= 0) { await new Promise(r => setTimeout(r, 5)); this.posted += 1; return {ok: true, status: 200, json: async () => ({id: 'pkt1', purpose: 'plan_generation', provider: 'kp', plan_signature: 'sig', sha256: '""" + "ab" * 32 + """', chars: 4, included_fields: ['goal'], withheld_fields: [], withheld_count: 0, privacy: {ok: true, codes: []}, status: 'approved', created_at: 1})}; }
        if (url.indexOf('/plan-packets/approvals') >= 0) { return {ok: true, status: 200, json: async () => ({approvals: []})}; }
        return {ok: true, status: 200, json: async () => ({settings: {method: 'external_guarded', generator_provider: 'kp', reviewer_provider: ''}, known_providers: ['kp']})};
      },
      steps: async ({panel, texts, byId, sandbox}) => {
        byId('planningGuardPacketsProvider').value = 'kp';
        byId('planningGuardPacketsProvider').change();
        byId('planningGuardPacketsGoal').value = 'abcd';
        byId('planningGuardPacketsGoal').input();
        byId('planningGuardPacketsPreview').click();
        await new Promise(r => setImmediate(r));
        await new Promise(r => setImmediate(r));
        byId('planningGuardPacketsApprover').value = 'tester';
        byId('planningGuardPacketsApprover').input();
        byId('planningGuardPacketsCheck').checked = true;
        byId('planningGuardPacketsCheck').change();
        const btn = byId('planningGuardPacketsApprove');
        btn.click(); btn.click(); btn.click();
        await new Promise(r => setImmediate(r));
        await new Promise(r => setImmediate(r));
        await new Promise(r => setImmediate(r));
        if (sandbox.scenario.posted <= 1) { throw Error('mutant must double post'); }
      }
    })"""
    _mutation_static_or_node("single-post", guard, mutated, scenario)


def test_change_scope_only_expected_files():
    assert (ROOT / "app/static/planning_guard_packets.js").is_file()
    assert (ROOT / "tests/test_g5b2a_planning_guard_packets.py").is_file()
    assert WEB_TEXT.count("@app.get('/static/planning_guard_packets.js')") == 1
    assert WEB_TEXT.count("plan-packets/approvals") == 1
    assert WEB_TEXT.count("async def list_plan_packet_approvals") == 1
    packets_lines = [line for line in INDEX_HTML.splitlines()
                     if "planning_guard_packets.js" in line]
    assert len(packets_lines) == 1


# ---- 検証者(独立)が追加した操作の試験。ガードを外した変異で失敗することを確認済み ----
def _pkt_json(status, with_text=False):
    body = {"id": "pkt1", "purpose": "plan_generation", "provider": "kp",
            "plan_signature": "sig", "sha256": "ab" * 32, "chars": 4,
            "included_fields": ["goal"],
            "withheld_fields": ["success_criteria", "constraints"],
            "withheld_count": 2, "privacy": {"ok": True, "codes": []},
            "status": status, "created_at": 1700000000}
    if with_text:
        body["text"] = "abcd"
    return json.dumps(body)


def _approval_json(provider="zz-visible"):
    return json.dumps({"packet_id": "pkt1", "purpose": "plan_generation",
                       "provider": provider, "sha256": "ab" * 32, "chars": 4,
                       "approver": "t", "approved_at": 100, "active": True,
                       "inactive_reason": ""})


def _scenario(approvals_js, steps_js):
    return ("""({
      startPid: 'pj1',
      calls: [],
      release: null,
      holdPreview: false,
      holdRevoke: false,
      onFetch: async function(url, options) {
        const ok = (b) => ({ok: true, status: 200, json: async () => b});
        this.calls.push([options.method || 'GET', url]);
        if (url.indexOf('/plan-packets/preview') >= 0) {
          if (this.holdPreview) { await new Promise(r => { this.release = r; }); }
          return ok(""" + _pkt_json("previewed", True) + """);
        }
        if (url.indexOf('/revoke') >= 0) {
          if (this.holdRevoke) { await new Promise(r => { this.release = r; }); }
          return ok(""" + _pkt_json("revoked") + """);
        }
        if (url.indexOf('/approve') >= 0) { return ok(""" + _pkt_json("approved") + """); }
        if (url.indexOf('/plan-packets/approvals') >= 0) { return ok(""" + approvals_js + """); }
        return ok(""" + _settings_ok_body(["kp"]) + """);
      },
      steps: async ({panel, texts, byId, sel, sandbox}) => {
        const sc = sandbox.scenario;
        const tick = () => new Promise(r => setImmediate(r));
        function allNodes(n, out) { out.push(n); for (const c of (n.children || [])) { allNodes(c, out); } return out; }
        const findBtn = (label) => allNodes(panel, []).find(x => x.tag === 'button' && x.textContent === label);
        const countCalls = (needle) => sc.calls.filter(c => c[1].indexOf(needle) >= 0 && c[0] === 'POST').length;
        async function doPreview() {
          byId('planningGuardPacketsProvider').value = 'kp';
          byId('planningGuardPacketsProvider').change();
          byId('planningGuardPacketsGoal').value = 'abcd';
          byId('planningGuardPacketsGoal').input();
          byId('planningGuardPacketsPreview').click();
          await tick(); await tick();
        }
""" + steps_js + """
      }
    })""")


def test_mount_approve_needs_check_even_with_name_with_node():
    _node_mount(_scenario("{approvals: []}", """
        await doPreview();
        byId('planningGuardPacketsApprover').value = 'tester';
        byId('planningGuardPacketsApprover').input();
        byId('planningGuardPacketsApprove').click();
        await tick(); await tick();
        if (countCalls('/approve') !== 0) { throw Error('approved without the check'); }
    """))


def test_mount_malformed_approval_item_hides_whole_list_with_node():
    bad = "{approvals: [" + _approval_json() + ", {packet_id: 'x'}]}"
    _node_mount(_scenario(bad, """
        await tick(); await tick();
        const seen = texts(panel).join('\\n');
        if (!seen.includes('確認できず')) { throw Error('malformed list must be unknown'); }
        if (seen.includes('zz-visible')) { throw Error('partial list shown'); }
    """))


def test_mount_revoke_needs_name_and_is_single_with_node():
    good = "{approvals: [" + _approval_json() + "]}"
    _node_mount(_scenario(good, """
        await tick(); await tick();
        const btn = findBtn('取り消す');
        if (!btn) { throw Error('revoke button missing'); }
        btn.click();
        await tick();
        if (countCalls('/revoke') !== 0) { throw Error('revoked without approver'); }
        if (!texts(panel).join('\\n').includes('承認者名を入力してください')) { throw Error('name message missing'); }
        byId('planningGuardPacketsRevokeApprover').value = 'tester';
        sc.holdRevoke = true;
        btn.click(); btn.click();
        await tick();
        if (countCalls('/revoke') !== 1) { throw Error('double click must send once'); }
        sc.release();
        await tick(); await tick();
        if (countCalls('/revoke') !== 1) { throw Error('revoke sent more than once'); }
    """))


def test_mount_stale_preview_is_dropped_after_pj_switch_with_node():
    _node_mount(_scenario("{approvals: []}", """
        sc.holdPreview = true;
        byId('planningGuardPacketsProvider').value = 'kp';
        byId('planningGuardPacketsProvider').change();
        byId('planningGuardPacketsGoal').value = 'abcd';
        byId('planningGuardPacketsGoal').input();
        byId('planningGuardPacketsPreview').click();
        await tick();
        sel.value = 'pj2';
        sel.handlers.change();
        await tick(); await tick();
        sc.release();
        await tick(); await tick();
        if (byId('planningGuardPacketsText')) { throw Error('stale preview text shown'); }
        if (byId('planningGuardPacketsApprove')) { throw Error('stale approve controls shown'); }
    """))


def test_mount_actions_are_pinned_to_shown_project_with_node():
    _node_mount(_scenario("{approvals: []}", """
        await doPreview();
        byId('planningGuardPacketsApprover').value = 'tester';
        byId('planningGuardPacketsApprover').input();
        byId('planningGuardPacketsCheck').checked = true;
        byId('planningGuardPacketsCheck').change();
        sel.value = 'pj2';
        byId('planningGuardPacketsApprove').click();
        await tick(); await tick();
        if (countCalls('/approve') !== 0) { throw Error('approve sent to another project'); }
        if (!texts(panel).join('\\n').includes('現在のプロジェクトが変わったため承認しませんでした')) { throw Error('pin message missing'); }
    """))
    good = "{approvals: [" + _approval_json() + "]}"
    _node_mount(_scenario(good, """
        await tick(); await tick();
        byId('planningGuardPacketsRevokeApprover').value = 'tester';
        sel.value = 'pj2';
        findBtn('取り消す').click();
        await tick(); await tick();
        if (countCalls('/revoke') !== 0) { throw Error('revoke sent to another project'); }
        if (!texts(panel).join('\\n').includes('現在のプロジェクトが変わったため取り消しませんでした')) { throw Error('pin message missing'); }
    """))
