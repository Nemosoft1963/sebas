import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.goal_completion_flag import enable
from app.goal_review import ReviewStore, begin_job
from app.memory.short_term import ShortTermMemory
from app.restart_convergence import ORPHAN_ERROR, reconcile_after_restart


def project(memory, name, flag=True):
    pid = memory.create_project(name)["id"]
    memory.save_mission(pid, "goal", "criteria", "", False, [])
    if flag:
        enable(memory.path, pid)
    return pid


def task(memory, pid, status="pending"):
    mission = memory.replace_plan(pid, "plan", [{"task_key": "one", "title": "one"}])
    tid = mission["tasks"][0]["id"]
    if status != "pending":
        memory.update_task(tid, status)
    return tid


def test_rc_01_jobs_need_attention_unless_completion_is_proven_and_can_retry(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db"); pid = project(memory, "p")
    store = ReviewStore(memory.path)
    orphan = begin_job(store, pid, "propose", "same-key")
    proven = begin_job(store, pid, "review_plan", "other-key", extra={"plan_signature": "sig"})
    store.put(pid, "plan", "sig", {"status": "passed", "job_id": proven["id"]})
    row = store.get(pid, "job", proven["id"]); row["last_completed_stage"] = "external_review"
    store.put(pid, "job", proven["id"], row)
    result = reconcile_after_restart(memory)
    assert result["jobs_needs_attention"] == 1 and result["jobs_succeeded"] == 1
    stopped = store.get(pid, "job", orphan["id"])
    assert stopped["status"] == "needs_attention" and stopped["blocking_error"] == ORPHAN_ERROR
    assert store.get(pid, "job", proven["id"])["status"] == "succeeded"
    assert begin_job(store, pid, "propose", "same-key")["status"] == "generating"


def test_rc_02_running_plan_returns_to_unverified(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db"); pid = project(memory, "p")
    store = ReviewStore(memory.path); store.put(pid, "plan", "sig", {"status": "running"})
    reconcile_after_restart(memory)
    assert store.get(pid, "plan", "sig")["status"] == "unverified"
    assert store.get(pid, "plan", "sig")["status"] not in {"verified", "passed"}


def test_rc_03_running_mission_pauses_without_changing_tasks(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db"); pid = project(memory, "p")
    tid = task(memory, pid, "pending")
    memory.set_mission_status(pid, "running")
    before = memory.get_mission(pid)["tasks"]
    reconcile_after_restart(memory)
    mission = memory.get_mission(pid)
    assert mission["status"] == "paused" and mission["status"] not in {"completed", "achieved"}
    assert [(x["id"], x["status"]) for x in mission["tasks"]] == [(x["id"], x["status"]) for x in before]
    assert any(e["kind"] == "restart_reconciled" for e in mission["events"])


def test_rc_04_flag_off_is_unchanged_and_flag_on_converges(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    off = project(memory, "off", False); on = project(memory, "on", True)
    store = ReviewStore(memory.path)
    store.put(off, "job", "j-off", {"id": "j-off", "status": "running", "idempotency_key": "off"})
    store.put(on, "job", "j-on", {"id": "j-on", "status": "running", "idempotency_key": "on"})
    before = (memory.get_mission(off), store.get(off, "job", "j-off"))
    reconcile_after_restart(memory)
    assert (memory.get_mission(off), store.get(off, "job", "j-off")) == before
    assert store.get(on, "job", "j-on")["status"] == "needs_attention"


def test_rc_05_idempotent_audited_and_one_project_error_isolated(tmp_path, caplog):
    memory = ShortTermMemory(tmp_path / "memory.db")
    bad = project(memory, "bad"); good = project(memory, "good")
    store = ReviewStore(memory.path)
    store.put(bad, "job", "bad-job", {"id": "bad-job", "status": "running"})
    store.put(good, "job", "good-job", {"id": "good-job", "status": "running"})
    original = store.list
    def flaky(pid, kind):
        if pid == bad:
            raise RuntimeError("injected")
        return original(pid, kind)
    with patch.object(store, "list", side_effect=flaky):
        with patch("app.restart_convergence.ReviewStore", return_value=store):
            first = reconcile_after_restart(memory)
    assert first["errors"] and store.get(good, "job", "good-job")["status"] == "needs_attention"
    assert "restart convergence failed" in caplog.text
    events = memory.get_mission(good)["events"]
    assert any(e["kind"] == "restart_convergence" for e in events)
    second = reconcile_after_restart(memory)
    assert second["changed"] == 1  # the previously isolated bad project is now safely converged
    third = reconcile_after_restart(memory)
    assert third["changed"] == 0


@pytest.mark.asyncio
async def test_rc_06_lifespan_calls_reconcile_and_failure_does_not_abort_startup(monkeypatch, tmp_path):
    import app.web as web
    class DummyTask:
        def cancel(self): pass
        def done(self): return True
        def __await__(self):
            async def value(): return None
            return value().__await__()
    class Publisher:
        config = SimpleNamespace(ready=False)
        async def close(self): pass
    class Manager:
        async def shutdown(self): pass
    monkeypatch.setattr(web, "DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(web, "WORKSPACE_ROOT", tmp_path / "workspace")
    monkeypatch.setattr(web, "ProjectOrchestrator", lambda *a, **k: Manager())
    monkeypatch.setattr(web, "GoogleFormsPublisher", lambda *a, **k: Publisher())
    monkeypatch.setattr(web.GooglePremarketingConfig, "from_env", classmethod(lambda cls: None))
    monkeypatch.setattr(web.asyncio, "create_task", lambda coro: (coro.close(), DummyTask())[1])
    monkeypatch.setattr(web.asyncio, "gather", AsyncGather())
    with patch("app.restart_convergence.reconcile_after_restart", side_effect=RuntimeError("boom")) as called:
        async with web.lifespan(web.app):
            pass
    called.assert_called_once()


class AsyncGather:
    def __call__(self, *args, **kwargs):
        async def done(): return []
        return done()
