"""RD-01〜05: 現行抽出後に残る読取失敗・統制値不足は人手確認で止める。"""
from types import SimpleNamespace
from unittest.mock import patch

from app.goal_completion_flag import enable
from app.vehicle_auto import REVISION
from app.vehicle_workflow import digest, requirements_hash
from app.workflow_readiness import _next_action, _vehicle_view
import app.next_action_controller as nac


class Memory:
    def __init__(self, path, mission):
        self.path = path
        self._mission = mission

    def get_mission(self, _pid):
        return self._mission


def _mission():
    return {"goal": "車両別損益", "status": "paused", "tasks": []}


def _vehicle(**updates):
    row = {
        "prepared": True,
        "stale": False,
        "unresolved_reads": 1,
        "controls": "unavailable",
    }
    row.update(updates)
    return row


def _readiness(vehicle):
    action = _next_action("accuracy_blocked", vehicle, [], "p")
    return {
        "next_action": action,
        "allowed_actions": {action["id"]: {"allowed": True, "reason": ""}},
        "gate": {"failed_criteria": []},
        "stop_reason": "原本の確認が必要です",
    }


def test_rd_01_current_extraction_with_unresolved_reads_requires_human(tmp_path):
    manager = SimpleNamespace(memory=Memory(tmp_path / "memory.db", _mission()))
    action = _next_action("accuracy_blocked", _vehicle(), [], "p")
    assert action["id"] == "resolve_source_reads"
    assert action["class"] == "human_fact"
    assert action["auto_executable"] is False
    with patch.object(nac, "build_readiness", return_value=_readiness(_vehicle())), \
            patch.object(nac, "read_goal_state", return_value={"state": "planned"}):
        computed = nac.compute(manager, "p")
    assert computed["executable"] is False
    assert computed["blocked"] is True


def test_rd_02_current_extraction_with_unavailable_controls_requires_human():
    action = _next_action(
        "accuracy_blocked", _vehicle(unresolved_reads=0, controls="unavailable"), [], "p"
    )
    assert action["id"] == "resolve_source_reads"
    assert action["class"] == "human_fact"
    assert action["auto_executable"] is False


def test_rd_03_missing_stale_or_stale_check_error_allows_prepare(tmp_path):
    for vehicle in (
        _vehicle(prepared=False, stale=True),
        _vehicle(prepared=True, stale=True),
    ):
        action = _next_action("accuracy_blocked", vehicle, [], "p")
        assert action["id"] == "prepare"
        assert action["auto_executable"] is True

    mission = _mission()
    manager = SimpleNamespace(memory=Memory(tmp_path / "memory.db", mission))
    envelope = {
        "mode": "vehicle-auto-v1",
        "extractor_revision": REVISION,
        "source_hash": "source",
        "requirements_hash": requirements_hash(mission),
        "data": {"auto_extraction": {"issues": []}},
    }
    source_calls = {"count": 0}

    def sources_with_stale_error(*_args):
        source_calls["count"] += 1
        if source_calls["count"] == 1:
            raise RuntimeError("read failed")
        return []

    with patch("app.workflow_readiness.applicable", return_value=True), \
            patch("app.workflow_readiness.load_input", return_value=envelope), \
            patch("app.workflow_readiness.sources", side_effect=sources_with_stale_error), \
            patch("app.workflow_readiness.source_reconciliation_report", return_value=(None, {}, [])):
        view = _vehicle_view(manager, "p", mission)
    assert view["prepared"] is True
    assert view["stale"] is True
    assert _next_action("accuracy_blocked", {**view, "unresolved_reads": 1}, [], "p")["id"] == "prepare"


def test_rd_03_current_stale_condition_matches_vehicle_service(tmp_path):
    mission = _mission()
    manager = SimpleNamespace(memory=Memory(tmp_path / "memory.db", mission))
    source_rows = [{"id": "s1"}]
    envelope = {
        "mode": "vehicle-auto-v1",
        "extractor_revision": REVISION,
        "source_hash": digest(source_rows),
        "requirements_hash": requirements_hash(mission),
        "data": {"auto_extraction": {"issues": []}},
    }
    with patch("app.workflow_readiness.applicable", return_value=True), \
            patch("app.workflow_readiness.load_input", return_value=envelope), \
            patch("app.workflow_readiness.sources", return_value=source_rows), \
            patch("app.workflow_readiness.source_reconciliation_report", return_value=(None, {}, [])):
        assert _vehicle_view(manager, "p", mission)["stale"] is False


def test_rd_04_mismatch_still_requires_source_difference_review():
    action = _next_action(
        "accuracy_blocked", _vehicle(controls="mismatch", unresolved_reads=1), [], "p"
    )
    assert action["id"] == "review_source_difference"
    assert action["class"] == "human_fact"
    assert action["auto_executable"] is False


def test_rd_05_chain_stops_human_required_instead_of_repeat(tmp_path):
    manager = SimpleNamespace(memory=Memory(tmp_path / "memory.db", _mission()))
    enable(manager.memory.path, "p")
    readiness = _readiness(_vehicle())
    with patch.object(nac, "build_readiness", return_value=readiness), \
            patch.object(nac, "read_goal_state", return_value={"state": "planned"}):
        result = nac.run_chain(manager, "p", "rd-05")
    assert result["steps"] == []
    assert result["stopped_reason"] == "human_required"
    assert result["stopped_reason"] != "repeat"
    assert result["next_action"]["action_id"] == "resolve_source_reads"
