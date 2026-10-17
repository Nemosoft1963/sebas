"""E 成果台帳の画面テスト (疑似PJのみ・静的検査中心)。

疑似PJのみ。実在PJのID・件数・版・語を使わない。外部通信なし。
製品コード(Python)は outcome_ledger.py の新規と web.py の追加のみ。
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
LEDGER_JS = (ROOT / "app/static/ledger_panel.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
MISSION_JS = (ROOT / "app/static/project_mission.js").read_text(encoding="utf-8")

DANGEROUS = ("innerHTML", "insertAdjacentHTML", "outerHTML", "document.write")


def _make_manager(tmp_path, tag="eui"):
    name = "e-疑似-画面-" + tag
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
        assert token not in LEDGER_JS, token
    assert LEDGER_JS.count("innerHTML") == 0
    added = [line for line in INDEX_HTML.splitlines()
             if "ledger_panel.js" in line]
    assert added, "ledger_panel.js の読込タグがありません"
    for line in added:
        for token in DANGEROUS:
            assert token not in line, token
    assert MISSION_JS.count("innerHTML") == 28
    assert "ledger_panel.js" in INDEX_HTML
    assert "ledgerPanelSection" in LEDGER_JS
    assert "成果台帳" in LEDGER_JS


def test_static_route_serves_ledger_js(tmp_path, monkeypatch):
    import app.web as web
    from fastapi.testclient import TestClient
    manager, _ = _make_manager(tmp_path, tag="route")
    monkeypatch.setattr(web, "memory", manager.memory)
    monkeypatch.setattr(web, "orchestrator", manager)
    client = TestClient(web.app, raise_server_exceptions=False)
    resp = client.get("/static/ledger_panel.js")
    assert resp.status_code == 200
    assert "metricText" in resp.text
    for token in DANGEROUS:
        assert token not in resp.text, token


def test_js_api_paths_exist_and_keys_match(tmp_path):
    import app.web as web
    routes = {str(getattr(r, "path", "")) for r in web.app.routes}
    assert "/api/ledger/metrics" in routes
    assert "/api/ledger/snapshot" in routes
    assert "/api/ledger/snapshots" in routes
    assert "/api/ledger/suggestions" in routes
    assert "/api/ledger/suggestions/{suggestion_id}/decision" in routes
    assert "/static/ledger_panel.js" in routes
    assert "/api/ledger/metrics" in LEDGER_JS
    assert "/api/ledger/snapshot" in LEDGER_JS
    assert "/api/ledger/snapshots" in LEDGER_JS
    assert "/api/ledger/suggestions" in LEDGER_JS
    from app import outcome_ledger as ledger
    manager, _ = _make_manager(tmp_path, tag="keys")
    metrics = ledger.compute_metrics(manager)
    for key in ("unknown_passed", "duplicate_proposals",
                "unauthorized_external", "false_achieved",
                "stop_to_action_rate", "criteria_fulfilment",
                "activity", "確認できず"):
        patterns = ["." + key, "'" + key + '"', '"' + key + '"',
                    "'" + key + "'"]
        assert any(p in LEDGER_JS for p in patterns), key
    assert metrics["format"] == "e1-ledger/1"
    # 0と「確認できず」の文言の区別。
    assert "問題なし" in LEDGER_JS
    assert "確認できず" in LEDGER_JS
    assert "要確認" in LEDGER_JS


def test_zero_vs_unknown_and_failure_text():
    assert "0件（問題なし）" in LEDGER_JS
    assert "確認できず(取得失敗)" in LEDGER_JS
    assert "取得できませんでした" in LEDGER_JS
    assert "今の指標を台帳に記録" in LEDGER_JS
    assert "担当者名を入力してください" in LEDGER_JS
    assert "理由を入力してください" in LEDGER_JS
    assert "自動変更：なし" in LEDGER_JS
    assert "活動量" in LEDGER_JS
    compact = LEDGER_JS.replace(" ", "")
    assert "catch(e){" in compact
    assert "isCurrent())return" in compact
    # 二重送信防止: 処理中は無効化。
    assert "disabled=true" in compact
    assert "disabled=false" in compact
    # 記録なしと確認できずの文言が別物。
    assert "記録なし" in LEDGER_JS
    assert "確認できず" in LEDGER_JS


def test_pure_functions_with_node_or_static():
    cases_metric = [
        ["unknown_passed", "判定不能の通過"],
        ["duplicate_proposals", "重複提案"],
        ["unauthorized_external", "無許可の外部作用"],
        ["false_achieved", "目標の誤達成"],
        ["stop_to_action_rate", "停止から次操作への到達率"],
        ["criteria_fulfilment", "達成条件の充足率"],
        ["nope", "確認できず"],
    ]
    if shutil.which("node") is None:
        assert "function metricText(key)" in LEDGER_JS
        assert "function rateText(item)" in LEDGER_JS
        assert "function activityText(activity)" in LEDGER_JS
        for _key, text in cases_metric:
            assert text in LEDGER_JS
        pytest.skip("Node が無いため静的検査で代替")
    probe_lines = [
        "const fs=require('fs');",
        "const src=fs.readFileSync('app/static/ledger_panel.js','utf8');",
        "const mod={exports:{}};",
        "require('vm').runInNewContext(src,{module:mod,document:undefined,window:{}});",
        "const f=mod.exports.metricText;",
        "const g=mod.exports.rateText;",
        "const h=mod.exports.activityText;",
        "const k=mod.exports.metricLineText;",
        "const casesMetric=" + json.dumps(cases_metric, ensure_ascii=False) + ";",
        "for(let i=0;i<casesMetric.length;i++){",
        "const c=casesMetric[i];",
        "if(f(c[0])!==c[1]){console.error('metric '+i+' '+f(c[0]));process.exit(1);}}",
        "if(g({status:'ok',value:null})!=='測定できない'){console.error('rate null');process.exit(1);}",
        "if(g({status:'unknown',value:null})!=='確認できず'){console.error('rate unknown');process.exit(1);}",
        "if(g({status:'ok',value:0.5})!=='50%'){console.error('rate 50 '+g({status:'ok',value:0.5}));process.exit(1);}",
        "if(k('unknown_passed',{status:'ok',value:0})!=='判定不能の通過：0件（問題なし）'){console.error('line zero');process.exit(1);}",
        "if(k('unknown_passed',{status:'unknown',value:null})!=='判定不能の通過：確認できず'){console.error('line unknown');process.exit(1);}",
        "if(k('unknown_passed',{status:'ok',value:2})!=='判定不能の通過：2件（要確認）'){console.error('line warn');process.exit(1);}",
        "if(h(null)!=='活動量：確認できず'){console.error('activity null');process.exit(1);}",
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
    for word in ("成果台帳", "方針の改善提案", "今の指標を台帳に記録",
                 "確認できず", "取得できませんでした", "記録なし",
                 "測定できない", "活動量", "採否", "理由"):
        assert word in LEDGER_JS, word
    assert LEDGER_JS.count("catch") >= 1
    assert "if(!isCurrent())return;" in LEDGER_JS
    assert "確認できず(取得失敗)" in LEDGER_JS
    # 色と文言の両方 (色だけの表示にしない)。
    assert "ledger-metric-ok" in LEDGER_JS
    assert "ledger-metric-warn" in LEDGER_JS
    assert "ledger-metric-unknown" in LEDGER_JS
    assert "問題なし" in LEDGER_JS
    assert "要確認" in LEDGER_JS
    # 見張りの通知パネルの近く (watchPanelSection を基点にする)。
    assert "watchPanelSection" in LEDGER_JS
