import pytest

from app.completion_gate import accept, evaluate
from app.goal_contract import activate
from app.vehicle_workflow import classify_unresolved, goal_failures
from app.workflow_readiness import build_readiness
from test_vehicle_workflow import setup as vehicle_setup


@pytest.mark.asyncio
async def test_provisional_vehicle_is_not_achieved(tmp_path):
    manager, pid = vehicle_setup(tmp_path)
    await manager.generate_plan(pid)
    activate(manager, pid)
    gate = evaluate(manager, pid)
    assert gate["achieved"] is False
    assert gate["failed_criteria"]
    assert "C07" in gate["failed_criteria"] or "C10" in gate["failed_criteria"] or "C09" in gate["failed_criteria"]
    with pytest.raises(ValueError, match="COMPLETION_GATE_FAILED"):
        accept(manager, pid, accepted_by="tester")
    assert goal_failures(manager, pid)


@pytest.mark.asyncio
async def test_mission_completed_alone_is_not_achieved(tmp_path):
    manager, pid = vehicle_setup(tmp_path)
    await manager.generate_plan(pid)
    activate(manager, pid)
    manager.memory.set_mission_status(pid, "completed", "強制完了", "forced")
    gate = evaluate(manager, pid)
    assert gate["achieved"] is False
    row = build_readiness(manager, pid)
    assert row["final_completed"] is False
    assert row["canonical_state"]
    assert row["next_action"]["id"]
    if row["phase"] not in {"idle", "running"}:
        assert row["next_action"].get("endpoint")


def test_classify_unresolved_does_not_zero_fill():
    data = {
        "auto_extraction": {
            "issues": [
                {"kind": "allocation", "status": "open", "id": "a1"},
                {"kind": "read", "status": "resolved", "id": "r0"},
                {"kind": "read", "status": "open", "id": "r1"},
            ]
        },
        "records": [{"id": "x", "allocations": []}],
        "source_controls": [{"status": "unavailable"}],
    }
    result = classify_unresolved(data)
    assert result["by_class"]["fact_pending"] >= 1
    assert result["by_class"]["missing_source"] == 1
    assert result["by_class"]["control_unavailable"] == 1
    assert result["total"] >= 3
    assert result["questions"]
