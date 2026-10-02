from types import SimpleNamespace
from unittest.mock import patch, AsyncMock
import pytest

from app.safe_auto_resume import enabled, records, resume, set_enabled


class Memory:
    def __init__(self, path, tasks): self.path, self.tasks = path, tasks
    def get_mission(self, _pid): return {"status": "ready", "tasks": self.tasks}


def task(key, deps=None, title="local"):
    return {"id": key, "task_key": key, "depends_on": deps or [], "title": title,
            "description": "ローカル集計", "acceptance_criteria": "成果物", "mode": "local", "status": "pending"}


def manager(tmp_path, tasks): return SimpleNamespace(memory=Memory(tmp_path / "memory.db", tasks))


@pytest.fixture(autouse=True)
def feature_flag(monkeypatch):
    monkeypatch.setenv("LOCALSAPORTER_AUTO_RESUME_AVAILABLE", "1")


@pytest.fixture
def approved():
    with patch("app.safe_auto_resume.plan_snapshot", return_value=({}, "sig")), \
         patch("app.safe_auto_resume.ReviewStore.get", return_value={"status": "passed"}), \
         patch("app.safe_auto_resume.development_blockers", return_value=[]), \
         patch("app.safe_auto_resume.issues_for", return_value=[]), \
         patch("app.safe_auto_resume.require_review", return_value=None):
        yield


@pytest.mark.asyncio
async def test_default_disabled_and_human_wait_does_not_block_independent(tmp_path, approved):
    tasks = [task("human", title="顧客へ公開"), task("safe")]
    m = manager(tmp_path, tasks)
    assert enabled(m, "p") is False
    assert (await resume(m, "p", lambda *_: None))["status"] == "disabled"
    set_enabled(m, "p", True)
    called = []
    row = await resume(m, "p", lambda t, key: called.append(t["task_key"]) or
                       {"artifact_hash": "h", "evidence": [{"path": "result.md"}]})
    assert called == ["safe"]
    assert row["waiting_human"] == ["human"]


@pytest.mark.asyncio
async def test_restart_skips_same_completed_idempotency_key(tmp_path, approved):
    m = manager(tmp_path, [task("safe")]); set_enabled(m, "p", True)
    calls = []
    executor = lambda t, key: calls.append(key) or {"artifact_hash": "hash", "evidence": ["proof"]}
    await resume(m, "p", executor)
    await resume(m, "p", executor)
    assert len(calls) == 1
    row = records(m, "p")[0]
    assert row["input_hash"] and row["artifact_hash"] and row["run_id"] and row["idempotency_key"]


@pytest.mark.asyncio
async def test_no_evidence_is_not_completed(tmp_path, approved):
    m = manager(tmp_path, [task("safe")]); set_enabled(m, "p", True)
    await resume(m, "p", lambda *_: {"result": "claimed complete"})
    row = records(m, "p")[0]
    assert row["status"] == "failed"
    assert row["artifact_hash"] == "" and row["evidence"] == ""


@pytest.mark.asyncio
async def test_retry_limit_and_connection_content_separation(tmp_path, approved):
    m = manager(tmp_path, [task("net"), task("bad")]); set_enabled(m, "p", True)
    counts = {"net": 0, "bad": 0}
    def fail(t, _key):
        counts[t["task_key"]] += 1
        if t["task_key"] == "net": raise ConnectionError("connection timeout")
        raise ValueError("content invalid")
    await resume(m, "p", fail, retry_limit=2)
    await resume(m, "p", fail, retry_limit=2)
    await resume(m, "p", fail, retry_limit=2)
    assert counts == {"net": 2, "bad": 2}
    rows = {x["task_key"]: x for x in records(m, "p")}
    assert rows["net"]["retry_count"] == 2 and rows["net"]["failure_kind"] == "connection"
    assert rows["bad"]["retry_count"] == 2 and rows["bad"]["failure_kind"] == "content"


@pytest.mark.asyncio
async def test_untrusted_default_executor_is_never_called(tmp_path, approved):
    current = task("safe")
    class ExistingManager:
        def __init__(self): self.memory = Memory(tmp_path / "existing.db", [current])
        async def _execute_task(self, _pid, _tid):
            raise AssertionError("untrusted task must not run")
    m = ExistingManager(); set_enabled(m, "p", True)
    row = await resume(m, "p")
    assert row["executed"] == [] and row["waiting_human"] == ["safe"]
    assert records(m, "p") == []


@pytest.mark.asyncio
async def test_failed_default_executor_cannot_retry_without_review(tmp_path, approved):
    current = task("safe")
    m = manager(tmp_path, [current]); set_enabled(m, "p", True)
    with patch("app.safe_auto_resume._local_safe", return_value=True), \
         patch("app.safe_auto_resume._execute_trusted_document", side_effect=ValueError("artifact failed")) as run:
        first = await resume(m, "p")
        second = await resume(m, "p")
    assert first["failed"] == ["safe"]
    assert second["failed"] == ["safe"]
    assert run.call_count == 1
    assert records(m, "p")[0]["retry_count"] == 2


@pytest.mark.asyncio
async def test_no_skipped_dependency_and_no_sidecar_on_read(tmp_path, approved):
    pending = task("child", deps=["parent"])
    skipped = task("parent")
    skipped["status"] = "skipped"
    m = manager(tmp_path, [skipped, pending])
    assert enabled(m, "p") is False
    assert records(m, "p") == []
    assert not (tmp_path / "memory.db.auto_resume.sqlite3").exists()
    set_enabled(m, "p", True)
    called = []
    row = await resume(m, "p", lambda task, key: called.append(task["task_key"]))
    assert called == [] and row["executed"] == []


@pytest.mark.asyncio
async def test_real_document_path_requires_persisted_artifact(tmp_path):
    from app.safe_auto_resume import _execute_trusted_document
    artifact = tmp_path / "report.md"
    current = task("safe")
    class RealMemory(Memory):
        def update_task(self, _tid, status, result="", error=""):
            current.update(status=status, result=result, error=error)
        def add_event(self, *args, **kwargs): pass
        def get_project(self, _pid): return {"workspace_path": "projects/p"}
    class Workspace:
        def resolve_file(self, *_args, **_kwargs): return None, None, artifact
    m = SimpleNamespace(memory=RealMemory(tmp_path / "real.db", [current]), workspace=Workspace())
    async def produce(*_args):
        artifact.write_text("verified document", encoding="utf-8")
        return "generated and verified"
    with patch("app.safe_auto_resume._local_safe", return_value=True), \
         patch("app.safe_auto_resume._approved", return_value=True), \
         patch("app.safe_auto_resume.plan_snapshot", return_value=({}, "sig")), \
         patch("app.safe_auto_resume.contract_of", return_value={"outputs": [{"path": "report.md"}]}), \
         patch("app.safe_auto_resume.execute_upgraded", side_effect=produce):
        evidence = await _execute_trusted_document(m, "p", current, "sig")
    assert current["status"] == "completed"
    assert evidence["artifact_hash"] and evidence["evidence"][0]["size"] > 0
    assert evidence["evidence"][0]["path"] == "report.md"


@pytest.mark.asyncio
async def test_real_document_path_missing_artifact_needs_review(tmp_path):
    from app.safe_auto_resume import _execute_trusted_document
    current = task("safe")
    class RealMemory(Memory):
        def update_task(self, _tid, status, result="", error=""):
            current.update(status=status, result=result, error=error)
        def add_event(self, *args, **kwargs): pass
        def get_project(self, _pid): return {"workspace_path": "projects/p"}
    class Workspace:
        def resolve_file(self, *_args, **_kwargs):
            raise FileNotFoundError("report.md")
    m = SimpleNamespace(memory=RealMemory(tmp_path / "missing.db", [current]), workspace=Workspace())
    with patch("app.safe_auto_resume._local_safe", return_value=True), \
         patch("app.safe_auto_resume._approved", return_value=True), \
         patch("app.safe_auto_resume.plan_snapshot", return_value=({}, "sig")), \
         patch("app.safe_auto_resume.contract_of", return_value={"outputs": [{"path": "report.md"}]}), \
         patch("app.safe_auto_resume.execute_upgraded", new_callable=AsyncMock, return_value="claimed"):
        with pytest.raises(FileNotFoundError):
            await _execute_trusted_document(m, "p", current, "sig")
    assert current["status"] == "needs_review"


def test_atomic_claim_and_revision_blocker(tmp_path, approved):
    from app.safe_auto_resume import _claim, _approved
    m = manager(tmp_path, [task("safe")])
    assert _claim(m, "p", "safe", "hash", "idem", 2) is not None
    assert _claim(m, "p", "safe", "hash", "idem", 2) is None
    def get(_self, _pid, kind, _sig):
        return {"blockers": [{"disposition": "unresolved"}]} if kind == "revision" else {"status": "passed"}
    with patch("app.safe_auto_resume.ReviewStore.get", get):
        assert _approved(m, "p", "sig", m.memory.get_mission("p")) is False


def test_document_allowlist_requires_enforced_contract(tmp_path):
    import json
    from app.safe_auto_resume import _local_safe
    from app.structured_planning import SCHEMA
    t = task("safe")
    t["acceptance_criteria"] = json.dumps({"schema": SCHEMA, "outputs": [{"path": "result/report.md"}]})
    m = manager(tmp_path, [t])
    with patch("app.safe_auto_resume.configured_mode", return_value="enforce"), \
         patch("app.safe_auto_resume.UpgradeStore.extension", return_value={"execution_strategy": "two-stage-v1"}):
        assert _local_safe(m, "p", t, {"plan_version": 1}) is True
        t["acceptance_criteria"] = json.dumps({"schema": SCHEMA, "outputs": [{"path": "result/report.md"}],
                                                 "action_requirements": [{"kind": "external"}]})
        assert _local_safe(m, "p", t, {"plan_version": 1}) is False
