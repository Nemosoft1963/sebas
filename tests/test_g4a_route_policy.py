"""G4-a: 旧経路の方式判定と2経路の送信前検査。偽ランナーのみ。実外部AIなし。"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json

import pytest

from app.goal_review import ReviewStore
from app.memory.short_term import ShortTermMemory
from app.project_manager import ProjectOrchestrator
import app.planning_settings as ps

SECRET = "AKIAIOSFODNN7EXAMPLE"
ORIGINAL = "登録原本テスト本文確認用追加説明詳細記載継続文章追加分です"
SNIPPET = ORIGINAL[:24]
GOOD_SUMMARY = "一般的な文書作成の目標・根拠・工程・出力を確認するための公開説明です。"


def _mission(memory, pid, goal="通常の目標", success="通常の達成条件",
             constraints="通常の制約", providers=("chatgpt",)):
    return memory.save_mission(pid, goal, success, constraints, True, list(providers))


def _manager(memory, **runners):
    return ProjectOrchestrator(
        memory, None, lambda project: ("", []), runners.get("research"),
        lambda: [{"id": "chatgpt", "configured": True}],
        runners.get("plan"), None, runners.get("capability"),
    )


def _blocked(memory, pid):
    return [e for e in memory.get_mission(pid, event_limit=500)["events"]
            if e.get("kind") == "external_send_blocked_privacy"]


def _blob(memory, pid):
    return "\n".join(
        str(e.get("message") or "") + "\n" + str(e.get("detail") or "")
        for e in memory.get_mission(pid, event_limit=500)["events"]
    )


def _set_external_guarded(memory_path, pid):
    store = ReviewStore(memory_path)
    ps.update_settings(
        store, pid,
        {"method": "external_guarded", "generator_provider": "fake-gen-a",
         "reviewer_provider": "fake-rev-b"},
        ["fake-gen-a", "fake-rev-b"],
    )
    ps.grant_permission(
        store, pid, "plan_review",
        {"plan_signature": "sig", "packet_sha256": "ab" * 32,
         "provider": "fake-rev-b", "purpose": "plan_review"},
        "tester",
    )


def _add_original(memory, pid, content=ORIGINAL, filename="g4a-original.md"):
    data = content.encode("utf-8")
    return memory.add_context_file(
        pid, filename, content, len(data), data,
        "text/markdown", "markdown", "extracted", "hash-g4a",
    )


class _FakeLlm:
    def __init__(self, *responses):
        self.responses = list(responses) or ["{}"]

    async def stream(self, messages):
        yield self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


def _plan_llm():
    return _FakeLlm(
        '{"summary":"draft","tasks":[{"task_key":"draft","depends_on":[],'
        '"title":"Draft","description":"d","acceptance_criteria":"a"}]}')


# ---- 1: _outbound_guard ----

def test_g4a_guard_local_only_ok(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    pid = memory.create_project("g4a guard local")["id"]
    _mission(memory, pid)
    manager = _manager(memory)
    result = manager._outbound_guard(pid, "plan_review", "通常の無関係な本文です", ["chatgpt"])
    assert result.get("ok") is True


def test_g4a_guard_external_guarded_blocks_benign(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    pid = memory.create_project("g4a guard ext")["id"]
    _mission(memory, pid)
    _set_external_guarded(memory.path, pid)
    manager = _manager(memory)
    text = "通常の無関係な本文です"
    result = manager._outbound_guard(pid, "plan_review", text, ["chatgpt"])
    assert result.get("ok") is False
    assert result.get("codes") == ["method_external_guarded"]
    blocked = _blocked(memory, pid)
    assert len(blocked) == 1
    detail = json.loads(blocked[0]["detail"])
    assert detail["purpose"] == "plan_review"
    assert detail["codes"] == ["method_external_guarded"]
    assert detail["sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    blob = _blob(memory, pid)
    assert text not in blob


def test_g4a_guard_check_error_on_settings_exception(tmp_path, monkeypatch):
    memory = ShortTermMemory(tmp_path / "memory.db")
    pid = memory.create_project("g4a guard err")["id"]
    _mission(memory, pid)
    manager = _manager(memory)
    for exc in (OSError("x"), ValueError("x")):
        def boom(store, p, _e=exc):
            raise _e
        monkeypatch.setattr(ps, "get_settings", boom)
        result = manager._outbound_guard(pid, "plan_review", "通常の本文", ["chatgpt"])
        assert result.get("ok") is False
        assert result.get("codes") == ["check_error"]
    monkeypatch.undo()


def test_g4a_guard_check_error_on_bad_shape(tmp_path, monkeypatch):
    memory = ShortTermMemory(tmp_path / "memory.db")
    pid = memory.create_project("g4a guard shape")["id"]
    _mission(memory, pid)
    manager = _manager(memory)
    for bad in (None, [], "x", {"method": 123}, {"method": None}):
        monkeypatch.setattr(ps, "get_settings", lambda store, p, _b=bad: _b)
        result = manager._outbound_guard(pid, "plan_review", "通常の本文", ["chatgpt"])
        assert result.get("ok") is False, bad
        assert result.get("codes") == ["check_error"], bad
    monkeypatch.undo()


def test_g4a_guard_local_only_leak_still_blocked(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    pid = memory.create_project("g4a guard leak")["id"]
    _mission(memory, pid)
    manager = _manager(memory)
    result = manager._outbound_guard(pid, "plan_review", f"計画 {SECRET} を含む", ["chatgpt"])
    assert result.get("ok") is False
    assert "secret" in (result.get("codes") or [])
    _add_original(memory, pid)
    result2 = manager._outbound_guard(pid, "plan_review", f"説明 {SNIPPET} を含む", ["chatgpt"])
    assert result2.get("ok") is False
    assert "source_quote" in (result2.get("codes") or [])


# ---- 2: 4経路 ----

@pytest.mark.asyncio
async def test_g4a_four_routes_blocked_when_external_guarded(tmp_path):
    # plan_review
    calls = []

    async def fake_runner(text, providers):
        calls.append((text, providers))
        return [{"id": "x", "label": "X", "model": "m", "ok": True, "review": "よい"}]

    memory = ShortTermMemory(tmp_path / "memory.db")
    pid = memory.create_project("g4a 4routes plan")["id"]
    _mission(memory, pid)
    _set_external_guarded(memory.path, pid)
    manager = ProjectOrchestrator(
        memory, _plan_llm(), lambda project: ("", []), None,
        lambda: [{"id": "chatgpt", "configured": True}], fake_runner,
    )
    await manager.generate_plan(pid)
    assert calls == []
    assert _blocked(memory, pid)
    await manager.shutdown()


@pytest.mark.asyncio
async def test_g4a_failover_blocked_when_external_guarded(tmp_path):
    calls = []

    async def fake_runner(payload, reviewers):
        calls.append((payload, reviewers))
        return [{"id": "x", "label": "X", "model": "m", "ok": True, "review": "よい"}]

    async def boom(project_id, task_id, recovery_guidance=""):
        raise ValueError("local execution failed")

    memory = ShortTermMemory(tmp_path / "memory.db")
    pid = memory.create_project("g4a failover ext")["id"]
    _mission(memory, pid)
    _set_external_guarded(memory.path, pid)
    mission = memory.replace_plan(pid, "plan", [{
        "task_key": "t1", "depends_on": [], "title": "作業",
        "description": "作業する", "acceptance_criteria": "完了", "mode": "local",
    }])
    manager = _manager(memory, capability=fake_runner)
    manager.llm = _FakeLlm("指針")
    manager._execute_task = boom
    result = await manager._retry_failed_task_with_alternate_ai(
        pid, mission["tasks"][0], "execution", "通常の失敗")
    assert result is None
    assert calls == []
    assert _blocked(memory, pid)
    await manager.shutdown()


@pytest.mark.asyncio
async def test_g4a_capability_blocked_when_external_guarded(tmp_path):
    calls = []

    async def fake_runner(payload, reviewers):
        calls.append((payload, reviewers))
        return [{"id": "x", "label": "X", "model": "m", "ok": True, "review": "よい"}]

    memory = ShortTermMemory(tmp_path / "memory.db")
    pid = memory.create_project("g4a capa ext")["id"]
    _mission(memory, pid)
    _set_external_guarded(memory.path, pid)
    mission = memory.replace_plan(pid, "plan", [{
        "task_key": "t1", "depends_on": [], "title": "作業",
        "description": "作業する", "acceptance_criteria": "完了", "mode": "local",
    }])
    manager = _manager(memory, capability=fake_runner)
    manager.llm = _FakeLlm("手順")
    await manager._resolve_capability_gaps(pid, mission["tasks"][0], [{"description": "不足"}])
    assert calls == []
    assert _blocked(memory, pid)
    await manager.shutdown()


@pytest.mark.asyncio
async def test_g4a_research_blocked_when_external_guarded(tmp_path):
    calls = []

    async def fake_research(prompt, providers, synthesizer, style, context):
        calls.append((prompt, providers, synthesizer, style, context))
        return {"synthesis": "知見"}

    memory = ShortTermMemory(tmp_path / "memory.db")
    pid = memory.create_project("g4a research ext")["id"]
    _mission(memory, pid)
    _set_external_guarded(memory.path, pid)
    mission = memory.replace_plan(pid, "plan", [{
        "task_key": "r1", "depends_on": [], "title": "公開調査",
        "description": "公開情報を調査する", "acceptance_criteria": "調査メモ",
        "mode": "research",
    }])
    manager = ProjectOrchestrator(
        memory,
        _FakeLlm('{"result":"調査した","operations":[],"table_operations":[],'
                 '"source_references":[],"capability_gaps":[]}'),
        lambda project: ("", []), fake_research,
        lambda: [{"id": "chatgpt", "configured": True}],
    )
    await manager._execute_legacy_task(pid, mission["tasks"][0]["id"])
    assert calls == []
    assert _blocked(memory, pid)
    await manager.shutdown()


@pytest.mark.asyncio
async def test_g4a_four_routes_local_only_calls_once(tmp_path):
    # plan_review local_only passes -> 1 call
    calls = []

    async def fake_runner(text, providers):
        calls.append((text, providers))
        return [{"id": "x", "label": "X", "model": "m", "ok": True,
                 "review": "検証タスクを追加する"}]

    memory = ShortTermMemory(tmp_path / "memory.db")
    pid = memory.create_project("g4a plan local")["id"]
    _mission(memory, pid, "計画を作る", "検証を含める", "削除なし")
    manager = ProjectOrchestrator(
        memory,
        _FakeLlm(
            '{"summary":"draft","tasks":[{"task_key":"draft","depends_on":[],'
            '"title":"Draft","description":"d","acceptance_criteria":"a"}]}',
            '{"summary":"refined","tasks":[{"task_key":"research","depends_on":[],'
            '"title":"Research","description":"d","acceptance_criteria":"a"},'
            '{"task_key":"verify","depends_on":["research"],'
            '"title":"Verify","description":"d","acceptance_criteria":"a"}]}'),
        lambda project: ("", []), None,
        lambda: [{"id": "chatgpt", "configured": True}], fake_runner,
    )
    await manager.generate_plan(pid)
    assert len(calls) == 1
    await manager.shutdown()


# ---- 3: goal_review ----

def _goal_setup(tmp_path, name="g4a-goal"):
    from app.structured_planning import compile_task
    from app.workspace_files import WorkspaceSandbox
    memory = ShortTermMemory(tmp_path / "conversations.db")
    p = memory.create_project("private", workspace_path="projects/private")
    pid = p["id"]
    memory.save_mission(pid, "機密会社の作業結果を整理する", "根拠を示す", "", True, ["a", "b"])
    task = compile_task(1, "根拠を示す", {"title": "機密の文書", "scope": "根拠を示す",
                                         "headings": ["概要", "確認"], "depends_on": []}, [])
    memory.replace_plan(pid, "test", [task])
    ws = WorkspaceSandbox(tmp_path / "workspace")
    manager = ProjectOrchestrator(
        memory, None, lambda pr: ("", []), None,
        lambda: [{"id": "a", "configured": True}, {"id": "b", "configured": True}],
        workspace=ws)
    (tmp_path / "goal_review_policy.json").write_text('{"required":true}')
    return manager, pid


@pytest.mark.asyncio
async def test_g4a_goal_review_source_quote_blocked(tmp_path):
    import json as _json
    from app.goal_review import plan_snapshot, review_plan
    manager, pid = _goal_setup(tmp_path)
    _add_original(manager.memory, pid)
    sent = []

    async def runner(text, providers):
        sent.append((text, providers))
        return [{"id": x, "ok": True,
                 "review": _json.dumps({"verdict": "pass", "issues": []})} for x in providers]

    manager.plan_review_runner = runner
    sig = plan_snapshot(manager, pid)[1]
    summary = GOOD_SUMMARY + " " + SNIPPET
    with pytest.raises(ValueError, match="privacy_check_failed"):
        await review_plan(manager, pid, sig, summary, True)
    assert sent == []
    store = ReviewStore(manager.memory.path)
    assert store.get(pid, "send_approval", sig) is None


@pytest.mark.asyncio
async def test_g4a_goal_review_external_guarded_blocked(tmp_path):
    import json as _json
    from app.goal_review import plan_snapshot, review_plan
    manager, pid = _goal_setup(tmp_path)
    _set_external_guarded(manager.memory.path, pid)
    sent = []

    async def runner(text, providers):
        sent.append((text, providers))
        return [{"id": x, "ok": True,
                 "review": _json.dumps({"verdict": "pass", "issues": []})} for x in providers]

    manager.plan_review_runner = runner
    sig = plan_snapshot(manager, pid)[1]
    with pytest.raises(ValueError, match="method_external_guarded"):
        await review_plan(manager, pid, sig, GOOD_SUMMARY, True)
    assert sent == []
    store = ReviewStore(manager.memory.path)
    assert store.get(pid, "send_approval", sig) is None


@pytest.mark.asyncio
async def test_g4a_goal_review_local_only_passes(tmp_path):
    import json as _json
    from app.goal_review import plan_snapshot, review_plan
    manager, pid = _goal_setup(tmp_path)
    sent = []

    async def runner(text, providers):
        sent.append((text, providers))
        return [{"id": x, "ok": True,
                 "review": _json.dumps({"verdict": "pass", "issues": []})} for x in providers]

    manager.plan_review_runner = runner
    sig = plan_snapshot(manager, pid)[1]
    result = await review_plan(manager, pid, sig, GOOD_SUMMARY, True)
    assert len(sent) == 1
    assert result["status"] == "passed"


# ---- 4: plan_review_loop._call_gemini ----

def _loop_manager(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    pid = memory.create_project("g4a-loop")["id"]
    memory.save_mission(pid, "通常の目標", "通常の達成条件", "", True, ["chatgpt"])
    manager = ProjectOrchestrator(
        memory, None, lambda project: ("", []), None,
        lambda: [{"id": "chatgpt", "configured": True}],
    )
    return manager, memory, pid


@pytest.mark.asyncio
async def test_g4a_call_gemini_source_quote_blocked(tmp_path, monkeypatch):
    import time as _time
    import app.plan_review_loop as loop
    import app.external_ai as ext
    manager, memory, pid = _loop_manager(tmp_path)
    _add_original(memory, pid)
    run = {"id": "r1", "project_id": pid, "external_calls": 0,
           "max_external_calls": 10, "deadline": _time.time() + 3600}
    calls = []

    async def fake_call(provider_id, prompt, system, max_tokens=2200, reasoning_effort=None):
        calls.append((provider_id, prompt))
        from types import SimpleNamespace
        return SimpleNamespace(text="{}", model="gemini-stub")

    monkeypatch.setattr(ext, "call_provider_with_metadata", fake_call)
    prompt = "通常の草案説明です " + SNIPPET
    with pytest.raises(ValueError, match="送信前検査により停止しました"):
        await loop._call_gemini(manager, pid, run, prompt, "draft")
    assert calls == []
    assert run["external_calls"] == 0


@pytest.mark.asyncio
async def test_g4a_call_gemini_external_guarded_blocked(tmp_path, monkeypatch):
    import time as _time
    import app.plan_review_loop as loop
    import app.external_ai as ext
    manager, memory, pid = _loop_manager(tmp_path)
    _set_external_guarded(memory.path, pid)
    run = {"id": "r1", "project_id": pid, "external_calls": 0,
           "max_external_calls": 10, "deadline": _time.time() + 3600}
    calls = []

    async def fake_call(provider_id, prompt, system, max_tokens=2200, reasoning_effort=None):
        calls.append((provider_id, prompt))
        from types import SimpleNamespace
        return SimpleNamespace(text="{}", model="gemini-stub")

    monkeypatch.setattr(ext, "call_provider_with_metadata", fake_call)
    with pytest.raises(ValueError, match="method_external_guarded"):
        await loop._call_gemini(manager, pid, run, "通常の草案説明です", "draft")
    assert calls == []
    assert run["external_calls"] == 0


@pytest.mark.asyncio
async def test_g4a_call_gemini_passes(tmp_path, monkeypatch):
    import time as _time
    import app.plan_review_loop as loop
    import app.external_ai as ext
    manager, memory, pid = _loop_manager(tmp_path)
    run = {"id": "r1", "project_id": pid, "external_calls": 0,
           "max_external_calls": 10, "deadline": _time.time() + 3600}

    async def fake_call(provider_id, prompt, system, max_tokens=2200, reasoning_effort=None):
        from types import SimpleNamespace
        return SimpleNamespace(text="草案本文", model="gemini-stub")

    monkeypatch.setattr(ext, "call_provider_with_metadata", fake_call)
    text = await loop._call_gemini(manager, pid, run, "通常の草案説明です", "draft")
    assert text == "草案本文"
    assert run["external_calls"] == 1

def test_g4a_guard_corrupt_stored_method_blocks(tmp_path):
    memory = ShortTermMemory(tmp_path / "corrupt-method.db")
    pid = memory.create_project("g4a corrupt method")["id"]
    _mission(memory, pid)
    manager = _manager(memory)
    store = ReviewStore(memory.path)
    for payload in ({"method": "unexpected"}, {"method": None}, {}):
        store.put(pid, "planning_settings", pid, payload)
        result = manager._outbound_guard(
            pid, "plan_review", "通常の無関係な本文です", ["chatgpt"])
        assert result["ok"] is False
        assert result["codes"] == ["check_error"]