import asyncio

import pytest

from app.memory.short_term import ShortTermMemory
from app.project_manager import ProjectOrchestrator


@pytest.mark.asyncio
async def test_capability_gap_is_reviewed_integrated_and_retried(tmp_path):
    class FakeLlm:
        def __init__(self):
            self.responses = [
                '{"result":"missing converter","operations":[],"capability_gaps":[{"description":"conversion unavailable","required_capability":"safe converter","reason":"not present","attempted":"checked workspace"}]}',
                "Use a project-scoped text conversion fallback and verify its output.",
                '{"result":"fallback implemented and verified","operations":[],"capability_gaps":[]}',
                '{"goal_status":"passed","report_markdown":"# Final report\\n\\nCompleted with the reviewed fallback.","unmet_conditions":[]}',
            ]

        async def stream(self, messages):
            yield self.responses.pop(0)

    review_payloads = []

    async def capability_reviews(payload, providers):
        review_payloads.append((payload, providers))
        assert "PRIVATE_STATIC_CONTEXT" not in payload
        assert "conversion unavailable" in payload
        return [
            {"id": "chatgpt", "label": "ChatGPT", "model": "fake", "ok": True,
             "review": "Use a safe text fallback and verify output."},
            {"id": "gemini", "label": "Gemini", "model": "fake", "ok": False,
             "error": "temporary failure"},
        ]

    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("Capability recovery", "PRIVATE_STATIC_CONTEXT")
    memory.save_mission(
        project["id"], "Complete safely", "Verified output", "No shell",
        True, ["chatgpt", "gemini"], 1,
    )
    memory.replace_plan(project["id"], "One task", [
        {"task_key": "implement", "depends_on": [], "title": "Implement fallback",
         "description": "Produce the artifact", "acceptance_criteria": "Verified"},
    ])
    orchestrator = ProjectOrchestrator(
        memory, FakeLlm(), lambda project: (project["context_text"], []),
        None,
        lambda: [
            {"id": "chatgpt", "configured": True},
            {"id": "gemini", "configured": True},
        ],
        capability_review_runner=capability_reviews,
    )
    orchestrator.approve(project["id"])
    await orchestrator.start(project["id"])
    for _ in range(200):
        mission = memory.get_mission(project["id"])
        if mission["status"] in {"completed", "failed"}:
            break
        await asyncio.sleep(0.01)

    assert mission["status"] == "completed"
    assert len(review_payloads) == 1
    assert "fallback implemented" in mission["tasks"][0]["result"]
    kinds = {event["kind"] for event in mission["events"]}
    assert {"capability_gap_detected", "capability_review_started",
            "capability_review_completed", "capability_resolution_created",
            "capability_gap_resolved"}.issubset(kinds)
    await orchestrator.shutdown()


@pytest.mark.asyncio
async def test_unresolved_capability_gap_produces_failure_report(tmp_path):
    class FakeLlm:
        def __init__(self):
            self.responses = [
                '{"result":"blocked","operations":[],"capability_gaps":[{"description":"licensed service required","required_capability":"licensed API","reason":"no credential","attempted":"checked configured providers"}]}',
                "A licensed API and human-provided credential are required; no safe local substitute exists.",
                '{"result":"still blocked","operations":[],"capability_gaps":[{"description":"licensed service still required","required_capability":"licensed API","reason":"no credential","attempted":"reviewed local alternatives"}]}',
            ]

        async def stream(self, messages):
            yield self.responses.pop(0)

    async def capability_reviews(payload, providers):
        return [{"id": "chatgpt", "label": "ChatGPT", "model": "fake", "ok": True,
                 "review": "No compliant substitute; report the credential requirement."}]

    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("Unresolved capability")
    memory.save_mission(
        project["id"], "Use licensed service", "Validated result", "No secret handling",
        True, ["chatgpt"], 1,
    )
    memory.replace_plan(project["id"], "Blocked plan", [
        {"task_key": "licensed", "depends_on": [], "title": "Call licensed service"},
    ])
    orchestrator = ProjectOrchestrator(
        memory, FakeLlm(), lambda project: ("", []), None,
        lambda: [{"id": "chatgpt", "configured": True}],
        capability_review_runner=capability_reviews,
    )
    orchestrator.approve(project["id"])
    await orchestrator.start(project["id"])
    for _ in range(200):
        mission = memory.get_mission(project["id"])
        if mission["status"] in {"completed", "failed"}:
            break
        await asyncio.sleep(0.01)

    assert mission["status"] == "failed"
    assert "licensed service still required" in mission["tasks"][0]["error"]
    assert "Unresolved capability" in mission["final_report"]
    assert "licensed service still required" in mission["final_report"]
    assert any(event["kind"] == "capability_gap_unresolved" for event in mission["events"])
    memos = memory.list_context_files(project["id"], include_content=True)
    final_memo = next(item for item in memos if item["filename"] == "__project_memory__/90_final_report.md")
    assert "failed" in final_memo["content"]
    await orchestrator.shutdown()


@pytest.mark.asyncio
async def test_capability_is_not_marked_resolved_before_evidence_passes(tmp_path):
    class FakeLlm:
        def __init__(self):
            self.responses = [
                '{"result":"missing","capability_gaps":[{"description":"need output"}]}',
                "Create the output with an allowed operation and verify it.",
                    '{"result":"claimed retry","operations":[],"capability_gaps":[]}',
                    '{"result":"claimed correction","operations":[],"capability_gaps":[]}',
                    '{"result":"claimed format repair","operations":[],"capability_gaps":[]}',
                ]

        async def stream(self, messages):
            yield self.responses.pop(0)

    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("strict remediation")
    memory.save_mission(project["id"], "save output", "file saved", "", False, [])
    mission = memory.replace_plan(project["id"], "one", [{
        "task_key": "save", "depends_on": [], "title": "成果ファイル保存",
        "description": "成果物を保存する", "acceptance_criteria": "output.mdファイルを保存",
    }])
    orchestrator = ProjectOrchestrator(
        memory, FakeLlm(), lambda selected: ("", []), None, lambda: [],
    )

    with pytest.raises(Exception, match="実証失敗"):
        await orchestrator._execute_task(project["id"], mission["tasks"][0]["id"])

    kinds = [event["kind"] for event in memory.get_mission(project["id"])["events"]]
    assert "capability_gap_resolved" not in kinds
    assert "task_correction_failed" in kinds


@pytest.mark.asyncio
async def test_failed_task_uses_permitted_alternate_ai_once(tmp_path):
    class FakeLlm:
        async def complete(self, messages):
            assert "許可済み外部AIの助言" in messages[-1]["content"]
            return "安全な宣言型操作へ変更し、成果物をバックエンド検証する"

    review_calls = []

    async def reviews(payload, providers):
        review_calls.append((payload, providers))
        assert "資料原本、抽出本文、Workspace内容" in payload
        return [{"id": "chatgpt", "label": "ChatGPT", "model": "test",
                 "ok": True, "review": "安全な表実行器を利用して再試行する"}]

    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("failover")
    memory.save_mission(
        project["id"], "成果物作成", "検証済み成果物", "ローカル境界を守る",
        True, ["chatgpt"],
    )
    mission = memory.replace_plan(project["id"], "one task", [{
        "task_key": "work", "depends_on": [], "title": "再調整対象",
        "description": "成果物を作る", "acceptance_criteria": "検証に合格", "mode": "local",
    }])
    orchestrator = ProjectOrchestrator(
        memory, FakeLlm(), lambda selected: ("", []), None,
        lambda: [{"id": "chatgpt", "configured": True}],
        capability_review_runner=reviews,
    )
    retry_guidance = []

    async def execute(project_id, task_id, recovery_guidance=""):
        retry_guidance.append(recovery_guidance)
        return "再実行成功"

    orchestrator._execute_task = execute
    result = await orchestrator._retry_failed_task_with_alternate_ai(
        project["id"], mission["tasks"][0], "verification", "成果物なし"
    )

    assert result == "再実行成功"
    assert len(review_calls) == 1
    assert retry_guidance and "安全な宣言型操作" in retry_guidance[0]
    kinds = [event["kind"] for event in memory.get_mission(project["id"])["events"]]
    assert "task_ai_failover_started" in kinds
    assert "task_ai_failover_succeeded" in kinds


@pytest.mark.asyncio
async def test_failed_task_does_not_contact_alternate_ai_without_consent(tmp_path):
    async def reviews(*args, **kwargs):
        raise AssertionError("外部AIを呼び出してはいけない")

    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("local only")
    memory.save_mission(project["id"], "local", "done", "no external", False, ["chatgpt"])
    mission = memory.replace_plan(project["id"], "one task", [{
        "task_key": "work", "depends_on": [], "title": "ローカル限定",
        "description": "ローカル実行", "acceptance_criteria": "完了", "mode": "local",
    }])
    orchestrator = ProjectOrchestrator(
        memory, object(), lambda selected: ("", []), None,
        lambda: [{"id": "chatgpt", "configured": True}],
        capability_review_runner=reviews,
    )
    result = await orchestrator._retry_failed_task_with_alternate_ai(
        project["id"], mission["tasks"][0], "execution", "failure"
    )
    assert result is None
    assert any(
        event["kind"] == "task_ai_failover_skipped"
        for event in memory.get_mission(project["id"])["events"]
    )


def test_retry_classifier_and_fingerprint_are_bounded():
    classification, retryable = ProjectOrchestrator._classify_task_failure(
        "execution", "許可された表データ操作はprofileまたはtransformだけです action=create_table"
    )
    assert classification == "unsupported_operation"
    assert retryable is True
    first = ProjectOrchestrator._task_failure_fingerprint(classification, "error row 123 id abcdef1234567890")
    second = ProjectOrchestrator._task_failure_fingerprint(classification, "error row 456 id ffffff1234567890")
    assert first == second

    classification, retryable = ProjectOrchestrator._classify_task_failure(
        "execution", "Workspace外へのアクセスは禁止です"
    )
    assert classification == "safety_or_permission"
    assert retryable is False
