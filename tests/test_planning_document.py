import json

import pytest

from app.memory.short_term import ShortTermMemory
from app.planning_document import planning_output, render_sections, capability_context
from app.project_manager import ProjectOrchestrator, TaskVerificationError
from app.structured_planning import compile_task, compile_plan, contract_of
from app.workspace_files import WorkspaceSandbox


def task_fixture():
    return compile_task(1, "詳細な計画を作成してください", {
        "title": "調査計画", "scope": "作業手順を設計する",
        "headings": ["調査手順とスケジュール", "公式情報収集項目一覧", "外部検索タスク詳細",
                     "事例とシステム構成図", "レビューと承認フロー"], "depends_on": [],
    }, [])


def payload(output):
    return {"sections": {f"section_{i}": (
        "以下は仮説に基づく実施案。担当者は確認対象を一覧に整理し、取得元と確認日を記録する。"
        "各項目を原本と照合し、不足項目には担当者、確認手順、完了条件を記載する。"
        "レビューの指摘を修正し、人間の承認を得てから次工程を開始する。"
        "本項目は実施計画であり、取得や承認を完了したという報告ではない。"
    ) for i, _ in enumerate(output["required_headings"], 1)}, "missing_inputs": []}


def test_render_owns_all_headings_and_rejects_missing_section():
    task = task_fixture()
    output = planning_output(task)
    data = payload(output)
    document = render_sections(task, output, json.dumps(data))
    assert all(f"## {h}" in document for h in output["required_headings"])
    del data["sections"]["section_1"]
    with pytest.raises(ValueError, match="必須項目"):
        render_sections(task, output, json.dumps(data))


@pytest.mark.parametrize("body", ["", "要確認" * 100])
def test_placeholder_cannot_pass(body):
    task = task_fixture()
    output = planning_output(task)
    data = payload(output)
    data["sections"]["section_1"] = body
    with pytest.raises(ValueError, match="仮置き"):
        render_sections(task, output, json.dumps(data))


def test_specific_missing_input_is_not_silently_removed():
    task = task_fixture()
    output = planning_output(task)
    data = payload(output)
    data["missing_inputs"] = ["対象年度の必須原本"]
    with pytest.raises(ValueError, match="対象年度"):
        render_sections(task, output, json.dumps(data))


def test_scope_excludes_research_and_multi_output():
    task = task_fixture()
    task["mode"] = "research"
    assert planning_output(task) is None
    task["mode"] = "local"
    contract = contract_of(task)
    contract["outputs"].append({"path": "result/table.csv"})
    task["acceptance_criteria"] = json.dumps(contract)
    assert planning_output(task) is None


def test_observed_web_metadata_distinguishes_extracted_hash():
    result = capability_context([{"id": "web1", "source": "web", "content":
        "- URL: https://example.org/rule.pdf\n- 本文SHA256: " + "a" * 64}], True)
    assert "context:web1" in result and "https://example.org/rule.pdf" in result
    assert "PDFバイナリ" in result and "新規取得は調査工程" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [False, True])
async def test_execute_local_plan_preserves_validation_and_never_reviews_externally(tmp_path, missing):
    class LocalLlm:
        calls = 0
        async def complete_json(self, messages, schema):
            self.calls += 1
            assert "バックエンドが観測" in messages[-1]["content"]
            data = payload(output)
            if missing:
                data["missing_inputs"] = ["必要な原本"]
            return json.dumps(data, ensure_ascii=False)

    async def no_external(*args, **kwargs):
        raise AssertionError("Local planning must not send data externally")

    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("計画検証", workspace_path="projects/check")
    memory.save_mission(project["id"], "調査計画を作る", "", "", False, [])
    task = task_fixture()
    output = planning_output(task)
    plan = compile_plan(["詳細な計画を作成してください"], [task])
    mission = memory.replace_plan(project["id"], plan["summary"], plan["tasks"])
    preparation = next(t for t in mission["tasks"] if t["task_key"] == "SC00")
    memory.update_task(preparation["id"], "completed", result="確認済み")
    current = next(t for t in memory.get_mission(project["id"])["tasks"] if t["task_key"] == "SC01")
    workspace = WorkspaceSandbox(tmp_path / "workspace")
    llm = LocalLlm()
    manager = ProjectOrchestrator(memory, llm, lambda p: ("", []), no_external, lambda: [], workspace=workspace)
    result = await manager._execute_task(project["id"], current["id"])
    assert "合格" in result
    assert llm.calls == 1
    if missing:
        document = next((tmp_path / "workspace").rglob("sc01.md")).read_text(encoding="utf-8")
        assert "必要な原本" in document and "実行前に解消する資料条件" in document
    assert any(e["kind"] == "structured_planning_document_generated"
               for e in memory.get_mission(project["id"])["events"])
@pytest.mark.parametrize("bad", ["検索を実施しました。", "公式PDFをダウンロードしました。"])
def test_unobserved_completed_actions_are_rejected(bad):
    task = task_fixture()
    output = planning_output(task)
    data = payload(output)
    data["sections"]["section_1"] += bad
    with pytest.raises(ValueError, match="完了表現"):
        render_sections(task, output, json.dumps(data))
@pytest.mark.asyncio
async def test_public_guideline_plan_uses_observed_sources_without_any_llm(tmp_path):
    from app.planning_document import public_research_plan_sections
    class NeverLlm:
        async def complete(self, *a):
            raise AssertionError("No LLM required for this procedural plan")
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("計画", workspace_path="projects/check")
    memory.save_mission(project["id"], "計画作成", "", "", False, [])
    task = task_fixture()
    task["title"] = "公募要領調査計画"
    contract = contract_of(task)
    contract["criterion"] = "まずは計画を詳細に作成してください"
    task["acceptance_criteria"] = json.dumps(contract, ensure_ascii=False)
    capture = "- URL: https://example.org/rule\n- 取得日時(UTC): 2026-09-15T00:00:00Z\n- 本文SHA256: " + "a" * 64 + "\n## 抽出本文\n公募要領の本文"
    source = memory.add_context_file(project["id"], "capture.md", capture, len(capture.encode()), source="web")
    files = memory.list_context_files(project["id"], include_content=True)
    assert public_research_plan_sections(task, contract["outputs"][0], []) is None
    candidate = public_research_plan_sections(task, contract["outputs"][0], files)
    document = render_sections(task, contract["outputs"][0], json.dumps(candidate, ensure_ascii=False))
    assert "context:" + source["id"] in document
    assert "仮説" in document and "D0" in document and "人間" in document
    plan = compile_plan([contract["criterion"]], [task])
    mission = memory.replace_plan(project["id"], plan["summary"], plan["tasks"])
    current = next(t for t in mission["tasks"] if t["task_key"] == "SC01")
    manager = ProjectOrchestrator(memory, NeverLlm(), lambda p: ("", files), None, lambda: [],
                                  workspace=WorkspaceSandbox(tmp_path / "workspace"))
    result = await manager._execute_task(project["id"], current["id"])
    assert "合格" in result
