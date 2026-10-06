"""P1-C acceptance: 計画の意味検証と修復ループ。

実在の関数・実在のデータ形で検証する。偽関数の注入だけでは通さない。
外部AI呼び出しだけは既存の流儀でスタブする。自己申告の合格値は信用しない。
"""
import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.memory.short_term import ShortTermMemory
from app.structured_planning import compile_task
from app.goal_review import ReviewStore, plan_snapshot


class Manager(SimpleNamespace):
    pass


def _manager(tmp_path):
    mem = ShortTermMemory(tmp_path / "memory" / "conversations.db")
    pid = mem.create_project("p1c")["id"]
    mem.save_mission(
        pid,
        "販売案件の目標\n1. 匿名リードの獲得\n2. 匿名案件の検証",
        "販売案件の目標\n1. 匿名リードの獲得\n2. 匿名案件の検証",
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

    criteria = ["匿名リードの獲得", "匿名案件の検証"]
    t1 = compile_task(
        1, criteria[0],
        {"title": "匿名施策", "scope": "匿名リードの獲得手順を整理する",
         "headings": ["現状確認", "手順"], "depends_on": []}, [],
    )
    t2 = compile_task(
        2, criteria[1],
        {"title": "匿名検証", "scope": "匿名案件の検証手順を整理する",
         "headings": ["現状確認", "検証"], "depends_on": ["SC01"]}, [],
    )
    compiled = compile_plan(criteria, [t1, t2], goal=mem.get_mission(pid)["goal"])
    mem.replace_plan(pid, "p1c plan", compiled["tasks"])
    return mem.get_mission(pid)


def _contract(mgr, pid):
    from app.goal_contract import activate

    return activate(mgr, pid)


def _pass_review(mgr, pid, signature):
    ReviewStore(mgr.memory.path).put(pid, "plan", signature, {
        "status": "passed", "reviews": [{"provider": "chatgpt", "status": "pass", "issues": []}],
        "packet": {}, "success_count": 1, "required_count": 1,
    })


def test_repair_loop_order_binding_and_no_skip(tmp_path):
    import app.plan_repair_loop as loop

    mgr, mem, pid = _manager(tmp_path)
    _plan(mgr, mem, pid)
    contract = _contract(mgr, pid)
    _pass_review(mgr, pid, plan_snapshot(mgr, pid)[1])
    run = loop.start(mgr, pid, "human-a")
    assert run["state"] == "proposed"
    assert run["plan_signature"] == plan_snapshot(mgr, pid)[1]
    assert run["goal_contract_hash"] == contract["content_hash"]
    assert run["issue_ids"] == []
    # 順序飛ばし不可
    with pytest.raises(ValueError, match="invalid repair transition"):
        asyncio.new_event_loop().run_until_complete(
            loop.advance(mgr, pid, run["id"], "structure_checked", "human-a", {}))
    row = asyncio.new_event_loop().run_until_complete(
        loop.advance(mgr, pid, run["id"], "patched", "human-a", {}))
    assert row["state"] == "patched"
    # 計画署名が変わったら旧ランは進めない(同一署名固定)
    mem.add_mission_instruction(pid, "追加の確認条件")
    with pytest.raises(ValueError, match="計画署名が変わりました|GoalContract"):
        asyncio.new_event_loop().run_until_complete(
            loop.advance(mgr, pid, run["id"], "structure_checked", "human-a", {}))


def test_repair_loop_binds_issues_and_blocks_business_fact_without_answer(tmp_path):
    import app.plan_repair_loop as loop

    mgr, mem, pid = _manager(tmp_path)
    _plan(mgr, mem, pid)
    _contract(mgr, pid)
    signature = plan_snapshot(mgr, pid)[1]
    issues = [
        {"id": "i-safe", "criterion": "SC01", "text": "公開前に承認し、送信前の検証順序を明記する"},
        {"id": "i-fact", "criterion": "SC01", "text": "リードの有効条件を決定する"},
        {"id": "i-dev", "criterion": "SC99", "text": "外部API連携を新規実装する"},
    ]
    run = loop.start(mgr, pid, "human-a", issues)
    by_id = {c["issue_id"]: c for c in run["candidates"]}
    assert by_id["i-safe"]["classification"] == "safe_plan_patch"
    assert by_id["i-safe"]["criterion_id"] == "SC01" and by_id["i-safe"]["task_key"] == "SC01"
    assert by_id["i-fact"]["classification"] == "business_fact" and by_id["i-fact"]["question"]
    assert by_id["i-dev"]["classification"] == "development_required"

    async def _review_plan(manager, _pid, _sig, _summary, _safe):
        _pass_review(manager, _pid, _sig)
        return {"status": "passed", "success_count": 1, "required_count": 1}

    import app.plan_repair_loop as loop_module

    real_review = loop_module.__dict__["_do_external_reviewed"]

    async def _fake_external(manager, project_id, runrow, payload, actor):
        ReviewStore(manager.memory.path).put(project_id, "plan", plan_snapshot(manager, project_id)[1], {
            "status": "passed", "reviews": [{"provider": "chatgpt", "status": "pass", "issues": []}],
            "packet": {}, "success_count": 1, "required_count": 1})
        runrow["external_calls"] = int(runrow.get("external_calls") or 0) + 1
        evidence = dict(runrow.get("evidence") or {})
        evidence["external_reviewed"] = {
            "actor": actor, "signature": plan_snapshot(manager, project_id)[1],
            "status": "passed", "outcome": "content_pass",
            "success_count": 1, "required_count": 1, "public_summary_chars": 42,
            "sent_private_originals": False, "connection_error": False,
        }
        runrow["evidence"] = evidence
        return runrow

    try:
        loop_module._do_external_reviewed = _fake_external
        # 承認なしでは patched へ進める(回答だけ記録)が、passed にはできない
        row = asyncio.new_event_loop().run_until_complete(
            loop.advance(mgr, pid, run["id"], "patched", "human-a", {}))
        assert row["state"] == "patched"
        row = asyncio.new_event_loop().run_until_complete(
            loop.advance(mgr, pid, run["id"], "structure_checked", "human-a", {}))
        assert row["state"] == "structure_checked"
        row = asyncio.new_event_loop().run_until_complete(
            loop.advance(mgr, pid, run["id"], "coverage_checked", "human-a", {}))
        assert row["state"] == "coverage_checked"
        row = asyncio.new_event_loop().run_until_complete(
            loop.advance(mgr, pid, run["id"], "external_reviewed", "human-a",
                         {"public_summary": "公開用の目標・達成条件・工程の要約です。" * 3,
                          "safe_to_send": True}))
        assert row["state"] == "external_reviewed"
        row = asyncio.new_event_loop().run_until_complete(
            loop.advance(mgr, pid, run["id"], "rediff_evaluated", "human-a", {}))
        assert row["state"] == "rediff_evaluated"
        # 未解決(要実装・未回答)があるため passed 不可
        with pytest.raises(ValueError, match="REPAIR_NOT_PASSED"):
            asyncio.new_event_loop().run_until_complete(
                loop.advance(mgr, pid, run["id"], "passed", "human-a", {}))
    finally:
        loop_module._do_external_reviewed = real_review
    assert signature == plan_snapshot(mgr, pid)[1]


def test_reduced_issues_only_does_not_pass(tmp_path):
    """「指摘が減った」だけでは passed にならない。"""
    import app.plan_repair_loop as loop

    mgr, mem, pid = _manager(tmp_path)
    _plan(mgr, mem, pid)
    _contract(mgr, pid)
    run = loop.start(mgr, pid, "human-a", [
        {"id": "only-one", "criterion": "SC01", "text": "公開前に承認し、送信前の検証順序を明記する"},
        {"id": "second-one", "criterion": "SC01", "text": "成果物を検証後に公開する依存順序を追加する"},
    ])
    # 2件→1件に減ったが1件が残っている状態を、実在の feedback 保存で再現する。
    remaining = {"id": "only-one", "provider": "chatgpt", "text": "公開前に承認し、送信前の検証順序を明記する",
                 "criterion": "SC01"}
    ReviewStore(mgr.memory.path).put(pid, "feedback", run["plan_signature"], {"issues": [remaining]})
    evidence = dict((loop.get_run(mgr, pid, run["id"]) or {}).get("evidence") or {})
    evidence["external_reviewed"] = {
        "actor": "human-a", "signature": run["plan_signature"], "status": "passed",
        "outcome": "content_pass", "success_count": 1, "required_count": 1,
        "public_summary_chars": 42, "sent_private_originals": False, "connection_error": False,
    }
    stored = loop.get_run(mgr, pid, run["id"])
    stored["evidence"] = evidence
    ReviewStore(mgr.memory.path).put(pid, "repair_run", run["id"], stored)
    ReviewStore(mgr.memory.path).put(pid, "plan", run["plan_signature"], {
        "status": "passed", "reviews": [{"provider": "chatgpt", "status": "pass", "issues": []}],
        "packet": {}, "success_count": 1, "required_count": 1})
    reasons = loop._evaluate_pass(mgr, pid, loop.get_run(mgr, pid, run["id"]))
    assert reasons, "実検査(構造/被覆/外部/未解決/質問/完走)のいずれかが残ること"
    assert any("UNRESOLVED" in r for r in reasons), reasons


def test_self_reported_pass_is_not_trusted_and_limits_block(tmp_path, monkeypatch):
    import app.plan_repair_loop as loop_module

    mgr, mem, pid = _manager(tmp_path)
    _plan(mgr, mem, pid)
    _contract(mgr, pid)
    run = loop_module.start(mgr, pid, "human-a", [
        {"id": "self-one", "criterion": "SC01", "text": "公開前に承認し、送信前の検証順序を明記する"},
    ])
    # 自己申告の合格値(passed:true等)を直接書いても信用しない:
    # 実在の feedback に未解決1件を残し、証拠だけ合格と偽装しても不可にする。
    ReviewStore(mgr.memory.path).put(pid, "feedback", run["plan_signature"], {"issues": [
        {"id": "self-one", "provider": "chatgpt", "text": "公開前に承認し、送信前の検証順序を明記する",
         "criterion": "SC01"},
    ]})
    stored = loop_module.get_run(mgr, pid, run["id"])
    stored["evidence"] = {"external_reviewed": {
        "actor": "attacker", "signature": run["plan_signature"], "status": "passed",
        "outcome": "content_pass", "success_count": 99, "required_count": 1,
        "public_summary_chars": 42, "sent_private_originals": False, "connection_error": False}}
    ReviewStore(mgr.memory.path).put(pid, "repair_run", run["id"], stored)
    ReviewStore(mgr.memory.path).put(pid, "plan", run["plan_signature"], {
        "status": "passed", "reviews": [], "packet": {}})
    reasons = loop_module._evaluate_pass(mgr, pid, loop_module.get_run(mgr, pid, run["id"]))
    assert reasons, "自己申告だけでは合格しない(サーバー側再検査が残る)"

    # 上限超過で blocked。外部呼び出し回数の上限を1に絞って2回目を拒否する
    monkeypatch.setattr(loop_module, "MAX_EXTERNAL_CALLS", 1)
    stored = loop_module.get_run(mgr, pid, run["id"])
    stored["external_calls"] = 1
    ReviewStore(mgr.memory.path).put(pid, "repair_run", run["id"], stored)

    async def _boom(*args, **kwargs):
        raise AssertionError("上限超過で外部へ送らない")

    # advance 経路で上限を踏む: patched まで進めてから external_reviewed を要求する
    run2 = loop_module.start(mgr, pid, "human-a", [])
    asyncio.new_event_loop().run_until_complete(
        loop_module.advance(mgr, pid, run2["id"], "patched", "human-a", {}))
    asyncio.new_event_loop().run_until_complete(
        loop_module.advance(mgr, pid, run2["id"], "structure_checked", "human-a", {}))
    asyncio.new_event_loop().run_until_complete(
        loop_module.advance(mgr, pid, run2["id"], "coverage_checked", "human-a", {}))
    stored2 = loop_module.get_run(mgr, pid, run2["id"])
    stored2["external_calls"] = 1
    ReviewStore(mgr.memory.path).put(pid, "repair_run", run2["id"], stored2)

    async def _fake_run_review_plan(*args, **kwargs):
        raise AssertionError("上限超過で外部へ送らない")

    monkeypatch.setattr("app.goal_review.review_plan", _fake_run_review_plan)
    with pytest.raises(ValueError, match="blocked"):
        asyncio.new_event_loop().run_until_complete(
            loop_module.advance(mgr, pid, run2["id"], "external_reviewed", "human-a",
                                {"public_summary": "公開用の目標・達成条件・工程の要約です。" * 3,
                                 "safe_to_send": True}))
    assert loop_module.get_run(mgr, pid, run2["id"])["state"] == "blocked"


def test_api_registers_runs_and_blocks_approval_until_passed(tmp_path, monkeypatch):
    import app.web as web_module
    import app.plan_repair_loop as loop_module

    mgr, mem, pid = _manager(tmp_path)
    _plan(mgr, mem, pid)
    _contract(mgr, pid)
    monkeypatch.setattr(web_module, "memory", mem)
    monkeypatch.setattr(web_module, "orchestrator", mgr)
    client = TestClient(web_module.app)

    resp = client.post(f"/api/projects/{pid}/plan/repair-runs/start", json={})
    assert resp.status_code == 422
    resp = client.post(f"/api/projects/{pid}/plan/repair-runs/start", json={"actor": "human-a"})
    assert resp.status_code == 200, resp.text
    rid = resp.json()["id"]
    resp = client.get(f"/api/projects/{pid}/plan/repair-runs")
    assert resp.status_code == 200 and any(x["id"] == rid for x in resp.json()["runs"])
    resp = client.post(f"/api/projects/{pid}/plan/repair-runs/{rid}/advance",
                       json={"next_state": "structure_checked", "actor": "human-a", "payload": {}})
    assert resp.status_code == 409
    # 修復ループ未完了の間は計画承認・実行開始を開かない
    with pytest.raises(ValueError, match="修復ループ未完了"):
        loop_module.approval_gate(mgr, pid)
    # pending_ledger の上位サマリにも露出する(読み取り専用・追加フィールド)
    from app.pending_ledger import build as ledger_build

    summary = ledger_build(mgr, pid)["summary"]
    assert summary["repair_blocking"] is True
    assert summary["repair_open_runs"] == [rid]
    from app.workflow_readiness import build_readiness
    ready = build_readiness(mgr, pid)
    assert ready["pending_summary"]["repair_blocking"] is True
    assert ready["allowed_actions"]["approve_plan"]["allowed"] is False
    assert ready["allowed_actions"]["start"]["allowed"] is False
    from app.next_action_controller import compute as nac_compute
    nac_view = nac_compute(mgr, pid)
    if nac_view["action_id"] in {"approve_plan", "start"}:
        assert nac_view["blocked"] is True
        assert "修復ループ" in (nac_view.get("reason") or "")
    from app.completion_replay import preview as replay_preview
    replay = replay_preview(mgr, pid)
    assert replay["repair_blocking"] is True
    assert replay["crossed_approval_boundary"] is False
