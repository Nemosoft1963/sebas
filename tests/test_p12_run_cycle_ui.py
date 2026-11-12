"""P1-2 受入: 実行サイクルの画面と操作(疑似PJ・実関数・外部通信なし)。

- GET /runs/latest の独立表示(終了/未達・判定待ち・異常停止・未判定・読取不能)。
- 達成は achieved のときだけ。finished だけでは成功・完了と見せない。
- 証拠登録(必須3項目・新しい版・解決済みにしない)と次の実行(束縛検証・実行中拒否・新しいrun_id)。
- 既存の goal_state 描画の回帰。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GOAL_JS = (ROOT / "app/static/goal_state.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app/static/index.html").read_text(encoding="utf-8")

DANGEROUS = ("innerHTML", "insertAdjacentHTML", "outerHTML", "document.write")


def _env(tmp_path, tag="p12"):
    from app.memory.short_term import ShortTermMemory

    mem_dir = tmp_path / f"p12-{tag}" / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    mem = mem_dir / "conversations.db"
    memory = ShortTermMemory(mem)
    pid = memory.create_project(f"架空P12-{tag}", "")["id"]
    return mem, memory, pid


def _seed_run(mem, pid, *, state="finished", verdict="unmet",
              pending="artifact", run_id="run-p12", sig="sig-p12",
              src="src-p12", stop="不足あり"):
    import app.run_cycles as runs

    runs.start_run(mem, pid, run_id, plan_signature=sig, source_hash=src)
    runs.add_open_item(
        mem, run_id, item_id="SC01-R01", kind="external_result",
        status_text="実施待ち", evidence_needed="観測値と証拠形式: 実測値",
        owner="external", next_action="人間が証拠を登録し新しいrun_idで再判定",
        artifacts=["result/a.md"])
    return runs.finish_run(mem, run_id, execution_state=state,
                           goal_verdict=verdict, pending_kind=pending,
                           stop_reason=stop)


def _client(memory):
    import app.web as web
    from fastapi.testclient import TestClient

    client = TestClient(web.app)
    web.memory = memory
    web.orchestrator = type("O", (), {})()
    return client


def test_p12_evidence_registers_new_version_without_resolving(tmp_path):
    mem, memory, pid = _env(tmp_path, "evidence")
    _seed_run(mem, pid)
    import app.run_cycles as runs

    before = runs.get_latest_run(mem, pid)
    current_id = before["open_items"][0]["id"]
    client = _client(memory)
    ok = client.post(
        f"/api/projects/{pid}/runs/run-p12/open-items/{current_id}/evidence",
        json={"assignee": "架空担当", "evidence": "実測値1件", "reason": "外部実施の記録"})
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["state"] == "証拠登録済み・再判定待ち"
    assert body["resolved"] is False
    assert body["goal_achieved"] is False
    assert body["supersedes_id"] == current_id
    after = runs.get_latest_run(mem, pid)
    assert after["run_id"] == "run-p12"
    assert [i["id"] for i in after["open_items"]] == [body["id"]]
    assert [i["id"] for i in after["open_item_history"]] == [current_id, body["id"]]
    # 空の必須項目は API でも拒否する。
    for payload in ({"assignee": "", "evidence": "e", "reason": "r"},
                    {"assignee": "a", "evidence": "", "reason": "r"},
                    {"assignee": "a", "evidence": "e", "reason": ""}):
        bad = client.post(
            f"/api/projects/{pid}/runs/run-p12/open-items/{current_id}/evidence",
            json=payload)
        assert bad.status_code == 422, bad.text
    # 置き換え済みの古い版には証拠を登録できない。
    stale = client.post(
        f"/api/projects/{pid}/runs/run-p12/open-items/{current_id}/evidence",
        json={"assignee": "架空担当", "evidence": "再登録", "reason": "古い版"})
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"]["code"] == "OPEN_ITEM_SUPERSEDED"
    # 前の実行の記録は変わらない(run は同じ)。
    assert runs.get_latest_run(mem, pid)["run_id"] == "run-p12"


def test_p12_next_run_preflight_validates_binding_without_creating_cycle(tmp_path, monkeypatch):
    import app.run_cycles as runs

    mem, memory, pid = _env(tmp_path, "next")
    _seed_run(mem, pid)
    import app.web as web
    monkeypatch.setattr(web, "orchestrator", type("O", (), {})())
    monkeypatch.setattr("app.goal_review.plan_snapshot", lambda _manager, _pid: ({"goal": "g"}, "sig-p12"))
    monkeypatch.setattr("app.vehicle_workflow.digest", lambda _sources: "src-p12")
    monkeypatch.setattr("app.vehicle_workflow.sources", lambda _manager, _pid: ["s"])
    client = _client(memory)
    bad = client.post(f"/api/projects/{pid}/runs/next",
                      json={"actor": "架空担当", "plan_signature": "other",
                            "source_hash": "src-p12"})
    assert bad.status_code == 409, bad.text
    missing = client.post(f"/api/projects/{pid}/runs/next",
                          json={"actor": "", "plan_signature": "sig-p12",
                                "source_hash": "src-p12"})
    assert missing.status_code == 422, missing.text
    before = runs.get_latest_run(mem, pid)
    ok = client.post(f"/api/projects/{pid}/runs/next",
                     json={"actor": "架空担当", "plan_signature": "sig-p12",
                           "source_hash": "src-p12", "reason": "証拠あり"})
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["can_start"] is True
    assert body["previous_run_id"] == "run-p12"
    assert body["plan_signature"] == "sig-p12"
    assert body["source_hash"] == "src-p12"
    assert body["read_only"] is True
    after = runs.get_latest_run(mem, pid)
    assert after["run_id"] == before["run_id"] == "run-p12"
    assert after["execution_state"] == "finished"


def test_p12_js_draws_run_states_and_existing_panel():
    for token in DANGEROUS:
        assert token not in GOAL_JS, token
    assert "goal_state.js?v=20261023-p12" in INDEX_HTML
    for word in ("今回の実行", "実行履歴未判定", "実行記録を読めません",
                 "停止（取消）", "次の実行を開始", "証拠を登録",
                 "担当者名（必須）",
                 "異常停止は完走と区別"):
        assert word in GOAL_JS, word
    # 追加した関数が実際に使われている(呼ばれない関数を作らない)。
    for name in ("renderRunCycle", "renderRunActions", "runStatusLine",
                 "isGoalAchievedOnly", "apiRunLatest", "postRunJson",
                 "mountRunCycle", "defaultRunCtx"):
        assert GOAL_JS.count(name) >= 2, name
    assert "runCycleSection" in GOAL_JS and "runCycleBody" in GOAL_JS
    # 変数スコープ: ハンドラが参照する変数は同じ関数内で宣言されている。
    assert "const safeCtx" in GOAL_JS
    assert "const checked=" in GOAL_JS
    assert "'/mission/start'" in GOAL_JS
    assert "checked.can_start!==true" in GOAL_JS
    # 既存パネルの描画関数は残る。
    assert "function renderGoalState(host,data){" in GOAL_JS
    assert "function apiGoalState(pid){" in GOAL_JS


def test_p12_js_pure_functions_with_node_or_static():
    import shutil
    import subprocess
    import tempfile

    cases_exec = [
        ["running", "実行中"], ["finished", "終了"],
        ["stopped_error", "異常停止"],
        ["stopped_time_limit", "時間切れで停止"],
        ["cancelled", "停止（取消）"], ["nope", "確認できず"],
    ]
    cases_goal = [
        ["achieved", "達成"], ["unmet", "未達"],
        ["undetermined", "判定待ち"], ["nope", "確認できず"],
    ]
    cases_status = [
        ["finished", "achieved", "今回の実行: 終了 / 目標: 達成"],
        ["finished", "unmet", "今回の実行: 終了 / 目標: 未達"],
        ["finished", "undetermined", "今回の実行: 終了 / 目標: 判定待ち"],
        ["running", "undetermined", "今回の実行: 実行中 / 目標: 判定待ち"],
        ["stopped_error", "undetermined", "今回の実行: 異常停止 / 目標: 判定待ち"],
    ]
    latest_finished_unmet = {
        "found": True, "run_id": "r1", "execution_state": "finished",
        "goal_verdict": "unmet", "plan_signature": "s", "source_hash": "h",
        "stop_reason": "不足", "open_items": [{
            "id": 7, "item_id": "SC01-R01", "kind": "external_result",
            "status_text": "実施待ち", "evidence_needed": "観測値",
            "owner": "external", "next_action": "証拠を登録",
            "artifacts": ["result/a.md"]}],
        "open_item_history": [],
    }
    latest_error = {
        "found": True, "run_id": "r2", "execution_state": "stopped_error",
        "goal_verdict": "undetermined", "stop_reason": "台帳不能",
        "open_items": [], "open_item_history": [],
    }
    latest_empty = {"found": False}
    if shutil.which("node") is None:
        for _state, label in cases_exec + cases_goal:
            assert label in GOAL_JS
        pytest.skip("Node が無いため静的検査で代替")
    probe = [
        "const fs=require('fs');",
        "const vm=require('vm');",
        "const src=fs.readFileSync('app/static/goal_state.js','utf8');",
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
         "window:{addEventListener(){}},fetch:async()=>{throw Error('no-net')},"
         "setTimeout:function(){return 0;},clearTimeout:function(){},"
         "setInterval:function(){return 0;},clearInterval:function(){},"
         "requestAnimationFrame:function(){return 0;},"
         "location:{href:'http://localhost/',origin:'http://localhost',host:'localhost',protocol:'http:'},"
         "localStorage:{getItem:function(){return null;},setItem:function(){},removeItem:function(){}},"
         "navigator:{},console:console});"),
        "const f=mod.exports.runExecutionText;",
        "const g=mod.exports.runGoalText;",
        "const h=mod.exports.runStatusLine;",
        "const casesExec=" + json.dumps(cases_exec, ensure_ascii=False) + ";",
        "const casesGoal=" + json.dumps(cases_goal, ensure_ascii=False) + ";",
        "const casesStatus=" + json.dumps(cases_status, ensure_ascii=False) + ";",
        "for(const c of casesExec){if(f(c[0])!==c[1]){console.error('exec '+c[0]);process.exit(1);}}",
        "for(const c of casesGoal){if(g(c[0])!==c[1]){console.error('goal '+c[0]);process.exit(1);}}",
        ("for(const c of casesStatus){if(h(c[0],c[1])!==c[2])"
         "{console.error('status '+c[0]+'/'+c[1]);process.exit(1);}}"),
        # 達成は achieved のときだけ。
        ("if(mod.exports.isGoalAchievedOnly({found:true,goal_verdict:'unmet'})!==false)"
         "{console.error('achieved-guard');process.exit(1);}"),
        ("if(mod.exports.isGoalAchievedOnly({found:true,goal_verdict:'achieved'})!==true)"
         "{console.error('achieved-ok');process.exit(1);}"),
        # 描画経路: 既存(goal)と新規(run)の両方が textContent で描画される。
        "const latest=" + json.dumps(latest_finished_unmet, ensure_ascii=False) + ";",
        "const hostA=mkEl('div');",
        ("mod.exports.renderRunCycle(hostA,latest,{"
         "selectedOpenItemId:'',expandedOpenItemId:'',"
         "submitEvidence:async()=>{throw Error('no-net')},"
         "startNext:async()=>{throw Error('no-net')},"
         "reload:async()=>{}});"),
        ("function texts(e){let out=[String(e.textContent||'')];"
         "for(const c of (e.children||[])){out=out.concat(texts(c));}return out;}"),
        "const seen=texts(hostA).join('\\n');",
        ("for(const w of ['今回の実行','目標：未達','実施待ち','必要な証拠',"
         "'担当主体','次の操作','結果を確認','証拠を登録','次の実行を開始']){"
         "if(!seen.includes(w)){console.error('missing '+w);process.exit(1);}}"),
        # 目標欄が『達成』と表示されていないこと。説明文中の『達成』の語ではなく、
        # 『目標：達成』『今回の実行: 終了 / 目標: 達成』の表示の有無で判定する。
        ("const goalLines=texts(hostA).filter(t=>t.indexOf('目標')===0||t.indexOf('目標：')===0);"),
        ("if(goalLines.some(t=>t.replace(/\\s/g,'').indexOf('目標：達成')>=0)){console.error('goal-must-not-show-achieved');process.exit(1);}"),
        ("if(seen.includes('目標：達成')){console.error('must-not-show-achieved');process.exit(1);}"),
        ("if(seen.includes('今回の実行: 終了 / 目標: 達成')){console.error('must-not-show-achieved-status');process.exit(1);}"),
        # 『未達』のときに目標欄が達成になる回帰は確実に検出する。
        # achieved では目標：達成が出る、unmet/undetermined では出ない。
        ("for(const v of ['unmet','undetermined']){"
         "const lv=Object.assign({},latest,{goal_verdict:v});"
         "const hh=mkEl('div');"
         "mod.exports.renderRunCycle(hh,lv,{submitEvidence:async()=>({}),startNext:async()=>({}),reload:async()=>{}});"
         "const tt=texts(hh).join('\\n');"
         "if(tt.includes('目標：達成')){console.error('achieved-leak '+v);process.exit(1);}"
         "if(!tt.includes('目標：')){console.error('goal-line-missing '+v);process.exit(1);}}"),
        ("{const lv=Object.assign({},latest,{goal_verdict:'achieved',execution_state:'finished'});"
         "const hh=mkEl('div');"
         "mod.exports.renderRunCycle(hh,lv,{submitEvidence:async()=>({}),startNext:async()=>({}),reload:async()=>{}});"
         "const tt=texts(hh).join('\\n');"
         "if(!tt.includes('目標：達成')){console.error('achieved-must-show');process.exit(1);}}"),
        # 異常停止・未判定・履歴なし・読取失敗ではなく未判定への退避も描画する。
        "const hostB=mkEl('div');",
        ("mod.exports.renderRunCycle(hostB,"
         + json.dumps(latest_error, ensure_ascii=False)
         + ",{submitEvidence:async()=>({}),startNext:async()=>({}),reload:async()=>{}});"),
        ("if(!texts(hostB).join('\\n').includes('異常停止'))"
         "{console.error('stopped_error');process.exit(1);}"),
        "const hostC=mkEl('div');",
        ("mod.exports.renderRunCycle(hostC,"
         + json.dumps(latest_empty, ensure_ascii=False)
         + ",{});"),
        ("if(!texts(hostC).join('\\n').includes('実行履歴未判定'))"
         "{console.error('no-history');process.exit(1);}"),
        # 既存の goal 描画が壊れていない。
        "const hostD=mkEl('div');",
        ("mod.exports.renderGoalState(hostD,{read_only:true,external_sends:0,"
         "plan_signature:'s',preflight_status:'ok',stop:{stop_class:'x',stop_code:'y'},"
         "summary:{total:0,consistent:true,goal_achieved:false,goal_remaining:[]},"
         "proposals:{readable:false},autopilot:{readable:false},"
         "next_step:{kind:'blocked_unknown',detail_ja:'d'},criteria:[]});"),
        ("if(!texts(hostD).join('\\n').includes('自走の現在地'))"
         "{console.error('goal-regression');process.exit(1);}"),
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".cjs", delete=False) as tmp:
        tmp.write("\n".join(probe))
        probe_path = tmp.name
    try:
        completed = subprocess.run(["node", probe_path], capture_output=True,
                                   text=True, timeout=30)
    finally:
        Path(probe_path).unlink(missing_ok=True)
    assert completed.returncode == 0, completed.stderr
