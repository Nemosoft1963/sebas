from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.completion_replay import _external_criteria, preview


class Memory:
    def __init__(self, path):
        self.path = path
    def get_mission(self, project_id):
        return {"status": "ready", "plan_version": 2, "tasks": [{"id": "t", "task_key": "SC01"}]}


def _manager(tmp_path):
    return SimpleNamespace(memory=Memory(tmp_path / "memory.sqlite3"))


def test_missing_stores_do_not_get_created(tmp_path):
    manager = _manager(tmp_path)
    with patch("app.completion_replay.plan_snapshot", return_value=({}, "sig")):
        result = preview(manager, "p")
    assert result["read_only"] is True and result["would_achieve"] is False
    assert all(not x["reachable"] for x in result["stages"])
    assert not (tmp_path / "goal_reviews.sqlite3").exists()
    assert not (tmp_path / "goal_completion.sqlite3").exists()
    assert not (tmp_path / "memory.sqlite3.auto_resume.sqlite3").exists()


def test_external_stage_uses_required_criteria_not_unrelated_action(tmp_path):
    manager = _manager(tmp_path)
    contract = {"criteria": [{"criterion_id": "SC01", "statement": "外部公開", "requires_external_action": True,
                              "exec_task_keys": ["SC01"]}]}
    gate = {"criteria": [{"criterion_id": "SC01", "status": "FAIL"}]}
    with patch("app.goal_contract.get_active", return_value=contract):
        required, passed = _external_criteria(manager, "p", manager.memory.get_mission("p"), gate)
    assert required == ["SC01"] and passed is False
    gate["criteria"][0]["status"] = "PASS"
    with patch("app.goal_contract.get_active", return_value=contract):
        assert _external_criteria(manager, "p", manager.memory.get_mission("p"), gate) == (["SC01"], True)


def test_plan_issues_and_external_failure_block_replay(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    monkeypatch.setenv("LOCALSAPORTER_AUTO_RESUME_AVAILABLE", "1")
    gate = {"achieved": False, "reason_code": "EXTERNAL_EVIDENCE_MISSING",
            "criteria": [{"criterion_id": "SC01", "status": "FAIL"}]}
    with patch("app.completion_replay._existing_stores", return_value=True), \
         patch("app.completion_replay.plan_snapshot", return_value=({}, "sig")), \
         patch("app.goal_review.ReviewStore") as reviews, \
         patch("app.local_patch_preview.preview", return_value={"candidates": [{"classification": "safe_plan_patch"}]}), \
         patch("app.pending_ledger.build", return_value={"summary": {"plan_status": "unapproved", "issue_count": 1, "unresolved_count": 1}}), \
         patch("app.completion_gate.evaluate", return_value=gate), \
         patch("app.completion_replay.auto_enabled", return_value=True), \
         patch("app.completion_replay._verified_local_runs", return_value=[]), \
         patch("app.completion_replay._external_criteria", return_value=(["SC01"], False)):
        reviews.return_value.get.return_value = {"status": "passed"}
        result = preview(manager, "p")
    by_id = {item["id"]: item for item in result["stages"]}
    assert by_id["plan_repair"]["reachable"] is True
    assert by_id["external_validation"]["reachable"] is False
    assert by_id["plan_approval"]["reachable"] is False
    assert by_id["local_execution"]["reachable"] is False
    assert by_id["external_operation"]["reachable"] is False
    assert result["would_achieve"] is False


def test_plan_snapshot_error_fails_closed(tmp_path):
    manager = _manager(tmp_path)
    with patch("app.completion_replay.plan_snapshot", side_effect=ValueError("unavailable")):
        result = preview(manager, "p")
    assert result["completion_gate"]["achieved"] is False
    assert result["would_achieve"] is False
