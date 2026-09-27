"""NAC safety contract tests (synthetic readiness and temporary SQLite)."""
from types import SimpleNamespace
from unittest.mock import patch
import json
import pytest

from app.goal_completion_flag import enable
from app.goal_completion_store import GoalCompletionStore
import app.next_action_controller as nac


class Memory:
    def __init__(self, path): self.path = path
    def get_mission(self, _): return {"status": "planning", "tasks": []}


def manager(tmp_path): return SimpleNamespace(memory=Memory(tmp_path / "memory.db"))


def ready(action_id="prepare", cls="local_safe", auto=True, allowed=True, criterion="c1"):
    return {"next_action": {"id": action_id, "label": action_id, "endpoint": "/x", "class": cls,
                            "auto_executable": auto},
            "allowed_actions": {action_id: {"allowed": allowed, "reason": "denied" if not allowed else ""}},
            "gate": {"failed_criteria": [criterion] if criterion else []}, "stop_reason": "stop"}


def state(*_): return {"state": "planned"}


def test_nac_01_compute_read_only_and_unsafe_classes_blocked(tmp_path):
    m = manager(tmp_path); store = GoalCompletionStore(m.memory.path); before = store.path.read_bytes()
    for cls in ("human_fact", "human_approval", "external", "development"):
        with patch.object(nac, "build_readiness", return_value=ready("x", cls)), patch.object(nac, "read_goal_state", state):
            row = nac.compute(m, "p"); assert row["blocked"] and not row["executable"]
    assert store.path.read_bytes() == before


def test_nac_02_refuses_unsafe_and_unregistered(tmp_path):
    m = manager(tmp_path); enable(m.memory.path, "p")
    for cls in ("human_fact", "human_approval", "external", "development"):
        with patch.object(nac, "build_readiness", return_value=ready("x", cls)), patch.object(nac, "read_goal_state", state):
            with pytest.raises(nac.NextActionRefused): nac.execute(m, "p", cls)
    with patch.object(nac, "build_readiness", return_value=ready("missing")), patch.object(nac, "read_goal_state", state):
        with pytest.raises(nac.NextActionRefused): nac.execute(m, "p", "missing")


def test_nac_03_idempotency_and_empty_key(tmp_path):
    m = manager(tmp_path); enable(m.memory.path, "p"); calls=[]
    nac.EXECUTORS["prepare"] = lambda *args: calls.append(1) or {"ok": True}
    with patch.object(nac, "build_readiness", return_value=ready()), patch.object(nac, "read_goal_state", state):
        first=nac.execute(m,"p","k"); second=nac.execute(m,"p","k"); nac.execute(m,"p","k2")
        assert first == second and len(calls)==2
        with pytest.raises(nac.NextActionRefused) as exc: nac.execute(m,"p","")
        assert exc.value.code == "EMPTY_IDEMPOTENCY_KEY"


def test_nac_04_flag_off_refuses_execute_and_chain_but_compute_works(tmp_path):
    m=manager(tmp_path)
    with patch.object(nac,"build_readiness",return_value=ready()), patch.object(nac,"read_goal_state",state):
        assert nac.compute(m,"p")["action_id"]=="prepare"
        for chain in (False,True):
            with pytest.raises(nac.NextActionRefused) as exc: nac.execute(m,"p","k",chain=chain)
            assert exc.value.code=="FLAG_OFF"


def test_nac_05_chain_only_local_safe_then_human(tmp_path):
    m=manager(tmp_path); enable(m.memory.path,"p"); calls=[]
    seq=[ready("prepare"),ready("confirm","human_fact",False)]
    def build(*_): return seq[min(len(calls),2)]
    nac.EXECUTORS["prepare"]=lambda *args: calls.append(1) or {}
    with patch.object(nac,"build_readiness",side_effect=build), patch.object(nac,"read_goal_state",state):
        row=nac.run_chain(m,"p","chain"); assert row["stopped_reason"]=="human_required" and len(row["steps"])==1


def test_nac_06_repeat_and_max_steps(tmp_path):
    m=manager(tmp_path); enable(m.memory.path,"p"); nac.reset_budget(m,"p","c1")
    ids=iter(["b","c","d","e","f"]); current={"id":"a"}
    def executor(*_): current["id"]=next(ids); return {}
    nac.EXECUTORS.update({x:executor for x in "abcdef"})
    def build(*_): return ready(current["id"])
    with patch.object(nac,"build_readiness",side_effect=build), patch.object(nac,"read_goal_state",state), patch.object(nac.RecoveryBudget,"recover",return_value="x"):
        row=nac.run_chain(m,"p","max"); assert row["stopped_reason"]=="max_steps" and len(row["steps"])==5
    nac.reset_budget(m,"p","c1"); nac.EXECUTORS["a"]=lambda *_:{}
    with patch.object(nac,"build_readiness",return_value=ready("a")), patch.object(nac,"read_goal_state",state):
        assert nac.run_chain(m,"p","repeat")["stopped_reason"]=="repeat"


def test_nac_07_budget_shared_and_recovery_stopped(tmp_path):
    m=manager(tmp_path); enable(m.memory.path,"p"); nac.reset_budget(m,"p","c1"); nac.EXECUTORS["a"]=lambda *_:{}
    with patch.object(nac,"build_readiness",return_value=ready("a")), patch.object(nac,"read_goal_state",state):
        nac.run_chain(m,"p","one")
        row=nac.run_chain(m,"p","two")
        assert row["stopped_reason"]=="budget" and row["needs_approval"]


def test_nac_08_executor_failure_is_persisted_and_not_retried(tmp_path):
    m=manager(tmp_path); enable(m.memory.path,"p"); calls=[]
    def fail(*_): calls.append(1); raise RuntimeError("boom")
    nac.EXECUTORS["prepare"]=fail
    with patch.object(nac,"build_readiness",return_value=ready()), patch.object(nac,"read_goal_state",state):
        assert nac.execute(m,"p","fail")["status"]=="failed"
        assert nac.execute(m,"p","fail")["status"]=="failed" and len(calls)==1
    assert GoalCompletionStore(m.memory.path).get_nac_execution("p","fail")["status"]=="failed"


def test_nac_09_ledger_is_append_only_and_initializes_old_db(tmp_path):
    m=manager(tmp_path); store=GoalCompletionStore(m.memory.path)
    store.append_nac_execution("p","k","a","succeeded",{"n":1})
    with pytest.raises(ValueError): store.append_nac_execution("p","k","a","succeeded",{"n":2})
    assert store.get_nac_execution("p","k")["result"]=={"n":1}
    with store.connect() as db: assert db.execute("SELECT COUNT(*) FROM nac_executions").fetchone()[0]==1
