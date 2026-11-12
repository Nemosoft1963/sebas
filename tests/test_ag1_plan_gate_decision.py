"""AG1: 評価ゲートの単一判定関数の試験。実外部AIなし。

ガードを外すと失敗する、挙動を直接見る試験にする(極性を逆に書かない)。
既存の期待値は変えない。
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from app.goal_review import ReviewStore
from app.memory.short_term import ShortTermMemory
from app.project_manager import ProjectOrchestrator
import app.planning_packet as packet
import app.planning_settings as ps
import app.independent_review as jobs
from app.plan_gate_decision import decide, enabled_for_project
import app.plan_gate_decision as gate_mod

GEN = "fake-gen"
REV = "fake-rev"
KNOWN = {GEN, REV}


def _manager(memory):
    return ProjectOrchestrator(
        memory, None, lambda project: ("", []), None, lambda: [])


def _ready(tmp_path, tag, method=None, gen="", rev=""):
    memory = ShortTermMemory(tmp_path / f"{tag}.db")
    pid = memory.create_project(tag)["id"]
    memory.save_mission(pid, "目標G", "条件S", "制約C", False, [])
    memory.replace_plan(pid, "計画", [{
        "task_key": "t1", "depends_on": [], "title": "作業1",
        "description": "作業する", "acceptance_criteria": "完了"}])
    memory.set_mission_status(pid, "ready", "承認済み", "approved")
    store = ReviewStore(memory.path)
    if method is not None:
        ps.update_settings(
            store, pid,
            {"method": method, "generator_provider": gen,
             "reviewer_provider": rev}, KNOWN)
    manager = _manager(memory)
    return memory, pid, manager, store


def _valid_plan(memory, pid):
    from app.structured_planning import compile_task, compile_plan
    criteria = ["架空手順Aを定義する", "架空手順Bを報告する"]
    memory.save_mission(pid, "疑似目標", "\n".join(f"{i + 1}. {c}" for i, c in enumerate(criteria)), "", False, [])
    t1 = compile_task(1, criteria[0], {
        "title": "架空資料A", "scope": "架空Aの整理",
        "headings": ["目的", "実施内容"], "depends_on": []}, [])
    t2 = compile_task(2, criteria[1], {
        "title": "架空資料B", "scope": "架空Bの整理",
        "headings": ["目的", "実施内容"], "depends_on": ["SC01"]}, [])
    compiled = compile_plan(criteria, [t1, t2], goal=memory.get_mission(pid)["goal"])
    memory.replace_plan(pid, "valid plan", compiled["tasks"])
    memory.set_mission_status(pid, "ready", "承認済み", "approved")


def _sig(memory, pid):
    return packet.plan_signature_of(memory.get_mission(pid))


def _put(store, pid, jid, sig, status, provider=REV, model="Rev-Model",
         sha="ab" * 32, reviewed_at=None, review_spec=jobs.REVIEW_SPEC_VERSION):
    job = {"id": jid, "pid": pid, "plan_signature": sig,
           "packet_sha256": sha, "provider": provider, "status": status,
           "model": model, "reviewed_at": time.time() if reviewed_at is None else reviewed_at,
           "verdict": "", "findings": [], "codes": [],
           "review_spec": review_spec}
    store.put(pid, jobs.KIND, jid, job)
    return job


def _start_ok(manager, pid):
    async def _fake(pid_a, **kw):
        return None
    manager._run = _fake

    async def _scenario():
        out = await manager.start(pid)
        await manager.shutdown()
        return out
    return asyncio.run(_scenario())


def _clear_flag(monkeypatch):
    monkeypatch.delenv("SEBAS_PLAN_GATE_V2_PROJECTS", raising=False)


# 1: legacy一致
def _policy_on(memory):
    policy_path = memory.path.parent / "goal_review_policy.json"
    policy_path.write_text('{"required":true}', encoding="utf-8")


def test_ag1_legacy_matches_current_for_external_guarded(tmp_path, monkeypatch):
    _clear_flag(monkeypatch)
    memory, pid, manager, store = _ready(
        tmp_path, "ag1-leg", method="external_guarded", gen=GEN, rev=REV)
    _valid_plan(memory, pid)
    _policy_on(memory)
    sig = _sig(memory, pid)
    _put(store, pid, "j1", sig, "review_passed")
    from app.independent_review_gate import gate_error
    assert gate_error(manager, pid) is None
    out = decide(manager, pid)
    assert out["mode"] == "legacy"
    # フラグ無効の external_guarded は現行どおり旧レビュー未合格で拒否される。
    # legacy の写しは require_review の失敗をそのまま表す。
    assert out["verdict"] == "failed"
    assert out["reason"] == "legacy_review_not_passed"
    # 旧レビューが無い external_guarded は現行どおり拒否される
    with pytest.raises(ValueError):
        asyncio.run(manager.start(pid))
    asyncio.run(manager.shutdown())


def test_ag1_legacy_local_only_matches(tmp_path, monkeypatch):
    _clear_flag(monkeypatch)
    memory, pid, manager, _ = _ready(tmp_path, "ag1-local")
    out = decide(manager, pid)
    assert out["mode"] == "legacy"
    assert out["verdict"] == "passed"
    assert out["reason"] == "ok_legacy"
    row = _start_ok(manager, pid)
    assert row["status"] == "ready"


# 2: v2合格
def test_ag1_v2_pass_allows_without_legacy_review(tmp_path, monkeypatch):
    memory, pid, manager, store = _ready(
        tmp_path, "ag1-v2pass", method="external_guarded", gen=GEN, rev=REV)
    _valid_plan(memory, pid)
    sig = _sig(memory, pid)
    monkeypatch.setenv("SEBAS_PLAN_GATE_V2_PROJECTS", pid)
    _put(store, pid, "j1", sig, "review_passed")
    out = decide(manager, pid)
    assert out["mode"] == "v2"
    assert out["verdict"] == "passed"
    assert out["reason"] == "ok_v2"
    assert out["evidence"] == [REV]
    # 旧レビューが無くても通る(矛盾の解消)
    row = _start_ok(manager, pid)
    assert row["status"] == "ready"


# 3: v2拒否の各単独条件
def _v2_base(tmp_path, monkeypatch, tag):
    memory, pid, manager, store = _ready(
        tmp_path, tag, method="external_guarded", gen=GEN, rev=REV)
    _valid_plan(memory, pid)
    sig = _sig(memory, pid)
    monkeypatch.setenv("SEBAS_PLAN_GATE_V2_PROJECTS", pid)
    return memory, pid, manager, store, sig


def test_ag1_v2_missing_is_unknown(tmp_path, monkeypatch):
    _, pid, manager, _, _ = _v2_base(tmp_path, monkeypatch, "ag1-miss")
    out = decide(manager, pid)
    assert out["mode"] == "v2"
    assert out["verdict"] == "unknown"
    assert out["reason"] == "independent_review_missing"
    with pytest.raises(ValueError) as exc:
        asyncio.run(manager.start(pid))
    assert str(exc.value) == "independent_review_missing"
    asyncio.run(manager.shutdown())


def test_ag1_v2_failed_is_failed(tmp_path, monkeypatch):
    _, pid, manager, store, sig = _v2_base(tmp_path, monkeypatch, "ag1-fail")
    _put(store, pid, "jf", sig, "review_failed")
    out = decide(manager, pid)
    assert out["verdict"] == "failed"
    assert out["reason"] == "independent_review_failed"


def test_ag1_v2_unknown_only_is_unknown(tmp_path, monkeypatch):
    _, pid, manager, store, sig = _v2_base(tmp_path, monkeypatch, "ag1-unk")
    _put(store, pid, "ju", sig, "unknown_verdict")
    out = decide(manager, pid)
    assert out["verdict"] == "unknown"
    assert out["reason"] == "independent_review_unknown"


def test_ag1_v2_old_signature_is_missing(tmp_path, monkeypatch):
    _, pid, manager, store, _ = _v2_base(tmp_path, monkeypatch, "ag1-old")
    _put(store, pid, "jo", "old-signature", "review_passed")
    out = decide(manager, pid)
    assert out["verdict"] == "unknown"
    assert out["reason"] == "independent_review_missing"


@pytest.mark.parametrize("kw,why", [
    ({"provider": GEN}, "provider_is_generator"),
    ({"sha": ""}, "empty_sha"),
    ({"model": ""}, "empty_model"),
    ({"reviewed_at": "bad"}, "bad_reviewed_at"),
])
def test_ag1_v2_invalid_pass_is_unknown(tmp_path, monkeypatch, kw, why):
    _, pid, manager, store, sig = _v2_base(tmp_path, monkeypatch, f"ag1-inv-{why}")
    _put(store, pid, "jb", sig, "review_passed", **kw)
    out = decide(manager, pid)
    assert out["verdict"] == "unknown", why
    assert out["reason"] == "independent_review_unknown", why


def test_ag1_v2_independent_disabled_is_unknown(tmp_path, monkeypatch):
    memory, pid, manager, store = _ready(
        tmp_path, "ag1-indoff", method="external_guarded", gen=GEN, rev=GEN)
    _valid_plan(memory, pid)
    sig = _sig(memory, pid)
    monkeypatch.setenv("SEBAS_PLAN_GATE_V2_PROJECTS", pid)
    _put(store, pid, "j1", sig, "review_passed", provider=GEN)
    out = decide(manager, pid)
    assert out["verdict"] == "unknown"
    assert out["reason"] in ("independent_review_unknown", "independent_review_check_error",
                             "gate_check_error")


def test_ag1_v2_blockers_present_is_failed(tmp_path, monkeypatch):
    from app.goal_review import plan_snapshot as _snap
    memory, pid, manager, store = _ready(
        tmp_path, "ag1-blk", method="external_guarded", gen=GEN, rev=REV)
    _valid_plan(memory, pid)
    sig = _sig(memory, pid)
    monkeypatch.setenv("SEBAS_PLAN_GATE_V2_PROJECTS", pid)
    _put(store, pid, "j1", sig, "review_passed")
    _, snap_sig = _snap(manager, pid)
    store.put(pid, "revision", snap_sig, {
        "id": "r1", "blockers": [{"disposition": "unresolved", "reason": "x"}]})
    out = decide(manager, pid)
    assert out["verdict"] == "failed"
    assert out["reason"] == "blockers_present"


def test_ag1_v2_deterministic_failure_is_failed(tmp_path, monkeypatch):
    memory, pid, manager, store = _ready(
        tmp_path, "ag1-det", method="external_guarded", gen=GEN, rev=REV)
    # わざと非構造化のまま(決定的検査が通らない)
    sig = _sig(memory, pid)
    monkeypatch.setenv("SEBAS_PLAN_GATE_V2_PROJECTS", pid)
    _put(store, pid, "j1", sig, "review_passed")
    out = decide(manager, pid)
    assert out["verdict"] == "failed"
    assert out["reason"] == "deterministic_check_failed"


def test_ag1_v2_review_policy_unmet_provider(tmp_path, monkeypatch):
    memory, pid, manager, store = _v2_base(
        tmp_path, monkeypatch, "ag1-pol1")[:4] + _v2_base(
        tmp_path, monkeypatch, "ag1-pol1-dummy")[:0]
    # 上の取り出しは冗長なので作り直す
    memory, pid, manager, store = _ready(
        tmp_path, "ag1-pol1b", method="external_guarded", gen=GEN, rev=REV)
    _valid_plan(memory, pid)
    sig = _sig(memory, pid)
    monkeypatch.setenv("SEBAS_PLAN_GATE_V2_PROJECTS", pid)
    _put(store, pid, "j1", sig, "review_passed")
    policy_path = memory.path.parent / "goal_review_policy.json"
    policy_path.write_text(json.dumps(
        {"required": True, "required_providers": ["other-provider"],
         "min_success_count": 1}), encoding="utf-8")
    out = decide(manager, pid)
    assert out["mode"] == "v2"
    assert out["verdict"] == "unknown"
    assert out["reason"] == "review_policy_unmet"


def test_ag1_v2_review_policy_unmet_min_count(tmp_path, monkeypatch):
    memory, pid, manager, store = _ready(
        tmp_path, "ag1-pol2", method="external_guarded", gen=GEN, rev=REV)
    _valid_plan(memory, pid)
    sig = _sig(memory, pid)
    monkeypatch.setenv("SEBAS_PLAN_GATE_V2_PROJECTS", pid)
    _put(store, pid, "j1", sig, "review_passed")
    policy_path = memory.path.parent / "goal_review_policy.json"
    policy_path.write_text(json.dumps(
        {"required": True, "required_providers": [REV],
         "min_success_count": 2}), encoding="utf-8")
    out = decide(manager, pid)
    assert out["verdict"] == "unknown"
    assert out["reason"] == "review_policy_unmet"


def test_ag1_v2_review_policy_multiple_required_not_partially_met(tmp_path, monkeypatch):
    memory, pid, manager, store = _ready(
        tmp_path, "ag1-pol-multi", method="external_guarded", gen=GEN, rev=REV)
    _valid_plan(memory, pid)
    sig = _sig(memory, pid)
    monkeypatch.setenv("SEBAS_PLAN_GATE_V2_PROJECTS", pid)
    _put(store, pid, "j1", sig, "review_passed")
    policy_path = memory.path.parent / "goal_review_policy.json"
    policy_path.write_text(json.dumps(
        {"required": True, "required_providers": [REV, "another-provider"],
         "min_success_count": 1}), encoding="utf-8")
    out = decide(manager, pid)
    assert out["mode"] == "v2"
    assert out["verdict"] == "unknown"
    assert out["reason"] == "review_policy_unmet"


# 4: 不明・読取不能を合格にしない
def test_ag1_v2_read_failures_are_not_passed(tmp_path, monkeypatch):
    memory, pid, manager, store = _ready(
        tmp_path, "ag1-read", method="external_guarded", gen=GEN, rev=REV)
    _valid_plan(memory, pid)
    sig = _sig(memory, pid)
    monkeypatch.setenv("SEBAS_PLAN_GATE_V2_PROJECTS", pid)
    _put(store, pid, "j1", sig, "review_passed")
    monkeypatch.setattr(manager.memory, "get_mission",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    out = decide(manager, pid)
    assert out["verdict"] in ("unknown", "failed", "connection_blocked")
    assert out["reason"] != "ok_v2"
    assert out["verdict"] != "passed"
    monkeypatch.undo()
    asyncio.run(manager.shutdown())


def test_ag1_v2_nondict_mission_is_unknown(tmp_path, monkeypatch):
    memory, pid, manager, store = _ready(
        tmp_path, "ag1-nondict", method="external_guarded", gen=GEN, rev=REV)
    _valid_plan(memory, pid)
    sig = _sig(memory, pid)
    monkeypatch.setenv("SEBAS_PLAN_GATE_V2_PROJECTS", pid)
    _put(store, pid, "j1", sig, "review_passed")
    monkeypatch.setattr(manager.memory, "get_mission", lambda *a, **k: [])
    out = decide(manager, pid)
    assert out["verdict"] != "passed"
    monkeypatch.undo()
    asyncio.run(manager.shutdown())


def test_ag1_v2_broken_job_row_is_unknown(tmp_path, monkeypatch):
    memory, pid, manager, store = _ready(
        tmp_path, "ag1-broken", method="external_guarded", gen=GEN, rev=REV)
    _valid_plan(memory, pid)
    sig = _sig(memory, pid)
    monkeypatch.setenv("SEBAS_PLAN_GATE_V2_PROJECTS", pid)
    store.put(pid, jobs.KIND, "bad", "broken-string")
    out = decide(manager, pid)
    assert out["verdict"] != "passed"
    assert out["verdict"] in ("unknown", "failed", "connection_blocked")


# 5: 読み取り専用
def test_ag1_decide_does_not_write(tmp_path, monkeypatch):
    memory, pid, manager, store = _ready(
        tmp_path, "ag1-nowrite", method="external_guarded", gen=GEN, rev=REV)
    _valid_plan(memory, pid)
    sig = _sig(memory, pid)
    monkeypatch.setenv("SEBAS_PLAN_GATE_V2_PROJECTS", pid)
    _put(store, pid, "j1", sig, "review_passed")
    before_jobs = sorted((s, dict(p)) for s, p in store.list(pid, jobs.KIND))
    before_settings = dict(store.get(pid, "planning_settings", pid))
    decide(manager, pid)
    after_jobs = sorted((s, dict(p)) for s, p in store.list(pid, jobs.KIND))
    assert before_jobs == after_jobs
    assert store.get(pid, "planning_settings", pid) == before_settings
    called = {"n": 0}
    orig_put = store.put

    def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("put must not be called")
    store.put = _boom
    try:
        decide(manager, pid)
    finally:
        store.put = orig_put
    assert called["n"] == 0


# 6: 呼び出し箇所の整合
def test_ag1_same_state_same_verdict_start_resume_readiness(tmp_path, monkeypatch):
    import app.safe_auto_resume as auto
    from app.workflow_readiness import build_readiness
    memory, pid, manager, store = _ready(
        tmp_path, "ag1-int", method="external_guarded", gen=GEN, rev=REV)
    _valid_plan(memory, pid)
    sig = _sig(memory, pid)
    monkeypatch.setenv("SEBAS_PLAN_GATE_V2_PROJECTS", pid)
    monkeypatch.setenv("LOCALSAPORTER_AUTO_RESUME_AVAILABLE", "1")
    _put(store, pid, "j1", sig, "review_passed")
    # start が通る
    row = _start_ok(manager, pid)
    assert row["status"] == "ready"
    # readiness の start が許可される
    memory2, pid2, manager2, store2 = _ready(
        tmp_path, "ag1-int2", method="external_guarded", gen=GEN, rev=REV)
    _valid_plan(memory2, pid2)
    sig2 = _sig(memory2, pid2)
    _put(store2, pid2, "j1", sig2, "review_passed")
    monkeypatch.setenv("SEBAS_PLAN_GATE_V2_PROJECTS", pid2)
    ready = build_readiness(manager2, pid2)
    assert ready["allowed_actions"]["start"]["allowed"] is True
    # auto-resume のゲートも通る(独立評価ゲート部分が同じ判定)。
    # なお _approved は旧レビュー合格も要求するため、ここではゲート部分だけを
    # 直接確認する(auto-resume のゲート置換と同じ decide() の verdict)。
    from app.plan_gate_decision import decide as _decide_direct
    assert _decide_direct(manager2, pid2)["verdict"] == "passed"
    monkeypatch.setattr(auto, "enabled", lambda *_args: True)
    monkeypatch.setattr(auto, "_approved", lambda *_a, **_k: True)
    out = asyncio.run(auto.resume(
        manager2, pid2, lambda *_args: {"artifact_hash": "h",
                                        "evidence": [{"path": "x.md"}]}))
    assert out["status"] in ("completed", "partial")


def test_ag1_v2_auto_resume_approved_without_unreachable_legacy_review(tmp_path, monkeypatch):
    import app.safe_auto_resume as auto
    from app.goal_review import plan_snapshot
    memory, pid, manager, store = _ready(
        tmp_path, "ag1-auto-approved", method="external_guarded", gen=GEN, rev=REV)
    _valid_plan(memory, pid)
    monkeypatch.setenv("SEBAS_PLAN_GATE_V2_PROJECTS", pid)
    _put(store, pid, "j1", _sig(memory, pid), "review_passed")
    _, legacy_signature = plan_snapshot(manager, pid)
    assert store.get(pid, "plan", legacy_signature) is None
    assert auto._approved(manager, pid, legacy_signature, memory.get_mission(pid)) is True


def test_ag1_v2_auto_resume_stays_blocked_without_independent_review(tmp_path, monkeypatch):
    import app.safe_auto_resume as auto
    from app.goal_review import plan_snapshot
    memory, pid, manager, _store = _ready(
        tmp_path, "ag1-auto-missing", method="external_guarded", gen=GEN, rev=REV)
    _valid_plan(memory, pid)
    monkeypatch.setenv("SEBAS_PLAN_GATE_V2_PROJECTS", pid)
    _, legacy_signature = plan_snapshot(manager, pid)
    assert auto._approved(manager, pid, legacy_signature, memory.get_mission(pid)) is False


def test_ag1_same_state_blocked_everywhere(tmp_path, monkeypatch):
    import app.safe_auto_resume as auto
    from app.workflow_readiness import build_readiness
    memory, pid, manager, store = _ready(
        tmp_path, "ag1-intb", method="external_guarded", gen=GEN, rev=REV)
    _valid_plan(memory, pid)
    monkeypatch.setenv("SEBAS_PLAN_GATE_V2_PROJECTS", pid)
    monkeypatch.setenv("LOCALSAPORTER_AUTO_RESUME_AVAILABLE", "1")
    # 独立評価なし -> 3箇所とも止まる
    with pytest.raises(ValueError) as exc:
        asyncio.run(manager.start(pid))
    assert str(exc.value) == "independent_review_missing"
    asyncio.run(manager.shutdown())
    ready = build_readiness(manager, pid)
    assert ready["allowed_actions"]["start"]["allowed"] is False
    assert "independent_review_missing" in ready["allowed_actions"]["start"]["reason"]
    monkeypatch.setattr(auto, "enabled", lambda *_args: True)
    out = asyncio.run(auto.resume(manager, pid, lambda *_args: None))
    assert out["status"] == "blocked"
    assert out["reason"] == "independent_review_missing"


# 7: 影モード
def test_ag1_shadow_records_once_and_start_stays_legacy(tmp_path, monkeypatch):
    _clear_flag(monkeypatch)
    memory, pid, manager, store = _ready(
        tmp_path, "ag1-shadow", method="external_guarded", gen=GEN, rev=REV)
    _valid_plan(memory, pid)
    _policy_on(memory)
    sig = _sig(memory, pid)
    _put(store, pid, "j1", sig, "review_passed")
    # フラグ無効: 旧は拒否(旧レビュー未合格)のまま
    from app.independent_review_gate import gate_error
    assert gate_error(manager, pid) is None
    with pytest.raises(ValueError):
        asyncio.run(manager.start(pid))
    events = [e for e in manager.memory.get_mission(pid)["events"]
              if e.get("kind") == "plan_gate_shadow_diff"]
    assert len(events) == 1
    assert events[0]["detail"] == "legacy=blocked;v2=passed:ok_v2"
    asyncio.run(manager.shutdown())


def test_ag1_shadow_no_event_when_agree(tmp_path, monkeypatch):
    _clear_flag(monkeypatch)
    memory, pid, manager, _ = _ready(tmp_path, "ag1-shadow-agree")
    before = [e for e in manager.memory.get_mission(pid)["events"]
              if e.get("kind") == "plan_gate_shadow_diff"]
    row = _start_ok(manager, pid)
    assert row["status"] == "ready"
    after = [e for e in manager.memory.get_mission(pid)["events"]
             if e.get("kind") == "plan_gate_shadow_diff"]
    assert before == after == []


def test_ag1_shadow_failure_does_not_change_outcome(tmp_path, monkeypatch):
    _clear_flag(monkeypatch)
    memory, pid, manager, store = _ready(
        tmp_path, "ag1-shadow-fail", method="external_guarded",
        gen=GEN, rev=REV)
    _valid_plan(memory, pid)
    _policy_on(memory)
    orig_decide = gate_mod.decide

    def _boom_decide(*a, **k):
        raise OSError("boom")
    monkeypatch.setattr(gate_mod, "decide", _boom_decide)
    # 影の計算に例外が出ても旧結果のまま(拒否)
    with pytest.raises(ValueError):
        asyncio.run(manager.start(pid))
    monkeypatch.setattr(gate_mod, "decide", orig_decide)
    asyncio.run(manager.shutdown())


def test_ag1_no_shadow_when_flag_enabled(tmp_path, monkeypatch):
    memory, pid, manager, store = _ready(
        tmp_path, "ag1-noshadow", method="external_guarded", gen=GEN, rev=REV)
    _valid_plan(memory, pid)
    sig = _sig(memory, pid)
    monkeypatch.setenv("SEBAS_PLAN_GATE_V2_PROJECTS", pid)
    _put(store, pid, "j1", sig, "review_passed")
    row = _start_ok(manager, pid)
    assert row["status"] == "ready"
    events = [e for e in manager.memory.get_mission(pid)["events"]
              if e.get("kind") == "plan_gate_shadow_diff"]
    assert events == []


def test_ag1_flagged_unset_method_uses_legacy_start(tmp_path, monkeypatch):
    memory, pid, manager, _ = _ready(tmp_path, "ag1-flag-unset")
    _valid_plan(memory, pid)
    monkeypatch.setenv("SEBAS_PLAN_GATE_V2_PROJECTS", pid)
    assert decide(manager, pid)["mode"] == "legacy"
    assert _start_ok(manager, pid)["status"] == "ready"


def test_ag1_flagged_local_only_uses_legacy_start(tmp_path, monkeypatch):
    memory, pid, manager, _ = _ready(
        tmp_path, "ag1-flag-local", method="local_only", gen=GEN, rev=REV)
    _valid_plan(memory, pid)
    monkeypatch.setenv("SEBAS_PLAN_GATE_V2_PROJECTS", pid)
    assert decide(manager, pid)["mode"] == "legacy"
    assert _start_ok(manager, pid)["status"] == "ready"


def test_ag1_v2_uses_effective_planning_criteria(tmp_path, monkeypatch):
    memory, pid, manager, store, sig = _v2_base(
        tmp_path, monkeypatch, "ag1-effective")
    _put(store, pid, "j1", sig, "review_passed")
    original = manager._effective_planning_criteria
    monkeypatch.setattr(
        manager, "_effective_planning_criteria",
        lambda mission: original(mission) + ["追加された未網羅条件"])
    out = decide(manager, pid)
    assert out["verdict"] != "passed"
    assert out["reason"] == "deterministic_check_failed"


@pytest.mark.parametrize("failure", [False, True])
def test_ag1_v2_approve_preserves_capability_gap_block(
        tmp_path, monkeypatch, failure):
    import app.capability_gap as capability_gap
    memory, pid, manager, store = _ready(
        tmp_path, f"ag1-cap-{failure}", method="external_guarded", gen=GEN, rev=REV)
    _valid_plan(memory, pid)
    sig = _sig(memory, pid)
    monkeypatch.setenv("SEBAS_PLAN_GATE_V2_PROJECTS", pid)
    _put(store, pid, "j1", sig, "review_passed")
    monkeypatch.setattr("app.plan_repair_loop.approval_gate", lambda *_a, **_k: None)
    monkeypatch.setattr("app.goal_completion_hooks.before_approve", lambda *_a, **_k: None)
    if failure:
        def _check(*_a, **_k):
            raise OSError("boom")
    else:
        def _check(*_a, **_k):
            return {"blocked": True, "reason": "capability blocked"}
    monkeypatch.setattr(capability_gap, "blocks_execution_approval", _check)
    result = manager.approve(pid)
    assert result["execution_gate"]["blocked"] is True


def test_ag1_legacy_gate_order_is_preserved(tmp_path, monkeypatch):
    memory, pid, manager, _ = _ready(tmp_path, "ag1-order")
    _valid_plan(memory, pid)
    monkeypatch.setenv("SEBAS_PLAN_GATE_V2_PROJECTS", pid)
    calls = []
    monkeypatch.setattr("app.goal_review.require_review", lambda *_a: calls.append("review"))
    monkeypatch.setattr("app.plan_repair_loop.approval_gate", lambda *_a: calls.append("approval"))
    monkeypatch.setattr("app.independent_review_gate.gate_error",
                        lambda *_a: calls.append("independent"))
    _start_ok(manager, pid)
    expected = ["review", "approval", "independent"]
    assert any(calls[index:index + 3] == expected
               for index in range(max(0, len(calls) - 2)))


def test_ag1_shadow_event_write_failure_keeps_legacy_outcome(tmp_path, monkeypatch):
    _clear_flag(monkeypatch)
    memory, pid, manager, store = _ready(
        tmp_path, "ag1-shadow-write", method="external_guarded", gen=GEN, rev=REV)
    _valid_plan(memory, pid)
    _policy_on(memory)
    _put(store, pid, "j1", _sig(memory, pid), "review_passed")
    original_add = memory.add_event

    def _fail_shadow(project_id, kind, *args, **kwargs):
        if kind == "plan_gate_shadow_diff":
            raise OSError("audit unavailable")
        return original_add(project_id, kind, *args, **kwargs)

    monkeypatch.setattr(memory, "add_event", _fail_shadow)
    with pytest.raises(ValueError):
        asyncio.run(manager.start(pid))
    asyncio.run(manager.shutdown())


# 8: 静的検査
def test_ag1_static_no_writes_no_swallow(tmp_path):
    src = gate_mod.__file__
    text = open(src, encoding="utf-8").read()
    code = "\n".join(line for line in text.splitlines()
                     if not line.strip().startswith('"""') and '"""' not in line
                     and not line.strip().startswith("状態を書かない")
                     and not line.strip().startswith("external_guarded")
                     and not line.strip().startswith("このモジュールは")
                     and not line.strip().startswith("戻り値"))
    assert "put(" not in code
    assert ".add_event" not in code
    assert "recover_interrupted" not in code
    assert "except Exception" not in code
    assert "except:" not in code
    # passed を返す箇所は2つだけ
    assert text.count('"passed"') == 2
    assert "ok_legacy" in text and "ok_v2" in text
