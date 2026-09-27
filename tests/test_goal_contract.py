from app.goal_completion_flag import enable
from app.goal_contract import from_mission, put_draft, preview
from app.requirement_retention import inspect, ensure_plannable
from app.vehicle_workflow import applicable
from test_vehicle_workflow import setup as vehicle_setup


def test_vehicle_template_has_twelve_criteria(tmp_path):
    manager, pid = vehicle_setup(tmp_path)
    mission = manager.memory.get_mission(pid)
    assert applicable(mission)
    contract = from_mission(mission)
    ids = [c["criterion_id"] for c in contract["criteria"]]
    assert ids == [f"C{i:02d}" for i in range(1, 13)]
    assert contract["retention"]["collapsed"] is False
    assert contract["retention"]["passed"] is True


def test_long_goal_with_one_success_is_collapsed(tmp_path):
    manager, pid = vehicle_setup(tmp_path)
    long_goal = "背景説明。" * 80 + "市場を整理して資料を作る。"
    manager.memory.save_mission(pid, long_goal, "資料を1つ作る", "", False, [])
    mission = manager.memory.get_mission(pid)
    # Force non-vehicle so template does not mask collapse: drop vehicle keywords
    manager.memory.save_mission(pid, "説明。" * 160 + "全体方針を文書化する", "方針メモを残す", "", False, [])
    mission = manager.memory.get_mission(pid)
    contract = from_mission(mission)
    retention = inspect(mission, contract)
    assert retention["collapsed"] is True
    assert retention["passed"] is False
    enable(manager.memory.path, pid)
    put_draft(manager, pid, contract)
    try:
        ensure_plannable(manager, pid, contract)
        raised = False
    except ValueError as exc:
        raised = "RETENTION_FAILED" in str(exc)
    assert raised


def test_preview_creates_hash(tmp_path):
    manager, pid = vehicle_setup(tmp_path)
    previewed = preview(manager, pid)
    assert previewed["content_hash"]
    assert previewed["schema"] == "sebas-goal-contract/v1"
