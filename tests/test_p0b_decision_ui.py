"""P0-B判断待ちUIの受入試験。疑似PJ・実関数・外部通信なし。特定案件の語彙なし。"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace


class Manager(SimpleNamespace):
    pass


def _snapshot_and_contract():
    tasks = []
    criteria = []
    for i in range(6):
        key = "SC00" if i == 0 else f"SC{i:02d}"
        cids = [] if i == 0 else [f"SC{i:02d}"]
        tasks.append({
            "id": f"t{i}", "task_key": key, "title": key, "description": "pseudo step",
            "acceptance_criteria": json.dumps({
                "schema": "local-cowork-plan/v1", "criterion_ids": cids,
                "role": "preparation" if key == "SC00" else "execution",
                "internal_preparation_gate_ids": ["PREP00"] if key == "SC00" else [],
            }, ensure_ascii=False),
            "depends_on": [],
        })
        if i >= 1:
            criteria.append({"criterion_id": f"SC{i:02d}",
                             "exec_task_keys": [f"SC{i:02d}"],
                             "verify_task_keys": [f"SC{i:02d}"]})
    return {"tasks": tasks}, {"criteria": criteria, "content_hash": "hash-p0b-ui"}


def _manager(tmp_path):
    from app.memory.short_term import ShortTermMemory
    from app.workspace_files import WorkspaceSandbox
    from app.structured_planning import compile_plan, compile_task

    mem = ShortTermMemory(tmp_path / "memory" / "conversations.db")
    pid = mem.create_project("p0b-decision-ui")["id"]
    mem.save_mission(pid, "pseudo goal\n1. pseudo item",
                     "pseudo goal\n1. pseudo item", "", True, ["chatgpt"])
    ws = WorkspaceSandbox(tmp_path / "workspace")
    mgr = Manager(memory=mem, workspace=ws, planning_projects=set(), llm=None,
                  workers={}, _sync_memos=lambda pid: None,
                  provider_statuses=lambda: [{"id": "chatgpt", "configured": True}],
                  plan_review_runner=None,
                  _outbound_guard=lambda pid, purpose, text, providers=(): {"ok": True, "codes": []})
    criteria = ["pseudo item"]
    task = compile_task(1, criteria[0], {"title": "pseudo doc",
        "scope": "pseudo整理", "headings": ["目的", "実施内容"],
        "depends_on": []}, [])
    compiled = compile_plan(criteria, [task], goal=mem.get_mission(pid)["goal"])
    mem.replace_plan(pid, "p0b ui pseudo plan", compiled["tasks"])
    from app.goal_contract import activate as _activate
    _activate(mgr, pid)
    return mgr, pid


def test_no_candidate_preview_and_confirm_roundtrip(tmp_path):
    """候補なし指摘の preview(decision_option)→confirm が保存され、古いハッシュは409。"""
    import app.plan_review_loop as loop

    snap, contract = _snapshot_and_contract()
    parsed = [{"provider": "chatgpt", "status": "fail", "issues": [
        {"step": "99", "unmet_goal": "missing step pseudo",
         "reason": "no such step pseudo", "remedy": "review pseudo", "severity": "major"},
    ]}]
    table = loop.build_issue_bindings(
        parsed, contract, snap, plan_signature="sig-ui",
        goal_contract_hash="hash-p0b-ui", packet_hash="pkt-ui")
    binding = table["bindings"][0]
    assert binding["candidate_task_keys"] == []
    assert binding["candidate_criterion_ids"] == []
    preview = loop.preview_binding_selection(binding, decision_option="fix_public_packet")
    assert preview["decision_option"] == "fix_public_packet"
    assert preview["selected_task_key"] == "" and preview["selected_criterion_id"] == ""
    assert "判断の記録のみ" in preview["diff"]
    assert "検証合格" not in preview["after"] or "ではない" in preview["note"]
    # 任意工程の割付は拒否する。
    try:
        loop.preview_binding_selection(binding, task_key="SC01")
    except ValueError as exc:
        assert "候補がない指摘" in str(exc)
    else:
        raise AssertionError("no-candidate must not accept task allocation")
    try:
        loop.preview_binding_selection(binding, task_key="SC01", criterion_id="SC01",
                                       decision_option="fix_public_packet")
    except ValueError as exc:
        assert "割り付けできません" in str(exc)
    else:
        raise AssertionError("no-candidate must not accept task allocation with decision")
    try:
        loop.preview_binding_selection(binding, decision_option="unknown-option")
    except ValueError as exc:
        assert "判断の選択肢" in str(exc)
    else:
        raise AssertionError("unknown decision option must be rejected")
    run = {"id": "run-ui", "project_id": "pid-ui", "plan_signature": "sig-ui",
           "goal_contract_hash": "hash-p0b-ui", "packet_hash": "pkt-ui",
           "issue_bindings": table["bindings"], "manual_bindings": []}
    wrapped = loop.binding_selection_preview_for_run(
        run, binding["issue_id"], decision_option="reask_evaluator")
    assert wrapped["decision_option"] == "reask_evaluator"
    assert wrapped["external_calls"] == 0
    assert wrapped["requires_preview_confirmation"] is True
    # 画面外からの呼び出しでも、候補なし判断に理由が必要。
    try:
        loop.confirm_binding_candidate(
            run, binding["issue_id"], actor="human-a", reason="  ",
            decision_option="unsupported_or_missing_capability",
            plan_signature="sig-ui", goal_contract_hash="hash-p0b-ui",
            packet_hash="pkt-ui", confirmed_preview=True)
    except ValueError as exc:
        assert "理由" in str(exc)
    else:
        raise AssertionError("no-candidate decision requires a reason")
    assert binding["selection_status"] != "confirmed"
    entry = loop.confirm_binding_candidate(
        run, binding["issue_id"], actor="human-a", reason="再確認が必要なため",
        decision_option="unsupported_or_missing_capability",
        plan_signature="sig-ui", goal_contract_hash="hash-p0b-ui",
        packet_hash="pkt-ui", confirmed_preview=True)
    assert entry["decision_option"] == "unsupported_or_missing_capability"
    assert entry["task_key"] == "" and entry["criterion_id"] == ""
    assert entry["reason"] == "再確認が必要なため"
    assert binding["selection_status"] == "confirmed"
    # 古い計画版での確定は409相当で拒否する。
    binding2 = dict(binding)
    binding2["selection_status"] = "unselected"
    run2 = {"issue_bindings": [binding2], "manual_bindings": []}
    try:
        loop.confirm_binding_candidate(
            run2, binding2["issue_id"], actor="human-a", reason="r",
            decision_option="fix_public_packet", plan_signature="other-sig",
            goal_contract_hash="hash-p0b-ui", packet_hash="pkt-ui",
            confirmed_preview=True)
    except ValueError as exc:
        assert "409" in str(exc)
    else:
        raise AssertionError("stale signature must be rejected")


def test_awaiting_view_and_js_static_contract():
    """取得API相当の awaiting_decision と JS の静的契約を確認する。"""
    import app.plan_review_loop as loop

    snap, contract = _snapshot_and_contract()
    parsed = [{"provider": "chatgpt", "status": "fail", "issues": [
        {"step": "2", "unmet_goal": "unique one pseudo",
         "reason": "single target is clear pseudo", "remedy": "add detail pseudo",
         "severity": "major"},
        {"step": "99", "unmet_goal": "missing step pseudo",
         "reason": "no such step pseudo", "remedy": "review pseudo", "severity": "major"},
    ]}]
    table = loop.build_issue_bindings(
        parsed, contract, snap, plan_signature="sig-ui2",
        goal_contract_hash="hash-p0b-ui", packet_hash="pkt-ui2")
    unconfirmed = loop.unconfirmed_issue_ids(table["bindings"], [])
    view = loop.awaiting_decision_view({}, table["bindings"], unconfirmed)
    assert view["status"] == "awaiting_decision"
    assert view["unconfirmed_count"] == len(unconfirmed)
    assert view["destination"]["note_ja"]
    no_cand = [c for c in view["candidates"]
               if not c["candidate_task_keys"] and not c["candidate_criterion_ids"]]
    assert no_cand and no_cand[0]["no_candidate_options"]
    assert [x["id"] for x in no_cand[0]["no_candidate_options"]] == [
        "reask_evaluator", "fix_public_packet", "unsupported_or_missing_capability"]
    js = Path("app/static/plan_review_loop.js").read_text(encoding="utf-8")
    assert "innerHTML" not in js
    assert "createElement" in js and "textContent" in js
    for key in ("awaiting_decision", "no_candidate_options", "decision_option",
                "binding-preview", "binding-confirm", "判断待ち", "確定済み",
                "判断の記録のみ", "理由（必須）"):
        assert key in js, key
    html = Path("app/static/index.html").read_text(encoding="utf-8")
    assert "plan_review_loop.js?v=20261021-p0b-decision-ui" in html


def test_full_run_saves_binding_and_records_no_candidate_decision(tmp_path, monkeypatch):
    """疑似外部AI→判断待ち保存→Web層の preview/confirm まで外部通信なしで通す。"""
    import app.external_ai as ext
    import app.plan_review_loop as loop
    from app.goal_review import ReviewStore

    mgr, pid = _manager(tmp_path)
    calls = []

    async def fake_runner(_packet, providers):
        calls.append(list(providers))
        return [{"id": "chatgpt", "ok": True, "review": json.dumps({
            "verdict": "fail",
            "issues": [{"severity": "blocking", "step": "99",
                        "unmet_goal": "架空の計画全体が不明確",
                        "reason": "存在しない工程番号を参照",
                        "remedy": "対象を確認する"}]})}]

    mgr.plan_review_runner = fake_runner
    monkeypatch.setattr(ext, "call_provider_with_metadata",
                        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no external")))
    summary = "架空資料を1件作成し、達成条件SC01と確認記録を照合する公開可能な計画です。"
    preview = loop.preview_run(mgr, pid, ["chatgpt"], explicit_summary=summary)
    started = loop.start_run(mgr, pid, "human-a", ["chatgpt"], summary,
                             "idem-p0b-ui", expected_packet_hash=preview["packet_hash"])
    result = asyncio.new_event_loop().run_until_complete(loop.run_loop(mgr, pid, started["id"]))
    assert calls == [["chatgpt"]]
    assert result["stop_kind"] == "human_required"
    decision = result["awaiting_decision"]
    assert decision["unconfirmed_count"] == 1
    issue_id = decision["unconfirmed_issue_ids"][0]
    assert decision["candidates"][0]["no_candidate_options"]
    signature = result["plan_signature"]
    stored = ReviewStore(mgr.memory.path).get(pid, "binding_snapshot", signature)
    assert stored and stored["snapshot"]["bindings"]
    # Web層の preview/confirm 相当を関数経由で検証する(外部通信なし)。
    import app.web as web

    run_row = loop.get_run(mgr, pid, started["id"])
    wrapped = loop.binding_selection_preview_for_run(
        run_row, issue_id, decision_option="fix_public_packet")
    assert wrapped["decision_option"] == "fix_public_packet"
    assert wrapped["external_calls"] == 0
    # 確定前に保存済みレビューから対応付け直し、スナップショットを現行化する(外部呼出0)。
    calls_before = len(calls)
    remapped = asyncio.new_event_loop().run_until_complete(loop.remap_run(
        mgr, pid, started["id"], actor="human-a", idempotency_key="remap-ui-1"))
    assert len(calls) == calls_before
    assert remapped["awaiting_decision"]["unconfirmed_issue_ids"] == [issue_id]
    entry = asyncio.new_event_loop().run_until_complete(loop.confirm_binding(
        mgr, pid, started["id"], issue_id, actor="human-a",
        reason="公開packetの目的を修正するため", decision_option="fix_public_packet",
        confirmed_preview=True))
    assert entry["decision_option"] == "fix_public_packet"
    assert entry["reason"] == "公開packetの目的を修正するため"
    saved = ReviewStore(mgr.memory.path).get(pid, "binding_snapshot", signature)["snapshot"]
    assert saved["review_fingerprint"]
    confirms = [c for c in saved["confirmed"]
                if c.get("issue_id") == issue_id
                and c.get("decision_option") == "fix_public_packet"
                and c.get("reason") == "公開packetの目的を修正するため"]
    assert confirms
    public = loop.public_view(loop.get_run(mgr, pid, started["id"]))
    assert public["awaiting_decision"]["unconfirmed_issue_ids"] == [issue_id]
    assert web.app is not None
