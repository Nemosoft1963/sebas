"""Stage 3-a acceptance: unconfirmed issues stop proposal as awaiting_decision.

Pseudo project, real functions, no external calls, no case-specific vocabulary.
6 issues: 1 confirmed, 2 ambiguous, 3 uniquely resolvable.
"""
import asyncio
import json

import pytest

from app.experience_store import fingerprint


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

    def _local_complete(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("awaiting_decision must stop before local LLM")


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
    return {"tasks": tasks}, {"criteria": criteria, "content_hash": "hash-3a"}


def _make_manager(tmp_path):
    from app.memory.short_term import ShortTermMemory
    from app.workspace_files import WorkspaceSandbox

    mem = ShortTermMemory(tmp_path / "memory" / "conversations.db")
    pid = mem.create_project("stage3a-pseudo")["id"]
    mem.save_mission(pid, "pseudo goal\n1. pseudo item one\n2. pseudo item two",
                     "pseudo goal\n1. pseudo item one\n2. pseudo item two",
                     "", True, ["chatgpt"])
    ws = WorkspaceSandbox(tmp_path / "workspace")
    mgr = Manager(mem, ws)
    # live計画を手作り6工程(snap)に寄せる。compile済み2工程のままでは
    # step番号の指す先が変わり、曖昧件数の再現がずれる。
    snap, _contract = _snapshot_and_contract()
    live_tasks = []
    for i, task in enumerate(snap["tasks"]):
        row = dict(task)
        row["id"] = f"live-t{i}"
        row["description"] = str(task.get("description") or "pseudo step")
        live_tasks.append(row)
    mem.replace_plan(pid, "stage3a pseudo plan", live_tasks)
    from app.goal_contract import activate as _activate
    _activate(mgr, pid)
    snapshot, contract = _snapshot_and_contract()
    return mgr, pid, snapshot, contract


def _issues_for(snap, contract):
    import app.plan_review_loop as loop

    parsed = [{"provider": "chatgpt", "status": "fail", "issues": [
        {"step": "1", "unmet_goal": "preparation inspection method is missing",
         "reason": "original confirmation method and stop condition are missing",
         "remedy": "specify inspection method and stop condition", "severity": "blocking"},
        {"step": "not-a-step", "unmet_goal": "unclear target description",
         "reason": "target cannot be identified", "remedy": "", "severity": "major"},
        {"step": "2,3", "unmet_goal": "steps two and three are unclear and contradictory",
         "reason": "mentions unknown target ZZ99 while steps point elsewhere",
         "remedy": "clarify the corresponding target", "severity": "major"},
        {"step": "2", "unmet_goal": "step two method", "reason": "step two detail is missing",
         "remedy": "add step two detail", "severity": "major"},
        {"step": "3", "unmet_goal": "step three method", "reason": "step three detail is missing",
         "remedy": "add step three detail", "severity": "major"},
        {"step": "4", "unmet_goal": "step four method", "reason": "step four detail is missing",
         "remedy": "add step four detail", "severity": "major"},
    ]}]
    table = loop.build_issue_bindings(parsed, contract, snap,
                                      plan_signature="sig-3a",
                                      goal_contract_hash="hash-3a",
                                      packet_hash="pkt-3a")
    assert len(table["bindings"]) == 6
    states = [b["state"] for b in table["bindings"]]
    assert states.count("ambiguous") + states.count("invalid_reference") >= 2
    by_id = {b["issue_id"]: b for b in table["bindings"]}
    return parsed, table, by_id


def test_unconfirmed_ids_exclude_confirmed_and_never_auto_confirm():
    import app.plan_review_loop as loop

    snap, contract = _snapshot_and_contract()
    _parsed, table, by_id = _issues_for(snap, contract)
    bindings = list(table["bindings"])
    confirmed_issue = next(b["issue_id"] for b in bindings
                           if b["state"] not in ("ambiguous", "invalid_reference"))
    unconfirmed = loop.unconfirmed_issue_ids(bindings, [confirmed_issue])
    assert confirmed_issue not in unconfirmed
    assert len(unconfirmed) >= 2
    # single candidate must not auto-confirm
    single = [b for b in bindings if b["issue_id"] in unconfirmed[:1]]
    assert loop.unconfirmed_issue_ids(single, []) == [unconfirmed[0]]


def test_propose_stops_awaiting_decision_with_candidates_and_next_action(tmp_path):
    import app.plan_feedback as fb
    from app.goal_review import ReviewStore

    mgr, pid, snap, contract = _make_manager(tmp_path)
    from app.goal_review import plan_snapshot
    from app.goal_contract import preview as _contract_preview_live
    _snap_live, signature = plan_snapshot(mgr, pid)
    contract = _contract_preview_live(mgr, pid) or contract
    parsed, table, by_id = _issues_for(snap, contract)
    store = ReviewStore(mgr.memory.path)
    store.put(pid, "plan", signature, {"reviews": parsed, "packet": {"v": 1}})
    from app.plan_feedback import issues_for as _issues_for_live
    live = _issues_for_live(mgr, pid, signature)
    assert [x["id"] for x in live] == [b["issue_id"] for b in table["bindings"]]
    issues = live
    from app.experience_store import canonical as _canonical
    from app.experience_store import fingerprint as _fingerprint
    from app.goal_contract import preview as _contract_preview
    saved_review = store.get(pid, "plan", signature) or {}
    contract_live = _contract_preview(mgr, pid) or {}
    packet_live = saved_review.get("packet")
    store.put(pid, "binding_snapshot", signature, {
        "snapshot": fb.build_binding_snapshot(
            bindings=list(table["bindings"]),
            plan_signature=signature,
            goal_contract_hash=str(contract_live.get("content_hash") or ""),
            packet_hash=_fingerprint(packet_live) if packet_live else "",
            review_fingerprint=_fingerprint([signature, _canonical(saved_review.get("reviews") or [])]),
            confirmed=[{"issue_id": list(by_id)[0], "task_key": "SC00",
                        "criterion_id": "", "actor": "human",
                        "plan_signature": signature,
                        "goal_contract_hash": str(contract_live.get("content_hash") or ""),
                        "packet_hash": _fingerprint(packet_live) if packet_live else ""}]),
        "saved_at": 0.0})

    with pytest.raises(fb.AwaitingDecisionError) as excinfo:
        asyncio.new_event_loop().run_until_complete(
            fb.propose(mgr, pid, signature, _issues_override=issues))
    err = excinfo.value
    assert len(err.unconfirmed_issue_ids) >= 2
    assert err.candidates
    assert all(c["issue_id"] in err.unconfirmed_issue_ids for c in err.candidates)
    assert all("candidate_task_keys" in c and "basis" in c for c in err.candidates)
    assert err.next_action
    assert str(err).startswith("未確定の指摘")
    resp = err.to_response()
    assert resp["status"] == "awaiting_decision"
    assert resp["unconfirmed_count"] == len(err.unconfirmed_issue_ids)
    assert "confirmed_issue_ids" in resp


def test_all_resolved_coverage_keeps_same_issue_ids():
    from app.plan_coverage_matrix import build_coverage_matrix, coverage_gate_error

    issues = [
        {"id": "issue-a", "binding": {"state": "resolved",
                                      "candidate_task_keys": ["SC01"],
                                      "candidate_criterion_ids": ["SC01"],
                                      "steps": [2]}},
        {"id": "issue-b", "binding": {"state": "resolved",
                                      "candidate_task_keys": ["SC02"],
                                      "candidate_criterion_ids": ["SC02"],
                                      "steps": [3]}},
    ]
    actions = [
        {"issue_id": "issue-a", "disposition": "amend", "target": "SC01",
         "change": "x" * 24, "reason": "y" * 12},
        {"issue_id": "issue-b", "disposition": "amend", "target": "SC02",
         "change": "x" * 24, "reason": "y" * 12},
    ]
    changes = [{"target": "SC01", "before": "o", "after": "n"},
               {"target": "SC02", "before": "o", "after": "n"}]
    matrix = build_coverage_matrix(issues=issues, actions=actions, changes=changes,
                                   task_order=["SC00", "SC01", "SC02"])
    assert coverage_gate_error(matrix) is None
    assert [r["issue_id"] for r in matrix["rows"]] == ["issue-a", "issue-b"]


def test_all_confirmed_passes_gate_without_auto_confirm(tmp_path):
    import app.plan_feedback as fb
    from app.goal_review import ReviewStore, plan_snapshot

    mgr, pid, snap, contract = _make_manager(tmp_path)
    from app.goal_contract import preview as _contract_preview_live
    _snap_live, signature = plan_snapshot(mgr, pid)
    contract = _contract_preview_live(mgr, pid) or contract
    parsed, table, by_id = _issues_for(snap, contract)
    store = ReviewStore(mgr.memory.path)
    store.put(pid, "plan", signature, {"reviews": parsed, "packet": {"v": 1}})
    live = fb.issues_for(mgr, pid, signature)
    unconfirmed_before = [b["issue_id"] for b in table["bindings"]
                          if b["state"] in ("ambiguous", "invalid_reference")]
    assert len(unconfirmed_before) >= 2
    from app.experience_store import canonical as _canonical
    from app.experience_store import fingerprint as _fingerprint
    contract_live = _contract_preview_live(mgr, pid) or {}
    saved_review = store.get(pid, "plan", signature) or {}
    packet_live = saved_review.get("packet")
    packet_hash = _fingerprint(packet_live) if packet_live else ""
    goal_hash = str(contract_live.get("content_hash") or "")
    store.put(pid, "binding_snapshot", signature, {
        "snapshot": fb.build_binding_snapshot(
            bindings=list(table["bindings"]),
            plan_signature=signature,
            goal_contract_hash=goal_hash,
            packet_hash=packet_hash,
            review_fingerprint=_fingerprint([signature, _canonical(saved_review.get("reviews") or [])]),
            confirmed=[{"issue_id": issue_id, "task_key": "SC02",
                        "criterion_id": "", "actor": "human",
                        "plan_signature": signature,
                        "goal_contract_hash": goal_hash,
                        "packet_hash": packet_hash}
                       for issue_id in unconfirmed_before]),
        "saved_at": 0.0})
    # 全件確定後は判断待ちで止まらず通過する(修正案生成自体は従来どおり)。
    # 人確定分は再構成で曖昧に戻らない。
    assert fb.raise_if_unconfirmed(mgr, pid, signature, live, _snap_live) is None


def test_stale_confirmed_snapshot_is_rejected_not_passed(tmp_path):
    import app.plan_feedback as fb

    mgr, pid, _snap, _contract = _make_manager(tmp_path)
    from app.goal_review import plan_snapshot
    _snap_live, signature = plan_snapshot(mgr, pid)
    snap_obj = fb.build_binding_snapshot(bindings=[], plan_signature="old-signature",
                                         goal_contract_hash="hash-3a",
                                         packet_hash="pkt-3a",
                                         review_fingerprint="fp-3a", confirmed=[])
    with pytest.raises(ValueError, match="計画版が変わった"):
        fb.check_binding_snapshot_current(snap_obj, plan_signature=signature,
                                          goal_contract_hash="hash-3a",
                                          packet_hash="pkt-3a",
                                          review_fingerprint="fp-3a")


def test_api_returns_awaiting_decision_before_ollama_health(monkeypatch):
    """The documented decision gate must win even when the local LLM is offline."""
    import app.plan_feedback as fb
    import app.web as web
    from fastapi import HTTPException

    class OfflineLLM:
        async def health(self):
            raise AssertionError("health must not be called before awaiting_decision")

    monkeypatch.setattr(web, "require_project", lambda _pid: None)
    monkeypatch.setattr(web, "orchestrator", object())
    monkeypatch.setattr(web, "llm", OfflineLLM())
    monkeypatch.setattr(web, "engine_enabled", True)
    monkeypatch.setattr(fb, "current", lambda *_args: ({}, {"tasks": []}, None))
    monkeypatch.setattr(fb, "issues_for", lambda *_args: [{"id": "issue-1"}])

    def pending(*_args):
        raise fb.AwaitingDecisionError(
            unconfirmed_issue_ids=["issue-1"],
            candidates=[{"issue_id": "issue-1", "candidate_task_keys": ["SC00"]}],
            next_action="confirm the binding",
        )

    monkeypatch.setattr(fb, "raise_if_unconfirmed", pending)
    payload = web.PlanFeedbackPayload(signature="sig-1")
    with pytest.raises(HTTPException) as caught:
        asyncio.run(web.propose_plan_feedback("dummy-project", payload))
    assert caught.value.status_code == 409
    assert caught.value.detail["status"] == "awaiting_decision"
    assert caught.value.detail["unconfirmed_issue_ids"] == ["issue-1"]

