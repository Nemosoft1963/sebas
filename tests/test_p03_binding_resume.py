"""P0-3 指摘の反映を再開可能にする。疑似PJ・実関数・外部通信なし。

- binding_decision_hash: 順序非依存・確定の変化で変化・本文/actor/時刻不変・確定0件で空文字。
- 確定後に同じ署名で1回だけ再開できる(従来は拒否)。同じ組の2回目は拒否。決定を変えると再び1回再開できる。
- 未確定/矛盾/決定なしは拒否。署名・指紋・packet・契約のいずれかが変われば拒否。
- 再開で external_calls が増えない。外部送信関数が呼ばれない(呼ばれたら失敗するスタブ)。
- 候補の検査失敗で再停止し、issues と bindings が保持される。
- 版変更再開の既存挙動は変えない(既存試験で担保)。
"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.structured_planning import compile_plan, compile_task


class Manager(SimpleNamespace):
    pass


def _make_manager(tmp_path):
    from app.memory.short_term import ShortTermMemory
    from app.workspace_files import WorkspaceSandbox

    mem = ShortTermMemory(Path(tmp_path) / "memory" / "conversations.db")
    pid = mem.create_project("p03-pseudo")["id"]
    mem.save_mission(pid, "疑似目標\n1. 架空手順Aを定義する",
                     "疑似目標\n1. 架空手順Aを定義する", "", True, ["chatgpt"])
    ws = WorkspaceSandbox(Path(tmp_path) / "workspace")
    mgr = Manager(memory=mem, workspace=ws, planning_projects=set(), llm=None,
                  workers={}, _sync_memos=lambda pid: None,
                  provider_statuses=lambda: [{"id": "chatgpt", "configured": True}],
                  plan_review_runner=None,
                  _outbound_guard=lambda pid, purpose, text, providers=(): {"ok": True, "codes": []})
    criteria = ["架空手順Aを定義する", "架空手順Bを確認する"]
    t1 = compile_task(1, criteria[0], {"title": "架空資料A", "scope": "架空Aの整理",
        "headings": ["目的", "実施内容"], "depends_on": []}, [])
    t2 = compile_task(2, criteria[1], {"title": "架空資料B", "scope": "架空Bの確認",
        "headings": ["目的", "実施内容"], "depends_on": []}, [])
    compiled = compile_plan(criteria, [t1, t2], goal=mem.get_mission(pid)["goal"])
    mem.replace_plan(pid, "p03 pseudo plan", compiled["tasks"])
    from app.goal_contract import activate as _activate
    _activate(mgr, pid)
    return mgr, pid


def _fail_review_runner(text, providers):
    raise AssertionError("外部AIを再送してはいけない")


def _stopped_run_with_two_ambiguous(mgr, pid):
    """run_loopでhuman_required停止ランを作る。指摘2件とも確定可能にする。"""
    import app.plan_review_loop as loop

    async def _runner(text, providers):
        return [{"id": p, "ok": True, "review": json.dumps({
            "verdict": "fail",
            "issues": [
                {"severity": "blocking", "step": "2, 3",
                 "unmet_goal": "検証方法が不足しています",
                 "reason": "対応手順が不足しています",
                 "remedy": "手順を追記する"},
                {"severity": "blocking", "step": "",
                 "unmet_goal": "検証記録が不足しています",
                 "reason": "指摘の対象は達成条件SC01 SC02です",
                 "remedy": "記録を追記する"},
                {"severity": "blocking", "step": "2",
                 "unmet_goal": "実施手順の確認記録が不足しています",
                 "reason": "SC01の実施手順の記録が不足しています",
                 "remedy": "SC01の記録を追記する"},
            ]})} for p in providers]

    mgr.plan_review_runner = _runner
    summary = "架空手順Aの定義資料を作成し、達成条件SC01と検証記録を確認する公開用の具体的な計画説明です。"
    preview = loop.preview_run(mgr, pid, ["chatgpt"], explicit_summary=summary)
    started = loop.start_run(mgr, pid, "human-a", ["chatgpt"], summary, "idem-p03-%s" % pid[:8],
                             expected_packet_hash=preview["packet_hash"])
    out = asyncio.new_event_loop().run_until_complete(loop.run_loop(mgr, pid, started["id"]))
    assert out["stop_kind"] == "human_required"
    run = loop.get_run(mgr, pid, started["id"])
    assert len(run["issue_bindings"]) == 3
    mgr.plan_review_runner = _fail_review_runner
    return run


def _confirm_all(mgr, pid, run_id, selections):
    import app.plan_review_loop as loop

    async def _confirm(issue_id, task_key, criterion_id):
        return await loop.confirm_binding(
            mgr, pid, run_id, issue_id, actor="human-a",
            task_key=task_key, criterion_id=criterion_id,
            reason="確認済み", confirmed_preview=True)

    for issue_id, task_key, criterion_id in selections:
        asyncio.new_event_loop().run_until_complete(
            _confirm(issue_id, task_key, criterion_id))


def _current_context(mgr, pid):
    import app.plan_review_loop as loop
    from app.plan_feedback import review_fingerprint_of as _fp

    _mission, _snapshot, signature, contract = loop._current(mgr, pid)
    return signature, contract


def test_binding_decision_hash_properties():
    import app.plan_review_loop as loop

    base = [
        {"issue_id": "i1", "selection_status": "confirmed",
         "selected_task_key": "SC01", "selected_criterion_id": "SC01",
         "reason": "自由記述A", "selection_actor": "alice", "at": 111.0,
         "issue_text": "本文A", "selected_extra": "x"},
        {"issue_id": "i2", "selection_status": "confirmed",
         "selected_task_key": "SC02", "selected_criterion_id": "",
         "reason": "自由記述B", "selection_actor": "bob", "at": 222.0,
         "issue_text": "本文B"},
        {"issue_id": "i3", "selection_status": "unselected",
         "selected_task_key": "SC01", "selected_criterion_id": "SC01"},
    ]
    h1 = loop.binding_decision_hash(base)
    assert h1 != ""
    # 順序非依存
    assert loop.binding_decision_hash(list(reversed(base))) == h1
    # 本文・actor・時刻・自由記述を変えても不変
    altered = [dict(b) for b in base]
    altered[0]["issue_text"] = "別の本文"
    altered[0]["selection_actor"] = "carol"
    altered[0]["at"] = 999.0
    altered[0]["reason"] = "別の理由"
    altered[1]["selected_extra"] = "y"
    assert loop.binding_decision_hash(altered) == h1
    # 確定が変わると変わる
    changed = [dict(b) for b in base]
    changed[1]["selected_task_key"] = "SC01"
    assert loop.binding_decision_hash(changed) != h1
    unconfirmed_changed = [dict(b) for b in base]
    unconfirmed_changed[2]["selected_task_key"] = "SC99"
    assert loop.binding_decision_hash(unconfirmed_changed) == h1
    # 確定0件で空文字
    assert loop.binding_decision_hash([]) == ""
    assert loop.binding_decision_hash(
        [{"issue_id": "i9", "selection_status": "unselected"}]) == ""
    assert loop.binding_decision_hash(None) == ""


def test_guard_keys_do_not_evict_older_decisions():
    import app.plan_review_loop as loop

    run = {"resume_guard_keys": []}
    for index in range(12):
        loop._append_resume_guard_key(run, "decision-" + str(index))
    assert "decision-0" in loop._resume_guard_keys(run)
    assert len(run["resume_guard_keys"]) == 12


def test_failure_code_vocabulary_and_guard_key_stability():
    import app.plan_review_loop as loop

    assert loop.normalize_resume_failure_code("human_required") == "human_required"
    assert loop.normalize_resume_failure_code("candidate_check_failed") == "candidate_check_failed"
    assert loop.normalize_resume_failure_code("stopped") == "stopped"
    assert loop.normalize_resume_failure_code("何か") == "unknown"
    assert loop.normalize_resume_failure_code("") == "unknown"
    assert loop.normalize_resume_failure_code(None) == "unknown"
    k1 = loop.binding_resume_guard_key(
        plan_signature="sig", review_fingerprint="fp",
        binding_decision_hash_value="dh", failure_code="human_required")
    assert k1 == loop.binding_resume_guard_key(
        plan_signature="sig", review_fingerprint="fp",
        binding_decision_hash_value="dh", failure_code="human_required")
    assert len(k1) == 64
    assert k1 != loop.binding_resume_guard_key(
        plan_signature="sig", review_fingerprint="fp",
        binding_decision_hash_value="dh2", failure_code="human_required")
    # 不明な失敗コードは unknown に落とす
    assert loop.binding_resume_guard_key(
        plan_signature="s", review_fingerprint="f",
        binding_decision_hash_value="d", failure_code="何か") == \
        loop.binding_resume_guard_key(
            plan_signature="s", review_fingerprint="f",
            binding_decision_hash_value="d", failure_code="unknown")


def test_decision_resume_once_then_reject_same_set(tmp_path):
    import app.plan_feedback as fb
    import app.plan_review_loop as loop

    mgr, pid = _make_manager(tmp_path)
    run = _stopped_run_with_two_ambiguous(mgr, pid)
    run_id = run["id"]
    sig = run["plan_signature"]
    ids = [b["issue_id"] for b in run["issue_bindings"]]
    # 2件とも確定する
    _confirm_all(mgr, pid, run_id, [(ids[0], "SC01", "SC01"), (ids[1], "SC01", "SC01")])
    run = loop.get_run(mgr, pid, run_id)
    assert loop.binding_decision_hash(run["issue_bindings"]) != ""
    from app.plan_feedback import review_fingerprint_of as _fp
    rounds = run["rounds"]
    current_fp = _fp(sig, rounds[0]["reviews"])
    stored = loop._store(mgr).get(pid, "plan", sig) or {}
    packet = stored.get("packet")
    contract = {"content_hash": run["goal_contract_hash"]}
    # 従来(版なし)の再開は拒否される
    with pytest.raises(ValueError, match="空転"):
        loop.record_resume(dict(run), actor="human-a")
    # 本番API経路だけで審査・記録・実行する(record_resumeを手で呼ばない)。

    proposal_keys = []

    async def _ok_propose(manager, pid_, signature_, tid, **kwargs):
        assert manager.plan_review_runner is _fail_review_runner
        proposal_keys.append((kwargs.get("idempotency_key"), kwargs.get("_prompt_version")))
        return {"candidate_id": "cand-p03-ok", "blockers": [],
                "changes": [{"target": "SC01", "before": "a", "after": "b"}]}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(fb, "propose", _ok_propose)
        out = asyncio.new_event_loop().run_until_complete(
            loop.remap_run(mgr, pid, run_id, "human-a", binding_decision=True))
    stored_run = loop.get_run(mgr, pid, run_id)
    assert stored_run["external_calls"] == int(run["external_calls"] or 0)
    assert out["revision_candidate_id"] == "cand-p03-ok"
    assert out["state"] == "stopped"
    decision_history = [e for e in (stored_run.get("resume_history") or [])
                        if e.get("resume_kind") == "binding_decision"]
    assert len(decision_history) == 1
    assert decision_history[0]["executed"] is True
    # 指摘とbindingsを保持する
    assert [b["issue_id"] for b in stored_run["issue_bindings"]] == ids
    # 同じ組の2回目は拒否される(同じ決定ハッシュでは二度目を停止)。
    # 記録ステップではなく実行(remap_run)で拒否されることを直接見る。
    with pytest.MonkeyPatch.context() as mp2:
        mp2.setattr(fb, "propose", _ok_propose)
        with pytest.raises(ValueError, match="同じ決定ハッシュでは二度目を停止"):
            asyncio.new_event_loop().run_until_complete(
                loop.remap_run(mgr, pid, run_id, "human-a", binding_decision=True))
    # 決定を変える(別の候補を確定)と再び1回再開できる。
    # confirm_binding APIは二重確定を拒否するため、人が選び直した状態を
    # manual_bindingsと対応表に反映して模擬する(決定ハッシュが変わること)。
    run2 = loop.get_run(mgr, pid, run_id)
    old_hash = loop.binding_decision_hash(run2["issue_bindings"])
    assert old_hash != ""
    for entry in run2.get("manual_bindings") or []:
        if isinstance(entry, dict) and str(entry.get("issue_id") or "") == ids[0]:
            entry["task_key"] = "SC02"
            entry["criterion_id"] = ""
    for b in run2["issue_bindings"] or []:
        if isinstance(b, dict) and str(b.get("issue_id") or "") == ids[0]:
            b["selected_task_key"] = "SC02"
            b["selected_criterion_id"] = ""
    loop._save(mgr, run2)
    run3 = loop.get_run(mgr, pid, run_id)
    new_hash = loop.binding_decision_hash(run3["issue_bindings"])
    assert new_hash != "" and new_hash != old_hash
    with pytest.MonkeyPatch.context() as mp3:
        mp3.setattr(fb, "propose", _ok_propose)
        out2 = asyncio.new_event_loop().run_until_complete(
            loop.remap_run(mgr, pid, run_id, "human-a", binding_decision=True))
    assert out2["revision_candidate_id"] == "cand-p03-ok"
    decision_history2 = [e for e in (loop.get_run(mgr, pid, run_id).get("resume_history") or [])
                         if e.get("resume_kind") == "binding_decision"]
    assert len(decision_history2) == 2
    assert all(e.get("executed") is True for e in decision_history2)
    assert len(proposal_keys) == 2
    assert proposal_keys[0][0] != proposal_keys[1][0]
    assert proposal_keys[0][1] != proposal_keys[1][1]


def test_decision_resume_rejects_unconfirmed_contradictory_empty(tmp_path):
    import app.plan_review_loop as loop
    from app.plan_feedback import review_fingerprint_of as _fp

    mgr, pid = _make_manager(tmp_path)
    run = _stopped_run_with_two_ambiguous(mgr, pid)
    sig = run["plan_signature"]
    stored = loop._store(mgr).get(pid, "plan", sig) or {}
    packet = stored.get("packet")
    contract = {"content_hash": run["goal_contract_hash"]}
    current_fp = _fp(sig, run["rounds"][0]["reviews"])
    # 未確定の指摘が残る → 拒否
    denied = loop.resume_eligibility(
        dict(run), binding_decision=True, review_fingerprint=current_fp,
        packet=packet, goal_contract=contract)
    assert denied["allowed"] is False
    assert "未確定" in denied["reason"]
    # 1件だけ確定しても残りが未確定 → 拒否
    ids = [b["issue_id"] for b in run["issue_bindings"]]
    asyncio.new_event_loop().run_until_complete(
        loop.confirm_binding(mgr, pid, run["id"], ids[0], actor="human-a",
                             task_key="SC01", criterion_id="SC01",
                             reason="一部確定", confirmed_preview=True))
    run1 = loop.get_run(mgr, pid, run["id"])
    denied1 = loop.resume_eligibility(
        dict(run1), binding_decision=True, review_fingerprint=current_fp,
        packet=packet, goal_contract=contract)
    assert denied1["allowed"] is False
    assert "未確定" in denied1["reason"]
    # 矛盾した確定がある → 拒否(2件とも確定させてから矛盾文言を付与)
    _confirm_all(mgr, pid, run["id"], [(ids[1], "SC01", "SC01")])
    run2 = loop.get_run(mgr, pid, run["id"])
    assert loop.binding_decision_hash(run2["issue_bindings"]) != ""
    tampered = json.loads(json.dumps(run2))
    for b in tampered["issue_bindings"]:
        if str(b.get("issue_id") or "") == ids[1]:
            b["basis"] = "step 1 の矛盾のため自動反映せず人の確認待ちです"
    denied2 = loop.resume_eligibility(
        tampered, binding_decision=True, review_fingerprint=current_fp,
        packet=packet, goal_contract=contract)
    assert denied2["allowed"] is False
    assert "矛盾" in denied2["reason"]
    # 決定ハッシュが空(確定なし) → 拒否。state=resolved の行は
    # 未確定扱いにならないため、決定的に確定なしの対応表で確認する
    resolved_only = [
        {"issue_id": "r1", "state": "resolved",
         "selection_status": "unselected",
         "plan_signature": sig,
         "candidate_task_keys": ["SC01"], "candidate_criterion_ids": ["SC01"]},
    ]
    denied3 = loop.resume_eligibility(
        {"state": "stopped", "stop_kind": "human_required",
         "plan_signature": sig, "goal_contract_hash": run["goal_contract_hash"],
         "packet_hash": run["packet_hash"], "issue_bindings": resolved_only,
         "manual_bindings": [],
         "rounds": [{"plan_signature": sig,
                     "reviews": run["rounds"][0]["reviews"]}]},
        binding_decision=True, review_fingerprint=current_fp,
        packet=packet, goal_contract=contract)
    assert denied3["allowed"] is False
    assert "確定した決定が無い" in denied3["reason"]


def test_decision_resume_rejects_changed_inputs(tmp_path):
    import app.plan_review_loop as loop
    from app.plan_feedback import review_fingerprint_of as _fp

    mgr, pid = _make_manager(tmp_path)
    run = _stopped_run_with_two_ambiguous(mgr, pid)
    run_id = run["id"]
    sig = run["plan_signature"]
    ids = [b["issue_id"] for b in run["issue_bindings"]]
    _confirm_all(mgr, pid, run_id, [(ids[0], "SC01", "SC01"), (ids[1], "SC01", "SC01")])
    run = loop.get_run(mgr, pid, run_id)
    stored = loop._store(mgr).get(pid, "plan", sig) or {}
    packet = stored.get("packet")
    contract = {"content_hash": run["goal_contract_hash"]}
    current_fp = _fp(sig, run["rounds"][0]["reviews"])
    good = dict(run)
    assert loop.resume_eligibility(
        good, binding_decision=True, review_fingerprint=current_fp,
        packet=packet, goal_contract=contract)["allowed"] is True
    # レビュー指紋が変われば拒否
    bad_fp = dict(run)
    denied_fp = loop.resume_eligibility(
        bad_fp, binding_decision=True, review_fingerprint="deadbeef",
        packet=packet, goal_contract=contract)
    assert denied_fp["allowed"] is False
    assert "レビュー" in denied_fp["reason"]
    # packetが変われば拒否
    denied_packet = loop.resume_eligibility(
        dict(run), binding_decision=True, review_fingerprint=current_fp,
        packet={"tampered": True}, goal_contract=contract)
    assert denied_packet["allowed"] is False
    assert "packet" in denied_packet["reason"]
    missing_packet = loop.resume_eligibility(
        dict(run), binding_decision=True, review_fingerprint=current_fp,
        packet=None, goal_contract=contract)
    assert missing_packet["allowed"] is False
    assert "packet" in missing_packet["reason"]
    # GoalContractが変われば拒否
    denied_contract = loop.resume_eligibility(
        dict(run), binding_decision=True, review_fingerprint=current_fp,
        packet=packet, goal_contract={"content_hash": "other-hash"})
    assert denied_contract["allowed"] is False
    assert "GoalContract" in denied_contract["reason"]
    missing_contract = loop.resume_eligibility(
        dict(run), binding_decision=True, review_fingerprint=current_fp,
        packet=packet, goal_contract=None)
    assert missing_contract["allowed"] is False
    assert "GoalContract" in missing_contract["reason"]
    # 計画署名が変われば拒否
    moved = json.loads(json.dumps(run))
    moved["plan_signature"] = "other-signature"
    denied_sig = loop.resume_eligibility(
        moved, binding_decision=True, review_fingerprint=current_fp,
        packet=packet, goal_contract=contract)
    assert denied_sig["allowed"] is False


def test_decision_resume_no_external_send_and_keeps_zero_calls(tmp_path):
    import app.plan_feedback as fb
    import app.plan_review_loop as loop
    from app.plan_feedback import review_fingerprint_of as _fp

    mgr, pid = _make_manager(tmp_path)
    run = _stopped_run_with_two_ambiguous(mgr, pid)
    run_id = run["id"]
    sig = run["plan_signature"]
    ids = [b["issue_id"] for b in run["issue_bindings"]]
    _confirm_all(mgr, pid, run_id, [(ids[0], "SC01", "SC01"), (ids[1], "SC01", "SC01")])
    run = loop.get_run(mgr, pid, run_id)
    external_before = int(run["external_calls"] or 0)

    async def _ok_propose(manager, pid_, signature_, tid, **kwargs):
        if manager.plan_review_runner is not _fail_review_runner:
            raise AssertionError("外部送信経路を使っている")
        return {"candidate_id": "cand-p03-external",
                "blockers": [], "changes": [{"target": "SC01", "before": "a", "after": "b"}]}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(fb, "propose", _ok_propose)
        out = asyncio.new_event_loop().run_until_complete(
            loop.remap_run(mgr, pid, run_id, "human-a", binding_decision=True))
    assert out["external_calls"] == external_before
    assert loop.get_run(mgr, pid, run_id)["external_calls"] == external_before
    assert out["revision_candidate_id"] == "cand-p03-external"


def test_candidate_check_failure_restops_and_keeps_issues(tmp_path):
    import app.plan_feedback as fb
    import app.plan_review_loop as loop
    from app.plan_feedback import review_fingerprint_of as _fp

    mgr, pid = _make_manager(tmp_path)
    run = _stopped_run_with_two_ambiguous(mgr, pid)
    run_id = run["id"]
    sig = run["plan_signature"]
    ids = [b["issue_id"] for b in run["issue_bindings"]]
    issue_texts_before = [str(b.get("issue_text") or "") for b in run["issue_bindings"]]
    _confirm_all(mgr, pid, run_id, [(ids[0], "SC01", "SC01"), (ids[1], "SC01", "SC01")])
    run = loop.get_run(mgr, pid, run_id)

    async def _bad_propose(manager, pid_, signature_, tid, **kwargs):
        return {"candidate_id": "", "blockers": [{"disposition": "unresolved"}],
                "changes": []}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(fb, "propose", _bad_propose)
        out = asyncio.new_event_loop().run_until_complete(
            loop.remap_run(mgr, pid, run_id, "human-a", binding_decision=True))
    assert out["state"] == "stopped"
    assert out["stop_kind"] == "human_required"
    stored_run = loop.get_run(mgr, pid, run_id)
    assert stored_run.get("failure_code") == "candidate_check_failed"
    assert [b["issue_id"] for b in stored_run["issue_bindings"]] == ids
    assert [str(b.get("issue_text") or "") for b in stored_run["issue_bindings"]] == issue_texts_before
    assert stored_run["external_calls"] == int(run["external_calls"] or 0)


def test_manual_binding_id_without_confirmed_row_stays_unconfirmed(tmp_path):
    import app.plan_review_loop as loop
    from app.plan_feedback import review_fingerprint_of as _fp

    mgr, pid = _make_manager(tmp_path)
    run = _stopped_run_with_two_ambiguous(mgr, pid)
    issue_id = run["issue_bindings"][0]["issue_id"]
    run["manual_bindings"] = [{"issue_id": issue_id, "task_key": "SC01",
                               "criterion_id": "SC01"}]
    loop._save(mgr, run)
    sig = run["plan_signature"]
    packet = (loop._store(mgr).get(pid, "plan", sig) or {}).get("packet")
    denied = loop.resume_eligibility(
        run, binding_decision=True,
        review_fingerprint=_fp(sig, run["rounds"][0]["reviews"]),
        packet=packet,
        goal_contract={"content_hash": run["goal_contract_hash"]})
    assert denied["allowed"] is False
    assert "未確定" in denied["reason"]


def test_execute_without_record_is_rejected(tmp_path):
    import app.plan_review_loop as loop
    from app.plan_feedback import review_fingerprint_of as _fp

    mgr, pid = _make_manager(tmp_path)
    run = _stopped_run_with_two_ambiguous(mgr, pid)
    ids = [b["issue_id"] for b in run["issue_bindings"]]
    _confirm_all(mgr, pid, run["id"],
                 [(ids[0], "SC01", "SC01"), (ids[1], "SC01", "SC01")])
    run = loop.get_run(mgr, pid, run["id"])
    sig = run["plan_signature"]
    packet = (loop._store(mgr).get(pid, "plan", sig) or {}).get("packet")
    denied = loop.resume_eligibility(
        run, binding_decision=True,
        review_fingerprint=_fp(sig, run["rounds"][0]["reviews"]),
        packet=packet, goal_contract={"content_hash": run["goal_contract_hash"]},
        plan_signature=sig, for_execute=True)
    assert denied["allowed"] is False
    assert "記録が無い" in denied["reason"]


def test_version_resume_behavior_unchanged(tmp_path):
    import app.plan_review_loop as loop

    mgr, pid = _make_manager(tmp_path)
    run = _stopped_run_with_two_ambiguous(mgr, pid)
    # 版なし・決定なしの従来経路は従来どおり空転ガードで止まる
    with pytest.raises(ValueError, match="空転"):
        loop.record_resume(dict(run), actor="human-a")
    denied = loop.resume_eligibility(dict(run))
    assert denied["allowed"] is False
