"""C-4 画面のテスト (疑似PJのみ・静的検査中心)。

疑似PJのみ。実在PJのID・件数・版・語を使わない。外部通信なし。
製品コード(Python)は shadow_mode.py の新規と web.py の追加のみ。
"""
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from app.memory.short_term import ShortTermMemory
from app.project_manager import ProjectOrchestrator
from app.structured_planning import compile_task
from app.workspace_files import WorkspaceSandbox

ROOT = Path(__file__).resolve().parents[1]
SHADOW_JS = (ROOT / "app/static/shadow_panel.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
MISSION_JS = (ROOT / "app/static/project_mission.js").read_text(encoding="utf-8")

DANGEROUS = ("innerHTML", "insertAdjacentHTML", "outerHTML", "document.write")


def _make_manager(tmp_path, tag="c4ui"):
    name = "c4-疑似-画面-" + tag
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
    for token in DANGEROUS:
        assert token not in SHADOW_JS, token
    assert SHADOW_JS.count("innerHTML") == 0
    added = [line for line in INDEX_HTML.splitlines()
             if "shadow_panel.js" in line]
    assert added, "shadow_panel.js の読込タグがありません"
    for line in added:
        for token in DANGEROUS:
            assert token not in line, token
    assert MISSION_JS.count("innerHTML") == 28
    assert "shadow_panel.js" in INDEX_HTML
    assert "shadowPanelSection" in SHADOW_JS
    assert "影モード" in SHADOW_JS


def test_static_route_serves_shadow_js(tmp_path, monkeypatch):
    import app.web as web
    from fastapi.testclient import TestClient
    manager, _ = _make_manager(tmp_path, tag="route")
    monkeypatch.setattr(web, "memory", manager.memory)
    monkeypatch.setattr(web, "orchestrator", manager)
    client = TestClient(web.app, raise_server_exceptions=False)
    resp = client.get("/static/shadow_panel.js")
    assert resp.status_code == 200
    assert "labelText" in resp.text
    for token in DANGEROUS:
        assert token not in resp.text, token


def test_js_api_paths_exist_and_keys_match(tmp_path):
    import app.web as web
    routes = {str(getattr(r, "path", "")) for r in web.app.routes}
    assert "/api/projects/{project_id}/shadow/observe" in routes
    assert "/api/shadow/observe-all" in routes
    assert "/api/projects/{project_id}/shadow/records" in routes
    assert "/api/shadow/records/{record_id}/label" in routes
    assert "/api/projects/{project_id}/shadow/metrics" in routes
    assert "/api/projects/{project_id}/shadow/gate" in routes
    assert "/api/shadow/summary" in routes
    assert "/static/shadow_panel.js" in routes
    assert "/api/projects/" in SHADOW_JS and "/shadow/observe" in SHADOW_JS
    # observe-all は画面から呼ばない (人がPJごとに押すだけ) が、
    # APIは存在すること (上の routes で確認済み)。
    assert "/shadow/records" in SHADOW_JS
    assert "/shadow/metrics" in SHADOW_JS
    assert "/shadow/gate" in SHADOW_JS
    assert "/api/projects/" in SHADOW_JS and "/autopilot/enable" in SHADOW_JS
    from app import shadow_mode as shadow
    manager, pid = _make_manager(tmp_path, tag="keys")
    data = shadow.metrics(manager, pid)
    assert data["count"] == 0
    assert data["agreement"] is None
    gate = shadow.gate(manager, pid)
    assert gate["passed"] is False
    for key in (["planned_action", "stop_class", "label", "danger",
                 "agreement", "passed", "reasons", "thresholds",
                 "確認できず"]):
        patterns = ["." + key, "'" + key + '"', '"' + key + '"',
                    "'" + key + "'"]
        assert any(p in SHADOW_JS for p in patterns), key
    # 「記録なし」は「記録なし(0件)」の文言で区別すること。
    assert "記録なし(0件)" in SHADOW_JS


def test_empty_vs_unknown_and_failure_text():
    assert "記録なし(0件)" in SHADOW_JS
    assert "確認できず(取得失敗)" in SHADOW_JS
    assert "取得できませんでした" in SHADOW_JS
    assert "正しい" in SHADOW_JS and "誤り" in SHADOW_JS
    assert "判断できない" in SHADOW_JS
    compact = SHADOW_JS.replace(" ", "")
    assert "catch(e){" in compact
    assert "isCurrent())return" in compact
    # 二重送信防止: 処理中は無効化。
    assert "disabled=true" in compact
    assert "disabled=false" in compact
    assert "今の予告を記録" in SHADOW_JS
    assert "関門" in SHADOW_JS
    assert "担当者名を入力してください" in SHADOW_JS
    # 409を受けた理由表示とオーバーライドの確認 (理由10字以上)。
    assert "prompt(" in SHADOW_JS
    assert "10字" in SHADOW_JS
    assert "gate_override_reason" in SHADOW_JS
    # 記録なしと確認できずの文言が別物。
    assert "recordsEmptyText" in SHADOW_JS
    m = re.search(r"function recordsEmptyText\(.*?\)\{.*?\}", SHADOW_JS, re.S)
    assert m, "recordsEmptyText がありません"
    body = m.group(0)
    assert "記録なし(0件)" in body and "未更新" in body


def test_pure_functions_with_node_or_static():
    cases_label = [
        ["correct", "正しい"],
        ["wrong", "誤り"],
        ["unsure", "判断できない"],
        ["", "未判定"],
        ["nope", "確認できず"],
    ]
    cases_agreement = [
        [None, "一致率：測定できない"],
        ["x", "一致率：確認できず"],
    ]
    if shutil.which("node") is None:
        assert "function labelText(label)" in SHADOW_JS
        assert "function agreementText(value)" in SHADOW_JS
        assert "function gateSummaryText(gate)" in SHADOW_JS
        for _label, text in cases_label:
            assert text in SHADOW_JS
        pytest.skip("Node が無いため静的検査で代替")
    probe_lines = [
        "const fs=require('fs');",
        "const src=fs.readFileSync('app/static/shadow_panel.js','utf8');",
        "const mod={exports:{}};",
        "require('vm').runInNewContext(src,{module:mod,document:undefined,window:{}});",
        "const f=mod.exports.labelText;",
        "const g=mod.exports.agreementText;",
        "const h=mod.exports.gateSummaryText;",
        "const casesLabel=" + json.dumps(cases_label, ensure_ascii=False) + ";",
        "for(let i=0;i<casesLabel.length;i++){",
        "const c=casesLabel[i];",
        "if(f(c[0])!==c[1]){console.error('label '+i+' '+f(c[0]));process.exit(1);}}",
        "if(g(null)!=='一致率：測定できない'){console.error('agree null');process.exit(1);}",
        "if(g('x')!=='一致率：確認できず'){console.error('agree x');process.exit(1);}",
        "if(g(0.95)!=='一致率：95%'){console.error('agree 95 '+g(0.95));process.exit(1);}",
        "if(h({passed:true})!=='関門：合格'){console.error('gate pass');process.exit(1);}",
        "if(h(null)!=='関門：確認できず'){console.error('gate null');process.exit(1);}",
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


def test_words_and_fail_closed_static():
    for word in ("影モード", "関門", "今の予告を記録", "確認できず",
                 "取得できませんでした", "記録なし", "危険だった"):
        assert word in SHADOW_JS, word
    assert SHADOW_JS.count("catch") >= 1
    assert "if(!isCurrent())return;" in SHADOW_JS
    assert "確認できず(取得失敗)" in SHADOW_JS
    # 色と文言の両方 (色だけの表示にしない)。
    assert "shadow-gate-pass" in SHADOW_JS
    assert "shadow-gate-fail" in SHADOW_JS
    assert "関門：合格" in SHADOW_JS
    assert "関門：未達" in SHADOW_JS
