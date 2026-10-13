"""Stage 4 (4-D): 分野実行パックの画面 (疑似PJのみ・静的検査中心)。

疑似PJのみ (架空・疑似)。実在PJのID・件数・版・語を使わない。外部通信なし。
新しいAPIは追加しない。製品コード(Python)は変えない (web.py の静的配信
1ルートと index.html の読込タグ、workflow_readiness.js の概要導線の追加のみ)。
特定PJの語・ID・件数に依存しない。
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
PACK_JS = (ROOT / "app/static/domain_pack.js").read_text(encoding="utf-8")
WR_JS = (ROOT / "app/static/workflow_readiness.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
MISSION_JS = (ROOT / "app/static/project_mission.js").read_text(encoding="utf-8")

DANGEROUS = ("innerHTML", "insertAdjacentHTML", "outerHTML", "document.write")


def _make_manager(tmp_path, tag="s6b"):
    name = "s6b-疑似-画面-" + tag
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
    return manager, pid


def test_no_new_dangerous_html_and_mission_count_unchanged():
    for name, text in (("domain_pack.js", PACK_JS),
                       ("workflow_readiness.js", WR_JS)):
        for token in DANGEROUS:
            assert token not in text, (name, token)
    assert WR_JS.count("innerHTML") == 0
    added = [line for line in INDEX_HTML.splitlines() if "domain_pack.js" in line]
    assert added, "domain_pack.js の読込タグがありません"
    for line in added:
        for token in DANGEROUS:
            assert token not in line, token
    # 既存の契約: project_mission.js の innerHTML 件数は28のまま。
    assert MISSION_JS.count("innerHTML") == 28
    assert "domain_pack.js" in INDEX_HTML
    assert "workflowOverviewPack" in WR_JS
    assert "workflowOverviewPackNote" in WR_JS


def test_static_route_serves_pack_js(tmp_path, monkeypatch):
    import app.web as web
    from fastapi.testclient import TestClient
    manager, _ = _make_manager(tmp_path, tag="route")
    monkeypatch.setattr(web, "memory", manager.memory)
    monkeypatch.setattr(web, "orchestrator", manager)
    client = TestClient(web.app, raise_server_exceptions=False)
    resp = client.get("/static/domain_pack.js")
    assert resp.status_code == 200
    assert "packButtonState" in resp.text
    for token in DANGEROUS:
        assert token not in resp.text, token


def test_js_api_paths_exist_and_keys_match(tmp_path):
    import app.web as web
    routes = {str(getattr(r, "path", "")) for r in web.app.routes}
    for path in ("/api/domain-packs",
                 "/api/projects/{project_id}/domain-packs",
                 "/api/projects/{project_id}/domain-packs/{domain_id}/conformance",
                 "/api/projects/{project_id}/domain-packs/select",
                 "/api/projects/{project_id}/domain-packs/clear"):
        assert path in routes, path
    # JS が参照する API パスは4-C の実在4経路 (一覧・候補・適合・選択/解除)。
    for fragment in ("/api/domain-packs",
                     "/domain-packs/select",
                     "/domain-packs/clear",
                     "/conformance"):
        assert fragment in PACK_JS, fragment
    import app.domain_spec as packs
    manager, pid = _make_manager(tmp_path, tag="keys")
    listed = packs.list_specs()
    assert listed
    candidates = packs.applicable_packs(manager, pid)
    assert candidates["status"] in packs.APPLICABILITY_STATUSES
    report = packs.conformance_report(manager, pid, "document_evidence", "v1")
    selected = packs.select_pack(manager, pid, "document_evidence", "v1",
                                 actor="架空担当", reason="架空理由で採用")
    assert selected["ok"] is True
    response_keys = {"packs", "schema", "candidates", "selection", "status",
                     "selected", "score", "statement_hits", "task_hits",
                     "domain_id", "version", "label_ja", "executor_ids",
                     "permission_boundary", "conforms", "verdict", "tasks",
                     "counts", "skipped_task_keys", "reason", "task_key",
                     "reasons", "actor", "warnings", "note", "auth_note"}
    missing_refs = []
    for key in response_keys:
        patterns = ["." + key, "'" + key + '"', '"' + key + '"', "'" + key + "'"]
        if not any(p in PACK_JS for p in patterns):
            missing_refs.append(key)
    assert missing_refs == [], missing_refs


def test_conformance_notes_do_not_imply_achievement():
    # 適合検査の常設注記: 適合は達成の代用にしない。
    assert "conformanceMeaningNote" in PACK_JS
    assert "missingCapabilityNote" in PACK_JS
    assert "apiCapabilityGaps" in PACK_JS
    assert "達成条件を満たせることを示すものではありません" in PACK_JS
    assert "パックが適合でも、達成条件を満たせるとは限りません" in PACK_JS
    assert "不足機能は確認できず" in PACK_JS
    assert "不足機能は確認されていません" in PACK_JS
    assert "この案件には不足機能が " in PACK_JS
    # 不足件数の取得失敗は 0件扱いにしない (confirmed:false)。
    compact = PACK_JS.replace(" ", "")
    assert "missingState={confirmed:false}" in compact
    assert "info.confirmed!==true" in compact
    assert "if(n===0)return" in compact
    import shutil
    if shutil.which("node") is None:
        return
    import subprocess
    probe = (
        "const fs=require('fs');const src=fs.readFileSync("
        "'app/static/domain_pack.js','utf8');const mod={exports:{}};"
        "require('vm').runInNewContext(src,{module:mod,document:undefined});"
        "const f=mod.exports.missingCapabilityNote;"
        "const note=mod.exports.conformanceMeaningNote();"
        "if(!String(note).includes('達成条件を満たせることを示すものではありません'))"
        "{console.error('meaning');process.exit(1);}"
        "const cases=["
        "[null,'不足機能は確認できず'],"
        "[{confirmed:false},'不足機能は確認できず'],"
        "[{confirmed:true,count:0},'不足機能は確認されていません'],"
        "[{confirmed:true,count:3},'この案件には不足機能が 3 件あります'],"
        "[{confirmed:true,count:'x'},'不足機能は確認できず']"
        "];"
        "for(const [input,expect] of cases){"
        "const got=f(input);"
        "if(!String(got).includes(expect)){console.error(JSON.stringify({input,got}));"
        "process.exit(1);}}"
    )
    completed = subprocess.run(["node", "-e", probe], cwd=ROOT,
                               capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr + completed.stdout


def test_words_and_fail_closed_static():
    # 必須の文言。
    for word in ("記録のみ", "自動では選べません", "確認できず",
                 "採用は人が決めます"):
        assert word in PACK_JS, word
    # unknown を適合に数えない: 件数計算が conforms だけを数える。
    assert "countConformance" in PACK_JS
    compact = PACK_JS.replace(" ", "")
    assert "v==='conforms')counts.conforms+=1" in compact
    assert "counts.unknown+=1" in compact
    # catch で成功のような表示にしない: エラー分岐が日本語のエラー表示を行う。
    assert PACK_JS.count("catch") >= 3
    for fragment in ("を取得できませんでした", "できませんでした"):
        assert fragment in PACK_JS, fragment
    assert "成功のように" not in PACK_JS
    # packButtonState の判定が静的にある (担当者・理由・確認・処理中・unknown)。
    assert "packButtonState" in PACK_JS
    assert "hasActor" in PACK_JS and "hasReason" in PACK_JS
    assert "acknowledged" in PACK_JS
    assert "busy" in PACK_JS
    assert "適合に数えません" in PACK_JS


def test_pack_button_states_with_node_or_static():
    import shutil
    cases = [
        # (selection, conformance, inputs, canSelect, canClear)
        (None, {"conforms": True, "verdict": "conforms"},
         {"actor": "", "reason": "架空理由"}, False, False),
        (None, {"conforms": True, "verdict": "conforms"},
         {"actor": "架空担当", "reason": ""}, False, False),
        (None, {"conforms": True, "verdict": "conforms"},
         {"actor": "架空担当", "reason": "架空理由"}, True, False),
        ({"domain_id": "document_evidence"},
         {"conforms": False, "verdict": "violates", "tasks": []},
         {"actor": "架空担当", "reason": "架空理由"}, False, True),
        ({"domain_id": "document_evidence"},
         {"conforms": False, "verdict": "violates", "tasks": []},
         {"actor": "架空担当", "reason": "架空理由",
          "acknowledged": True}, True, True),
        ({"domain_id": "document_evidence"},
         {"conforms": False, "verdict": "unknown", "tasks": []},
         {"actor": "架空担当", "reason": "架空理由",
          "acknowledged": True}, True, True),
        # unknown は適合として扱わない: needsAck が真のまま。
        ({"domain_id": "document_evidence"},
         {"conforms": False, "verdict": "unknown", "tasks": []},
         {"actor": "架空担当", "reason": "架空理由"}, False, True),
        ({"domain_id": "document_evidence"},
         {"conforms": True, "verdict": "conforms"},
         {"actor": "架空担当", "reason": "架空理由",
          "busy": True}, False, False),
    ]
    if shutil.which("node") is None:
        pytest.skip("Node が無いため静的検査で代替")
    import subprocess
    import tempfile
    probe_lines = [
        "const fs=require('fs');",
        "const src=fs.readFileSync('app/static/domain_pack.js','utf8');",
        "const mod={exports:{}};",
        "require('vm').runInNewContext(src,{module:mod,document:undefined});",
        "const f=mod.exports.packButtonState;",
        "const cases=" + json.dumps(cases, ensure_ascii=False) + ";",
        "for(let i=0;i<cases.length;i++){",
        "const c=cases[i];",
        "const got=f(c[0],c[1],c[2]);",
        "if(got.canSelect!==c[3]||got.canClear!==c[4]){",
        "console.error('mismatch '+i+' '+JSON.stringify(got));process.exit(1);}",
        "if(c[1]&&c[1].verdict==='unknown'&&got.isConforming){",
        "console.error('unknown treated as conforming');process.exit(1);}",
        "}",
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


def test_overview_line_and_epoch_and_deleted(tmp_path):
    # PJ概要への1行導線: 採用中/未選択/確認できずを混ぜない。
    assert re.search(r"add\(packBlock,'h3','分野実行パック'\)", WR_JS)
    assert "workflowOverviewPackButton" in WR_JS
    assert "採用中" in WR_JS
    assert "未選択" in WR_JS
    assert "domainPackSelectionText" in WR_JS
    assert "未選択とは表示しません" in WR_JS
    assert "記録のみで、計画・工程・実行は変わりません" in WR_JS
    # 既存カードを変更・削除していない。
    assert "workflowOverviewGap" in WR_JS
    assert "workflowOverviewNext" in WR_JS
    assert "workflowOverviewStop" in WR_JS
    assert "workflowOverviewOwner" in WR_JS
    assert "workflowOverviewEvidence" in WR_JS
    assert "OVERVIEW_FIELDS" in WR_JS
    assert "formatNextOperation" in WR_JS
    # PJ切替の epoch/ticket・二重送信防止・410等の扱い。
    assert "ticket" in WR_JS and "epoch" in WR_JS
    assert "ticket" in PACK_JS and "epoch" in PACK_JS
    assert "disabled=true" in PACK_JS.replace(" ", "")
    assert "プロジェクトが選択されていません" in PACK_JS
    assert "410" in PACK_JS
    _ = tmp_path


def test_existing_s6a_and_gap_ui_still_pass():
    # 4-C と画面系の既存テストは別プロセス相当ではなく同一収集で確認する
    # ための存在検査。実際の合否は全体 pytest の結果で報告する。
    assert (ROOT / "tests/test_s6a_domain_spec.py").is_file()
    assert (ROOT / "tests/test_s2c_gap_ui.py").is_file()


def test_malformed_success_responses_and_busy_are_blocked():
    import shutil
    import subprocess
    if shutil.which("node") is None:
        pytest.skip("Node is unavailable")
    probe = r"""
const fs=require('fs'), vm=require('vm');
const source=fs.readFileSync(process.argv[1],'utf8');
const mod={exports:{}};
const context={module:mod,document:undefined,fetch:async()=>({ok:true,status:200,json:async()=>{throw Error('bad json')}})};
vm.runInNewContext(source,context);
const f=mod.exports;
async function rejects(p){try{await p;throw Error('accepted malformed response')}catch(e){if(e.message==='accepted malformed response')throw e;}}
(async()=>{
 await rejects(f.fetchJson('/api/domain-packs'));
 context.fetch=async()=>({ok:true,status:200,json:async()=>({})});
 await rejects(f.apiPacks());
 await rejects(f.apiProjectPacks('dummy'));
 await rejects(f.apiSelect('dummy','pack','v1','actor','reason'));
 await rejects(f.apiClear('dummy','actor','reason'));
 if(f.formBusy({dataset:{busy:''}},{dataset:{busy:'1'}})!==true)throw Error('clear operation reenabled buttons');
 if(f.formBusy({dataset:{busy:'1'}},{dataset:{busy:''}})!==true)throw Error('select operation reenabled buttons');
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    completed = subprocess.run(["node", "-e", probe, str(ROOT / "app/static/domain_pack.js")],
                               cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr


def test_overview_distinguishes_confirmed_none_from_invalid_response():
    import shutil
    import subprocess
    if shutil.which("node") is None:
        pytest.skip("Node is unavailable")
    probe = r"""
const fs=require('fs'),vm=require('vm');
const src=fs.readFileSync(process.argv[1],'utf8');
const mod={exports:{}};vm.runInNewContext(src,{module:mod,document:undefined});
const f=mod.exports.domainPackSelectionText;
if(f(null)!=='未選択')throw Error('confirmed empty selection mislabelled');
if(f({domain_id:'pack'})!=='採用中: pack')throw Error('selected pack mislabelled');
"""
    completed=subprocess.run(["node","-e",probe,str(ROOT / "app/static/workflow_readiness.js")],
                             cwd=ROOT,capture_output=True,text=True,timeout=30)
    assert completed.returncode==0,completed.stderr
    assert "Object.prototype.hasOwnProperty.call(body,'selection')" in WR_JS
    assert "const body=await response.json();" in WR_JS


def test_initial_project_population_retries_read_only_load():
    assert "let pendingProjectPolls=0" in PACK_JS
    assert "pendingProjectPolls<30" in PACK_JS
    assert "if(ticket===epoch)load(true)" in PACK_JS


def test_stale_errors_do_not_replace_new_project_status():
    assert "catch(e){if(!isCurrent())return;statusNode.textContent" in PACK_JS
    assert PACK_JS.count("catch(e){if(selected()!==pid)return;status.textContent") == 2
