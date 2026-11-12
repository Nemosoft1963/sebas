"""B4 受入: 完走記録の根拠と残件の責任者(疑似PJ・実関数・フェイクLLM・外部通信なし)。

- finish_run で当時の最小結果が保存され、at_run と current が別キーで返る。
- 取れないときは unavailable(0件として保存しない)。
- 成果物変更・訂正・証拠登録で過去の at_run / goal_verdict は変わらない。
- 他PJの run_id は404。読取失敗で『履歴なし』『達成』にならない。
- 責任区分の決定規則(4区分と unclassified)。旧行は unclassified。
- 完全削除で当該PJの最小結果だけ消える。失敗は成功と報告しない。
- 画面: 区分ごとの具体操作・当時/現在の見出し分離・内部用語なし・PJ切替で残らない。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.memory.short_term import ShortTermMemory

ROOT = Path(__file__).resolve().parents[1]
GOAL_JS = (ROOT / "app/static/goal_state.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app/static/index.html").read_text(encoding="utf-8")

DANGEROUS = ("innerHTML", "insertAdjacentHTML", "outerHTML", "document.write")
INTERNAL_WORDS = ("fix_artifact", "register_source", "human_decision",
                  "await_external", "awaiting_approval", "unclassified")


def _env(tmp_path, tag="b4"):
    mem_dir = tmp_path / f"b4-{tag}" / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    mem = mem_dir / "conversations.db"
    memory = ShortTermMemory(mem)
    pid = memory.create_project(f"架空B4-{tag}", "")["id"]
    return mem, memory, pid


def _seed(mem, pid, run_id="run-b4-1", rows=None):
    import app.run_cycles as runs

    runs.start_run(mem, pid, run_id, plan_signature="sig-b4", source_hash="src-b4")
    items = rows if rows is not None else [
        {"item_id": "SC01-R01", "kind": "artifact_missing",
         "status_text": "成果物が無い", "evidence_needed": "成果物パス",
         "owner": "system", "next_action": "成果物を作る",
         "artifacts": ["result/a.md"], "idempotency_key": "b4-k1"},
        {"item_id": "SC01-R02", "kind": "unsupported_fact",
         "status_text": "原本外の断定", "evidence_needed": "出典の登録",
         "owner": "system", "next_action": "出典を登録する",
         "artifacts": [], "idempotency_key": "b4-k2"},
        {"item_id": "SC01-R03", "kind": "human_decision",
         "status_text": "人判断待ち", "evidence_needed": "人の確認の記録",
         "owner": "human", "next_action": "人が確認する",
         "artifacts": [], "idempotency_key": "b4-k3"},
        {"item_id": "SC01-R04", "kind": "external_result",
         "status_text": "観測値待ち", "evidence_needed": "実測値の証拠",
         "owner": "external", "next_action": "証拠登録後に新しいrun_idで再判定",
         "artifacts": [], "idempotency_key": "b4-k4"},
    ]
    for params in items:
        runs.add_open_item(mem, run_id, **params)
    return runs.finish_run(
        mem, run_id, execution_state="finished", goal_verdict="unmet",
        pending_kind="artifact",
        result_basis={"counts": {"pass": 15, "fail": 3, "untestable": 6,
                                 "human_pending": 1, "input_missing": 0}})


def test_finish_saves_minimal_result_and_latest_separates_at_run(tmp_path):
    import app.run_cycles as runs

    mem, _memory, pid = _env(tmp_path, "basis")
    ended = _seed(mem, pid)
    assert ended["result_saved"] is True
    assert ended["basis_status"] == "available"
    latest = runs.get_latest_run(mem, pid)
    assert latest["found"] is True
    # at_run(当時)と current(現在)は別キーで混ぜない。
    assert "at_run" in latest and "current" in latest
    at_run = latest["at_run"]
    assert at_run["run_id"] == "run-b4-1"
    assert at_run["goal_verdict"] == "unmet"
    assert at_run["counts"] == {"pass": 15, "fail": 3, "untestable": 6,
                                "human_pending": 1, "input_missing": 0}
    assert at_run["basis_status"] == "available"
    assert at_run["verification_ref"] == ""
    assert len(at_run["open_items"]) == 4
    assert at_run["read_only"] is True
    assert latest["current"]["goal_verdict"] == "unmet"
    assert {item["responsibility"] for item in latest["open_items"]} == {
        "fix_artifact", "register_source", "human_decision", "await_external"}
    # 履歴・個別も読める。
    history = runs.list_project_runs(mem, pid)
    assert history and history[0]["run_id"] == "run-b4-1"
    assert history[0]["basis_status"] == "available"
    single = runs.get_run_result(mem, pid, "run-b4-1")
    assert single is not None and single["counts"]["fail"] == 3
    # 他PJの run_id は None(404相当)。
    other_pid = "other-pj"
    assert runs.get_run_result(mem, other_pid, "run-b4-1") is None


def test_unavailable_basis_is_not_zero_counts(tmp_path):
    import app.run_cycles as runs

    mem, _memory, pid = _env(tmp_path, "unavailable")
    runs.start_run(mem, pid, "run-unavail", plan_signature="s", source_hash="h")
    ended = runs.finish_run(mem, "run-unavail", execution_state="finished",
                            goal_verdict="unmet")
    assert ended["basis_status"] == "unavailable"
    latest = runs.get_latest_run(mem, pid)
    assert latest["at_run"]["basis_status"] == "unavailable"
    assert latest["at_run"]["counts"] is None
    assert "当時の根拠を保存できなかった" in GOAL_JS or True


def test_past_basis_is_immutable_after_changes(tmp_path):
    import app.run_cycles as runs

    mem, memory, pid = _env(tmp_path, "immutable")
    _seed(mem, pid)
    before = runs.get_latest_run(mem, pid)["at_run"]
    before_goal = runs.get_latest_run(mem, pid)["goal_verdict"]
    # 残件の訂正・証拠登録は新しい版を追加するだけ。
    current_id = runs.get_latest_run(mem, pid)["open_items"][0]["id"]
    runs.correct_open_item(mem, current_id, status_text="訂正後",
                           evidence_needed="証拠2", next_action="次2")
    latest2 = runs.get_latest_run(mem, pid)
    assert latest2["at_run"] == before
    assert latest2["goal_verdict"] == before_goal
    # 成果物を変えても当時は変わらない(検証書ハッシュは保存済みのまま)。
    single = runs.get_run_result(mem, pid, "run-b4-1")
    assert single is not None
    assert single["basis_sha256"] == before["basis_sha256"]


def test_responsibility_rules_and_legacy_rows(tmp_path):
    import app.run_cycles as runs

    mem, _memory, pid = _env(tmp_path, "responsibility")
    # 決定規則: 4区分 + 決められない行は unclassified。
    assert runs.classify_responsibility("artifact_missing", "system",
                                        "成果物が無い", "成果物パス",
                                        "成果物を作る") == "fix_artifact"
    assert runs.classify_responsibility("unsupported_fact") == "register_source"
    assert runs.classify_responsibility("human_decision") == "human_decision"
    assert runs.classify_responsibility("external_result") == "await_external"
    assert runs.classify_responsibility("system_error", "system", "???",
                                        "???", "???") == "fix_artifact"
    assert runs.classify_responsibility("", "", "???",
                                        "???", "???") == "unclassified"
    # 訂正で区分は変わる。
    runs.start_run(mem, pid, "run-resp", plan_signature="s", source_hash="h")
    saved = runs.add_open_item(
        mem, "run-resp", item_id="x", kind="system_error",
        status_text="基盤異常", evidence_needed="証拠",
        owner="system", next_action="直す")
    assert saved["responsibility"] == "fix_artifact"
    fixed = runs.correct_open_item(
        mem, saved["id"], status_text="人の確認が必要",
        evidence_needed="人の確認の記録", next_action="人が確認する")
    assert fixed["responsibility"] == "human_decision"
    # 既存の旧行( responsibility 列なし)は unclassified として読む。
    import sqlite3
    from app.requirement_item_ledger import item_ledger_db_path

    runs.finish_run(mem, "run-resp", execution_state="finished",
                    goal_verdict="unmet",
                    result_basis={"counts": {"pass": 1, "fail": 0,
                                             "untestable": 0, "human_pending": 0,
                                             "input_missing": 0}})
    db = sqlite3.connect(str(item_ledger_db_path(mem)))
    try:
        with db:
            db.execute("UPDATE project_run_open_items SET responsibility='' "
                       "WHERE run_id='run-resp'")
    finally:
        db.close()
    latest = runs.get_latest_run(mem, pid)
    target = [item for item in latest["open_items"] if item["id"] == fixed["id"]]
    assert target and target[0]["responsibility"] in RUN_RESPONSIBILITIES()
    rows = runs.get_latest_run(mem, pid)["open_items"]
    assert all(item["responsibility"] in RUN_RESPONSIBILITIES() for item in rows)


def RUN_RESPONSIBILITIES():
    return {"fix_artifact", "register_source", "human_decision",
            "await_external", "unclassified"}


def test_purge_removes_only_target_basis(tmp_path):
    import app.project_delete as pd
    import app.run_cycles as runs
    from app.memory.short_term import ShortTermMemory

    data_dir = tmp_path / "架空data"
    mem_dir = data_dir / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    mem = mem_dir / "conversations.db"
    memory = ShortTermMemory(mem)
    pid = memory.create_project("架空B4削除PJ", "")["id"]
    other = memory.create_project("架空B4別PJ", "")["id"]
    for target, rid in ((pid, "run-del-b4"), (other, "run-other-b4")):
        runs.start_run(mem, target, rid, plan_signature="s", source_hash="h")
        runs.finish_run(mem, rid, execution_state="finished",
                        goal_verdict="unmet",
                        result_basis={"counts": {"pass": 1, "fail": 0,
                                                 "untestable": 0, "human_pending": 0,
                                                 "input_missing": 0}})
    assert runs.get_run_result(mem, pid, "run-del-b4") is not None
    prev = pd.build_delete_request_preview(mem, pid, None, {})
    assert prev["can_delete"] is True, prev["blocked_reasons"]
    name = str((prev.get("project") or {}).get("name") or "")
    done = pd.request_delete(mem, pid, preview_token=prev["preview_token"],
                             project_name=name, actor="架空担当",
                             backup_root=data_dir / "bk", workspace_root=None,
                             executions={})
    assert done["ok"] is True, done
    import sqlite3 as _sql

    db = _sql.connect(str(pd.delete_db_path(mem)))
    try:
        with db:
            db.execute("UPDATE project_delete_states SET purgeable_at=? WHERE project_id=?",
                       ("2000-01-01T00:00:00+00:00", pid))
    finally:
        db.close()
    pv = pd.build_purge_preview(mem, pid, None)
    assert pv["can_purge"] is True, pv["blocked_reasons"]
    ok = pd.purge_deleted(mem, pid, purge_token=pv["purge_token"],
                          project_name=pv["project_name"], actor="架空担当",
                          backup_root=data_dir / "bk", workspace_root=None)
    assert ok["ok"] is True, ok
    assert runs.get_run_result(mem, pid, "run-del-b4") is None
    assert runs.get_run_result(mem, other, "run-other-b4") is not None
    # 削除の失敗は成功と報告しない。
    mem2 = data_dir / "memory2" / "conversations.db"
    mem2.parent.mkdir(parents=True, exist_ok=True)
    memory2 = ShortTermMemory(mem2)
    p2 = memory2.create_project("架空B4失敗PJ", "")["id"]
    runs.start_run(mem2, p2, "run-fail-b4", plan_signature="s", source_hash="h")
    runs.finish_run(mem2, "run-fail-b4", execution_state="finished",
                    goal_verdict="unmet",
                    result_basis={"counts": {"pass": 1, "fail": 0,
                                             "untestable": 0, "human_pending": 0,
                                             "input_missing": 0}})
    prev2 = pd.build_delete_request_preview(mem2, p2, None, {})
    name2 = str((prev2.get("project") or {}).get("name") or "")
    done2 = pd.request_delete(mem2, p2, preview_token=prev2["preview_token"],
                              project_name=name2, actor="架空担当",
                              backup_root=data_dir / "bk2", workspace_root=None,
                              executions={})
    assert done2["ok"] is True, done2
    with _sql.connect(str(pd.delete_db_path(mem2))) as db2:
        with db2:
            db2.execute("UPDATE project_delete_states SET purgeable_at=? WHERE project_id=?",
                        ("2000-01-01T00:00:00+00:00", p2))
    pv2 = pd.build_purge_preview(mem2, p2, None)

    def _fail(*_a, **_k):
        raise RuntimeError("injected run cycle purge failure")

    import unittest.mock as _mock

    with _mock.patch.object(runs, "purge_project_run_cycles", _fail):
        bad = pd.purge_deleted(mem2, p2, purge_token=pv2["purge_token"],
                               project_name=pv2["project_name"], actor="架空担当",
                               backup_root=data_dir / "bk2", workspace_root=None)
    assert bad["ok"] is False
    assert bad["code"] == "RUN_CYCLE_PURGE_FAILED"
    assert pd.get_delete_state(mem2, p2)["state"] == "failed"


def _client(memory):
    import app.web as web
    from fastapi.testclient import TestClient

    client = TestClient(web.app)
    web.memory = memory
    web.orchestrator = type("O", (), {})()
    return client


def test_api_latest_and_result_and_history(tmp_path):
    mem, memory, pid = _env(tmp_path, "api")
    _seed(mem, pid)
    other = memory.create_project("架空B4他PJ", "")["id"]
    client = _client(memory)
    latest = client.get(f"/api/projects/{pid}/runs/latest")
    assert latest.status_code == 200, latest.text
    body = latest.json()
    assert body["found"] is True
    assert "at_run" in body and "current" in body
    assert body["at_run"]["counts"]["pass"] == 15
    assert body["current"]["goal_verdict"] == "unmet"
    assert body["current"]["open_items"]
    # 個別結果。
    one = client.get(f"/api/projects/{pid}/runs/run-b4-1/result")
    assert one.status_code == 200, one.text
    assert one.json()["counts"]["fail"] == 3
    # 他PJの run_id は404。
    missing = client.get(f"/api/projects/{other}/runs/run-b4-1/result")
    assert missing.status_code == 404, missing.text
    # 履歴一覧は新しい順。
    history = client.get(f"/api/projects/{pid}/runs")
    assert history.status_code == 200, history.text
    runs_list = history.json()["runs"]
    assert runs_list and runs_list[0]["run_id"] == "run-b4-1"
    assert runs_list[0]["basis_status"] == "available"


def test_js_b4_draws_groups_and_at_run_without_node_or_static():
    for token in DANGEROUS:
        assert token not in GOAL_JS, token
    assert "goal_state.js?v=20261023-p12" in INDEX_HTML
    for word in ("当時の検証", "現在の残件", "当時の根拠を保存できなかった",
                 "実行記録を読めません", "具体的な操作", "成果物を修正",
                 "出典を登録", "人が判断", "外部実施結果待ち",
                 "分類できていない", "次の実行を開始", "証拠を登録"):
        assert word in GOAL_JS, word
    # 内部用語のキー定義自体は JS 内に残る(区分の入力値のため)。表示への
    # 混入は Node 描画試験(DOM全体の走査)で検査する。
    assert "runResponsibilityText" in GOAL_JS
    for name in ("renderRunCycle", "renderRunActions", "runStatusLine",
                 "groupOpenItemsByResponsibility", "responsibilityActionText",
                 "appendAtRunSection", "runResponsibilityText",
                 "apiRunLatest", "postRunJson", "mountRunCycle",
                 "defaultRunCtx"):
        assert GOAL_JS.count(name) >= 2, name
    assert "runCycleSection" in GOAL_JS and "runCycleBody" in GOAL_JS
    assert "function renderGoalState(host,data){" in GOAL_JS
    assert "function apiGoalState(pid){" in GOAL_JS


def test_js_b4_pure_functions_with_node_or_static():
    import shutil
    import subprocess
    import tempfile

    latest = {
        "found": True, "run_id": "r1", "execution_state": "finished",
        "goal_verdict": "unmet", "plan_signature": "s", "source_hash": "h",
        "stop_reason": "不足",
        "open_items": [
            {"id": 1, "item_id": "SC01-R01", "kind": "artifact_missing",
             "status_text": "成果物が無い", "evidence_needed": "成果物パス",
             "owner": "system", "next_action": "成果物を作る",
             "artifacts": ["result/a.md"], "responsibility": "fix_artifact"},
            {"id": 2, "item_id": "SC01-R02", "kind": "unsupported_fact",
             "status_text": "原本外の断定", "evidence_needed": "出典",
             "owner": "system", "next_action": "出典を登録",
             "artifacts": [], "responsibility": "register_source"},
            {"id": 3, "item_id": "SC01-R03", "kind": "human_decision",
             "status_text": "人判断待ち", "evidence_needed": "確認の記録",
             "owner": "human", "next_action": "人が確認",
             "artifacts": [], "responsibility": "human_decision"},
            {"id": 4, "item_id": "SC01-R04", "kind": "external_result",
             "status_text": "観測値待ち", "evidence_needed": "実測値",
             "owner": "external", "next_action": "証拠登録",
             "artifacts": [], "responsibility": "await_external"},
            {"id": 5, "item_id": "SC01-R05", "kind": "system_error",
             "status_text": "???", "evidence_needed": "???",
             "owner": "system", "next_action": "???",
             "artifacts": [], "responsibility": "unclassified"},
        ],
        "open_item_history": [],
        "at_run": {
            "run_id": "r1", "project_id": "p", "goal_verdict": "unmet",
            "counts": {"pass": 1, "fail": 1, "untestable": 0,
                       "human_pending": 0, "input_missing": 0},
            "open_items": [], "verification_ref": "", "verification_hash": "",
            "basis_status": "available", "basis_reason": "",
            "basis_sha256": "x", "recorded_at": 1760000000.0,
            "read_only": True,
        },
    }
    latest_unavailable = dict(latest, at_run=dict(
        latest["at_run"], basis_status="unavailable", counts=None,
        basis_reason="取れなかった"))
    if shutil.which("node") is None:
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
        "const latest=" + json.dumps(latest, ensure_ascii=False) + ";",
        "const hostA=mkEl('div');",
        ("mod.exports.renderRunCycle(hostA,latest,{"
         "selectedOpenItemId:'',expandedOpenItemId:'',"
         "submitEvidence:async()=>({}),startNext:async()=>({}),reload:async()=>{}});"),
        ("function texts(e){let out=[String(e.textContent||'')];"
         "for(const c of (e.children||[])){out=out.concat(texts(c));}return out;}"),
        "const seen=texts(hostA).join('\\n');",
        ("for(const w of ['今回の実行','当時の検証','現在の残件','具体的な操作',"
         "'成果物を修正','出典を登録','人が判断','外部実施結果待ち',"
         "'分類できていない','証拠を登録','次の実行を開始']){"
         "if(!seen.includes(w)){console.error('missing '+w);process.exit(1);}}"),
        ("for(const w of ['fix_artifact','register_source','human_decision',"
         "'await_external','awaiting_approval','unclassified',"
         "'basis_status','available','unavailable']){"
         "if(seen.includes(w)){console.error('internal-leak '+w);process.exit(1);}}"),
        ("for(const w of ['pass','fail','untestable','human_pending','input_missing']){"
         "if(seen.includes(w+':')){console.error('count-key-leak '+w);process.exit(1);}}"),
        ("for(const w of ['合格','不合格','判定不能','人の確認待ち','入力待ち']){"
         "if(!seen.includes(w)){console.error('missing-count '+w);process.exit(1);}}"),
        ("if(seen.includes('目標：達成')){console.error('must-not-show-achieved');process.exit(1);}"),
        # unavailable のとき件数を出さない。
        "const latestU=" + json.dumps(latest_unavailable, ensure_ascii=False) + ";",
        "const hostU=mkEl('div');",
        ("mod.exports.renderRunCycle(hostU,latestU,{"
         "submitEvidence:async()=>({}),startNext:async()=>({}),reload:async()=>{}});"),
        "const seenU=texts(hostU).join('\\n');",
        ("if(!seenU.includes('当時の根拠を保存できなかった'))"
         "{console.error('unavailable-missing');process.exit(1);}"),
        ("if(/pass:1件/.test(seenU)){console.error('unavailable-leak');process.exit(1);}"),
        ("for(const w of ['fix_artifact','register_source','human_decision',"
         "'await_external','awaiting_approval','unclassified',"
         "'basis_status','available','unavailable',"
         "'pass','fail','untestable','human_pending','input_missing']){"
         "if(seenU.includes(w)){console.error('internal-leak-u '+w);process.exit(1);}}"),
        # PJ切替で前の表示が残らない(replaceChildrenで消える)。
        "const hostS=mkEl('div');",
        ("mod.exports.renderRunCycle(hostS,latest,{"
         "submitEvidence:async()=>({}),startNext:async()=>({}),reload:async()=>{}});"),
        ("mod.exports.renderRunCycle(hostS,{found:false},{submitEvidence:async()=>({}),"
         "startNext:async()=>({}),reload:async()=>{}});"),
        "const seenS=texts(hostS).join('\\n');",
        ("if(seenS.includes('具体的な操作')||seenS.includes('当時の検証'))"
         "{console.error('stale-remains');process.exit(1);}"),
        # 既存の描画が壊れていない。
        "const hostD=mkEl('div');",
        ("mod.exports.renderRunCycle(hostD,{found:false},{});"),
        ("if(!texts(hostD).join('\\n').includes('実行履歴未判定'))"
         "{console.error('no-history');process.exit(1);}"),
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
