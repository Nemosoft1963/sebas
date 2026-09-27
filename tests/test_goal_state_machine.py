import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.goal_completion_flag import enable
from app.goal_completion_store import GoalCompletionStore
from app.goal_state_machine import (
    current_state,
    list_events,
    settle_achieved,
    transition,
)
from app.memory.short_term import ShortTermMemory


def _store(tmp_path):
    return GoalCompletionStore(tmp_path / "memory.sqlite3")


def _memory_project(tmp_path, *, flag_on=False):
    memory = ShortTermMemory(tmp_path / "memory.sqlite3")
    project = memory.create_project("goal-sm")
    pid = project["id"]
    memory.save_mission(pid, "目標", "", "", False, [])
    if flag_on:
        enable(memory.path, pid)
    return memory, pid


def _event_count(store, project_id):
    with store.connect() as db:
        return db.execute(
            "SELECT COUNT(*) FROM goal_state_events WHERE project_id=?",
            (project_id,),
        ).fetchone()[0]


def _event_row(store, event_id):
    with store.connect() as db:
        return db.execute(
            "SELECT * FROM goal_state_events WHERE id=?",
            (event_id,),
        ).fetchone()


def test_gt_sm_01_illegal_transitions_are_rejected_and_legal_ones_record_events(tmp_path):
    store = _store(tmp_path)
    first = transition(store, "p1", "draft", reason="start", actor="test")
    assert first["state"] == "draft"
    assert first["changed"] is True
    assert _event_count(store, "p1") == 1
    with pytest.raises(ValueError, match="ILLEGAL_TRANSITION"):
        transition(store, "p1", "achieved", reason="skip", actor="test")
    with pytest.raises(ValueError, match="ILLEGAL_TRANSITION"):
        transition(store, "p1", "verified", reason="skip", actor="test")
    assert current_state(store, "p1") == "draft"
    assert _event_count(store, "p1") == 1
    second = transition(store, "p1", "planned", reason="plan", actor="test")
    assert second["state"] == "planned"
    assert _event_count(store, "p1") == 2


def test_gt_sm_02_achieved_requires_live_gate_and_can_leave_on_drift(tmp_path):
    manager = SimpleNamespace(memory=SimpleNamespace(path=tmp_path / "memory.sqlite3"))
    store = GoalCompletionStore(manager.memory.path)
    transition(store, "p1", "produced", reason="artifact", actor="test")
    with pytest.raises(ValueError, match="ILLEGAL_TRANSITION"):
        transition(store, "p1", "achieved", reason="direct", actor="test")
    untestable = {
        "achieved": False, "human_accepted": False,
        "criteria": [{"status": "UNTESTABLE", "criterion_id": "C01"}],
    }
    with patch("app.completion_gate.evaluate", return_value=untestable):
        with pytest.raises(ValueError, match="ACHIEVED_REQUIRES_GATE"):
            settle_achieved(manager, "p1")
    no_accept = {
        "achieved": False, "human_accepted": False,
        "criteria": [{"status": "PASS", "criterion_id": "C01"}],
    }
    with patch("app.completion_gate.evaluate", return_value=no_accept):
        with pytest.raises(ValueError, match="ACHIEVED_REQUIRES_GATE"):
            settle_achieved(manager, "p1")
    drift = {
        "achieved": False, "human_accepted": False,
        "criteria": [{"status": "PASS", "criterion_id": "C01"}],
        "reason_code": "HUMAN_ACCEPTANCE_HASH_DRIFT:artifact_hash",
    }
    with patch("app.completion_gate.evaluate", return_value=drift):
        with pytest.raises(ValueError, match="ACHIEVED_REQUIRES_GATE"):
            settle_achieved(manager, "p1")
    passing = {
        "achieved": True, "human_accepted": True,
        "criteria": [{"status": "PASS", "criterion_id": "C01"}],
        "contract_hash": "c", "plan_signature": "p", "artifact_hash": "a",
    }
    with patch("app.completion_gate.evaluate", return_value=passing):
        settled = settle_achieved(manager, "p1")
    assert settled["state"] == "achieved"
    back = transition(store, "p1", "produced", reason="hash_drift", actor="test")
    assert back["state"] == "produced"
    with patch("app.completion_gate.evaluate", return_value=passing):
        settle_achieved(manager, "p1")
    verified = transition(store, "p1", "verified", reason="hash_drift", actor="test")
    assert verified["state"] == "verified"
    history = list_events(store, "p1", 20)
    assert any(item["to_state"] == "achieved" for item in history)
    assert any(item["from_state"] == "achieved" and item["to_state"] == "produced" for item in history)
    assert any(item["from_state"] == "achieved" and item["to_state"] == "verified" for item in history)


def test_gt_sm_03_events_are_append_only_atomic_and_idempotent(tmp_path):
    store = _store(tmp_path)
    first = transition(store, "p1", "draft", reason="start", actor="test")
    original = _event_row(store, first["last_event_id"])
    second = transition(store, "p1", "planned", reason="plan", actor="test")
    assert _event_row(store, first["last_event_id"]) == original
    assert _event_count(store, "p1") == 2
    again = transition(store, "p1", "planned", reason="repeat", actor="test")
    assert again["changed"] is False
    assert again["state"] == "planned"
    assert _event_count(store, "p1") == 2
    assert _event_row(store, first["last_event_id"]) == original
    assert _event_row(store, second["last_event_id"])[0] == second["last_event_id"]

    with store.connect() as db:
        db.execute(
            """CREATE TRIGGER fail_goal_states_write
               BEFORE UPDATE ON goal_states
               BEGIN SELECT RAISE(ABORT, 'injected failure'); END"""
        )
    with pytest.raises(sqlite3.Error, match="injected failure"):
        transition(store, "p1", "approved", reason="approve", actor="test")
    assert current_state(store, "p1") == "planned"
    assert _event_count(store, "p1") == 2
    assert _event_row(store, first["last_event_id"]) == original


def test_gt_sm_04_dual_write_maps_completed_only_to_produced(tmp_path):
    memory, pid = _memory_project(tmp_path, flag_on=True)
    store = GoalCompletionStore(memory.path)
    memory.set_mission_status(pid, "planning", "計画中", "status")
    assert memory.get_mission(pid)["status"] == "planning"
    assert current_state(store, pid) == "draft"
    memory.set_mission_status(pid, "ready", "承認待ち", "status")
    assert current_state(store, pid) == "planned"
    memory.set_mission_status(pid, "running", "実行", "status")
    assert current_state(store, pid) == "running"
    memory.set_mission_status(pid, "completed", "暫定完了", "status")
    mission = memory.get_mission(pid)
    assert mission["status"] == "completed"
    assert current_state(store, pid) == "produced"
    assert current_state(store, pid) not in {"achieved", "verified"}
    events = list_events(store, pid, 20)
    assert any(item["to_state"] == "produced" and item["legacy_status"] == "completed" for item in events)
    assert all(item["to_state"] not in {"achieved", "verified"} for item in events)


def test_gt_sm_05_flag_off_writes_nothing_to_goal_ledger(tmp_path):
    memory, pid = _memory_project(tmp_path, flag_on=False)
    store = GoalCompletionStore(memory.path)
    before = store.path.read_bytes()
    memory.set_mission_status(pid, "running", "実行", "status")
    after = store.path.read_bytes()
    assert before == after
    assert memory.get_mission(pid)["status"] == "running"
    assert current_state(store, pid) == ""
    assert _event_count(store, pid) == 0


def test_gt_sm_06_hook_failure_does_not_break_legacy_write(tmp_path, caplog):
    memory, pid = _memory_project(tmp_path, flag_on=True)
    with patch(
        "app.goal_state_machine.observe_legacy_status",
        side_effect=sqlite3.OperationalError("readonly"),
    ):
        result = memory.set_mission_status(pid, "running", "実行", "status")
    assert result["status"] == "running"
    assert memory.get_mission(pid)["status"] == "running"
    assert "observe_legacy_status failed" in caplog.text


def test_gt_sm_07_unmapped_and_illegal_mapping_keep_legacy_path(tmp_path, caplog):
    memory, pid = _memory_project(tmp_path, flag_on=True)
    store = GoalCompletionStore(memory.path)
    memory.set_mission_status(pid, "planning", "計画", "status")
    assert current_state(store, pid) == "draft"
    memory.set_mission_status(pid, "mystery", "不明", "status")
    assert memory.get_mission(pid)["status"] == "mystery"
    assert current_state(store, pid) == "draft"
    assert "unmapped legacy status" in caplog.text
    memory.set_mission_status(pid, "cancelled", "中止", "status")
    assert current_state(store, pid) == "cancelled"
    caplog.clear()
    memory.set_mission_status(pid, "running", "再開", "status")
    assert memory.get_mission(pid)["status"] == "running"
    assert current_state(store, pid) == "cancelled"
    rejected = [
        item for item in list_events(store, pid, 20)
        if item["reason"] == "rejected"
    ]
    assert rejected
    assert rejected[0]["to_state"] == "running"
    assert "rejected mapped transition" in caplog.text


@pytest.mark.asyncio
async def test_gt_sm_08_goal_state_api_maps_unrecorded_without_write(tmp_path, monkeypatch):
    import app.web as web

    memory, pid = _memory_project(tmp_path, flag_on=False)
    memory.set_mission_status(pid, "planning", "計画", "status")
    ledger = tmp_path / "goal_completion.sqlite3"
    before = ledger.read_bytes() if ledger.exists() else None
    monkeypatch.setattr(web, "memory", memory)
    monkeypatch.setattr(web, "orchestrator", SimpleNamespace(memory=memory))
    body = await web.get_goal_state(pid)
    assert body["recorded"] is False
    assert body["mapped"] is True
    assert body["state"] == "draft"
    assert body["legacy_status"] == "planning"
    assert body["events"] == []
    assert "gate" in body
    assert body["gate"]["achieved"] is False
    after = ledger.read_bytes() if ledger.exists() else None
    assert before == after

    enable(memory.path, pid)
    memory.set_mission_status(pid, "ready", "計画承認", "status")
    recorded = await web.get_goal_state(pid)
    assert recorded["recorded"] is True
    assert recorded["state"] == "planned"
    assert recorded["legacy_status"] == "ready"
    assert recorded["events"]
    assert recorded["mapped"] is True


def test_gt_sm_09_legacy_goal_states_schema_is_migrated_without_dropping_rows(tmp_path):
    ledger = tmp_path / "goal_completion.sqlite3"
    db = sqlite3.connect(ledger)
    db.execute("CREATE TABLE goal_states(project_id TEXT PRIMARY KEY, state TEXT)")
    db.execute("INSERT INTO goal_states VALUES('p1','draft')")
    db.commit()
    db.close()
    store = GoalCompletionStore(tmp_path / "memory.sqlite3")
    with store.connect() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(goal_states)")}
        assert {"project_id", "state", "updated_at", "last_event_id"} <= columns
        assert conn.execute("SELECT state FROM goal_states WHERE project_id='p1'").fetchone()[0] == "draft"
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "goal_state_events" in names
