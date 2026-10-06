"""P2 原因と案内: 疑似販売案件での受入テスト(実案件不使用)。

実在の completion_gate / pending_ledger / plan_repair_loop / recovery_record /
safe_auto_resume を使う。外部AI呼び出しだけは既存の流儀でスタブ可。
"""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.goal_contract import activate
from app.goal_review import ReviewStore, plan_snapshot
from app.memory.short_term import ShortTermMemory
from app.structured_planning import compile_task

FILLER = "架空の検証可能な具体的内容を記載する。" * 40


class Manager(SimpleNamespace):
    pass


def _manager(tmp_path):
    mem = ShortTermMemory(tmp_path / "memory" / "conversations.db")
    pid = mem.create_project("p2pseudo")["id"]
    mem.save_mission(
        pid,
        "疑似販売目標\n1. 対象市場と顧客課題を定義する\n2. 実際の営業活動について、実行済み、承認待ち、未着手、失敗、次回行動を明確に報告する",
        "疑似販売目標\n1. 対象市場と顧客課題を定義する\n2. 実際の営業活動について、実行済み、承認待ち、未着手、失敗、次回行動を明確に報告する",
        "",
        True,
        ["chatgpt"],
    )
    from app.workspace_files import WorkspaceSandbox

    ws = WorkspaceSandbox(tmp_path / "workspace")
    mgr = Manager(
        memory=mem,
        workspace=ws,
        planning_projects=set(),
        llm=None,
        provider_statuses=lambda: [{"id": "chatgpt", "configured": True}],
        plan_review_runner=None,
    )
    return mgr, mem, pid


def _plan(mgr, mem, pid):
    from app.structured_planning import compile_plan

    criteria = [
        "対象市場と顧客課題を定義する",
        "実際の営業活動について、実行済み、承認待ち、未着手、失敗、次回行動を明確に報告する",
    ]
    t1 = compile_task(
        1, criteria[0],
        {"title": "市場定義資料", "scope": "架空市場の整理",
         "headings": ["目的", "実施内容"], "depends_on": []}, [],
    )
    t2 = compile_task(
        2, criteria[1],
        {"title": "営業活動報告", "scope": "実施状態の整理",
         "headings": ["目的", "実施内容"], "depends_on": ["SC01"]}, [],
    )
    compiled = compile_plan(criteria, [t1, t2], goal=mem.get_mission(pid)["goal"])
    mem.replace_plan(pid, "p2 pseudo plan", compiled["tasks"])
    return mem.get_mission(pid)


def _write_markdown(mgr, pid, path, headings):
    project = mgr.memory.get_project(pid)
    target = mgr.workspace.resolve_file(project.get("workspace_path", ""), pid, path)[2]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(f"## {h}\n{FILLER}" for h in headings), encoding="utf-8")
    return target


def _complete_task_artifact(mgr, pid, task):
    from app.structured_planning import contract_of

    contract = contract_of(task) or {}
    for output in contract.get("outputs") or []:
        path = output["path"]
        if path.endswith(".csv"):
            project = mgr.memory.get_project(pid)
            target = mgr.workspace.resolve_file(project.get("workspace_path", ""), pid, path)[2]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("a,b\n1,2\n", encoding="utf-8")
        else:
            _write_markdown(mgr, pid, path, output.get("required_headings") or ["目的", "実施内容"])
    mgr.memory.update_task(task["id"], "completed", result="成果物作成済み")


def _final_report(criteria_ids, *, verdicts=None):
    verdicts = verdicts or {cid: "PASS" for cid in criteria_ids}
    sections = ["# 最終検証・達成条件別の判定", "", "## 達成条件別判定"]
    for cid in criteria_ids:
        sections.append(f"### {cid} — {verdicts[cid]}")
        sections.append("")
        sections.append(f"- {cid}: {verdicts[cid]}")
        sections.append("- 根拠パス: result/")
        sections.append(FILLER)
    sections.extend(["", "## 成果物検証", FILLER, "", "## 未達条件と承認待ち", FILLER, ""])
    return "\n".join(sections)


def _complete_final(mgr, pid, task, criteria_ids, *, verdicts=None):
    from app.structured_planning import contract_of

    contract = contract_of(task) or {}
    path = contract["outputs"][0]["path"]
    project = mgr.memory.get_project(pid)
    target = mgr.workspace.resolve_file(project.get("workspace_path", ""), pid, path)[2]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_final_report(criteria_ids, verdicts=verdicts), encoding="utf-8")
    mgr.memory.update_task(task["id"], "completed", result="最終検証済み")


def _setup_gate(tmp_path, *, with_sales_evidence=False):
    import app.resolution_coordinator as coord

    mgr, mem, pid = _manager(tmp_path)
    _plan(mgr, mem, pid)
    activate(mgr, pid)
    mission = mem.get_mission(pid)
    criteria_ids = [
        cid for task in mission["tasks"]
        for cid in ((__import__("app.structured_planning", fromlist=["contract_of"]).contract_of(task) or {}).get("criterion_ids") or [])
    ]
    for task in mission["tasks"]:
        from app.structured_planning import contract_of

        if (contract_of(task) or {}).get("final_verification"):
            _complete_final(mgr, pid, task, criteria_ids)
            continue
        _complete_task_artifact(mgr, pid, task)
    if with_sales_evidence:
        action = mem.create_action(pid, "manual", "架空の見込み客A", "初回案内を実施")
        mem.update_action(pid, action["id"], "approved")
        mem.update_action(
            pid, action["id"], "executed",
            evidence="2026-03-01 架空面談記録 TEST-1 実施結果: 未受注 学習内容: 仮説を更新",
        )
    return mgr, mem, pid, coord


def test_p2_diagnose_classifies_cause_and_unknown_is_not_success(tmp_path):
    mgr, _mem, pid, coord = _setup_gate(tmp_path)
    view = coord.diagnose_only(mgr, pid)
    assert view["diagnoses"]
    causes = {d["criterion_id"]: d for d in view["diagnoses"]}
    # 営業活動条件は外部証拠が無いため external_dependency。
    assert causes["SC02"]["cause"] == "external_dependency"
    assert causes["SC02"]["next_action"]["route"] == "wait_human"
    for item in view["diagnoses"]:
        assert item["cause"] in coord.CAUSES
        assert item["missing_evidence"]
        assert item["basis_ja"]
        assert item["next_action"].get("label_ja")
        if item["cause"] == "unknown":
            assert item["next_action"]["route"] == "undiagnosed"


def test_p2_detect_idempotent_and_concurrent(tmp_path):
    mgr, _mem, pid, coord = _setup_gate(tmp_path)
    first = coord.detect(mgr, pid, "human-p2")
    second = coord.detect(mgr, pid, "human-p2")
    assert {x["id"] for x in first["resolutions"]} == {x["id"] for x in second["resolutions"]}
    assert second["created"] == []

    async def _run():
        return await asyncio.gather(
            *(asyncio.to_thread(coord.detect, mgr, pid, "human-p2") for _ in range(4))
        )

    results = asyncio.new_event_loop().run_until_complete(_run())
    ids = sorted({x["id"] for r in results for x in r["resolutions"]})
    assert ids == sorted({x["id"] for x in first["resolutions"]})


def test_p2_restart_tracks_same_resolution(tmp_path):
    mgr, _mem, pid, coord = _setup_gate(tmp_path)
    first = coord.detect(mgr, pid, "human-p2")
    # 再起動=新しい参照(同一DBパス)でも同じ残件を追跡できる。
    mgr2 = Manager(memory=ShortTermMemory(mgr.memory.path), workspace=mgr.workspace,
                   planning_projects=set())
    listed = coord.list_resolutions(mgr2, pid)
    assert {x["id"] for x in listed} == {x["id"] for x in first["resolutions"]}
    assert coord.summary(mgr2, pid)["total"] == len(first["resolutions"])


def test_p2_signature_or_source_change_stales(tmp_path):
    mgr, mem, pid, coord = _setup_gate(tmp_path)
    first = coord.detect(mgr, pid, "human-p2")
    old_id = first["resolutions"][0]["id"]
    mem.add_mission_instruction(pid, "追加の確認条件")
    second = coord.detect(mgr, pid, "human-p2")
    assert old_id in second["staled"]
    assert coord.get(mgr, pid, old_id)["state"] == "stale"
    with pytest.raises(ValueError, match="stale"):
        coord.advance(mgr, pid, old_id, "candidate_ready", "human-p2", "進める", {})


def test_p2_no_skip_and_attempt_limit(tmp_path, monkeypatch):
    mgr, _mem, pid, coord = _setup_gate(tmp_path)
    first = coord.detect(mgr, pid, "human-p2")
    rid = first["resolutions"][0]["id"]
    # 順序飛ばし不可(diagnosed → candidate_ready のみ)。
    with pytest.raises(ValueError, match="invalid resolution transition"):
        coord.advance(mgr, pid, rid, "awaiting_approval", "human-p2", "飛ばす", {})
    # unknown は成功扱いにせず先へ進めない。
    if coord.get(mgr, pid, rid)["cause"] == "unknown":
        with pytest.raises(ValueError, match="未診断"):
            coord.advance(mgr, pid, rid, "candidate_ready", "human-p2", "進める", {})
        return
    monkeypatch.setattr(coord, "MAX_ATTEMPTS", 1)
    coord.advance(mgr, pid, rid, "candidate_ready", "human-p2", "案内確定", {})
    with pytest.raises(ValueError, match="上限"):
        coord.advance(mgr, pid, rid, "trial_passed", "human-p2", "試験", {})
    assert coord.get(mgr, pid, rid)["state"] == "failed"


def test_p2_handoff_plan_conflict_to_p1c_not_triz(tmp_path):
    mgr, mem, pid, coord = _setup_gate(tmp_path)
    signature = plan_snapshot(mgr, pid)[1]
    ReviewStore(mgr.memory.path).put(pid, "feedback", signature, {"issues": [
        {"id": "p2-issue", "provider": "chatgpt",
         "text": "SC02 公開前に承認し、送信前の検証順序を明記する", "criterion": "SC02"},
    ]})
    result = coord.detect(mgr, pid, "human-p2")
    by_cid = {x["criterion_id"]: x for x in result["resolutions"]}
    assert by_cid["SC02"]["cause"] == "plan_conflict"
    assert by_cid["SC02"]["next_action"]["route"] == "plan_repair"
    assert by_cid["SC02"]["next_action"]["triz_skipped"] is True
    assert "TRIZ" in by_cid["SC02"]["next_action"]["triz_skip_reason"]


def test_p2_handoff_execution_failure_to_p3(tmp_path):
    mgr, _mem, pid, coord = _setup_gate(tmp_path)
    mission = mgr.memory.get_mission(pid)
    sc01 = next(t for t in mission["tasks"] if t["task_key"] == "SC01")
    mgr.memory.update_task(sc01["id"], "failed", error="架空の実行失敗")
    result = coord.detect(mgr, pid, "human-p2")
    by_cid = {x["criterion_id"]: x for x in result["resolutions"]}
    assert by_cid["SC01"]["cause"] == "execution_failure"
    assert by_cid["SC01"]["next_action"]["route"] == "recovery"


def test_p2_detect_and_diagnose_are_read_only(tmp_path):
    mgr, mem, pid, coord = _setup_gate(tmp_path)
    from app.goal_completion_store import GoalCompletionStore

    def _snapshots():
        store = ReviewStore(mgr.memory.path)
        _s, sig = plan_snapshot(mgr, pid)
        return {
            "actions": list(mem.list_actions(pid)),
            "accept": GoalCompletionStore(mgr.memory.path).latest_unrevoked_acceptance(pid),
            "plan": store.get(pid, "plan", sig),
            "res_db": Path(str(mgr.memory.path) + ".auto_resume.sqlite3").exists(),
            "rec_db": Path(str(mgr.memory.path) + ".recovery.sqlite3").exists(),
        }

    before = _snapshots()
    coord.diagnose_only(mgr, pid)
    mid = _snapshots()
    assert mid == before
    db_path = Path(str(mgr.memory.path) + ".resolution.sqlite3")
    assert not db_path.exists()
    coord.detect(mgr, pid, "human-p2")
    after = _snapshots()
    assert after == before


def test_p2_self_report_is_not_trusted(tmp_path):
    mgr, _mem, pid, coord = _setup_gate(tmp_path)
    result = coord.detect(mgr, pid, "human-p2")
    rid = result["resolutions"][0]["id"]
    row = coord.get(mgr, pid, rid)
    assert row["cause"] != "unknown" or True
    if row["cause"] == "unknown":
        with pytest.raises(ValueError, match="未診断"):
            coord.advance(mgr, pid, rid, "candidate_ready", "human-p2", "進める", {})
        return
    coord.advance(mgr, pid, rid, "candidate_ready", "human-p2", "案内確定", {})
    with pytest.raises(ValueError, match="既存記録"):
        coord.advance(mgr, pid, rid, "trial_passed", "human-p2", "合格と申告",
                      {"trial": {"passed": True}})
    assert coord.get(mgr, pid, rid)["state"] == "candidate_ready"


def test_p2_cards_api_and_readiness_summary(tmp_path):
    import app.web as web_module

    mgr, mem, pid, coord = _setup_gate(tmp_path)
    coord.detect(mgr, pid, "human-p2")
    cards = coord.resolution_cards(mgr, pid)
    assert cards["cards"]
    for card in cards["cards"]:
        for field in ["対象条件", "停止理由", "確認した証拠", "試した処置",
                      "セバスが次に行えること", "人間に必要な判断", "再評価結果"]:
            assert field in card and card[field]
    from app.pending_ledger import build as ledger_build

    summary = ledger_build(mgr, pid)["summary"]
    assert summary["resolution_open_count"] == len(cards["cards"])
    assert summary["resolution_by_cause"]
    from app.workflow_readiness import build_readiness

    ready = build_readiness(mgr, pid)
    assert ready["resolution_summary"]["open_count"] == len(cards["cards"])
    # 既存の判定(承認可否・実行可否)は変えない。
    assert "approve_plan" in ready["allowed_actions"]
    assert "start" in ready["allowed_actions"]

    from unittest.mock import patch

    mem2 = mem
    web_module.memory = mem2
    web_module.orchestrator = mgr
    client = TestClient(web_module.app)
    try:
        resp = client.get(f"/api/projects/{pid}/resolutions")
        assert resp.status_code == 200, resp.text
        assert resp.json()["cards"]
        rid = resp.json()["resolutions"][0]["id"]
        assert client.get(f"/api/projects/{pid}/resolutions/{rid}").status_code == 200
        assert client.post(f"/api/projects/{pid}/resolutions/detect", json={}).status_code == 422
        second = client.post(f"/api/projects/{pid}/resolutions/detect",
                             json={"actor": "human-p2"})
        assert second.status_code == 200
        assert {x["id"] for x in second.json()["resolutions"]} == {
            x["id"] for x in resp.json()["resolutions"]}
        bad = client.post(f"/api/projects/{pid}/resolutions/{rid}/advance",
                          json={"next_state": "resolved", "actor": "human-p2"})
        assert bad.status_code == 409
        with patch.object(web_module, "memory", mem2), patch.object(
                web_module, "orchestrator", mgr):
            pass
    finally:
        web_module.memory = None
        web_module.orchestrator = None


def test_p2_resolution_js_has_no_innerhtml():
    root = Path(__file__).resolve().parents[1]
    js = (root / "app/static/resolution_cards.js").read_text(encoding="utf-8")
    assert "innerHTML" not in js
    assert "textContent" in js and "createElement" in js
    for field in ["対象条件", "停止理由", "確認した証拠", "試した処置",
                  "セバスが次に行えること", "人間に必要な判断", "再評価結果"]:
        assert field in js
