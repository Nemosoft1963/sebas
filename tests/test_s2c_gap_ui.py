"""Stage 2-C: 不足機能の画面 (疑似PJのみ・静的検査中心)。

疑似PJのみ (架空・疑似)。実在PJのID・件数・版・語を使わない。外部通信なし。
Jenkinsへの送信は行わない (応答にファイル本文が無いこと・送信コードが無いこと)。
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
GAP_JS = (ROOT / "app/static/capability_gap.js").read_text(encoding="utf-8")
WR_JS = (ROOT / "app/static/workflow_readiness.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
MISSION_JS = (ROOT / "app/static/project_mission.js").read_text(encoding="utf-8")


def _make_manager(tmp_path, tag="s2c"):
    name = "s2c-疑似-画面-" + tag
    db_path = tmp_path / (name + ".db")
    memory = ShortTermMemory(db_path)
    project = memory.create_project(name, workspace_path="projects/" + name)
    pid = project["id"]
    criteria = ["架空文書01の作成と検証", "架空顧客へ送信02の実行と検証",
                "架空告知文書03の作成と検証"]
    memory.save_mission(pid, "架空目標-画面", "\n".join(
        f"{i}. {text}" for i, text in enumerate(criteria, 1)), "", True, ["chatgpt"])
    tasks = []
    for i, text in enumerate(criteria, 1):
        if i == 2:
            title, scope = f"架空送信工程{i}", f"架空顧客へ送信{i:02d}を実行する。"
        else:
            title, scope = f"架空文書工程{i}", f"架空文書{i:02d}の成果物を作成する。"
        tasks.append(compile_task(i, text, {
            "title": title, "scope": scope, "headings": ["目的", "実施内容"],
        }, []))
    memory.replace_plan(pid, name + " plan", tasks)
    manager = ProjectOrchestrator(
        memory, None, lambda p: ("", []), None,
        lambda: [{"id": "chatgpt", "configured": True}],
        workspace=WorkspaceSandbox(tmp_path / ("ws-" + name)),
    )
    return manager, pid


def test_no_new_innerhtml_and_mission_count_unchanged():
    # 新規画面と追加部分に新規の危険なHTML生成が無い。index.html は既存の
    # インライン script に innerHTML を含むため、追加行自体に無いことを見る。
    for name, text in (("capability_gap.js", GAP_JS), ("workflow_readiness.js", WR_JS)):
        assert "innerHTML" not in text, name
        assert "insertAdjacentHTML" not in text, name
        assert "document.write" not in text, name
        assert "outerHTML" not in text, name
    assert WR_JS.count("innerHTML") == 0
    added = [line for line in INDEX_HTML.splitlines() if "capability_gap.js" in line]
    assert added, "capability_gap.js の読込タグがありません"
    for line in added:
        assert "innerHTML" not in line
        assert "insertAdjacentHTML" not in line
        assert "document.write" not in line
        assert "outerHTML" not in line
    # 既存の契約: project_mission.js の innerHTML 件数は28のまま。
    assert MISSION_JS.count("innerHTML") == 28
    assert "capability_gap.js" in INDEX_HTML
    assert "workflowOverviewGap" in WR_JS
    assert "workflowOverviewGapNote" in WR_JS


def test_static_route_serves_gap_js(tmp_path, monkeypatch):
    import app.web as web
    from fastapi.testclient import TestClient
    manager, _ = _make_manager(tmp_path, tag="route")
    monkeypatch.setattr(web, "memory", manager.memory)
    monkeypatch.setattr(web, "orchestrator", manager)
    client = TestClient(web.app, raise_server_exceptions=False)
    resp = client.get("/static/capability_gap.js")
    assert resp.status_code == 200
    assert "buttonStateFor" in resp.text
    assert "innerHTML" not in resp.text


def test_js_api_keys_match_response(tmp_path):
    import app.capability_gap as gap
    manager, pid = _make_manager(tmp_path, tag="keys")
    preflight = gap.capability_preflight(manager, pid)
    assert preflight["missing_capability"]
    out = gap.generate_proposals(manager, pid, preflight, actor="架空担当")
    target = out["proposals"][0]["proposal_id"]
    gap.set_proposal_status(manager, pid, target, "reviewed",
                            actor="架空担当", reason="架空確認")
    listed = gap.list_proposals(manager, pid)
    assert listed
    # API応答のキーをJSが参照している (静的抽出で照合)。
    # content_hash は画面に表示しない (応答にあるが参照しない) ため対象外。
    response_keys = {"preflight", "proposals", "missing_capability",
                     "proposal_id", "required_operation", "criterion_ids",
                     "task_keys", "existing_alternatives", "interface_contract",
                     "permission_class", "acceptance_tests", "status",
                     "package_rel", "instruction_sha256", "request_sha256",
                     "manifest_sha256", "base_ref"}
    missing_refs = []
    for key in response_keys:
        # .key / ['key'] / "key" のいずれかで参照されている。
        patterns = ["." + key, "'" + key + "'", '"' + key + '"']
        if not any(p in GAP_JS for p in patterns):
            missing_refs.append(key)
    assert missing_refs == [], missing_refs
    # required_capability_id は仮IDであり表示しない (本文にも出さない)。
    assert "required_capability_id" not in GAP_JS


def test_button_states_for_all_statuses():
    # JSから切り出した純粋関数と同等の期待表 (静的に実装の分岐を確認)。
    for status in ("draft", "reviewed", "export_ready", "submitted", "delivered",
                   "verified", "rejected", "stale", "needs_evidence"):
        assert status in GAP_JS, status
    assert "buttonStateFor" in GAP_JS
    assert "canReview:true" in GAP_JS.replace(" ", "")
    assert "canExport:true" in GAP_JS.replace(" ", "")
    # draft のとき review のみ、reviewed/export_ready のとき export のみ。
    # 既存の canReview/canExport の組合せは壊さない (2-D で canSubmit/canVerify を追加)。
    draft_block = GAP_JS[GAP_JS.find("s==='draft'"):GAP_JS.find("s==='draft'") + 160]
    assert "canReview:true" in draft_block.replace(" ", "")
    assert "canExport:false" in draft_block.replace(" ", "")
    reviewed_block = GAP_JS[GAP_JS.find("s==='reviewed'"):GAP_JS.find("s==='reviewed'") + 200]
    assert "canReview:false" in reviewed_block.replace(" ", "")
    assert "canExport:true" in reviewed_block.replace(" ", "")
    # 2-D の追加状態: submitted/delivered/verified の表示が別の言葉である。
    assert "Jenkinsに渡した" in GAP_JS
    assert "実装が納品されました。まだ能力は有効ではありません" in GAP_JS
    assert "元の停止が再判定で解消しました" in GAP_JS
    # Node があれば実行して検証し、無ければ静的検査で代替した旨を残す。
    import shutil
    if shutil.which("node") is None:
        pytest.skip("Node が無いため静的検査で代替")
    import subprocess
    import tempfile
    probe = ("const fs=require('fs');"
             "global.document=undefined;"
             "const src=fs.readFileSync('app/static/capability_gap.js','utf8');"
             "const mod={exports:{}};"
             "require('vm').runInNewContext(src,{module:mod,document:undefined});"
             "const f=mod.exports.buttonStateFor;"
             "const cases={draft:[true,false],reviewed:[false,true],"
             "export_ready:[false,true],rejected:[false,false],"
             "stale:[false,false],needs_evidence:[false,false]};"
             "for(const k of Object.keys(cases)){"
             "const got=f(k);"
             "if(got.canReview!==cases[k][0]||got.canExport!==cases[k][1])"
             "{console.error('mismatch '+k);process.exit(1);}}")
    with tempfile.NamedTemporaryFile("w", suffix=".cjs", delete=False) as tmp:
        tmp.write(probe)
        probe_path = tmp.name
    try:
        completed = subprocess.run(["node", probe_path], capture_output=True,
                                   text=True, timeout=30)
    finally:
        Path(probe_path).unlink(missing_ok=True)
    assert completed.returncode == 0, completed.stderr


def test_zero_vs_unknown_and_export_vs_verified_words():
    # 0件と未確認の区別。
    assert "不足機能はありません" in GAP_JS
    assert "不足機能：未確認" in GAP_JS
    assert "不足機能はありません" in WR_JS or "不足機能：未確認" in WR_JS
    assert "gapCountText" in WR_JS
    assert "未確認" in WR_JS
    # export_ready と verified の表示が別であること (2-D の別の言葉)。
    assert "Jenkins指示を作成済み" in GAP_JS
    assert "元の停止が再判定で解消しました" in GAP_JS
    assert GAP_JS.count("Jenkins指示を作成済み") >= 1
    assert GAP_JS.count("元の停止が再判定で解消しました") >= 1
    # delivered は「実装が納品された」ことのみ (能力は有効になっていない)。
    assert "実装が納品されました。まだ能力は有効ではありません" in GAP_JS
    # 混同させない注記がある。
    assert "別の状態" in GAP_JS
    # 未実装なのに実行可能と表示しない。
    assert "実行可能" in GAP_JS
    assert "未実装なのに実行可能" in GAP_JS or "未実装なのに「実行可能」とは表示しません" in GAP_JS.replace("「", "").replace("」", "") or "実行可能とは表示しません" in GAP_JS


def test_delivery_verify_routes_and_no_send(tmp_path, monkeypatch):
    import app.web as web
    from fastapi.testclient import TestClient
    manager, pid = _make_manager(tmp_path, tag="nosend")
    monkeypatch.setattr(web, "memory", manager.memory)
    monkeypatch.setattr(web, "orchestrator", manager)
    client = TestClient(web.app, raise_server_exceptions=False)
    # 2-D: delivery の納品入力は画面では行わない (状態表示と再判定のみ)。
    # 取込み画面・送信コードは無い。再判定ボタンと送付記録ボタンだけがある。
    assert "納品の入力は画面では行いません" in GAP_JS
    assert "Jenkinsに渡した（記録）" in GAP_JS
    assert "再判定する" in GAP_JS
    assert "artifact_sha256" not in GAP_JS
    assert "failure_logs" not in GAP_JS
    assert "innerHTML" not in GAP_JS
    routes = [str(getattr(r, "path", "")) for r in web.app.routes]
    # 2-D の3経路がある (納品の受付はAPIで受け、画面は状態表示と再判定のみ)。
    assert ("/api/projects/{project_id}/capability-gaps/proposals/{proposal_id}/submitted"
            in routes)
    assert ("/api/projects/{project_id}/capability-gaps/proposals/{proposal_id}/delivery"
            in routes)
    assert ("/api/projects/{project_id}/capability-gaps/proposals/{proposal_id}/verify"
            in routes)
    # 8本のまま (既存5本 + submitted/delivery/verify。順序は問わない)。
    gap_routes = sorted({p for p in routes if "capability-gaps" in p})
    assert sorted(gap_routes) == sorted([
        "/api/projects/{project_id}/capability-gaps",
        "/api/projects/{project_id}/capability-gaps/propose",
        "/api/projects/{project_id}/capability-gaps/proposals/{proposal_id}",
        "/api/projects/{project_id}/capability-gaps/proposals/{proposal_id}/export",
        "/api/projects/{project_id}/capability-gaps/proposals/{proposal_id}/review",
        "/api/projects/{project_id}/capability-gaps/proposals/{proposal_id}/submitted",
        "/api/projects/{project_id}/capability-gaps/proposals/{proposal_id}/delivery",
        "/api/projects/{project_id}/capability-gaps/proposals/{proposal_id}/verify",
    ]), gap_routes
    # export応答にファイル本文が無い・Jenkinsへ送信しない旨がある。
    import app.capability_gap as gap
    preflight = gap.capability_preflight(manager, pid)
    out = gap.generate_proposals(manager, pid, preflight, actor="架空担当")
    target = out["proposals"][0]["proposal_id"]
    gap.set_proposal_status(manager, pid, target, "reviewed",
                            actor="架空担当", reason="架空確認")
    exported = client.post(
        f"/api/projects/{pid}/capability-gaps/proposals/{target}/export",
        json={"actor": "架空担当"})
    assert exported.status_code == 200
    body = exported.json()
    blob = json.dumps(body, ensure_ascii=False)
    assert "package_rel" in body and "instruction_sha256" in body
    assert "承認前のローカル保存" in blob
    assert "送信" in blob
    for key in ("instruction", "REQUEST", "required_operation", "content"):
        assert key not in body, key


def test_gap_overview_card_separate_and_next_action(tmp_path):
    # PJ概要に不足機能カードが別カードで存在し、次操作が追加で「機能提案を確認」になる。
    assert re.search(r"add\(gapBlock,'h3','不足機能'\)", WR_JS)
    assert "workflowOverviewGap" in WR_JS
    assert "機能提案を確認" in WR_JS
    # 既存の次操作を削除していない (既存ID・既存ラベルが残る)。
    assert "workflowOverviewNext" in WR_JS
    assert "workflowOverviewStop" in WR_JS
    assert "workflowOverviewOwner" in WR_JS
    assert "workflowOverviewEvidence" in WR_JS
    assert "止まっている理由" in WR_JS
    # 既存のOVERVIEW_FIELDS・次操作の仕組みが残る。
    assert "OVERVIEW_FIELDS" in WR_JS
    assert "formatNextOperation" in WR_JS
    # PJ切替の epoch/ticket の流儀がある。
    assert "ticket" in WR_JS and "epoch" in WR_JS
    assert "ticket" in GAP_JS and "epoch" in GAP_JS
    # 論理削除・存在しないPJでは描画せずエラーを表示する。
    assert "プロジェクトが選択されていません" in GAP_JS
    _ = tmp_path
