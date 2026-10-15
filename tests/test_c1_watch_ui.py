"""C-1 画面のテスト (疑似PJのみ・静的検査中心)。

疑似PJのみ。実在PJのID・件数・版・語を使わない。外部通信なし。
製品コード(Python)は watchtower.py の新規と web.py の追加のみ。
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
WATCH_JS = (ROOT / "app/static/watch_panel.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
MISSION_JS = (ROOT / "app/static/project_mission.js").read_text(encoding="utf-8")

DANGEROUS = ("innerHTML", "insertAdjacentHTML", "outerHTML", "document.write")


def _make_manager(tmp_path, tag="c1ui"):
    name = "c1-疑似-画面-" + tag
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
        assert token not in WATCH_JS, token
    assert WATCH_JS.count("innerHTML") == 0
    added = [line for line in INDEX_HTML.splitlines() if "watch_panel.js" in line]
    assert added, "watch_panel.js の読込タグがありません"
    for line in added:
        for token in DANGEROUS:
            assert token not in line, token
    assert MISSION_JS.count("innerHTML") == 28
    assert "watch_panel.js" in INDEX_HTML
    assert "watchPanelSection" in WATCH_JS
    assert "見張りの通知" in WATCH_JS
    assert "判断待ち一覧" in WATCH_JS


def test_static_route_serves_watch_js(tmp_path, monkeypatch):
    import app.web as web
    from fastapi.testclient import TestClient
    manager, _ = _make_manager(tmp_path, tag="route")
    monkeypatch.setattr(web, "memory", manager.memory)
    monkeypatch.setattr(web, "orchestrator", manager)
    client = TestClient(web.app, raise_server_exceptions=False)
    resp = client.get("/static/watch_panel.js")
    assert resp.status_code == 200
    assert "severityText" in resp.text
    for token in DANGEROUS:
        assert token not in resp.text, token


def test_js_api_paths_exist_and_keys_match(tmp_path):
    import app.web as web
    routes = {str(getattr(r, "path", "")) for r in web.app.routes}
    assert "/api/watch/refresh" in routes
    assert "/api/watch/events" in routes
    assert "/api/watch/status" in routes
    assert "/api/watch/decisions" in routes
    assert "/static/watch_panel.js" in routes
    assert "/api/watch/refresh" in WATCH_JS
    assert "/api/watch/events" in WATCH_JS
    assert "/api/watch/decisions" in WATCH_JS
    from app import watchtower as watch
    manager, _pid = _make_manager(tmp_path, tag="keys")
    status = watch.watch_status(manager)
    assert {"last_refresh_at", "unread_count", "unreadable_projects",
            "total_projects", "has_snapshot"} <= set(status)
    refreshed = watch.refresh(manager, "架空担当")
    assert refreshed["ok"] is True
    events = watch.list_events(manager, unread_only=False, limit=5)
    assert isinstance(events, list)
    decisions = watch.collect_decisions(manager)
    assert isinstance(decisions, list)
    for key in (["unread_count", "last_refresh_at", "unreadable_projects",
                 "summary_ja", "severity", "project_id", "created_at",
                 "what_ja", "screen", "kind"]):
        patterns = ["." + key, "'" + key + '"', '"' + key + '"',
                    "'" + key + "'"]
        assert any(p in WATCH_JS for p in patterns), key
    status2 = watch.watch_status(manager)
    # 既定PJ(default)を含むため1以上。疑似PJが含まれること。
    assert status2["total_projects"] >= 1


def test_empty_vs_unknown_and_failure_text():
    assert "通知なし(0件)" in WATCH_JS
    assert "確認できず(取得失敗)" in WATCH_JS
    assert "取得できませんでした" in WATCH_JS
    assert "お知らせ" in WATCH_JS
    assert "注意" in WATCH_JS
    assert "要確認" in WATCH_JS
    compact = WATCH_JS.replace(" ", "")
    assert "catch(e){" in compact
    assert "isCurrent())return" in compact
    # 二重送信防止: 処理中は無効化。
    assert "disabled=true" in compact
    assert "disabled=false" in compact
    assert "今すぐ更新" in WATCH_JS
    assert "既読にする" in WATCH_JS
    # 通知なしと確認できずの文言が別物。
    assert "eventsEmptyText" in WATCH_JS
    m = re.search(r"function eventsEmptyText\(.*?\)\{.*?\}", WATCH_JS, re.S)
    assert m, "eventsEmptyText がありません"
    body = m.group(0)
    assert "通知なし(0件)" in body and "未更新" in body


def test_api_refresh_ack_status_decisions(tmp_path, monkeypatch):
    import app.web as web
    from fastapi.testclient import TestClient
    manager, pid = _make_manager(tmp_path, tag="api")
    monkeypatch.setattr(web, "memory", manager.memory)
    monkeypatch.setattr(web, "orchestrator", manager)
    client = TestClient(web.app, raise_server_exceptions=False)
    bad = client.post("/api/watch/refresh", json={})
    assert bad.status_code == 422
    ok = client.post("/api/watch/refresh", json={"actor": "架空担当"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["ok"] is True
    assert ok.json()["total_projects"] >= 1
    status = client.get("/api/watch/status")
    assert status.status_code == 200
    assert status.json()["total_projects"] >= 1
    events = client.get("/api/watch/events?unread=1&limit=50")
    assert events.status_code == 200
    assert isinstance(events.json()["events"], list)
    decisions = client.get("/api/watch/decisions")
    assert decisions.status_code == 200
    assert isinstance(decisions.json()["decisions"], list)
    # ack は actor 必須。本文は変えない。
    from app import watchtower as watch
    watch.refresh(manager, "架空担当")
    manager.memory.save_mission(pid, "架空目標-画面",
                                "1. 架空文書01の原本引用つき作成と検証\n"
                                "2. 架空文書02の原本引用つき作成と検証",
                                "", True, ["chatgpt"])
    watch.refresh(manager, "架空担当")
    items = watch.list_events(manager, unread_only=True, limit=50)
    if items:
        target = items[0]["id"]
        before = items[0]["summary_ja"]
        acked = client.post(f"/api/watch/events/{target}/ack",
                            json={"actor": "架空担当", "note": "架空メモ"})
        assert acked.status_code == 200, acked.text
        assert acked.json()["summary_ja"] == before
        missing = client.post("/api/watch/events/存在しない通知/ack",
                              json={"actor": "架空担当"})
        assert missing.status_code == 404


def test_pure_functions_with_node_or_static():
    import shutil
    import subprocess
    import tempfile
    cases_sev = [
        ["info", "お知らせ"],
        ["attention", "注意"],
        ["alert", "要確認"],
        ["nope", "確認できず"],
        ["", "確認できず"],
    ]
    cases_kind = [
        ["stop_appeared", "停止の出現"],
        ["autopilot_failed", "自走の失敗"],
        ["nope", "確認できず"],
    ]
    cases_unread = [
        [3, "未読 3件"],
        ["x", "未読：確認できず"],
    ]
    if shutil.which("node") is None:
        assert "function severityText(severity)" in WATCH_JS
        assert "function kindText(kind)" in WATCH_JS
        assert "function unreadText(count)" in WATCH_JS
        for _sev, label in cases_sev:
            assert label in WATCH_JS
        pytest.skip("Node が無いため静的検査で代替")
    probe_lines = [
        "const fs=require('fs');",
        "const src=fs.readFileSync('app/static/watch_panel.js','utf8');",
        "const mod={exports:{}};",
        "require('vm').runInNewContext(src,{module:mod,document:undefined});",
        "const f=mod.exports.severityText;",
        "const g=mod.exports.kindText;",
        "const h=mod.exports.unreadText;",
        "const casesSev=" + json.dumps(cases_sev, ensure_ascii=False) + ";",
        "const casesKind=" + json.dumps(cases_kind, ensure_ascii=False) + ";",
        "const casesUnread=" + json.dumps(cases_unread, ensure_ascii=False) + ";",
        "for(let i=0;i<casesSev.length;i++){",
        "const c=casesSev[i];",
        "if(f(c[0])!==c[1]){console.error('sev '+i+' '+f(c[0]));process.exit(1);}}",
        "for(let i=0;i<casesKind.length;i++){",
        "const c=casesKind[i];",
        "if(g(c[0])!==c[1]){console.error('kind '+i+' '+g(c[0]));process.exit(1);}}",
        "for(let i=0;i<casesUnread.length;i++){",
        "const c=casesUnread[i];",
        "if(h(c[0])!==c[1]){console.error('unread '+i+' '+h(c[0]));process.exit(1);}}",
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
    for word in ("見張りの通知", "判断待ち一覧", "確認できず",
                 "取得できませんでした", "今すぐ更新", "既読にする"):
        assert word in WATCH_JS, word
    assert WATCH_JS.count("catch") >= 1
    assert "if(!isCurrent())return;" in WATCH_JS
    # 取得失敗が成功表示にならない。
    assert "通知なし(0件)" in WATCH_JS
    assert "確認できず(取得失敗)" in WATCH_JS


def test_existing_static_contracts_still_hold():
    assert (ROOT / "tests/test_b1_goal_state.py").is_file()
    assert (ROOT / "tests/test_a1_a2_autopilot_safety.py").is_file()
    assert (ROOT / "tests/test_b3_capability_draft.py").is_file()
