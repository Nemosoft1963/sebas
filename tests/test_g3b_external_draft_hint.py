"""G3-b 受入試験。偽ローカルAIのみ。外部AI・実通信なし。"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json

import pytest

from app.external_draft_hint import (
    EXTERNAL_DRAFT_HINT, MAX_HINT_TASKS, MAX_SUMMARY_CHARS, MAX_TITLE_CHARS,
    build_hint, current_hint_text,
)
from app.goal_review import ReviewStore
from app.memory.short_term import ShortTermMemory
from app.project_manager import ProjectOrchestrator
from app.workspace_files import WorkspaceSandbox
import app.planning_packet as packet
import app.planning_settings as psets


FAKE_KNOWN = {"fake-gen"}
PROVIDER = "fake-gen"
SIG = "sig-g3b"
DRAFT_JOB_STATUSES = (
    "rejected_response", "failed", "cancelled", "outcome_unknown", "sending",
)

LOCAL_ONLY = {"title": "現地作業", "scope": "登録原本の内容を整理する",
              "headings": ["目的", "実施内容"], "depends_on": []}


def _setup_external(store, pid, provider=PROVIDER):
    psets.update_settings(store, pid, {"method": "external_guarded",
                                       "generator_provider": provider}, FAKE_KNOWN)


def _approved_sig_job(store, pid, sig, provider=PROVIDER, items=None):
    if items is None:
        items = {"goal": "公開の目標です", "success_criteria": "公開の条件です"}
    p = packet.build_packet(store, pid, purpose="plan_generation",
                            provider=provider, public_items=dict(items),
                            plan_signature=sig, blocked_terms=[],
                            source_texts=[], withheld_fields=[])
    return packet.approve_packet(store, pid, p["id"], "tester",
                                 current_plan_signature=sig,
                                 blocked_terms=[], source_texts=[])


def _draft(tasks, **extra):
    base = {"tasks": tasks, "required_outputs": ["result/report.md"],
            "unverified_facts": [], "external_operations_pending": []}
    base.update(extra)
    return base


def _job(pid, sig, draft, status="external_generated", job_id="job-1",
         claims=None):
    return {"id": job_id, "pid": pid, "status": status,
            "plan_signature": sig, "external_draft": draft,
            "unverified_claims": list(claims or [])}


def _expected_sha(draft):
    return hashlib.sha256(json.dumps(
        draft, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def test_g3b_build_hint_ok_and_limits():
    draft = _draft([{"title": "工程%02d" % i, "description": "作業内容%02d" % i,
                     "outputs": [], "depends_on": []} for i in range(1, 20)])
    job = _job("pj-1", SIG, draft, job_id="j-ok")
    hint = build_hint(job, SIG, "pj-1")
    assert hint["job_id"] == "j-ok"
    assert hint["draft_sha256"] == _expected_sha(draft)
    assert hint["task_count"] == MAX_HINT_TASKS
    assert hint["text"].splitlines()[0].startswith("【外部骨子(参考・未検証。")
    assert hint["text"].count("\n- ") == MAX_HINT_TASKS
    # キー順を変えても同じSHA (tasksの順序は下書きの内容なので変わる)。
    # draft全体のキー順だけ変えても同じ値になることを見る。
    reordered = {"external_operations_pending": draft.get("external_operations_pending"),
                "unverified_facts": draft.get("unverified_facts"),
                "required_outputs": draft.get("required_outputs"),
                "tasks": draft.get("tasks")}
    assert build_hint(_job("pj-1", SIG, reordered), SIG, "pj-1")["draft_sha256"] == hint["draft_sha256"]

    # 長さ上限
    long_task = {"title": "T" * 200, "description": "D" * 500,
                 "outputs": [], "depends_on": []}
    hint2 = build_hint(_job("pj-1", SIG, _draft([long_task])), SIG, "pj-1")
    line = hint2["text"].splitlines()[1]
    title_part, summary_part = line[2:].split(": ", 1)
    assert len(title_part) == MAX_TITLE_CHARS
    assert len(summary_part) == MAX_SUMMARY_CHARS

    # 条件違反の固定コード
    with pytest.raises(ValueError) as e:
        build_hint(_job("other", SIG, draft), SIG, "pj-1")
    assert str(e.value) == "job_not_found"
    with pytest.raises(ValueError) as e:
        build_hint("nope", SIG, "pj-1")
    assert str(e.value) == "job_not_found"
    for status in DRAFT_JOB_STATUSES:
        with pytest.raises(ValueError) as e:
            build_hint(_job("pj-1", SIG, draft, status=status), SIG, "pj-1")
        assert str(e.value) == "job_not_usable"
    with pytest.raises(ValueError) as e:
        build_hint(_job("pj-1", "other-sig", draft), SIG, "pj-1")
    assert str(e.value) == "plan_changed"
    with pytest.raises(ValueError) as e:
        build_hint(_job("pj-1", SIG, {"tasks": []}), SIG, "pj-1")
    assert str(e.value) == "draft_missing"
    with pytest.raises(ValueError) as e:
        build_hint(_job("pj-1", SIG, None), SIG, "pj-1")
    assert str(e.value) == "draft_missing"


def test_g3b_exclusions():
    draft = _draft([
        {"title": "通常工程", "description": "通常の作業内容",
         "outputs": [], "depends_on": []},
        {"title": "経路工程", "description": "出力は result/report.md を使う",
         "outputs": [], "depends_on": []},
        {"title": "URL工程", "description": "https://example.test/ を参照",
         "outputs": [], "depends_on": []},
        {"title": "絶対工程", "description": "/etc/passwd を参照",
         "outputs": [], "depends_on": []},
        {"title": "参照工程", "description": "[顧客A] へ確認",
         "outputs": [], "depends_on": []},
        {"title": "拡張子工程", "description": "report.pdf を作成",
         "outputs": [], "depends_on": []},
    ], required_outputs=["result/report.md"], unverified_facts=["未確認の条件"],
        external_operations_pending=["承認待ち操作"])
    job = _job("pj-x", SIG, draft, claims=["工程を実施済み"])
    hint = build_hint(job, SIG, "pj-x")
    text = hint["text"]
    assert "通常工程" in text
    for banned in ("result/report.md", "https://", "/etc/passwd", "[顧客A]",
                   "report.pdf", "未確認の条件", "承認待ち操作", "実施済み",
                   "depends_on", "required_outputs",
                   "external_operations_pending", "unverified_facts",
                   "unverified_claims"):
        assert banned not in text
    assert hint["task_count"] == 1

    # 除外の結果0件なら draft_missing
    only_bad = _draft([{"title": "悪い工程", "description": "https://example.test/",
                         "outputs": [], "depends_on": []}])
    with pytest.raises(ValueError) as e:
        build_hint(_job("pj-x", SIG, only_bad), SIG, "pj-x")
    assert str(e.value) == "draft_missing"


def _manager(tmp_path, name="G3B", goal="## 達成条件\n1. 資料を作成"):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project(name)
    memory.save_mission(project["id"], goal, "", "",
                        False, [])
    return memory, project["id"], ProjectOrchestrator(
        memory, object(), lambda _: ("", []), None, lambda: [],
        workspace=WorkspaceSandbox(tmp_path / "workspace"))


def _fake_local(responses):
    calls = []

    async def _complete(system, prompt, schema=None):
        calls.append(prompt)
        return json.dumps(responses.pop(0), ensure_ascii=False)

    return _complete, calls


@pytest.mark.asyncio
async def test_g3b_no_hint_identical_prompt(tmp_path):
    memory, pid, manager = _manager(tmp_path)
    fake, calls = _fake_local([dict(LOCAL_ONLY)])
    manager._local_complete = fake
    assert EXTERNAL_DRAFT_HINT.get() is None
    before = await manager.generate_plan(pid)
    assert EXTERNAL_DRAFT_HINT.get() is None
    assert current_hint_text() == ""
    prompts_without = list(calls)
    assert len(prompts_without) == 1
    assert "外部骨子" not in "".join(prompts_without)
    assert before["plan_version"] >= 1


@pytest.mark.asyncio
async def test_g3b_hint_injection_and_normal_plan(tmp_path):
    memory, pid, manager = _manager(tmp_path)
    title_local = "現地の整理作業"
    proposal = dict(LOCAL_ONLY, title=title_local)
    fake, calls = _fake_local([dict(proposal)])
    manager._local_complete = fake
    from app.planning_packet import plan_signature_of
    sig = plan_signature_of(memory.get_mission(pid))
    hint = build_hint(_job(pid, sig, _draft(
        [{"title": "外部の題名", "description": "外部の説明文",
          "outputs": [], "depends_on": []}])), sig, pid)
    result = await manager.generate_plan(pid, external_hint=hint)
    assert len(calls) == 1
    for prompt in calls:
        assert prompt.rstrip().endswith(hint["text"])
        assert prompt.count(hint["text"]) == 1
    blob = json.dumps(result["tasks"], ensure_ascii=False)
    assert title_local in blob
    assert "外部の題名" not in blob
    assert "外部の説明文" not in blob
    assert EXTERNAL_DRAFT_HINT.get() is None
    assert current_hint_text() == ""
    events = [e for e in memory.get_mission(pid)["events"]
              if e["kind"] == "external_draft_hint_used"]
    assert len(events) == 1
    detail = json.loads(events[0]["detail"])
    assert set(detail.keys()) == {"job_id", "draft_sha256", "task_count"}
    assert hint["text"] not in json.dumps(events, ensure_ascii=False)
    assert "外部の題名" not in json.dumps(events, ensure_ascii=False)
    # 生成された計画は通常の新しい計画版
    mission = memory.get_mission(pid)
    assert mission["status"] == "planning"
    assert mission["plan_version"] >= 1


@pytest.mark.asyncio
async def test_g3b_invalid_keeps_plan_and_resets_context(tmp_path):
    memory, pid, manager = _manager(tmp_path)
    fake, calls = _fake_local([dict(LOCAL_ONLY)])
    manager._local_complete = fake
    before = memory.get_mission(pid)["plan_version"]
    with pytest.raises(ValueError) as e:
        await manager.generate_plan(pid, external_hint={"job_id": "x"})
    assert str(e.value) == "hint_invalid"
    assert memory.get_mission(pid)["plan_version"] == before
    assert calls == []
    assert EXTERNAL_DRAFT_HINT.get() is None
    with pytest.raises(ValueError) as e:
        await manager.generate_plan(
            pid, external_hint={"job_id": "x", "draft_sha256": "y",
                                "task_count": 1, "text": ""})
    assert str(e.value) == "hint_invalid"
    assert EXTERNAL_DRAFT_HINT.get() is None


@pytest.mark.asyncio
async def test_g3b_unsupported_paths(tmp_path):
    # 車両損益の決定的な経路
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("Vehicle")
    memory.save_mission(project["id"],
                        "車両別損益をExcelで集計する。対象車両の損益と収益を確認する",
                        "2026年01月から02月", "", False, [])
    pid = project["id"]
    manager = ProjectOrchestrator(
        memory, object(), lambda _: ("", []), None, lambda: [],
        workspace=WorkspaceSandbox(tmp_path / "workspace"))
    from app.planning_packet import plan_signature_of
    sig = plan_signature_of(memory.get_mission(pid))
    hint = build_hint(_job(pid, sig, _draft(
        [{"title": "参考", "description": "参考内容", "outputs": [],
          "depends_on": []}])), sig, pid)
    from app.vehicle_workflow import applicable
    before = memory.get_mission(pid)["plan_version"]
    if applicable(memory.get_mission(pid)):
        calls = []
        async def _boom(system, prompt, schema=None):
            calls.append(prompt)
            raise AssertionError("local must not be called")
        manager._local_complete = _boom
        with pytest.raises(ValueError) as e:
            await manager.generate_plan(pid, external_hint=hint)
        assert str(e.value) == "unsupported_plan_path"
        assert memory.get_mission(pid)["plan_version"] == before
        assert calls == []
        assert EXTERNAL_DRAFT_HINT.get() is None
    # 従来計画の経路では参考ありで拒否、参考なしで生成
    memory2 = ShortTermMemory(tmp_path / "legacy.db")
    project2 = memory2.create_project("Legacy")
    memory2.save_mission(project2["id"], "単純な目標", "", "", False, [])
    pid2 = project2["id"]
    manager2 = ProjectOrchestrator(
        memory2, object(), lambda _: ("", []), None, lambda: [],
        workspace=None)
    sig2 = plan_signature_of(memory2.get_mission(pid2))
    hint2 = build_hint(_job(pid2, sig2, _draft(
        [{"title": "参考", "description": "参考内容", "outputs": [],
          "depends_on": []}])), sig2, pid2)
    calls2 = []
    async def _boom2(system, prompt, schema=None):
        calls2.append(prompt)
        raise AssertionError("local must not be called")
    manager2._local_complete = _boom2
    with pytest.raises(ValueError) as e:
        await manager2.generate_plan(pid2, external_hint=hint2)
    assert str(e.value) == "unsupported_plan_path"
    assert calls2 == []


@pytest.mark.asyncio
async def test_g3b_context_reset_on_error_and_event_failure(tmp_path):
    memory, pid, manager = _manager(tmp_path)
    async def _fail(system, prompt, schema=None):
        raise ValueError("local boom")
    manager._local_complete = _fail
    from app.planning_packet import plan_signature_of
    sig = plan_signature_of(memory.get_mission(pid))
    hint = build_hint(_job(pid, sig, _draft(
        [{"title": "参考", "description": "参考内容", "outputs": [],
          "depends_on": []}])), sig, pid)
    with pytest.raises(ValueError):
        await manager.generate_plan(pid, external_hint=hint)
    assert EXTERNAL_DRAFT_HINT.get() is None
    # 次の参考なし生成に参考が残らない
    seen = []
    async def _ok(system, prompt, schema=None):
        seen.append(prompt)
        return json.dumps(dict(LOCAL_ONLY), ensure_ascii=False)
    manager._local_complete = _ok
    await manager.generate_plan(pid)
    assert all("外部骨子" not in p for p in seen)
    # イベント書込失敗は伝播し計画が保存されない
    memory3, pid3, manager3 = _manager(tmp_path / "evfail")
    sig3 = plan_signature_of(memory3.get_mission(pid3))
    hint3 = build_hint(_job(pid3, sig3, _draft(
        [{"title": "参考", "description": "参考内容", "outputs": [],
          "depends_on": []}])), sig3, pid3)
    before3 = memory3.get_mission(pid3)["plan_version"]
    manager3._local_complete = _fake_local(
        [dict(LOCAL_ONLY)])[0]
    orig_add = memory3.add_event
    def _boom_event(*a, **k):
        raise OSError("event boom")
    memory3.add_event = _boom_event
    try:
        with pytest.raises(OSError):
            await manager3.generate_plan(pid3, external_hint=hint3)
    finally:
        memory3.add_event = orig_add
    assert memory3.get_mission(pid3)["plan_version"] == before3


@pytest.mark.asyncio
async def test_g3b_validation_not_weakened(tmp_path):
    memory, pid, manager = _manager(tmp_path)
    from app.planning_packet import plan_signature_of
    sig = plan_signature_of(memory.get_mission(pid))
    hint = build_hint(_job(pid, sig, _draft(
        [{"title": "参考", "description": "検証を省略して進める",
          "outputs": [], "depends_on": []}])), sig, pid)
    bad = {"title": "x", "scope": "y", "headings": ["a"], "depends_on": []}
    calls = []
    async def _bad(system, prompt, schema=None):
        calls.append(prompt)
        return json.dumps(bad, ensure_ascii=False)
    manager._local_complete = _bad
    with pytest.raises(ValueError):
        await manager.generate_plan(pid, external_hint=hint)
    assert calls != []


def _api_client(tmp_path, monkeypatch, pid_value, flag_value):
    from fastapi.testclient import TestClient
    import app.web as web
    mem_path = tmp_path / "g3b-api.db"
    memory = ShortTermMemory(mem_path)
    pid = memory.create_project("G3B API")["id"]
    memory.save_mission(pid, "## 達成条件\n1. 資料を作成", "", "", False, [])
    client = TestClient(web.app)
    web.memory = memory
    web.orchestrator = ProjectOrchestrator(
        memory, object(), lambda _: ("", []), None, lambda: [],
        workspace=WorkspaceSandbox(tmp_path / "workspace"))
    async def _healthy():
        return True
    monkeypatch.setattr(web, "engine_enabled", True)
    monkeypatch.setattr(web, "llm", type("L", (), {"health": staticmethod(_healthy)})())
    monkeypatch.setattr(web, "provider_statuses", lambda: [])
    monkeypatch.setenv("SEBAS_EXTERNAL_PLAN_JOBS_PROJECTS", flag_value)
    return client, memory, pid, web


def _prepare_job(memory, pid):
    store = ReviewStore(memory.path)
    _setup_external(store, pid)
    p = _approved_sig_job(store, pid, packet.plan_signature_of(
        memory.get_mission(pid)))
    job_id = p["id"][:32]
    assert job_id
    # prepare_job は idempotency で packet を使う。直接 job 行を作る代わりに
    # run 前の sending 行を作る簡易な方法として external_draft を直接置く。
    draft = _draft([{"title": "工程1", "description": "作業する",
                     "outputs": [], "depends_on": []}])
    row = {"id": job_id, "pid": pid, "packet_id": p["id"],
           "provider": PROVIDER, "purpose": "plan_generation",
           "plan_signature": packet.plan_signature_of(memory.get_mission(pid)),
           "packet_sha256": p["sha256"], "status": "external_generated",
           "external_draft": draft, "unverified_claims": []}
    store.put(pid, "external_plan_job", job_id, row)
    return job_id


def test_g3b_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import app.web as web

    mem_path = tmp_path / "api2.db"
    memory = ShortTermMemory(mem_path)
    pid = memory.create_project("G3B API2")["id"]
    memory.save_mission(pid, "## 達成条件\n1. 資料を作成", "", "", False, [])
    client = TestClient(web.app)
    web.memory = memory
    manager = ProjectOrchestrator(
        memory, object(), lambda _: ("", []), None, lambda: [],
        workspace=WorkspaceSandbox(tmp_path / "workspace2"))
    web.orchestrator = manager

    async def _healthy():
        return True

    async def _fake_complete(system, prompt, schema=None):
        return json.dumps(dict(LOCAL_ONLY), ensure_ascii=False)

    manager._local_complete = _fake_complete
    monkeypatch.setattr(web, "engine_enabled", True)
    monkeypatch.setattr(web, "llm", type("L", (), {"health": staticmethod(_healthy)})())
    monkeypatch.setattr(web, "provider_statuses", lambda: [])
    monkeypatch.setenv("SEBAS_EXTERNAL_PLAN_JOBS_PROJECTS", pid)
    store = ReviewStore(memory.path)
    _setup_external(store, pid)
    sig = packet.plan_signature_of(memory.get_mission(pid))
    p = _approved_sig_job(store, pid, sig)
    draft = _draft([{"title": "工程1", "description": "作業する",
                     "outputs": [], "depends_on": []}])
    job_id = "job-api-1"
    store.put(pid, "external_plan_job", job_id,
              {"id": job_id, "pid": pid, "packet_id": p["id"],
               "provider": PROVIDER, "purpose": "plan_generation",
               "plan_signature": sig, "packet_sha256": p["sha256"],
               "status": "external_generated", "external_draft": draft,
               "unverified_claims": []})

    before_version = memory.get_mission(pid)["plan_version"]
    # 本文なしは従来どおり (計画生成の応答は従来と同じ)
    res = client.post(f"/api/projects/{pid}/mission/plan/generate")
    assert res.status_code == 200, res.text
    # external_job_id ありで生成する前に署名を取り直す (1回目の生成で計画版が進むため)
    sig = packet.plan_signature_of(memory.get_mission(pid))
    store.put(pid, "external_plan_job", job_id,
              {"id": job_id, "pid": pid, "packet_id": p["id"],
               "provider": PROVIDER, "purpose": "plan_generation",
               "plan_signature": sig, "packet_sha256": p["sha256"],
               "status": "external_generated", "external_draft": draft,
               "unverified_claims": []})
    # external_job_id ありで生成
    before_calls = {"n": 0}
    orig_generate = manager.generate_plan
    async def _counting(pid_arg, external_hint=None):
        before_calls["n"] += 1
        return await orig_generate(pid_arg, external_hint=external_hint)
    manager._local_complete = _fake_complete
    web.orchestrator = manager
    manager.generate_plan = _counting
    res = client.post(f"/api/projects/{pid}/mission/plan/generate",
                      json={"external_job_id": job_id})
    assert res.status_code == 200, res.text
    assert before_calls["n"] == 1
    assert "工程1" not in res.text and "作業する" not in res.text
    events = [e for e in memory.get_mission(pid)["events"]
              if e["kind"] == "external_draft_hint_used"]
    assert len(events) == 1
    # 実行中は409
    import sqlite3 as _sql
    with _sql.connect(str(memory.path)) as _db:
        with _db:
            _db.execute("UPDATE project_missions SET status='running' WHERE project_id=?", (pid,))
    try:
        res = client.post(f"/api/projects/{pid}/mission/plan/generate",
                          json={"external_job_id": job_id})
        assert res.status_code == 409, res.text
    finally:
        with _sql.connect(str(memory.path)) as _db:
            with _db:
                _db.execute("UPDATE project_missions SET status='paused' WHERE project_id=?", (pid,))
    # missions の外部AI設定は変わらない
    after = memory.get_mission(pid)
    assert after["allow_external_ai"] is False
    assert after["external_providers"] == []


def test_g3b_api_errors_and_flag(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import app.web as web
    import app.external_plan_jobs as jobs

    memory = ShortTermMemory(tmp_path / "api3.db")
    pid = memory.create_project("G3B API3")["id"]
    memory.save_mission(pid, "## 達成条件\n1. 資料を作成", "", "", False, [])
    client = TestClient(web.app)
    web.memory = memory
    manager = ProjectOrchestrator(
        memory, object(), lambda _: ("", []), None, lambda: [],
        workspace=WorkspaceSandbox(tmp_path / "workspace3"))
    web.orchestrator = manager

    async def _healthy():
        return True

    monkeypatch.setattr(web, "engine_enabled", True)
    monkeypatch.setattr(web, "llm", type("L", (), {"health": staticmethod(_healthy)})())
    monkeypatch.setattr(web, "provider_statuses", lambda: [])

    # フラグ未設定/許可外は403でget_jobを読まない
    monkeypatch.setenv("SEBAS_EXTERNAL_PLAN_JOBS_PROJECTS", "other")
    calls = {"n": 0}
    orig_get = jobs.get_job
    def _counting(store, pid_arg, job_arg):
        calls["n"] += 1
        return orig_get(store, pid_arg, job_arg)
    monkeypatch.setattr(jobs, "get_job", _counting)
    res = client.post(f"/api/projects/{pid}/mission/plan/generate",
                      json={"external_job_id": "whatever"})
    assert res.status_code == 403
    assert res.json()["detail"] == "feature_disabled"
    assert calls["n"] == 0
    # 空文字は422
    monkeypatch.setenv("SEBAS_EXTERNAL_PLAN_JOBS_PROJECTS", pid)
    res = client.post(f"/api/projects/{pid}/mission/plan/generate",
                      json={"external_job_id": ""})
    assert res.status_code == 422
    # 存在しないジョブは404
    res = client.post(f"/api/projects/{pid}/mission/plan/generate",
                      json={"external_job_id": "missing"})
    assert res.status_code == 404
    # 他PJ・rejected・署名不一致は422
    store = ReviewStore(memory.path)
    _setup_external(store, pid)
    sig = packet.plan_signature_of(memory.get_mission(pid))
    p = _approved_sig_job(store, pid, sig)
    draft = _draft([{"title": "工程1", "description": "作業する",
                     "outputs": [], "depends_on": []}])
    store.put(pid, "external_plan_job", "other-pj",
              {"id": "other-pj", "pid": "other", "status": "external_generated",
               "plan_signature": sig, "external_draft": draft,
               "unverified_claims": []})
    res = client.post(f"/api/projects/{pid}/mission/plan/generate",
                      json={"external_job_id": "other-pj"})
    assert res.status_code == 422
    store.put(pid, "external_plan_job", "rej-1",
              {"id": "rej-1", "pid": pid, "status": "rejected_response",
               "plan_signature": sig, "external_draft": draft,
               "unverified_claims": []})
    res = client.post(f"/api/projects/{pid}/mission/plan/generate",
                      json={"external_job_id": "rej-1"})
    assert res.status_code == 422
    store.put(pid, "external_plan_job", "sig-1",
              {"id": "sig-1", "pid": pid, "status": "external_generated",
               "plan_signature": "other-sig", "external_draft": draft,
               "unverified_claims": []})
    res = client.post(f"/api/projects/{pid}/mission/plan/generate",
                      json={"external_job_id": "sig-1"})
    assert res.status_code == 422


def test_g3b_mutations_show_failures():
    # (i) unsupported 検査が必要
    import inspect
    from app import project_manager as pm
    src = inspect.getsource(pm.ProjectOrchestrator._generate_plan_impl)
    assert "unsupported_plan_path" in src
    assert "EXTERNAL_DRAFT_HINT" in src
    # (ii) finally 復元が必要
    src2 = inspect.getsource(pm.ProjectOrchestrator.generate_plan)
    assert src2.count("EXTERNAL_DRAFT_HINT.reset") >= 1
    assert "finally" in src2
    # (iii) 署名比較が必要
    from app import external_draft_hint as hint_mod
    src3 = inspect.getsource(hint_mod.build_hint)
    assert "plan_signature" in src3 and "plan_changed" in src3
    # (iv) 除外が必要
    assert "depends_on" in src3 and "required_outputs" in src3
