import pytest

from app.goal_completion_flag import enable
from app.goal_contract import from_mission
from app.plan_coverage import build, ensure_approvable
from app.structured_planning import validate_plan
from test_vehicle_workflow import setup as vehicle_setup


@pytest.mark.asyncio
async def test_vehicle_plan_covers_template_criteria(tmp_path):
    manager, pid = vehicle_setup(tmp_path)
    mission = await manager.generate_plan(pid)
    assert validate_plan({"tasks": mission["tasks"]}, {"SC01", "SC02"})["passed"]
    contract = from_mission(mission)
    coverage = build(mission, contract)
    assert coverage["passed"], coverage["issues"]
    assert {row["criterion_id"] for row in coverage["rows"]} == {f"C{i:02d}" for i in range(1, 13)}
    assert all(row["exec_task_key"] and row["verify_task_key"] for row in coverage["rows"])


@pytest.mark.asyncio
async def test_flag_blocks_approve_when_coverage_forced_empty(tmp_path):
    manager, pid = vehicle_setup(tmp_path)
    await manager.generate_plan(pid)
    enable(manager.memory.path, pid)
    contract = from_mission(manager.memory.get_mission(pid))
    contract["criteria"] = [{
        "criterion_id": "CX",
        "statement": "孤立条件",
        "exec_task_keys": ["missing"],
        "verify_task_keys": ["missing"],
    }]
    with pytest.raises(ValueError, match="COVERAGE_INCOMPLETE"):
        ensure_approvable(manager, pid, contract)
