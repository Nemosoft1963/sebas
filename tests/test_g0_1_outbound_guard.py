"""G0-1: 外部AIへの送信前検査(_outbound_guard)の試験。偽のランナーのみ使用。

外部AI・実通信は使わない。秘密・検出値はイベント・メッセージに記録しない。
"""
import hashlib
import json

import pytest

from app.memory.short_term import ShortTermMemory
from app.planning_privacy import check_outbound_text
from app.project_manager import ProjectOrchestrator

SECRET = "AKIAIOSFODNN7EXAMPLE"
MAIL = "blocked-test@example.com"


def _mission(memory, pid, goal="通常の目標", success="通常の達成条件",
             constraints="通常の制約"):
    return memory.save_mission(pid, goal, success, constraints, True, ["chatgpt"])


def _manager(memory, **runners):
    return ProjectOrchestrator(
        memory, None, lambda project: ("", []), runners.get("research"),
        lambda: [{"id": "chatgpt", "configured": True}],
        runners.get("plan"), None, runners.get("capability"),
    )


def _events(memory, pid, kind):
    return [e for e in memory.get_mission(pid, event_limit=500)["events"]
            if e.get("kind") == kind]


def _blocked_events(memory, pid):
    return _events(memory, pid, "external_send_blocked_privacy")


def _audited_events(memory, pid):
    return [e for e in memory.get_mission(pid, event_limit=500)["events"]
            if e.get("kind") == "external_send_audited"]


def _no_leak(memory, pid, *values):
    blob = "\n".join(
        str(e.get("message") or "") + "\n" + str(e.get("detail") or "")
        for e in memory.get_mission(pid, event_limit=500)["events"]
    )
    for value in values:
        assert value not in blob


# ---- 受入1: check_outbound_text ----

def test_plain_japanese_plan_text_is_ok():
    result = check_outbound_text(
        "通常の日本語の計画文です。SC01の達成に向けて工程を進めます。C02の条件も満たします。")
    assert result == {"ok": True, "codes": [], "details": {}}


def test_secret_is_blocked():
    result = check_outbound_text(f"計画の説明 {SECRET} を含む")
    assert result["ok"] is False
    assert "secret" in result["codes"]


def test_email_is_blocked():
    result = check_outbound_text(f"連絡先 {MAIL} へ送る")
    assert result["ok"] is False
    assert "email" in result["codes"]


def test_unc_path_is_blocked():
    result = check_outbound_text("資料は \\\\server\\share\\doc を参照")
    assert result["ok"] is False
    assert "abs_path" in result["codes"]


def test_drive_path_is_blocked():
    for text in ("C:\\data\\x を参照", "Z:/docs/y を参照"):
        result = check_outbound_text(text)
        assert result["ok"] is False, text
        assert "abs_path" in result["codes"], text


def test_host_abs_path_is_blocked():
    for text in ("/home/user/data を参照", "/mnt/data/x を参照"):
        result = check_outbound_text(text)
        assert result["ok"] is False, text
        assert "abs_path" in result["codes"], text


def test_project_relative_path_is_ok():
    result = check_outbound_text("成果物は result/report.md を参照してください")
    assert result["ok"] is True, result


def test_url_query_is_blocked():
    result = check_outbound_text("https://example.com/search?q=abc を参照")
    assert result["ok"] is False
    assert "url_query" in result["codes"]


def test_url_without_query_is_ok():
    result = check_outbound_text("https://example.com/about を参照")
    assert result["ok"] is True, result


def test_hash_like_is_blocked():
    result = check_outbound_text("値は " + "0123456789abcdef" * 4 + " です")
    assert result["ok"] is False
    assert "hash_like" in result["codes"]


def test_uuid_is_blocked():
    result = check_outbound_text("ID 123e4567-e89b-12d3-a456-426614174000 を参照")
    assert result["ok"] is False
    assert "hash_like" in result["codes"]


def test_uuid_with_separators_is_blocked():
    result = check_outbound_text("ID 123e4567 e89b 12d3 a456 426614174000 を参照")
    assert result["ok"] is False
    assert "hash_like" in result["codes"], result


def test_blocked_term_is_blocked():
    result = check_outbound_text("原本の内容を使う", blocked_terms=("reg-12345",))
    assert result["ok"] is True, result
    result = check_outbound_text("reg-12345 を使う", blocked_terms=("reg-12345",))
    assert result["ok"] is False
    assert "blocked_term" in result["codes"]


def test_short_blocked_terms_are_ignored():
    result = check_outbound_text("C02 の条件", blocked_terms=("C02", "AB"))
    assert result["ok"] is True, result


def test_condition_numbers_are_not_blocked_terms():
    result = check_outbound_text(
        "SC01 と C02 の条件を満たす", blocked_terms=("SC01", "C02"))
    assert result["ok"] is True, result


def test_fullwidth_email_is_blocked():
    result = check_outbound_text("連絡先 ａｂｃ＠ｅｘａｍｐｌｅ．ｃｏｍ へ")
    assert result["ok"] is False, result
    assert "email" in result["codes"], result


def test_zero_width_email_is_blocked():
    text = "連絡先 a​b​c@example.com へ".replace("​", "​")
    text = "連絡先 a\u200bb\u200bc@example.com へ"
    result = check_outbound_text(text)
    assert result["ok"] is False, result
    assert "email" in result["codes"], result


def test_soft_hyphen_email_is_blocked():
    result = check_outbound_text("連絡先 test\u00ad@example.com へ")
    assert result["ok"] is False, result
    assert "email" in result["codes"], result


def test_spaced_email_is_blocked():
    result = check_outbound_text("連絡先 test @ example.com へ")
    assert result["ok"] is False, result
    assert "email" in result["codes"], result


def test_fullwidth_drive_path_is_blocked():
    result = check_outbound_text("Ｚ：＼ドキュメントを見る")
    assert result["ok"] is False, result
    assert "abs_path" in result["codes"], result


def test_fullwidth_url_query_is_blocked():
    result = check_outbound_text("ｈｔｔｐｓ：／／ｅｘａｍｐｌｅ．ｃｏｍ／ｓ？ｑ＝１を見る")
    assert result["ok"] is False, result
    assert "url_query" in result["codes"], result


def test_unusable_input():
    for text in (None, 123, "", "   "):
        result = check_outbound_text(text)
        assert result["ok"] is False, repr(text)
        assert result["codes"] == ["unusable_input"], repr(text)


def test_check_error_on_expected_exception(monkeypatch):
    import app.plan_review_loop as loop

    def boom(text):
        raise ValueError("injected")

    monkeypatch.setattr(loop, "contains_secret", boom)
    result = check_outbound_text("通常の計画文です")
    assert result["ok"] is False
    assert result["codes"] == ["check_error"], result


def test_unexpected_exception_propagates(monkeypatch):
    import app.plan_review_loop as loop

    def boom(text):
        raise RuntimeError("injected")

    monkeypatch.setattr(loop, "contains_secret", boom)
    with pytest.raises(RuntimeError):
        check_outbound_text("通常の計画文です")


# ---- 受入2: 4か所のブロック ----

class _FakeLlm:
    def __init__(self, *responses):
        self.responses = list(responses) or ["{}"]

    async def stream(self, messages):
        yield self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


def _plan_llm():
    return _FakeLlm(
        '{"summary":"draft","tasks":[{"task_key":"draft","depends_on":[],'
        '"title":"Draft","description":"d","acceptance_criteria":"a"}]}')


@pytest.mark.asyncio
async def test_plan_review_blocked(tmp_path):
    calls = []

    async def fake_runner(text, providers):
        calls.append((text, providers))
        return [{"id": "x", "label": "X", "model": "m", "ok": True, "review": "よい"}]

    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("G0-1 plan block")
    pid = project["id"]
    _mission(memory, pid, goal=f"計画を作る {SECRET}")
    manager = ProjectOrchestrator(
        memory, _plan_llm(), lambda project: ("", []), None,
        lambda: [{"id": "chatgpt", "configured": True}], fake_runner,
    )
    mission = await manager.generate_plan(pid)

    assert calls == []
    blocked = _blocked_events(memory, pid)
    assert len(blocked) == 1
    detail = json.loads(blocked[0]["detail"])
    assert detail["purpose"] == "plan_review"
    assert detail["codes"] == ["secret"]
    assert mission["plan_reviews"] == [{
        "id": "privacy_guard", "label": "送信前検査", "model": "",
        "ok": False, "error": "送信保留: privacy_check_failed:secret",
    }]
    assert [t["task_key"] for t in mission["tasks"]] == ["draft"]
    _no_leak(memory, pid, SECRET)
    await manager.shutdown()


@pytest.mark.asyncio
async def test_failover_review_blocked(tmp_path):
    calls = []

    async def fake_runner(payload, reviewers):
        calls.append((payload, reviewers))
        return [{"id": "x", "label": "X", "model": "m", "ok": True, "review": "よい"}]

    async def boom(project_id, task_id, recovery_guidance=""):
        raise ValueError("local execution failed")

    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("G0-1 failover block")
    pid = project["id"]
    _mission(memory, pid)
    mission = memory.replace_plan(pid, "plan", [{
        "task_key": "t1", "depends_on": [], "title": "作業",
        "description": "作業する", "acceptance_criteria": "完了", "mode": "local",
    }])
    task = mission["tasks"][0]
    manager = _manager(memory, capability=fake_runner)
    manager.llm = _FakeLlm("再調整の指針")
    manager._execute_task = boom

    result = await manager._retry_failed_task_with_alternate_ai(
        pid, task, "execution", f"失敗の詳細 {SECRET}")

    assert result is None
    assert calls == []
    blocked = _blocked_events(memory, pid)
    assert len(blocked) == 1
    detail = json.loads(blocked[0]["detail"])
    assert detail["purpose"] == "failover_review"
    assert detail["codes"] == ["secret"]
    assert _events(memory, pid, "task_ai_failover_skipped")
    _no_leak(memory, pid, SECRET)
    await manager.shutdown()


@pytest.mark.asyncio
async def test_capability_review_blocked(tmp_path):
    calls = []

    async def fake_runner(payload, reviewers):
        calls.append((payload, reviewers))
        return [{"id": "x", "label": "X", "model": "m", "ok": True, "review": "よい"}]

    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("G0-1 capability block")
    pid = project["id"]
    _mission(memory, pid)
    mission = memory.replace_plan(pid, "plan", [{
        "task_key": "t1", "depends_on": [], "title": "作業",
        "description": f"作業する {SECRET}", "acceptance_criteria": "完了",
        "mode": "local",
    }])
    task = mission["tasks"][0]
    manager = _manager(memory, capability=fake_runner)
    manager.llm = _FakeLlm("解決の手順")

    resolution, reviews = await manager._resolve_capability_gaps(
        pid, task, [{"description": "不足"}])

    assert calls == []
    assert reviews == []
    assert "解決の手順" in resolution
    blocked = _blocked_events(memory, pid)
    assert len(blocked) == 1
    detail = json.loads(blocked[0]["detail"])
    assert detail["purpose"] == "capability_review"
    assert detail["codes"] == ["secret"]
    assert _events(memory, pid, "capability_review_skipped")
    _no_leak(memory, pid, SECRET)
    await manager.shutdown()


@pytest.mark.asyncio
async def test_research_blocked(tmp_path):
    calls = []

    async def fake_research(prompt, providers, synthesizer, style, context):
        calls.append((prompt, providers, synthesizer, style, context))
        return {"synthesis": "公開の知見"}

    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("G0-1 research block")
    pid = project["id"]
    _mission(memory, pid)
    mission = memory.replace_plan(pid, "plan", [{
        "task_key": "r1", "depends_on": [], "title": "公開調査",
        "description": f"公開情報を調査する {SECRET}",
        "acceptance_criteria": "調査メモ", "mode": "research",
    }])
    manager = ProjectOrchestrator(
        memory,
        _FakeLlm('{"result":"調査した","operations":[],"table_operations":[],'
                 '"source_references":[],"capability_gaps":[]}'),
        lambda project: ("", []), fake_research,
        lambda: [{"id": "chatgpt", "configured": True}],
    )

    result = await manager._execute_legacy_task(pid, mission["tasks"][0]["id"])

    assert calls == []
    assert "調査した" in result
    blocked = _blocked_events(memory, pid)
    assert len(blocked) == 1
    detail = json.loads(blocked[0]["detail"])
    assert detail["purpose"] == "research"
    assert detail["codes"] == ["secret"]
    assert _events(memory, pid, "external_fallback")
    _no_leak(memory, pid, SECRET)
    await manager.shutdown()


# ---- 受入3: 4か所の通過 ----

@pytest.mark.asyncio
async def test_plan_review_passes_with_identical_body(tmp_path):
    calls = []

    async def fake_runner(text, providers):
        calls.append((text, providers))
        return [{"id": "x", "label": "X", "model": "m", "ok": True,
                 "review": "検証タスクを追加する"}]

    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("G0-1 plan pass")
    pid = project["id"]
    goal, success, constraints = "計画を作る", "検証を含める", "削除なし"
    _mission(memory, pid, goal, success, constraints)
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
    mission = await manager.generate_plan(pid)

    assert len(calls) == 1
    sent, providers = calls[0]
    assert providers == ["chatgpt"]
    plan = {"summary": "draft", "tasks": [
        {"task_key": "draft", "depends_on": [], "title": "Draft",
         "description": "d", "acceptance_criteria": "a", "mode": "local"}]}
    expected = json.dumps({
        "project": "G0-1 plan pass", "goal": goal,
        "success_criteria": success, "constraints": constraints,
        "draft_plan": plan,
    }, ensure_ascii=False, indent=2)
    assert sent.encode("utf-8") == expected.encode("utf-8")
    audited = _audited_events(memory, pid)
    assert len(audited) == 1
    detail = json.loads(audited[0]["detail"])
    assert detail["purpose"] == "plan_review"
    assert detail["providers"] == ["chatgpt"]
    assert detail["sha256"] == hashlib.sha256(sent.encode("utf-8")).hexdigest()
    assert detail["chars"] == len(sent)
    assert sent not in audited[0]["detail"]
    assert not _blocked_events(memory, pid)
    assert mission["plan_reviews"][0]["ok"] is True
    await manager.shutdown()


@pytest.mark.asyncio
async def test_failover_review_passes_with_identical_body(tmp_path):
    calls = []

    async def fake_runner(payload, reviewers):
        calls.append((payload, reviewers))
        return [{"id": "x", "label": "X", "model": "m", "ok": True, "review": "よい"}]

    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("G0-1 failover pass")
    pid = project["id"]
    goal, success, constraints = "通常の目標", "通常の達成条件", "通常の制約"
    _mission(memory, pid, goal, success, constraints)
    mission = memory.replace_plan(pid, "plan", [{
        "task_key": "t1", "depends_on": [], "title": "作業",
        "description": "作業する", "acceptance_criteria": "完了", "mode": "local",
    }])
    task = mission["tasks"][0]
    manager = _manager(memory, capability=fake_runner)
    manager.llm = _FakeLlm("再調整の指針")

    async def ok_execute(project_id, task_id, recovery_guidance=""):
        return "回復した実行結果"

    manager._execute_task = ok_execute
    failure_detail = "通常の失敗の詳細"
    result = await manager._retry_failed_task_with_alternate_ai(
        pid, task, "execution", failure_detail)

    assert result == "回復した実行結果"
    assert len(calls) == 1
    sent, reviewers = calls[0]
    assert reviewers == ["chatgpt"]
    classification = manager._classify_task_failure("execution", failure_detail)[0]
    fingerprint = manager._task_failure_fingerprint(classification, failure_detail)
    expected = json.dumps({
        "goal": goal, "success_criteria": success, "constraints": constraints,
        "task": {"title": "作業", "description": "作業する",
                 "acceptance_criteria": "完了"},
        "failure_kind": "execution", "failure_class": classification,
        "failure_fingerprint": fingerprint,
        "failure_detail": failure_detail[:12000],
        "data_policy": "資料原本、抽出本文、Workspace内容、認証情報は外部AIへ送信しない",
        "forbidden": ["delete", "arbitrary shell", "access outside /workspace",
                      "secret transmission", "invented source data"],
    }, ensure_ascii=False, indent=2)
    assert sent.encode("utf-8") == expected.encode("utf-8")
    audited = _audited_events(memory, pid)
    assert len(audited) == 1
    detail = json.loads(audited[0]["detail"])
    assert detail["purpose"] == "failover_review"
    assert detail["sha256"] == hashlib.sha256(sent.encode("utf-8")).hexdigest()
    assert not _blocked_events(memory, pid)
    await manager.shutdown()


@pytest.mark.asyncio
async def test_capability_review_passes_with_identical_body(tmp_path):
    calls = []

    async def fake_runner(payload, reviewers):
        calls.append((payload, reviewers))
        return [{"id": "x", "label": "X", "model": "m", "ok": True, "review": "よい"}]

    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("G0-1 capability pass")
    pid = project["id"]
    goal, success, constraints = "通常の目標", "通常の達成条件", "通常の制約"
    _mission(memory, pid, goal, success, constraints)
    mission = memory.replace_plan(pid, "plan", [{
        "task_key": "t1", "depends_on": [], "title": "作業",
        "description": "作業する", "acceptance_criteria": "完了", "mode": "local",
    }])
    task = mission["tasks"][0]
    manager = _manager(memory, capability=fake_runner)
    manager.llm = _FakeLlm("解決の手順")
    gaps = [{"description": "不足"}]

    resolution, reviews = await manager._resolve_capability_gaps(pid, task, gaps)

    assert len(calls) == 1
    sent, reviewers = calls[0]
    assert reviewers == ["chatgpt"]
    expected = json.dumps({
        "project": "G0-1 capability pass", "goal": goal,
        "success_criteria": success, "constraints": constraints,
        "task": {"title": "作業", "description": "作業する",
                 "acceptance_criteria": "完了"},
        "detected_capability_gaps": gaps,
        "available_capabilities": [
            "local Ollama reasoning", "project-scoped Workspace read",
            "mkdir/write_text/append_text/copy/move inside the project Workspace",
            "safe local CSV/TSV/XLSX/XLSM profile, filter, aggregate and one-to-one join",
            "selected external AI review only when explicitly allowed",
        ],
        "forbidden": ["delete", "arbitrary shell", "access outside /workspace",
                      "secret transmission"],
    }, ensure_ascii=False, indent=2)
    assert sent.encode("utf-8") == expected.encode("utf-8")
    assert reviews[0]["ok"] is True
    assert "解決の手順" in resolution
    audited = _audited_events(memory, pid)
    assert len(audited) == 1
    detail = json.loads(audited[0]["detail"])
    assert detail["purpose"] == "capability_review"
    assert detail["sha256"] == hashlib.sha256(sent.encode("utf-8")).hexdigest()
    assert not _blocked_events(memory, pid)
    await manager.shutdown()


@pytest.mark.asyncio
async def test_research_passes_with_identical_body(tmp_path):
    calls = []

    async def fake_research(prompt, providers, synthesizer, style, context):
        calls.append((prompt, providers, synthesizer, style, context))
        return {"synthesis": "公開の知見"}

    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("G0-1 research pass")
    pid = project["id"]
    _mission(memory, pid)
    description = "公開情報を調査する"
    mission = memory.replace_plan(pid, "plan", [{
        "task_key": "r1", "depends_on": [], "title": "公開調査",
        "description": description, "acceptance_criteria": "調査メモ",
        "mode": "research",
    }])
    manager = ProjectOrchestrator(
        memory,
        _FakeLlm('{"result":"調査した","operations":[],"table_operations":[],'
                 '"source_references":[],"capability_gaps":[]}'),
        lambda project: ("", []), fake_research,
        lambda: [{"id": "chatgpt", "configured": True}],
    )

    result = await manager._execute_legacy_task(pid, mission["tasks"][0]["id"])

    assert len(calls) == 1
    prompt, providers, synthesizer, style, context = calls[0]
    expected = ("公開情報だけを対象に次の調査タスクを評価してください。"
                "資料原本、Workspace、試算表、個人情報は提供されていません。"
                "不明な事実やURLを作らず、提案はローカル側で検証します。\n\n"
                f"タスク: 公開調査\n条件: {description[:4000]}")
    assert prompt.encode("utf-8") == expected.encode("utf-8")
    assert (providers, synthesizer, style, context) == (["chatgpt"], "chatgpt", "report", "")
    assert "調査した" in result
    audited = _audited_events(memory, pid)
    assert len(audited) == 1
    detail = json.loads(audited[0]["detail"])
    assert detail["purpose"] == "research"
    assert detail["providers"] == ["chatgpt"]
    assert detail["sha256"] == hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    assert not _blocked_events(memory, pid)
    await manager.shutdown()


# ---- 受入4: 判定不能は送らない ----

@pytest.mark.asyncio
async def test_undecidable_originals_are_not_sent(tmp_path):
    calls = []

    async def fake_runner(text, providers):
        calls.append((text, providers))
        return [{"id": "x", "label": "X", "model": "m", "ok": True, "review": "よい"}]

    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("G0-1 undecidable")
    pid = project["id"]
    _mission(memory, pid)
    manager = ProjectOrchestrator(
        memory, _plan_llm(), lambda project: ("", []), None,
        lambda: [{"id": "chatgpt", "configured": True}], fake_runner,
    )

    def boom(project_id, include_content=False):
        if not include_content:
            raise OSError("list unavailable")
        return ShortTermMemory.list_context_files(memory, project_id,
                                                  include_content=True)

    memory.list_context_files = boom
    mission = await manager.generate_plan(pid)

    assert calls == []
    blocked = _blocked_events(memory, pid)
    assert len(blocked) == 1
    assert json.loads(blocked[0]["detail"])["codes"] == ["check_error"]
    assert mission["plan_reviews"][0]["id"] == "privacy_guard"
    assert [t["task_key"] for t in mission["tasks"]] == ["draft"]
    await manager.shutdown()


# ---- 受入5: ランナーNoneの既存動作は不変 ----

@pytest.mark.asyncio
async def test_no_runner_keeps_existing_behavior(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("G0-1 no runner")
    pid = project["id"]
    _mission(memory, pid)
    manager = ProjectOrchestrator(
        memory, _plan_llm(), lambda project: ("", []), None,
        lambda: [{"id": "chatgpt", "configured": True}], None,
    )
    mission = await manager.generate_plan(pid)

    assert mission["plan_reviews"] == []
    kinds = {e["kind"] for e in memory.get_mission(pid, event_limit=500)["events"]}
    assert "external_send_blocked_privacy" not in kinds
    assert "external_send_audited" not in kinds
    await manager.shutdown()


@pytest.mark.asyncio
async def test_failover_without_runner_keeps_existing_behavior(tmp_path):
    async def boom(project_id, task_id, recovery_guidance=""):
        raise ValueError("local execution failed")

    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("G0-1 failover no runner")
    pid = project["id"]
    _mission(memory, pid)
    mission = memory.replace_plan(pid, "plan", [{
        "task_key": "t1", "depends_on": [], "title": "作業",
        "description": "作業する", "acceptance_criteria": "完了", "mode": "local",
    }])
    manager = _manager(memory)
    manager.llm = _FakeLlm("再調整の指針")
    manager._execute_task = boom

    result = await manager._retry_failed_task_with_alternate_ai(
        pid, mission["tasks"][0], "execution", "通常の失敗")

    assert result is None
    kinds = {e["kind"] for e in memory.get_mission(pid, event_limit=500)["events"]}
    assert "task_ai_failover_skipped" in kinds
    assert "external_send_blocked_privacy" not in kinds
    assert "external_send_audited" not in kinds
    await manager.shutdown()


@pytest.mark.asyncio
async def test_capability_without_runner_keeps_existing_behavior(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("G0-1 capability no runner")
    pid = project["id"]
    _mission(memory, pid)
    mission = memory.replace_plan(pid, "plan", [{
        "task_key": "t1", "depends_on": [], "title": "作業",
        "description": "作業する", "acceptance_criteria": "完了", "mode": "local",
    }])
    manager = _manager(memory)
    manager.llm = _FakeLlm("解決の手順")

    resolution, reviews = await manager._resolve_capability_gaps(
        pid, mission["tasks"][0], [{"description": "不足"}])

    assert reviews == []
    kinds = {e["kind"] for e in memory.get_mission(pid, event_limit=500)["events"]}
    assert "capability_review_skipped" in kinds
    assert "external_send_blocked_privacy" not in kinds
    assert "external_send_audited" not in kinds
    await manager.shutdown()


@pytest.mark.asyncio
async def test_research_without_providers_keeps_existing_behavior(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("G0-1 research no providers")
    pid = project["id"]
    memory.save_mission(pid, "通常の目標", "通常の達成条件", "通常の制約",
                        True, [])
    mission = memory.replace_plan(pid, "plan", [{
        "task_key": "r1", "depends_on": [], "title": "公開調査",
        "description": "公開情報を調査する", "acceptance_criteria": "調査メモ",
        "mode": "research",
    }])
    manager = ProjectOrchestrator(
        memory,
        _FakeLlm('{"result":"調査した","operations":[],"table_operations":[],'
                 '"source_references":[],"capability_gaps":[]}'),
        lambda project: ("", []), None, lambda: [],
    )

    result = await manager._execute_legacy_task(pid, mission["tasks"][0]["id"])

    assert "調査した" in result
    kinds = {e["kind"] for e in memory.get_mission(pid, event_limit=500)["events"]}
    assert "external_fallback" in kinds
    assert "external_send_blocked_privacy" not in kinds
    assert "external_send_audited" not in kinds
    await manager.shutdown()


# ---- 原本名の統合 ----

@pytest.mark.asyncio
async def test_registered_original_name_blocks(tmp_path):
    calls = []

    async def fake_runner(text, providers):
        calls.append((text, providers))
        return [{"id": "x", "label": "X", "model": "m", "ok": True, "review": "よい"}]

    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("G0-1 original name")
    pid = project["id"]
    source = memory.add_context_file(
        pid, "g0-1-source-secret98.md", "確認可能な原本文", 9,
        "確認可能な原本文".encode("utf-8"), "text/markdown", "markdown",
        "extracted", "hash-g01",
    )
    _mission(memory, pid, goal="計画を作る g0-1-source-secret98.md")
    manager = ProjectOrchestrator(
        memory, _plan_llm(), lambda project: ("", []), None,
        lambda: [{"id": "chatgpt", "configured": True}], fake_runner,
    )
    mission = await manager.generate_plan(pid)

    assert calls == []
    blocked = _blocked_events(memory, pid)
    assert len(blocked) == 1
    assert json.loads(blocked[0]["detail"])["codes"] == ["blocked_term"]
    assert mission["plan_reviews"][0]["id"] == "privacy_guard"
    assert source["id"]
    _no_leak(memory, pid, "g0-1-source-secret98.md", "確認可能な原本文")
    await manager.shutdown()
