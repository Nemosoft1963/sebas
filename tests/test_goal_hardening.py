import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.goal_completion_flag import enable
from app.goal_completion_hooks import after_generate, before_approve, before_generate
from app.goal_completion_store import GoalCompletionStore
from app.goal_metrics import collect_baseline
from app.workflow_readiness import _GATE_CACHE, build_readiness
from test_vehicle_workflow import setup as vehicle_setup


def test_fl01_flag_off_generation_survives_hook_write_failure(tmp_path, caplog):
    manager, pid = vehicle_setup(tmp_path)
    with patch("app.goal_completion_hooks.enabled", return_value=True), patch(
        "app.goal_completion_hooks.activate", side_effect=sqlite3.OperationalError("readonly")
    ):
        assert after_generate(manager, pid) is None
    assert "goal_completion: after_generate failed" in caplog.text


@pytest.mark.parametrize("hook,code", [(before_generate, "RETENTION_FAILED"), (before_approve, "COVERAGE_INCOMPLETE")])
def test_flag_on_business_rejections_are_not_swallowed(tmp_path, hook, code):
    manager, pid = vehicle_setup(tmp_path)
    enable(manager.memory.path, pid)
    target = "app.goal_completion_hooks.ensure_plannable" if hook is before_generate else "app.goal_completion_hooks.ensure_approvable"
    with patch(target, side_effect=ValueError(code + ": rejected")):
        with pytest.raises(ValueError, match=code):
            hook(manager, pid)


def _readiness_patches(counter, hashes=None, achieved=False):
    current = hashes or {
        "contract_hash": "c", "plan_signature": "p", "input_hash": "i",
        "source_hash": "s", "artifact_hash": "a",
    }
    def evaluate(*args, **kwargs):
        counter[0] += 1
        return {"achieved": achieved, "failed_criteria": [], "artifact_class": "provisional"}
    return patch("app.completion_gate._current_hashes", return_value=current), patch("app.completion_gate.evaluate", side_effect=evaluate)


def test_wr03_readiness_gate_is_calculated_once(tmp_path):
    manager, pid = vehicle_setup(tmp_path)
    _GATE_CACHE.clear(); calls = [0]
    a, b = _readiness_patches(calls)
    with a, b:
        build_readiness(manager, pid); build_readiness(manager, pid)
    assert calls[0] == 1


@pytest.mark.parametrize("changed", ["input_hash", "plan_signature", "artifact_hash"])
def test_readiness_cache_recalculates_when_hash_changes(tmp_path, changed):
    manager, pid = vehicle_setup(tmp_path)
    _GATE_CACHE.clear(); calls = [0]
    first = {"contract_hash": "c", "plan_signature": "p", "input_hash": "i", "source_hash": "s", "artifact_hash": "a"}
    second = dict(first); second[changed] += "2"
    with patch("app.completion_gate._current_hashes", side_effect=[first, second]), patch(
        "app.completion_gate.evaluate", side_effect=lambda *a, **k: calls.__setitem__(0, calls[0] + 1) or {"achieved": False}
    ):
        build_readiness(manager, pid); build_readiness(manager, pid)
    assert calls[0] == 2


def test_readiness_cache_key_includes_valid_acceptance_id(tmp_path):
    manager, pid = vehicle_setup(tmp_path)
    _GATE_CACHE.clear(); calls = [0]
    hashes = {"contract_hash": "c", "plan_signature": "p", "input_hash": "i", "source_hash": "s", "artifact_hash": "a"}
    store = GoalCompletionStore(manager.memory.path)
    with patch("app.completion_gate._current_hashes", return_value=hashes), patch(
        "app.completion_gate.evaluate", side_effect=lambda *a, **k: calls.__setitem__(0, calls[0] + 1) or {"achieved": calls[0] > 1}
    ):
        assert build_readiness(manager, pid)["gate"]["achieved"] is False
        store.record_acceptance(pid, **hashes, accepted_by="Human")
        assert build_readiness(manager, pid)["gate"]["achieved"] is True
    assert calls[0] == 2


def test_readiness_gate_error_is_visible_and_never_achieved(tmp_path, caplog):
    manager, pid = vehicle_setup(tmp_path)
    _GATE_CACHE.clear()
    with patch("app.completion_gate._current_hashes", side_effect=RuntimeError("gate broke")):
        row = build_readiness(manager, pid)
    assert row["gate"]["achieved"] is False
    assert "gate broke" in row["gate"]["error"]
    assert "readiness gate evaluation failed" in caplog.text


def test_goal_states_not_created_but_existing_row_is_preserved(tmp_path):
    # Phase 2-A recreates goal_states with the canonical schema. The 1.5-C
    # "do not create" guarantee is replaced by "do not drop existing rows".
    memory = tmp_path / "memory.sqlite3"
    store = GoalCompletionStore(memory)
    with store.connect() as db:
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "goal_states" in names
        db.execute(
            "INSERT INTO goal_states(project_id,state,updated_at) VALUES(?,?,?)",
            ("p1", "draft", "now"),
        )
    GoalCompletionStore(memory)
    with store.connect() as db:
        assert db.execute("SELECT state FROM goal_states WHERE project_id='p1'").fetchone()[0] == "draft"


def test_collect_baseline_unknowns_are_none_and_read_only(tmp_path):
    manager, pid = vehicle_setup(tmp_path)
    db = GoalCompletionStore(manager.memory.path)
    before = db.path.read_bytes()
    with patch("app.goal_metrics.evaluate", return_value={"achieved": False, "criteria": []}), patch(
        "app.goal_metrics.applicable", return_value=False
    ):
        result = collect_baseline(manager, pid)
    after = db.path.read_bytes()
    assert result["unresolved_reads"] is None
    assert result["unresolved_allocations"] is None
    assert result["stop_count"] is None
    assert result["provisional_artifact"] is None
    assert before == after


@pytest.mark.asyncio
async def test_goal_metrics_api_responds(tmp_path, monkeypatch):
    import app.web as web
    manager, pid = vehicle_setup(tmp_path)
    monkeypatch.setattr(web, "memory", manager.memory)
    monkeypatch.setattr(web, "orchestrator", manager)
    with patch("app.goal_metrics.evaluate", return_value={"achieved": False, "criteria": []}):
        result = await web.get_goal_metrics(pid)
    assert result["project_id"] == pid
    assert "gate" in result


def test_flag_on_approval_check_error_fails_closed(tmp_path):
    manager, pid = vehicle_setup(tmp_path)
    enable(manager.memory.path, pid)
    with patch("app.goal_contract.preview", side_effect=sqlite3.OperationalError("readonly")):
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            before_approve(manager, pid)
