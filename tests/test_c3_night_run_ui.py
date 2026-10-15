"""C-3 画面のテスト (疑似PJのみ・静的検査中心)。

疑似PJのみ。実在PJのID・件数・版・語を使わない。外部通信なし。
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
NIGHT_JS = (ROOT / "app/static/night_run_panel.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
MISSION_JS = (ROOT / "app/static/project_mission.js").read_text(encoding="utf-8")

DANGEROUS = ("innerHTML", "insertAdjacentHTML", "outerHTML", "document.write")


def _make_manager(tmp_path, tag="c3ui"):
    name = "c3-疑似-画面-" + tag
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
        assert token not in NIGHT_JS, token
    assert NIGHT_JS.count("innerHTML") == 0
    added = [line for line in INDEX_HTML.splitlines()
             if "night_run_panel.js" in line]
    assert added, "night_run_panel.js の読込タグがありません"
    for line in added:
        for token in DANGEROUS:
            assert token not in line, token
    assert MISSION_JS.count("innerHTML") == 28
    assert "night_run_panel.js" in INDEX_HTML
    assert "nightRunPanelSection" in NIGHT_JS
    assert "夜間実行" in NIGHT_JS


def test_static_route_serves_night_js(tmp_path, monkeypatch):
    import app.web as web
    from fastapi.testclient import TestClient
    manager, _ = _make_manager(tmp_path, tag="route")
    monkeypatch.setattr(web, "memory", manager.memory)
    monkeypatch.setattr(web, "orchestrator", manager)
    client = TestClient(web.app, raise_server_exceptions=False)
    resp = client.get("/static/night_run_panel.js")
    assert resp.status_code == 200
    assert "killStateText" in resp.text
    for token in DANGEROUS:
        assert token not in resp.text, token


def test_js_api_paths_exist_and_keys_match(tmp_path):
    import app.web as web
    routes = {str(getattr(r, "path", "")) for r in web.app.routes}
    assert "/api/night-run/start" in routes
    assert "/api/night-run/{run_id}/tick-next" in routes
    assert "/api/night-run/{run_id}/finish" in routes
    assert "/api/night-run/latest" in routes
    assert "/api/night-run/{run_id}" in routes
    assert "/api/autopilot/disable-all" in routes
    assert "/api/autopilot/kill-switch/release" in routes
    assert "/api/autopilot/kill-switch" in routes
    assert "/static/night_run_panel.js" in routes
    assert "/api/night-run/latest" in NIGHT_JS
    assert "/api/autopilot/disable-all" in NIGHT_JS
    assert "/api/autopilot/kill-switch" in NIGHT_JS
    from app import night_run as night
    manager, _pid = _make_manager(tmp_path, tag="keys")
    latest = night.get_latest(manager)
    assert latest["has_run"] is False
    assert latest["reason"] == "実行なし"
    assert night.kill_switch_state(manager) == "inactive"
    for key in (["has_run", "total_projects", "total_ticks",
                 "project_id", "outcome", "next_action",
                 "state", "実行なし", "確認できず"]):
        patterns = ["." + key, "'" + key + '"', '"' + key + '"',
                    "'" + key + "'"]
        assert any(p in NIGHT_JS for p in patterns), key


def test_empty_vs_unknown_and_failure_text():
    assert "実行なし" in NIGHT_JS
    assert "確認できず(取得失敗)" in NIGHT_JS
    assert "取得できませんでした" in NIGHT_JS
    assert "有効" in NIGHT_JS and "無効" in NIGHT_JS
    compact = NIGHT_JS.replace(" ", "")
    assert "catch(e){" in compact
    assert "isCurrent())return" in compact
    # 二重送信防止: 処理中は無効化。
    assert "disabled=true" in compact
    assert "disabled=false" in compact
    assert "全PJの自走を止める" in NIGHT_JS
    assert "キルスイッチを解除" in NIGHT_JS
    assert "担当者名を入力してください" in NIGHT_JS
    assert "confirm(" in NIGHT_JS
    # 実行なしと確認できずの文言が別物。
    assert "latestEmptyText" in NIGHT_JS
    m = re.search(r"function latestEmptyText\(.*?\)\{.*?\}", NIGHT_JS, re.S)
    assert m, "latestEmptyText がありません"
    body = m.group(0)
    assert "実行なし" in body and "報告なし" in body


def test_api_night_run_and_kill_switch(tmp_path, monkeypatch):
    import app.autopilot as auto
    import app.web as web
    from fastapi.testclient import TestClient
    manager, pid = _make_manager(tmp_path, tag="api")
    auto.set_enabled(manager, pid, True, actor="架空担当",
                     reason="架空の有効化理由")
    monkeypatch.setattr(web, "memory", manager.memory)
    monkeypatch.setattr(web, "orchestrator", manager)
    client = TestClient(web.app, raise_server_exceptions=False)
    bad = client.post("/api/night-run/start", json={})
    assert bad.status_code == 422
    ok = client.post("/api/night-run/start",
                     json={"actor": "架空担当",
                           "budget": {"max_ticks": 3}})
    assert ok.status_code == 200, ok.text
    run_id = ok.json()["run_id"]
    token = ok.json()["token"]
    # 2つ目は409。
    dup = client.post("/api/night-run/start",
                      json={"actor": "架空担当", "budget": {}})
    assert dup.status_code == 409
    ticked = client.post(f"/api/night-run/{run_id}/tick-next",
                         json={"token": token})
    assert ticked.status_code == 200, ticked.text
    assert "done" in ticked.json() and "reason" in ticked.json()
    finished = client.post(f"/api/night-run/{run_id}/finish",
                           json={"token": token, "actor": "架空担当",
                                 "end_reason": "架空の終了"})
    assert finished.status_code == 200, finished.text
    assert finished.json()["report_status"] == "ok"
    latest = client.get("/api/night-run/latest")
    assert latest.status_code == 200
    assert latest.json()["has_run"] is True
    one = client.get(f"/api/night-run/{run_id}")
    assert one.status_code == 200
    assert one.json()["run_id"] == run_id
    missing = client.get("/api/night-run/nr_存在しない")
    assert missing.status_code == 404
    # キルスイッチ。
    kill = client.get("/api/autopilot/kill-switch")
    assert kill.status_code == 200
    assert kill.json()["state"] == "inactive"
    assert client.post("/api/autopilot/kill-switch/release",
                       json={}).status_code == 422
    # 全停止。
    assert client.post("/api/autopilot/disable-all",
                       json={}).status_code == 422
    stopped = client.post("/api/autopilot/disable-all",
                          json={"actor": "架空担当"})
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["ok"] is True
    kill2 = client.get("/api/autopilot/kill-switch")
    assert kill2.json()["state"] == "active"
    released = client.post("/api/autopilot/kill-switch/release",
                           json={"actor": "架空担当"})
    assert released.status_code == 200
    assert released.json()["state"] == "inactive"


def test_pure_functions_with_node_or_static():
    cases_run = [
        ["running", "実行中"],
        ["finished", "終了"],
        ["aborted", "中止"],
        ["killed", "全体停止で終了"],
        ["expired", "heartbeat途絶で終了"],
        ["nope", "確認できず"],
        ["", "確認できず"],
    ]
    cases_kill = [
        ["active", "有効（停止中）"],
        ["inactive", "無効"],
        ["unknown", "確認できず"],
        ["nope", "確認できず"],
    ]
    cases_empty = [
        [True, "報告なし"],
        [False, "実行なし"],
    ]
    if shutil.which("node") is None:
        assert "function runStateText(state)" in NIGHT_JS
        assert "function killStateText(state)" in NIGHT_JS
        assert "function latestEmptyText(hasRun)" in NIGHT_JS
        for _state, label in cases_run:
            assert label in NIGHT_JS
        pytest.skip("Node が無いため静的検査で代替")
    probe_lines = [
        "const fs=require('fs');",
        "const src=fs.readFileSync('app/static/night_run_panel.js','utf8');",
        "const mod={exports:{}};",
        "require('vm').runInNewContext(src,{module:mod,document:undefined,window:{}});",
        "const f=mod.exports.runStateText;",
        "const g=mod.exports.killStateText;",
        "const h=mod.exports.latestEmptyText;",
        "const casesRun=" + json.dumps(cases_run, ensure_ascii=False) + ";",
        "const casesKill=" + json.dumps(cases_kill, ensure_ascii=False) + ";",
        "const casesEmpty=" + json.dumps(cases_empty, ensure_ascii=False) + ";",
        "for(let i=0;i<casesRun.length;i++){",
        "const c=casesRun[i];",
        "if(f(c[0])!==c[1]){console.error('run '+i+' '+f(c[0]));process.exit(1);}}",
        "for(let i=0;i<casesKill.length;i++){",
        "const c=casesKill[i];",
        "if(g(c[0])!==c[1]){console.error('kill '+i+' '+g(c[0]));process.exit(1);}}",
        "for(let i=0;i<casesEmpty.length;i++){",
        "const c=casesEmpty[i];",
        "if(h(c[0])!==c[1]){console.error('empty '+i+' '+h(c[0]));process.exit(1);}}",
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
    for word in ("夜間実行", "キルスイッチ", "全PJの自走を止める",
                 "確認できず", "取得できませんでした", "実行なし"):
        assert word in NIGHT_JS, word
    assert NIGHT_JS.count("catch") >= 1
    assert "if(!isCurrent())return;" in NIGHT_JS
    assert "確認できず(取得失敗)" in NIGHT_JS
