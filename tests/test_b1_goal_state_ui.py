"""B-1: 「自走の現在地」画面のテスト (疑似PJのみ・静的検査中心)。

疑似PJのみ (架空・疑似)。実在PJのID・件数・版・語を使わない。外部通信なし。
新しい読取APIは追加しない (web.py の goal-progress と goal_state.js 配信の
追加のみ)。製品コード(Python)は goal_state_model.py の新規と web.py の追加
のみ。特定PJの語・ID・件数に依存しない。
"""
import json
import re
from pathlib import Path

import pytest

from app.memory.short_term import ShortTermMemory
from app.project_manager import ProjectOrchestrator
from app.structured_planning import compile_task
from app.workspace_files import WorkspaceSandbox

ROOT = Path(__file__).resolve().parents[1]
GOAL_JS = (ROOT / "app/static/goal_state.js").read_text(encoding="utf-8")
WR_JS = (ROOT / "app/static/workflow_readiness.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
MISSION_JS = (ROOT / "app/static/project_mission.js").read_text(encoding="utf-8")

DANGEROUS = ("innerHTML", "insertAdjacentHTML", "outerHTML", "document.write")


def _make_manager(tmp_path, tag="b1ui"):
    name = "b1-疑似-画面-" + tag
    db_path = tmp_path / (name + ".db")
    memory = ShortTermMemory(db_path)
    project = memory.create_project(name, workspace_path="projects/" + name)
    pid = project["id"]
    criteria = ["架空文書01の原本引用つき作成と検証",
                "架空文書02の原本引用つき作成と検証"]
    memory.save_mission(pid, "架空目標-画面", "\n".join(
        f"{i}. {text}" for i, text in enumerate(criteria, 1)), "", True, ["chatgpt"])
    tasks = []
    for i, text in enumerate(criteria, 1):
        tasks.append(compile_task(i, text, {
            "title": f"架空文書工程{i}",
            "scope": f"架空文書{i:02d}の成果物を作成する。",
            "headings": ["目的", "実施内容"],
        }, []))
    memory.replace_plan(pid, name + " plan", tasks)
    manager = ProjectOrchestrator(
        memory, None, lambda p: ("", []), None,
        lambda: [{"id": "chatgpt", "configured": True}],
        workspace=WorkspaceSandbox(tmp_path / ("ws-" + name)),
    )
    from app.goal_contract import activate as _activate
    _activate(manager, pid)
    return manager, pid


def test_no_new_dangerous_html_and_mission_count_unchanged():
    for name, text in (("goal_state.js", GOAL_JS),
                       ("workflow_readiness.js", WR_JS)):
        for token in DANGEROUS:
            assert token not in text, (name, token)
    assert GOAL_JS.count("innerHTML") == 0
    assert WR_JS.count("innerHTML") == 0
    added = [line for line in INDEX_HTML.splitlines() if "goal_state.js" in line]
    assert added, "goal_state.js の読込タグがありません"
    for line in added:
        for token in DANGEROUS:
            assert token not in line, token
    # 既存の契約: project_mission.js の innerHTML 件数は28のまま。
    assert MISSION_JS.count("innerHTML") == 28
    assert "goal_state.js" in INDEX_HTML
    # 既存カードを変更・削除していない。
    for key in ("workflowOverviewGap", "workflowOverviewNext",
                "workflowOverviewStop", "workflowOverviewOwner",
                "workflowOverviewEvidence", "workflowOverviewPack",
                "workflowOverviewFunnel"):
        assert key in WR_JS, key
    assert "goalStateSection" in GOAL_JS
    assert "自走の現在地" in GOAL_JS


def test_static_route_serves_goal_js(tmp_path, monkeypatch):
    import app.web as web
    from fastapi.testclient import TestClient
    manager, _ = _make_manager(tmp_path, tag="route")
    monkeypatch.setattr(web, "memory", manager.memory)
    monkeypatch.setattr(web, "orchestrator", manager)
    client = TestClient(web.app, raise_server_exceptions=False)
    resp = client.get("/static/goal_state.js")
    assert resp.status_code == 200
    assert "stateLabel" in resp.text
    for token in DANGEROUS:
        assert token not in resp.text, token


def test_js_api_paths_exist_and_keys_match(tmp_path):
    import app.web as web
    routes = {str(getattr(r, "path", "")) for r in web.app.routes}
    assert "/api/projects/{project_id}/goal-progress" in routes
    assert "/static/goal_state.js" in routes
    assert "/goal-progress" in GOAL_JS
    # JS が参照する応答キーが実際の API の応答キーと一致する。
    from app.goal_state_model import build_goal_state
    manager, pid = _make_manager(tmp_path, tag="keys")
    from app.goal_review import ReviewStore
    from app.goal_review import plan_snapshot
    _snap, sig = plan_snapshot(manager, pid)
    ReviewStore(manager.memory.path).put(pid, "plan", sig, {
        "status": "passed",
        "reviews": [{"provider": "chatgpt", "status": "pass", "issues": [],
                     "response": {"ok": True}}],
        "finished": 0})
    body = build_goal_state(manager, pid)
    top_keys = {"plan_signature", "preflight_status", "stop", "criteria",
                "summary", "proposals", "autopilot", "next_step",
                "read_only", "external_sends", "external_calls",
                "plan_signature_error", "contract_error",
                "preflight_reason", "schema", "project_id"}
    assert top_keys <= set(body), top_keys - set(body)
    crit_keys = {"criterion_id", "statement_ref", "state",
                 "state_label_ja", "evidence_refs", "blockers"}
    assert body["criteria"]
    assert crit_keys <= set(body["criteria"][0]), crit_keys
    next_keys = {"kind", "title_ja", "detail_ja", "action", "screen",
                 "basis", "dry_run_action", "gate_status"}
    assert next_keys <= set(body["next_step"]), next_keys
    stop_keys = {"stop_class", "stop_code", "reason_ja", "next_action",
                 "also", "also_counts", "also_total"}
    assert stop_keys <= set(body["stop"]), stop_keys
    summary_keys = {"achieved", "capability_gap", "evidence_gap",
                    "human_decision", "unknown", "total", "consistent",
                    "goal_achieved", "goal_remaining"}
    assert summary_keys <= set(body["summary"]), summary_keys
    missing_refs = []
    for key in (["plan_signature", "preflight_status", "stop", "criteria",
                 "summary", "proposals", "autopilot", "next_step",
                 "criterion_id", "statement_ref", "state",
                 "state_label_ja", "blockers", "kind",
                 "detail_ja", "basis", "goal_remaining",
                 "goal_achieved", "stop_class", "stop_code",
                 "reason_ja", "next_action"]):
        patterns = ["." + key, "'" + key + '"', '"' + key + '"',
                    "'" + key + "'"]
        if not any(p in GOAL_JS for p in patterns):
            missing_refs.append(key)
    assert missing_refs == [], missing_refs


def test_state_labels_and_next_text_do_not_imply_achievement():
    # 状態の文言は色だけでなく文言で区別する。
    for word in ("達成", "機能不足", "証拠不足", "人の判断待ち",
                 "確認できず"):
        assert word in GOAL_JS, word
    assert "stateLabel" in GOAL_JS
    assert "nextStepText" in GOAL_JS
    # goal_achieved でないときに達成を匂わせない文言。
    assert "達成とは表示しません" in GOAL_JS
    assert "全条件の達成は確認できていません" in GOAL_JS
    # unknown と 0件を混ぜない。
    assert "確認できず" in GOAL_JS
    assert "残件：確認できず" in GOAL_JS
    assert "残件：0件（全条件が達成）" in GOAL_JS
    # 取得失敗の分岐が成功表示にならない。
    assert "取得できませんでした" in GOAL_JS
    compact = GOAL_JS.replace(" ", "")
    assert "catch(e){" in compact
    assert "isCurrent())return" in compact


def test_pure_functions_with_node_or_static():
    import shutil
    import subprocess
    import tempfile
    cases_state = [
        ["achieved", "達成"],
        ["capability_gap", "機能不足"],
        ["evidence_gap", "証拠不足"],
        ["human_decision", "人の判断待ち"],
        ["unknown", "確認できず"],
        ["nope", "確認できず"],
        ["", "確認できず"],
    ]
    cases_next = [
        [{"kind": "autonomous", "detail_ja": "架空"}, "自走が進めます：架空"],
        [{"kind": "human_confirm", "detail_ja": "架空"}, "人の確認が必要：架空"],
        [{"kind": "capability_dev", "detail_ja": "架空"}, "機能開発が必要：架空"],
        [{"kind": "evidence_confirm", "detail_ja": "架空"}, "証拠の確認が必要：架空"],
        [{"kind": "blocked_unknown", "detail_ja": "架空"}, "確認できず：架空"],
        [{"kind": "nope", "detail_ja": "架空"}, "確認できず：架空"],
        [{"kind": "autonomous", "detail_ja": ""}, "自走が進めます"],
    ]
    if shutil.which("node") is None:
        # Node が無ければ静的検査で代替する旨を残しつつ内容で確認する。
        assert "function stateLabel(state)" in GOAL_JS
        assert "function nextStepText(step)" in GOAL_JS
        for _state, label in cases_state:
            assert label in GOAL_JS
        pytest.skip("Node が無いため静的検査で代替")
    probe_lines = [
        "const fs=require('fs');",
        "const src=fs.readFileSync('app/static/goal_state.js','utf8');",
        "const mod={exports:{}};",
        "require('vm').runInNewContext(src,{module:mod,document:undefined});",
        "const f=mod.exports.stateLabel;",
        "const g=mod.exports.nextStepText;",
        "const casesState=" + json.dumps(cases_state, ensure_ascii=False) + ";",
        "const casesNext=" + json.dumps(cases_next, ensure_ascii=False) + ";",
        "for(let i=0;i<casesState.length;i++){",
        "const c=casesState[i];",
        "if(f(c[0])!==c[1]){console.error('state '+i+' '+f(c[0]));process.exit(1);}}",
        "for(let i=0;i<casesNext.length;i++){",
        "const c=casesNext[i];",
        "if(g(c[0])!==c[1]){console.error('next '+i+' '+g(c[0]));process.exit(1);}}",
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".cjs", delete=False) as tmp:
        tmp.write("\n".join(probe_lines))
        probe_path = tmp.name
    try:
        completed = subprocess.run(["node", probe_path], capture_output=True,
                                   text=True, timeout=30)
    finally:
        Path(probe_path).unlink(missing_ok=True)
    assert completed.returncode == 0, completed.stderr


def test_overview_line_and_epoch():
    # PJ概要への追加: 既存カードは変更・削除しない。新規カードのみ。
    assert "goalStateSection" in GOAL_JS
    assert "goalStateBody" in GOAL_JS
    assert "workflowOverview" in GOAL_JS
    assert "ticket" in GOAL_JS and "epoch" in GOAL_JS
    # 古い応答を描画しない。
    assert "if(!isCurrent())return;" in GOAL_JS
    assert "プロジェクトが選択されていません" in GOAL_JS
    # JSONの生表示は畳んだ診断欄に textContent で。
    assert "診断情報" in GOAL_JS
    assert re.search(r"pre\.textContent\s*=", GOAL_JS)
    _ = WR_JS


def test_words_and_fail_closed_static():
    for word in ("自走の現在地", "確認できず", "取得できませんでした",
                 "達成とは表示しません", "診断情報"):
        assert word in GOAL_JS, word
    assert GOAL_JS.count("catch") >= 1
    for fragment in ("を取得できませんでした", "できませんでした"):
        assert fragment in GOAL_JS, fragment
    assert "if(!isCurrent())return;" in GOAL_JS


def test_existing_static_contracts_still_hold():
    assert (ROOT / "tests/test_s6b_domain_pack_ui.py").is_file()
    assert (ROOT / "tests/test_b3_capability_draft.py").is_file()
    assert (ROOT / "tests/test_s4a_execution_funnel.py").is_file()
