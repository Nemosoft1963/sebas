"""P0-B前半の受入試験。疑似PJ・実関数・外部通信なし。特定案件の語彙なし。"""
import asyncio
import json

import pytest

from app.experience_store import canonical, fingerprint


class Manager:
    def __init__(self, memory, workspace):
        self.memory = memory
        self.workspace = workspace
        self.planning_projects = set()
        self.llm = None
        self.workers = {}
        self._sync_memos = lambda pid: None
        self.provider_statuses = lambda: [{"id": "chatgpt", "configured": True}]
        self.plan_review_runner = None
        self._outbound_guard = lambda pid, purpose, text, providers=(): {"ok": True, "codes": []}


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
    return {"tasks": tasks}, {"criteria": criteria, "content_hash": "hash-p0b"}


def _manager(tmp_path):
    from app.memory.short_term import ShortTermMemory
    from app.workspace_files import WorkspaceSandbox
    from app.structured_planning import compile_plan, compile_task

    mem = ShortTermMemory(tmp_path / "memory" / "conversations.db")
    pid = mem.create_project("p0b-front-half")["id"]
    mem.save_mission(pid, "pseudo goal\n1. pseudo item",
                     "pseudo goal\n1. pseudo item", "", True, ["chatgpt"])
    ws = WorkspaceSandbox(tmp_path / "workspace")
    mgr = Manager(mem, ws)
    criteria = ["pseudo item"]
    task = compile_task(1, criteria[0], {"title": "pseudo doc",
        "scope": "pseudo整理", "headings": ["目的", "実施内容"],
        "depends_on": []}, [])
    compiled = compile_plan(criteria, [task], goal=mem.get_mission(pid)["goal"])
    mem.replace_plan(pid, "p0b pseudo plan", compiled["tasks"])
    from app.goal_contract import activate as _activate
    _activate(mgr, pid)
    return mgr, pid


def test_p0b_front_half_six_patterns_saved_and_awaiting(tmp_path):
    import app.plan_review_loop as loop

    snap, contract = _snapshot_and_contract()
    parsed = [{"provider": "chatgpt", "status": "fail", "issues": [
        {"step": "2", "unmet_goal": "unique one",
         "reason": "single target is clear", "remedy": "add detail",
         "severity": "major"},
        {"step": "1-2", "unmet_goal": "range pattern",
         "reason": "range refers to two steps", "remedy": "clarify",
         "severity": "major"},
        {"step": "1,3", "unmet_goal": "list pattern",
         "reason": "list refers to two steps", "remedy": "clarify",
         "severity": "major"},
        {"step": "99", "unmet_goal": "missing step",
         "reason": "no such step", "remedy": "review", "severity": "major"},
        {"step": "", "unmet_goal": "whole plan",
         "reason": "no step reference", "remedy": "review", "severity": "major"},
        {"step": "1", "unmet_goal": "preparation inspection",
         "reason": "original confirmation method and stop condition are missing",
         "remedy": "specify method", "severity": "blocking"},
    ]}]
    table = loop.build_issue_bindings(
        parsed, contract, snap, plan_signature="sig-p0b",
        goal_contract_hash="hash-p0b", packet_hash="pkt-p0b")
    assert len(table["bindings"]) == 6
    ids = [b["issue_id"] for b in table["bindings"]]
    assert len(set(ids)) == 6
    for binding in table["bindings"]:
        assert binding["issue_id"]
        assert binding["issue_text"]
        assert "step_raw" in binding
        assert "candidate_task_keys" in binding
        assert "candidate_criterion_ids" in binding
    zero = [b for b in table["bindings"]
            if not b["candidate_task_keys"] and not b["candidate_criterion_ids"]]
    assert zero
    assert [x["id"] for x in loop.NO_CANDIDATE_OPTIONS] == [
        "reask_evaluator", "fix_public_packet", "unsupported_or_missing_capability"]
    unconfirmed = loop.unconfirmed_issue_ids(table["bindings"], [])
    assert len(unconfirmed) >= 2
    view = loop.awaiting_decision_view({}, table["bindings"], unconfirmed)
    assert view["status"] == "awaiting_decision"
    assert view["destination"]["screen_id"] == "autoLoopCards"
    assert view["unconfirmed_count"] == len(unconfirmed)
    for item in view["candidates"]:
        assert "candidate_task_keys" in item and "basis" in item


def test_p0b_front_half_confirmed_kept_and_stale_rejected(tmp_path):
    import app.plan_feedback as fb
    import app.plan_review_loop as loop

    mgr, pid = _manager(tmp_path)
    _snap_live, signature = __import__("app.goal_review", fromlist=["plan_snapshot"]).plan_snapshot(mgr, pid)
    snap, _contract = _snapshot_and_contract()
    from app.goal_contract import preview as _preview
    contract = _preview(mgr, pid) or _contract
    parsed = [{"provider": "chatgpt", "status": "fail", "issues": [
        {"step": "2", "unmet_goal": "unique one",
         "reason": "single target is clear", "remedy": "add detail", "severity": "major"},
        {"step": "1-2", "unmet_goal": "range pattern",
         "reason": "range refers to two steps", "remedy": "clarify", "severity": "major"},
        {"step": "99", "unmet_goal": "missing step",
         "reason": "no such step", "remedy": "review", "severity": "major"},
    ]}]
    table = loop.build_issue_bindings(
        parsed, contract, snap, plan_signature=signature,
        goal_contract_hash=str(contract.get("content_hash") or ""),
        packet_hash="pkt-p0b")
    assert len(table["bindings"]) == 3
    resolved = [b for b in table["bindings"] if b["state"] == "resolved"]
    assert resolved
    saved = loop.save_binding_table_first(mgr, pid, signature, list(table["bindings"]))
    assert saved.get("saved") is True
    snapshot = fb.build_binding_snapshot(
        bindings=list(table["bindings"]), plan_signature=signature,
        goal_contract_hash=str(contract.get("content_hash") or ""),
        packet_hash="pkt-p0b", review_fingerprint="fp-p0b",
        confirmed=[{"issue_id": resolved[0]["issue_id"], "task_key": "SC02",
                    "criterion_id": "", "actor": "human-a", "reason": "確認した",
                    "plan_signature": signature,
                    "goal_contract_hash": str(contract.get("content_hash") or ""),
                    "packet_hash": "pkt-p0b", "review_fingerprint": "fp-p0b"}])
    fb.check_binding_snapshot_current(snapshot, plan_signature=signature,
                                      goal_contract_hash=str(contract.get("content_hash") or ""),
                                      packet_hash="pkt-p0b", review_fingerprint="fp-p0b")
    with pytest.raises(ValueError, match="計画版|目標契約|評価packet|評価結果"):
        fb.check_binding_snapshot_current(snapshot, plan_signature="other",
                                          goal_contract_hash=str(contract.get("content_hash") or ""),
                                          packet_hash="pkt-p0b", review_fingerprint="fp-p0b")


def test_p0b_front_half_old_stopped_run_limited_recovery(tmp_path, monkeypatch):
    import app.external_ai as ext
    import app.plan_review_loop as loop
    from app.goal_review import ReviewStore

    mgr, pid = _manager(tmp_path)
    from app.goal_contract import activate as _activate  # noqa: F401 (activate済み)

    async def _runner(text, providers):
        return [{"id": p, "ok": True, "review": json.dumps({
            "verdict": "fail",
            "issues": [{"severity": "blocking", "step": "2",
                        "unmet_goal": "unique one",
                        "reason": "single target is clear",
                        "remedy": "add detail"}]})} for p in providers]

    mgr.plan_review_runner = _runner
    monkeypatch.setattr(ext, "call_provider_with_metadata",
                        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no external")))
    summary = "架空手順の定義資料を作成し、達成条件SC01と検証記録を確認する公開用の具体的な計画説明です。"
    preview = loop.preview_run(mgr, pid, ["chatgpt"], explicit_summary=summary)
    started = loop.start_run(mgr, pid, "human-a", ["chatgpt"], summary, "idem-p0b-front",
                             expected_packet_hash=preview["packet_hash"])
    out = asyncio.new_event_loop().run_until_complete(loop.run_loop(mgr, pid, started["id"]))
    assert out["stop_kind"] in {"human_required", "stopped"}
    run = loop.get_run(mgr, pid, started["id"])
    run["state"] = "stopped"
    run["stop_kind"] = "human_required"
    loop._save(mgr, run)
    saved = loop.get_run(mgr, pid, started["id"])
    _mission, _snap, signature, _contract = loop._current(mgr, pid)
    rounds = saved.get("rounds") or []
    assert rounds
    ok = loop.p0b_stored_review_resume(mgr, pid, saved, signature=signature,
                                       parsed=rounds[0].get("reviews") or [])
    assert ok.get("ok") is True
    assert ok.get("external_calls") == 0
    assert ok.get("bindings")
    bad = loop.p0b_stored_review_resume(mgr, pid, saved, signature="other",
                                        parsed=rounds[0].get("reviews") or [])
    assert bad.get("ok") is False
    assert "新規" in str(bad.get("reason") or "") or "新規" in str(bad.get("action") or "")

    # 本番入口(remap_run)が限定回復を使い、API用public_viewまで判断情報を返す。
    saved["awaiting_decision"] = loop.awaiting_decision_view(
        saved, saved.get("issue_bindings") or [],
        loop.unconfirmed_issue_ids(saved.get("issue_bindings") or [], []))
    loop._save(mgr, saved)
    remapped = asyncio.new_event_loop().run_until_complete(
        loop.remap_run(mgr, pid, started["id"], actor="human-a",
                       idempotency_key="remap-p0b"))
    assert remapped["external_calls"] == out["external_calls"]
    assert remapped["awaiting_decision"]["destination"]["screen_id"] == "autoLoopCards"
    assert "unconfirmed_count" in remapped["awaiting_decision"]


def test_save_binding_table_first_preserves_existing_confirmation(tmp_path):
    import app.plan_feedback as fb
    import app.plan_review_loop as loop
    from app.goal_contract import preview as contract_preview
    from app.goal_review import ReviewStore, plan_snapshot

    mgr, pid = _manager(tmp_path)
    snapshot, signature = plan_snapshot(mgr, pid)
    contract = contract_preview(mgr, pid)
    bindings = [{"issue_id": "issue-a", "state": "ambiguous",
                 "target_type": "plan_level", "candidate_task_keys": [],
                 "candidate_criterion_ids": [], "steps": [], "basis": "none"}]
    store = ReviewStore(mgr.memory.path)
    plan = store.get(pid, "plan", signature) or {}
    packet_hash = fingerprint(plan.get("packet")) if plan.get("packet") else ""
    review_fp = fingerprint([signature, canonical(plan.get("reviews") or [])])
    confirmed = [{"issue_id": "issue-a", "task_key": "SC01", "actor": "human-a",
                  "plan_signature": signature,
                  "goal_contract_hash": str(contract.get("content_hash") or ""),
                  "packet_hash": packet_hash, "review_fingerprint": review_fp}]
    original = fb.build_binding_snapshot(
        bindings=bindings, plan_signature=signature,
        goal_contract_hash=str(contract.get("content_hash") or ""),
        packet_hash=packet_hash, review_fingerprint=review_fp, confirmed=confirmed)
    store.put(pid, "binding_snapshot", signature, {"snapshot": original})

    assert loop.save_binding_table_first(mgr, pid, signature, bindings)["saved"] is True
    saved = store.get(pid, "binding_snapshot", signature)["snapshot"]
    assert saved["confirmed"] == original["confirmed"]


def test_ambiguous_fake_review_reaches_saved_human_decision(tmp_path):
    """An ambiguous fake review must persist the binding table before stopping."""
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
    summary = "架空資料を1件作成し、達成条件SC01と確認記録を照合する公開可能な計画です。"
    preview = loop.preview_run(mgr, pid, ["chatgpt"], explicit_summary=summary)
    assert preview["can_start"] is True
    started = loop.start_run(mgr, pid, "human-a", ["chatgpt"], summary,
                             "idem-p0b-ambiguous", expected_packet_hash=preview["packet_hash"])
    result = asyncio.new_event_loop().run_until_complete(loop.run_loop(mgr, pid, started["id"]))
    assert calls == [["chatgpt"]]
    assert result["stop_kind"] == "human_required"
    decision = result["awaiting_decision"]
    assert decision["status"] == "awaiting_decision"
    assert decision["unconfirmed_count"] == 1
    assert decision["candidates"][0]["no_candidate_options"]
    signature = result["plan_signature"]
    stored = ReviewStore(mgr.memory.path).get(pid, "binding_snapshot", signature)
    assert stored and stored["snapshot"]["bindings"]
    assert stored["snapshot"]["bindings"][0]["issue_id"] == decision["unconfirmed_issue_ids"][0]
    assert result["binding_snapshot_save_failed"] is False