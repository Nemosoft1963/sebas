"""NAC-10〜15: prepare接続・予算時間回復・既回答適用・blocked統合。"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.goal_completion_flag import enable
from app.goal_completion_store import GoalCompletionStore
from app.recovery_policy import RecoveryBudget
from app.vehicle_auto import REVISION
from app.vehicle_service import envelope_input_hash
from app.vehicle_workflow import digest, write_json, resolve, INPUT_PATH
from app.workflow_readiness import UNIMPLEMENTED_ACTIONS, build_readiness
import app.next_action_controller as nac
from test_next_action_controller import manager, ready, state
from test_vehicle_workflow import setup as vehicle_setup
from test_workflow_readiness import vehicle_plan


@pytest.fixture(autouse=True)
def restore_executors_and_budgets():
    original = dict(nac.EXECUTORS)
    nac.register_default_executors(overwrite=True)
    yield
    nac.EXECUTORS.clear()
    nac.EXECUTORS.update(original)
    nac._BUDGETS.clear()


def _enable(m, pid="p"):
    enable(m.memory.path, pid)
    return m


def test_nac_10_prepare_executor_same_order_and_safety(tmp_path):
    assert "prepare" in nac.EXECUTORS
    assert nac.EXECUTORS["prepare"] is nac.execute_prepare

    manager, pid = vehicle_setup(tmp_path)
    enable(manager.memory.path, pid)
    mission = manager.memory.get_mission(pid)
    mission = {**mission, "status": "paused", "plan_version": mission["plan_version"] or 1}
    previous = {
        "mode": "vehicle-auto-v1",
        "extractor_revision": REVISION,
        "source_hash": "same-src",
        "requirements_hash": "same-req",
    }
    order = []

    def idle(mgr, project_id, version, input_hash=None):
        order.append(("idle_current", version))
        assert project_id == pid
        return mission, previous

    def begin(store, project_id, kind, key, extra=None):
        order.append(("begin_job", kind, key))
        return {"id": "job-1"}

    def finish(store, project_id, job_id, status, **updates):
        order.append(("finish_job", status, updates.get("blocking_error") or updates.get("last_completed_stage") or ""))
        return {}

    def save(store, project_id, **fields):
        order.append(("save_orchestration", fields.get("last_completed_stage")))

    def prepare(mgr, project_id, miss):
        order.append(("prepare", project_id, miss["plan_version"]))
        assert project_id in mgr.planning_projects
        return previous

    with patch("app.vehicle_service.idle_current", idle), \
            patch("app.vehicle_workflow.applicable", return_value=True), \
            patch("app.goal_review.begin_job", begin), \
            patch("app.goal_review.finish_job", finish), \
            patch("app.goal_review.save_orchestration", save), \
            patch("app.vehicle_auto.prepare", prepare), \
            patch("app.vehicle_workflow.digest", return_value="same-src"), \
            patch("app.vehicle_workflow.requirements_hash", return_value="same-req"), \
            patch("app.vehicle_workflow.sources", return_value=[]), \
            patch("app.vehicle_service.state", return_value={"prepared": True}), \
            patch.object(nac, "build_readiness", return_value=ready("prepare")), \
            patch.object(nac, "read_goal_state", state):
        skipped = nac.execute(manager, pid, "prep-skip")
        assert skipped["status"] == "succeeded"
        assert all(item[0] != "prepare" for item in order)
        assert [item[0] for item in order[:2]] == ["idle_current", "begin_job"]
        assert ("finish_job", "succeeded", "prepare") in order

        order.clear()
        previous["source_hash"] = "changed"
        ran = nac.execute(manager, pid, "prep-run")
        assert ran["status"] == "succeeded"
        names = [item[0] for item in order]
        assert names.index("idle_current") < names.index("begin_job") < names.index("prepare")
        assert names.index("prepare") < names.index("finish_job")
        assert ("prepare", pid, mission["plan_version"]) in order
        assert ("save_orchestration", "prepare") in order
        assert pid not in manager.planning_projects

        order.clear()

        def boom(*_args, **_kwargs):
            order.append(("idle_current", "err"))
            raise ValueError("実行中・計画生成中または計画版が変わりました")

        with patch("app.vehicle_service.idle_current", boom):
            failed = nac.execute(manager, pid, "prep-fail")
        assert failed["status"] == "failed" and failed.get("needs_approval") is True
        assert GoalCompletionStore(manager.memory.path).get_nac_execution(pid, "prep-fail")["status"] == "failed"


@pytest.mark.asyncio
async def test_nac_10_existing_prepare_http_still_maps_valueerror(tmp_path, monkeypatch):
    import app.web as web
    manager, pid = vehicle_setup(tmp_path)
    monkeypatch.setattr(web, "memory", manager.memory)
    monkeypatch.setattr(web, "orchestrator", manager)
    with pytest.raises(HTTPException) as err:
        await web.prepare_vehicle_profit(pid, web.VehiclePreparePayload(version=999, idempotency_key="x"))
    assert err.value.status_code == 409


def test_nac_11_time_budget_recovers_after_cooldown_counts_do_not(tmp_path):
    m = _enable(manager(tmp_path))
    nac.reset_budget(m, "p", "c1")
    nac.EXECUTORS["a"] = lambda *_: {}
    clock = {"now": 1000.0}

    def mono():
        return clock["now"]

    with patch("app.recovery_policy.time.monotonic", mono), \
            patch("app.next_action_controller.time.monotonic", mono), \
            patch.object(nac, "build_readiness", return_value=ready("a")), \
            patch.object(nac, "read_goal_state", state):
        key = (str(m.memory.path), "p", "c1")
        stale = RecoveryBudget()
        stale.started = 1000.0
        nac._BUDGETS[key] = stale
        clock["now"] = 1000.0 + 301
        stopped = nac.run_chain(m, "p", "time-1")
        assert stopped["stopped_reason"] == "budget" and stopped["needs_approval"] is True
        assert stopped["status"] == "stopped"
        assert all(step.get("status") != "succeeded" for step in stopped["steps"]) or stopped["steps"] == []

        clock["now"] = 1000.0 + 301
        still = nac.run_chain(m, "p", "time-2")
        assert still["stopped_reason"] == "budget"

        clock["now"] = 1000.0 + 300 + nac.BUDGET_TIME_COOLDOWN_SECONDS + 1
        recovered = nac.run_chain(m, "p", "time-3")
        assert recovered["stopped_reason"] != "budget" or recovered["steps"]
        assert recovered["needs_approval"] in {True, False}

        nac.reset_budget(m, "p", "c1")
        counted = RecoveryBudget()
        counted.tool_calls = counted.tool_limit
        counted.cycles = counted.cycle_limit
        counted.started = 1000.0
        nac._BUDGETS[key] = counted
        clock["now"] = 1000.0 + 10_000
        frozen = nac.run_chain(m, "p", "count-later")
        assert frozen["stopped_reason"] == "budget" and frozen["needs_approval"] is True


def _prior_envelope(input_hash, *, month_rule="2026-01", subject_company="関東", extra_issue=True):
    issues = [
        {
            "id": "i1", "kind": "allocation", "status": "unresolved", "month": "2026-01",
            "allocation_subject": "山田太郎", "record_id": "r1", "message": "帰属",
        },
    ]
    records = [
        {
            "id": "r1", "month": "2026-01", "company": "関東", "allocation_subject": "山田太郎",
            "allocations": [], "amount": 100, "quality": "actual", "category": "payroll",
        },
    ]
    if extra_issue:
        issues.append({
            "id": "i2", "kind": "allocation", "status": "unresolved", "month": "2026-02",
            "allocation_subject": "山田太郎", "record_id": "r2", "message": "帰属",
        })
        records.append({
            "id": "r2", "month": "2026-02", "company": "関東", "allocation_subject": "山田太郎",
            "allocations": [], "amount": 200, "quality": "actual", "category": "payroll",
        })
    envelope = {
        "mode": "vehicle-auto-v1",
        "source_hash": "src-1",
        "requirements_hash": "req-1",
        "extractor_revision": REVISION,
        "data": {
            "vehicles": [{"id": "足立101か1234", "company": "関東"}],
            "records": records,
            "auto_extraction": {"issues": issues, "recoveries": []},
            "decision_log": [],
        },
        "assignments": [{
            "id": "rule-prior",
            "status": "active",
            "subject_type": "employee",
            "subject_key": {"company": subject_company, "employee": "山田太郎"},
            "effective_from": month_rule,
            "effective_to": month_rule,
            "allocations": [{"vehicle_id": "足立101か1234", "ratio": 1}],
            "input_hash": input_hash,
            "source_hash": "src-1",
            "decision_id": 0,
            "reason": "当月は車両A",
        }],
        "decisions": [],
    }
    envelope["assignments"][0]["input_hash"] = envelope_input_hash(envelope) if input_hash == "AUTO" else input_hash
    return envelope


def test_nac_12_prior_answers_require_subject_period_and_input_hash(tmp_path):
    manager, pid = vehicle_setup(tmp_path)
    enable(manager.memory.path, pid)
    envelope = _prior_envelope("AUTO")
    matching_hash = envelope_input_hash(envelope)
    envelope["assignments"][0]["input_hash"] = matching_hash
    write_json(resolve(manager, pid, INPUT_PATH), envelope)

    from app.vehicle_service import apply_prior_answers, preview_prior_answers
    preview = preview_prior_answers(envelope)
    assert [row["issue_id"] for row in preview["applied"]] == ["i1"]
    skipped = {row["issue_id"]: row["reason"] for row in preview["skipped"]}
    assert skipped["i2"] == "period_mismatch"

    result = apply_prior_answers(manager, pid)
    assert result["achieved"] is False and result["human_accepted"] is False
    applied_ids = [row["issue_id"] for row in result["applied"]]
    assert applied_ids == ["i1"]
    assert result["applied"][0]["source"]["id"] == "rule-prior"
    assert any(row["issue_id"] == "i2" for row in result["skipped"])

    from app.vehicle_workflow import load_input
    saved = load_input(manager, pid)
    rec1 = next(r for r in saved["data"]["records"] if r["id"] == "r1")
    rec2 = next(r for r in saved["data"]["records"] if r["id"] == "r2")
    assert rec1["allocations"] == [{"vehicle_id": "足立101か1234", "ratio": 1}]
    assert rec1.get("allocation_reused") is True
    assert not rec2.get("allocations")
    issue2 = next(i for i in saved["data"]["auto_extraction"]["issues"] if i["id"] == "i2")
    assert issue2["status"] == "unresolved"

    other = _prior_envelope("wrong-hash", extra_issue=False)
    preview_hash = preview_prior_answers(other)
    assert preview_hash["applied"] == []
    assert all(row["reason"] == "input_hash_mismatch" for row in preview_hash["skipped"])

    subject = _prior_envelope("AUTO", subject_company="弘和", extra_issue=False)
    subject["assignments"][0]["input_hash"] = envelope_input_hash(subject)
    preview_subject = preview_prior_answers(subject)
    assert preview_subject["applied"] == []
    assert all(row["reason"] == "subject_key_mismatch" for row in preview_subject["skipped"])


def test_nac_13_full_apply_does_not_write_achieved_or_acceptance(tmp_path):
    manager, pid = vehicle_setup(tmp_path)
    enable(manager.memory.path, pid)
    envelope = _prior_envelope("AUTO", extra_issue=False)
    envelope["assignments"][0]["input_hash"] = envelope_input_hash(envelope)
    write_json(resolve(manager, pid, INPUT_PATH), envelope)
    store = GoalCompletionStore(manager.memory.path)
    before_accept = store.latest_unrevoked_acceptance(pid)

    from app.vehicle_service import apply_prior_answers
    result = apply_prior_answers(manager, pid)
    assert result["applied"] and not result["skipped"]
    assert result["achieved"] is False and result["human_accepted"] is False
    assert store.latest_unrevoked_acceptance(pid) == before_accept
    with store.connect() as db:
        achieved = db.execute(
            "SELECT COUNT(*) FROM completion_evaluations WHERE project_id=? AND achieved=1",
            (pid,),
        ).fetchone()[0]
    assert achieved == 0
    from app.goal_state_machine import current_state
    assert current_state(store, pid) not in {"achieved", "verified"}


def test_nac_14_chain_applies_then_stops_for_human_remainder(tmp_path):
    m = _enable(manager(tmp_path))
    calls = []
    avail = iter([True, True, False, False, False, False])

    def answers(*_args, **_kwargs):
        return next(avail)

    nac.EXECUTORS["apply_prior_answers"] = lambda *args: calls.append(1) or {
        "applied": [{"issue_id": "i1"}], "skipped": [{"issue_id": "i2", "reason": "period_mismatch"}],
        "achieved": False, "human_accepted": False,
    }
    with patch.object(nac, "build_readiness", return_value=ready("confirm_allocation", "human_fact", False)), \
            patch.object(nac, "read_goal_state", state), \
            patch.object(nac, "_prior_answers_available", answers):
        row = nac.run_chain(m, "p", "prior-chain")
    assert len(calls) == 1
    assert row["stopped_reason"] == "human_required" and row["needs_approval"] is True
    assert row["steps"][0]["status"] == "succeeded"
    assert row["next_action"]["action_id"] == "confirm_allocation"
    assert row["next_action"]["executable"] is False


def test_nac_15_unimplemented_are_blocked_on_nac_and_unchanged_on_readiness(tmp_path):
    m = manager(tmp_path)
    enable(m.memory.path, "p")
    with patch.object(nac, "build_readiness", return_value=ready("prepare")), patch.object(nac, "read_goal_state", state):
        row = nac.compute(m, "p")
    blocked = {item["id"]: item["reason"] for item in row["blocked_actions"]}
    for ident, reason in UNIMPLEMENTED_ACTIONS:
        assert blocked[ident] == reason

    for ident, reason in UNIMPLEMENTED_ACTIONS:
        with patch.object(nac, "build_readiness", return_value=ready(ident, "local_safe", True)), \
                patch.object(nac, "read_goal_state", state):
            computed = nac.compute(m, "p")
            assert computed["action_id"] == ident
            assert computed["executable"] is False and computed["blocked"] is True
            assert computed["reason"] == reason
            with pytest.raises(nac.NextActionRefused) as exc:
                nac.execute(m, "p", ident)
            assert exc.value.code == "ACTION_REFUSED"

    mgr, pid = vehicle_plan(tmp_path)
    readiness = build_readiness(mgr, pid)
    unimplemented = {ident for ident, _ in UNIMPLEMENTED_ACTIONS}
    assert unimplemented.isdisjoint(readiness["allowed_actions"])
    blocked_ids = {x["id"] for x in readiness["blocked_actions"]}
    assert unimplemented <= blocked_ids
    for ident, reason in UNIMPLEMENTED_ACTIONS:
        match = next(x for x in readiness["blocked_actions"] if x["id"] == ident)
        assert match["reason"] == reason


@pytest.mark.asyncio
async def test_nac_15_execute_unimplemented_is_http_409(tmp_path, monkeypatch):
    import app.web as web
    mgr, pid = vehicle_plan(tmp_path)
    enable(mgr.memory.path, pid)
    monkeypatch.setattr(web, "memory", mgr.memory)
    monkeypatch.setattr(web, "orchestrator", mgr)
    with patch.object(nac, "build_readiness", return_value=ready("triz_adopt", "local_safe", True)), \
            patch.object(nac, "read_goal_state", state):
        with pytest.raises(HTTPException) as err:
            await web.post_next_action_execute(pid, web.NextActionExecutePayload(idempotency_key="triz"))
    assert err.value.status_code == 409
    assert err.value.detail["code"] == "ACTION_REFUSED"
