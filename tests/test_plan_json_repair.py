import pytest

from app.memory.short_term import ShortTermMemory
from app.project_manager import ProjectOrchestrator


@pytest.mark.asyncio
async def test_plan_generation_repairs_invalid_json_with_local_llm(tmp_path):
    class FakeLlm:
        def __init__(self):
            self.responses = [
                '{"summary":"壊れた計画","tasks":[{"task_key":"collect" "depends_on":[],"title":"収集"}]}',
                '{"summary":"修復済み計画","tasks":['
                '{"task_key":"collect","depends_on":[],"title":"収集","mode":"local"},'
                '{"task_key":"report","depends_on":["collect"],"title":"Excel報告","mode":"local"}'
                ']}',
            ]
            self.messages = []

        async def stream(self, messages):
            self.messages.append(messages)
            yield self.responses.pop(0)

    async def no_external(*args, **kwargs):
        raise AssertionError("JSON修復で外部AIを呼び出してはいけない")

    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("JSON修復")
    memory.save_mission(
        project["id"], "損益表を作る", "Excel完成", "外部送信禁止", False, [],
    )
    llm = FakeLlm()
    orchestrator = ProjectOrchestrator(
        memory, llm, lambda project: ("", []), no_external, lambda: [],
    )

    mission = await orchestrator.generate_plan(project["id"])

    assert mission["plan_summary"] == "修復済み計画"
    assert [task["task_key"] for task in mission["tasks"]] == ["collect", "report"]
    assert mission["tasks"][1]["depends_on"] == ["collect"]
    assert "Expecting ',' delimiter" in llm.messages[1][-1]["content"]
    assert "壊れた計画" in llm.messages[1][-1]["content"]
    kinds = [event["kind"] for event in mission["events"]]
    assert "plan_json_repair_started" in kinds
    assert "plan_json_repaired" in kinds
    await orchestrator.shutdown()


@pytest.mark.asyncio
async def test_plan_generation_reports_second_invalid_json(tmp_path):
    class AlwaysInvalidLlm:
        async def stream(self, messages):
            yield '{"summary":"still broken"'

    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("JSON再失敗")
    memory.save_mission(project["id"], "計画を作る", "検証済み", "", False, [])
    orchestrator = ProjectOrchestrator(
        memory, AlwaysInvalidLlm(), lambda project: ("", []), None, lambda: [],
    )

    with pytest.raises(ValueError, match="再生成しても不正"):
        await orchestrator.generate_plan(project["id"])

    mission = memory.get_mission(project["id"])
    assert mission["tasks"] == []
    assert any(event["kind"] == "plan_json_repair_failed" for event in mission["events"])
    assert any(event["kind"] == "plan_json_repair_retry" for event in mission["events"])
    await orchestrator.shutdown()
