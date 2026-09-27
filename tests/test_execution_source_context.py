import pytest

from app.memory.short_term import ShortTermMemory
from app.project_manager import ProjectOrchestrator, build_execution_source_context
from app.workspace_files import WorkspaceSandbox


def test_execution_sources_are_fair_and_generated_memos_cannot_displace_uploads():
    files = [
        {"filename": "__project_memory__/20_execution_log.md", "source": "memo",
         "file_kind": "memo", "content": "MEMO_ONLY " * 5000},
        {"filename": "vehicle.xlsx", "source": "upload", "file_kind": "spreadsheet",
         "content": "VEHICLE_DATA " * 1000},
        {"filename": "payroll.xlsx", "source": "upload", "file_kind": "spreadsheet",
         "content": "PAYROLL_DATA " * 1000},
        {"filename": "insurance.pdf", "source": "upload", "file_kind": "pdf",
         "content": "INSURANCE_DATA " * 1000},
    ]

    result = build_execution_source_context("fallback", files, max_chars=1800)

    assert len(result) <= 1800
    assert "vehicle.xlsx" in result and "VEHICLE_DATA" in result
    assert "payroll.xlsx" in result and "PAYROLL_DATA" in result
    assert "insurance.pdf" in result and "INSURANCE_DATA" in result
    assert "MEMO_ONLY" not in result


def test_execution_source_context_identifies_the_exact_presented_reference():
    files = [{
        "id": "source-123",
        "filename": "facts.md",
        "source": "upload",
        "file_kind": "markdown",
        "content": "確認可能な原本文",
    }]

    result = build_execution_source_context("", files)

    assert "### Extracted content:" in result
    assert "ref=context:source-123" in result


def test_backend_attaches_evidence_for_sources_presented_to_local_llm(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("source evidence")
    source = memory.add_context_file(
        project["id"], "facts.md", "確認可能な原本文", 27,
        "確認可能な原本文".encode("utf-8"), "text/markdown",
        "markdown", "extracted", "hash",
    )
    mission = memory.replace_plan(project["id"], "read", [{
        "task_key": "read", "depends_on": [], "title": "原本確認",
        "description": "原本を読み取る", "acceptance_criteria": "", "mode": "local",
    }])
    orchestrator = ProjectOrchestrator(
        memory, object(), lambda selected: ("", []), None, lambda: [],
        workspace=WorkspaceSandbox(tmp_path / "workspace"),
    )

    _, _, evidence = orchestrator._apply_execution_response(
        project, project["id"], mission["tasks"][0]["id"],
        '{"result":"確認","operations":[],"table_operations":[],"source_references":[],"capability_gaps":[]}',
        {source["id"]}, {source["id"]},
    )

    assert evidence["source_references"][0]["reference"] == f"context:{source['id']}"
    events = memory.get_mission(project["id"])["events"]
    assert any(event["kind"] == "source_evidence_attached" for event in events)


def test_verifier_rejects_unrelated_employee_content_and_unsupported_freshness_claim(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("artifact quality")
    workspace = WorkspaceSandbox(tmp_path / "workspace")
    audit = workspace.apply_operations(project["workspace_path"], project["id"], [{
        "action": "write_text",
        "path": "result/report.md",
        "content": (
            "# Company Employee Summary\n\n"
            "この報告では、すべての原本が最新であることを確認済みです。\n\n"
            + "検証対象の説明。" * 30
        ),
    }])
    orchestrator = ProjectOrchestrator(
        memory, object(), lambda selected: ("", []), None, lambda: [], workspace=workspace,
    )

    failures, _ = orchestrator._verify_execution_evidence(
        project,
        project["id"],
        {
            "title": "原本確認報告",
            "description": "result/report.mdへ報告を保存する",
            "acceptance_criteria": "result/report.mdが存在する",
        },
        "完了",
        {"workspace_operations": audit, "table_operations": [], "source_references": []},
        [],
    )

    assert any("無関係な応答" in failure for failure in failures)
    assert any("最新性の断定" in failure for failure in failures)


@pytest.mark.asyncio
async def test_extracted_financial_sources_are_sent_only_to_local_executor(tmp_path):
    class FakeLlm:
        def __init__(self):
            self.prompts = []

        async def stream(self, messages):
            self.prompts.append(messages[-1]["content"])
            yield '{"result":"analyzed extracted table","operations":[],"capability_gaps":[]}'

    research_prompts = []

    async def research_runner(prompt, providers, synthesizer, style, context):
        research_prompts.append(prompt + context)
        return {"synthesis": "public methodology only", "results": []}

    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("local source confinement")
    memory.save_mission(
        project["id"], "analyze locally", "table checked", "",
        True, ["chatgpt"], 1,
    )
    mission = memory.replace_plan(project["id"], "one step", [
        {"task_key": "analyze", "depends_on": [], "title": "analyze",
         "description": "read the supplied table", "mode": "research"},
    ])
    source_files = [{
        "filename": "payroll.xlsx", "source": "upload", "file_kind": "spreadsheet",
        "content": "PRIVATE_PAYROLL_VALUE_123",
    }]
    llm = FakeLlm()
    orchestrator = ProjectOrchestrator(
        memory, llm, lambda project: ("non-sensitive project summary", source_files),
        research_runner,
        lambda: [{"id": "chatgpt", "configured": True}],
    )

    result = await orchestrator._execute_task(project["id"], mission["tasks"][0]["id"])

    assert result == "analyzed extracted table"
    assert research_prompts and "PRIVATE_PAYROLL_VALUE_123" not in research_prompts[0]
    assert "PRIVATE_PAYROLL_VALUE_123" in llm.prompts[0]
    assert "missing Python runtime" in llm.prompts[0]
    await orchestrator.shutdown()
