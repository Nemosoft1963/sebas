import asyncio
import json

import pytest

from app.memory.short_term import ShortTermMemory
from app.project_manager import (
    ProjectOrchestrator, _constrain_table_operation_sources, assess_plan_quality,
    ensure_final_verification_task, parse_plan_response,
)


def test_mission_persists_plan_tasks_events_and_progress(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("目標管理")
    mission = memory.save_mission(
        project["id"], "報告書を完成させる", "2章構成でレビュー済み", "ローカル優先",
        False, [],
    )
    assert mission["status"] == "draft"
    mission = memory.replace_plan(project["id"], "調査して執筆する", [
        {"title": "調査", "description": "前提を整理", "acceptance_criteria": "論点一覧", "mode": "local"},
        {"title": "執筆", "description": "本文を作成", "acceptance_criteria": "2章構成", "mode": "local"},
    ])
    assert mission["status"] == "planning"
    assert mission["progress"] == {"completed": 0, "total": 2, "percent": 0}
    first = mission["tasks"][0]
    memory.update_task(first["id"], "running")
    memory.update_task(first["id"], "completed", result="論点一覧")
    mission = memory.get_mission(project["id"])
    assert mission["progress"] == {"completed": 1, "total": 2, "percent": 50}
    assert mission["tasks"][0]["attempts"] == 1
    assert any(event["kind"] == "plan_generated" for event in mission["events"])


def test_running_mission_is_recovered_as_paused(tmp_path):
    path = tmp_path / "memory.db"
    memory = ShortTermMemory(path)
    project = memory.create_project("再起動")
    memory.save_mission(project["id"], "復元確認", "", "", False, [])
    mission = memory.replace_plan(project["id"], "復元テスト", [{"title": "処理"}])
    memory.set_mission_status(project["id"], "running")
    memory.update_task(mission["tasks"][0]["id"], "running")

    recovered = ShortTermMemory(path).get_mission(project["id"])
    assert recovered["status"] == "paused"
    assert recovered["tasks"][0]["status"] == "pending"
    assert recovered["events"][0]["kind"] == "recovered"


def test_parse_plan_response_handles_fenced_json_and_external_policy():
    response = chr(96) * 3 + """json
{"summary":"確認計画","tasks":[{"title":"調査","mode":"research"}]}
""" + chr(96) * 3
    local = parse_plan_response(response, allow_external_ai=False)
    external = parse_plan_response(response, allow_external_ai=True)
    assert local["tasks"][0]["mode"] == "local"
    assert external["tasks"][0]["mode"] == "research"


@pytest.mark.parametrize("description", [
    "PythonとpandasでDataFrameを作成する",
    "結果をreport.xlsxとして出力する",
    "収入は仮に0として損益を計算する",
    "シート名は損益と仮定する",
    "成果物をfinal.zipへ圧縮する",
])
def test_parse_plan_rejects_unavailable_or_placeholder_execution(description):
    response = (
        '{"summary":"invalid","tasks":[{"task_key":"bad","title":"処理",'
        '"description":' + __import__("json").dumps(description, ensure_ascii=False) + '}]} '
    )

    with pytest.raises(ValueError):
        parse_plan_response(response)


def test_parse_plan_allows_xlsx_input_when_output_is_csv():
    plan = parse_plan_response(
        '{"summary":"valid","tasks":[{"task_key":"convert","title":"CSV化",'
        '"description":"input.xlsxを読み込み、output.csvを作成して保存する",'
        '"acceptance_criteria":"output.csvを保存し、入力input.xlsxのrefを確認する"}]}'
    )

    assert plan["tasks"][0]["task_key"] == "convert"


def test_parse_plan_normalizes_workspace_absolute_paths_in_task_text():
    plan = parse_plan_response(
        '{"summary":"valid","tasks":[{"task_key":"save","title":"保存",'
        '"description":"結果を/ workspace/output/result.csvへ出力",'
        '"acceptance_criteria":"/workspace/output/result.csvが存在する"}]}'
    )

    assert plan["tasks"][0]["description"].endswith("output/result.csvへ出力")
    assert plan["tasks"][0]["acceptance_criteria"] == "output/result.csvが存在する"


def test_parse_plan_rejects_source_schema_counts_that_disagree_with_inventory():
    inventory = """- source=context:source123456 | ledger.xlsx | kind=spreadsheet
  sheet=車両関係 | header_row=4 | rows=34 | columns=[車番, 売上] | column_count=17"""
    response = (
        '{"summary":"invalid","tasks":[{"task_key":"read","title":"読込",'
        '"description":"ledger.xlsxを読む",'
        '"acceptance_criteria":"ref=source123456、行数32、列数36"}]}'
    )

    with pytest.raises(ValueError, match="実スキーマ"):
        parse_plan_response(response, table_inventory=inventory)


def test_parse_plan_accepts_source_schema_counts_that_match_inventory():
    inventory = """- source=context:source123456 | ledger.xlsx | kind=spreadsheet
  sheet=車両関係 | header_row=4 | rows=34 | columns=[車番, 売上] | column_count=17"""
    response = (
        '{"summary":"valid","tasks":[{"task_key":"read","title":"読込",'
        '"description":"ledger.xlsxを読む",'
        '"acceptance_criteria":"ref=context:source123456、行数34、列数17"}]}'
    )

    plan = parse_plan_response(response, table_inventory=inventory)
    assert plan["tasks"][0]["task_key"] == "read"


def test_parse_plan_rejects_missing_required_registered_source_reference():
    response = (
        '{"summary":"燃料を集計する","tasks":[{"task_key":"fuel","title":"燃料",'
        '"description":"ref=context:fuel123456を使用する"}]}'
    )

    with pytest.raises(ValueError, match="欠落"):
        parse_plan_response(
            response,
            required_source_refs={"fuel123456", "sales123456"},
        )


def test_parse_plan_accepts_all_required_registered_source_references():
    response = (
        '{"summary":"全資料を使用する","tasks":[{"task_key":"fuel","title":"燃料",'
        '"description":"ref=context:fuel123456を使用する"},'
        '{"task_key":"sales","depends_on":["fuel"],"title":"売上",'
        '"description":"ref=context:sales123456を使用する"}]}'
    )

    plan = parse_plan_response(
        response,
        required_source_refs={"fuel123456", "sales123456"},
    )
    assert len(plan["tasks"]) == 2


@pytest.mark.asyncio
async def test_orchestrator_generates_and_executes_plan_with_local_llm(tmp_path):
    class FakeLlm:
        def __init__(self):
            self.responses = [
                '{"summary":"二段階計画","tasks":[{"title":"整理","description":"要件整理","acceptance_criteria":"一覧"},{"title":"作成","description":"成果作成","acceptance_criteria":"完成"}]}',
                "要件を整理しました。受入条件を満たします。",
                "成果を作成しました。受入条件を満たします。",
                '{"goal_status":"passed","report_markdown":"# 最終報告\\n\\n目標を達成しました。","unmet_conditions":[]}',
            ]

        async def stream(self, messages):
            yield self.responses.pop(0)

    async def no_external(*args, **kwargs):
        raise AssertionError("外部AIは呼び出されない")

    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("自律実行", "固定前提")
    memory.save_mission(project["id"], "成果物を完成させる", "確認済み", "", False, [])
    orchestrator = ProjectOrchestrator(
        memory, FakeLlm(), lambda project: (project["context_text"], []),
        no_external, lambda: [],
    )
    planned = await orchestrator.generate_plan(project["id"])
    assert len(planned["tasks"]) == 2
    orchestrator.approve(project["id"])
    await orchestrator.start(project["id"])

    for _ in range(50):
        result = memory.get_mission(project["id"])
        if result["status"] == "completed":
            break
        await asyncio.sleep(0)
    assert result["status"] == "completed"
    assert result["progress"]["percent"] == 100
    assert all(task["result"] for task in result["tasks"])
    assert "最終報告" in result["final_report"]
    memos = memory.list_context_files(project["id"], include_content=True)
    by_name = {item["filename"]: item["content"] for item in memos if item["source"] == "memo"}
    assert set(by_name) == {
        "__project_memory__/00_goal.md", "__project_memory__/10_plan_and_procedure.md",
        "__project_memory__/20_execution_log.md", "__project_memory__/90_final_report.md",
    }
    assert "成果物を完成させる" in by_name["__project_memory__/00_goal.md"]
    assert "タスク完了" in by_name["__project_memory__/20_execution_log.md"]
    await orchestrator.shutdown()


@pytest.mark.asyncio
async def test_real_world_plan_runs_local_work_before_waiting_for_evidence(tmp_path):
    class FakeLlm:
        def __init__(self):
            self.responses = [
                "ローカル準備の整理結果です。",
                '{"goal_status":"passed","report_markdown":"# 最終報告\\n\\n実行証拠を確認しました。","unmet_conditions":[]}',
            ]

        async def stream(self, messages):
            yield self.responses.pop(0)

    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("外部実行待ち")
    memory.save_mission(
        project["id"], "顧客への提案を実行する", "準備と実行証拠", "", False, [],
    )
    memory.replace_plan(project["id"], "準備後に実行", [{
        "task_key": "prepare", "depends_on": [], "title": "提案準備",
        "description": "提案内容を整理する", "acceptance_criteria": "整理結果",
        "mode": "local",
    }])
    memory.set_mission_status(project["id"], "ready")
    manager = ProjectOrchestrator(
        memory, FakeLlm(), lambda _: ("", []), None, lambda: [],
    )

    await manager.start(project["id"])
    for _ in range(100):
        result = memory.get_mission(project["id"])
        if result["status"] == "paused":
            break
        await asyncio.sleep(0)
    assert result["status"] == "paused"
    assert result["tasks"][0]["status"] == "completed"
    assert any(event["kind"] == "external_execution_required" for event in result["events"])

    action = memory.create_action(project["id"], "proposal", "承認済み対象", "提案内容")
    memory.update_action(project["id"], action["id"], "approved")
    memory.update_action(project["id"], action["id"], "executed", evidence="実施記録")
    await manager.start(project["id"])
    for _ in range(100):
        result = memory.get_mission(project["id"])
        if result["status"] == "completed":
            break
        await asyncio.sleep(0)
    assert result["status"] == "completed"
    await manager.shutdown()


@pytest.mark.asyncio
async def test_action_gated_task_stays_pending_and_pauses_without_evidence(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("最終証拠ゲート")
    memory.save_mission(project["id"], "顧客への提案を実行する", "", "", False, [])
    contract = {
        "schema": "local-cowork-plan/v1", "criterion_ids": [], "inputs": [],
        "source_refs": [], "outputs": [{"path": "result/final_verification.md"}],
        "external_actions": "approval_required", "final_verification": True,
        "action_requirements": [{"kind": "approved_external_action",
                                 "minimum_executed": 1, "evidence_required": True}],
    }
    memory.replace_plan(project["id"], "最終検証", [{
        "task_key": "final_verification", "depends_on": [], "title": "最終検証",
        "description": "実行証拠を検証する", "acceptance_criteria": json.dumps(contract),
        "mode": "local",
    }])
    memory.set_mission_status(project["id"], "ready")
    manager = ProjectOrchestrator(memory, None, lambda _: ("", []), None, lambda: [])

    await manager.start(project["id"])
    for _ in range(50):
        result = memory.get_mission(project["id"])
        if result["status"] == "paused":
            break
        await asyncio.sleep(0)
    assert result["status"] == "paused"
    assert result["tasks"][0]["status"] == "pending"
    assert result["tasks"][0]["attempts"] == 0
    await manager.shutdown()


def test_social_publication_url_counts_as_external_execution_evidence(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("SNS証拠")
    campaign = memory.create_campaign(
        project["id"], "相談", "担当者", "課題整理", "問い合わせ", "",
    )
    memory.save_social_drafts(project["id"], campaign["id"], [{
        "channel": "x", "label": "X", "mode": "composer", "variant": "primary",
        "post_text": "投稿", "tracking_url": "https://example.test/?utm_source=x",
        "compose_url": "https://twitter.com/intent/tweet",
    }])
    memory.update_social_campaign_status(
        project["id"], campaign["id"], "draft_ready", "awaiting_approval",
    )
    memory.update_social_campaign_status(
        project["id"], campaign["id"], "awaiting_approval", "approved",
    )
    memory.register_social_evidence(
        project["id"], campaign["id"], "x", "https://x.com/example/status/1",
    )
    manager = ProjectOrchestrator(memory, None, lambda _: ("", []), None, lambda: [])

    evidence = manager._external_execution_evidence(project["id"])

    post = next(item for item in evidence if item["kind"] == "social_post")
    assert post["channel"] == "x"
    assert post["reference"] == "https://x.com/example/status/1"
    assert {"social_copy_approval", "manual_social_post", "post_url_registration"} <= {
        item["kind"] for item in evidence
    }
    assert not {"form_response_sync", "lead_evaluation"} & {item["kind"] for item in evidence}


@pytest.mark.asyncio
async def test_final_goal_partial_result_is_not_marked_completed(tmp_path):
    class FakeLlm:
        def __init__(self):
            self.responses = [
                "確認結果です。",
                '{"goal_status":"partial","report_markdown":"# 未完了報告",'
                '"unmet_conditions":["必要な成果物がありません"]}',
            ]

        async def stream(self, messages):
            yield self.responses.pop(0)

    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("strict final")
    memory.save_mission(project["id"], "目標達成", "必要な成果物", "", False, [])
    memory.replace_plan(project["id"], "verify", [{
        "task_key": "check", "depends_on": [], "title": "内容確認",
        "description": "内容を確認", "acceptance_criteria": "確認結果", "mode": "local",
    }])
    memory.set_mission_status(project["id"], "ready")
    orchestrator = ProjectOrchestrator(
        memory, FakeLlm(), lambda selected: ("", []), None, lambda: [],
    )

    await orchestrator.start(project["id"])
    for _ in range(100):
        result = memory.get_mission(project["id"])
        if result["status"] == "failed":
            break
        await asyncio.sleep(0)

    assert result["status"] == "failed"
    assert result["tasks"][0]["status"] == "completed"
    assert len(result["tasks"]) > 1
    assert any(event["kind"] == "goal_supplement_planned" for event in result["events"])
    assert not any(event["kind"] == "completed" for event in result["events"])
    assert any(event["kind"] == "goal_verification_failed" for event in result["events"])
    await orchestrator.shutdown()


@pytest.mark.asyncio
async def test_plan_reviews_are_combined_by_local_llm(tmp_path):
    class FakeLlm:
        def __init__(self):
            self.responses = [
                '{"summary":"draft","tasks":[{"task_key":"draft","depends_on":[],"title":"Draft"}]}',
                '{"summary":"refined","tasks":[{"task_key":"research","depends_on":[],"title":"Research"},{"task_key":"verify","depends_on":["research"],"title":"Verify"}]}',
            ]
            self.prompts = []

        async def stream(self, messages):
            self.prompts.append(messages[-1]["content"])
            yield self.responses.pop(0)

    review_calls = []

    async def review_runner(plan_text, provider_ids):
        review_calls.append((plan_text, provider_ids))
        assert '"goal"' in plan_text
        assert '"success_criteria"' in plan_text
        assert '"constraints"' in plan_text
        assert "NEVER_SEND_STATIC_CONTEXT" not in plan_text
        return [
            {"id": "chatgpt", "label": "ChatGPT", "model": "fake-openai", "ok": True,
             "review": "Add a verification task and explicit dependency."},
            {"id": "gemini", "label": "Gemini", "model": "fake-gemini", "ok": False,
             "error": "temporary failure"},
        ]

    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("Reviewed plan", "NEVER_SEND_STATIC_CONTEXT")
    memory.save_mission(
        project["id"], "Improve the plan", "Include verification", "No deletion",
        True, ["chatgpt", "gemini"], 2,
    )
    llm = FakeLlm()
    orchestrator = ProjectOrchestrator(
        memory, llm, lambda project: (project["context_text"], []),
        None,
        lambda: [
            {"id": "chatgpt", "configured": True},
            {"id": "gemini", "configured": True},
        ],
        review_runner,
    )

    mission = await orchestrator.generate_plan(project["id"])
    assert mission["plan_summary"] == "refined"
    assert mission["tasks"][1]["depends_on"] == ["research"]
    assert len(review_calls) == 1
    assert "ChatGPT (fake-openai)" in llm.prompts[1]
    assert len(mission["plan_reviews"]) == 2
    assert any(event["kind"] == "plan_refined" for event in mission["events"])
    await orchestrator.shutdown()

@pytest.mark.asyncio
async def test_parallel_scheduler_waits_for_dependencies_and_honors_limit(tmp_path):
    class FakeLlm:
        async def stream(self, messages):
                yield '{"goal_status":"passed","report_markdown":"# 完了報告","unmet_conditions":[]}'

    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("並列計画")
    memory.save_mission(project["id"], "並列で完了", "CはAとBの後", "", False, [], 2)
    memory.replace_plan(project["id"], "依存付き並列", [
        {"task_key": "A", "depends_on": [], "title": "A"},
        {"task_key": "B", "depends_on": [], "title": "B"},
        {"task_key": "C", "depends_on": ["A", "B"], "title": "C"},
    ])
    orchestrator = ProjectOrchestrator(
        memory, FakeLlm(), lambda project: ("", []), None, lambda: [],
    )
    active = 0
    max_active = 0
    started = []
    c_dependencies = {}

    async def execute(project_id, task_id):
        nonlocal active, max_active, c_dependencies
        task = next(item for item in memory.get_mission(project_id)["tasks"] if item["id"] == task_id)
        started.append(task["task_key"])
        if task["task_key"] == "C":
            c_dependencies = {item["task_key"]: item["status"] for item in memory.get_mission(project_id)["tasks"]}
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return task["task_key"] + " done"

    orchestrator._execute_task = execute
    orchestrator.approve(project["id"])
    await orchestrator.start(project["id"])
    for _ in range(200):
        mission = memory.get_mission(project["id"])
        if mission["status"] in {"completed", "failed"}:
            break
        await asyncio.sleep(0.01)

    assert mission["status"] == "completed"
    assert set(started[:2]) == {"A", "B"}
    assert started[-1] == "C"
    assert c_dependencies["A"] == c_dependencies["B"] == "completed"
    assert max_active == 2
    await orchestrator.shutdown()


def test_plan_parser_rejects_unknown_or_forward_dependency():
    with pytest.raises(ValueError, match="依存先"):
        parse_plan_response(
            '{"summary":"bad","tasks":['
            '{"task_key":"A","depends_on":["B"],"title":"A"},'
            '{"task_key":"B","depends_on":[],"title":"B"}]}'
        )


def test_table_operation_sources_exclude_non_tabular_contexts_at_every_level():
    operations = [{
        "action": "merge",
        "source": "context:insurance_pdf",
        "inputs": ["context:insurance_pdf", "context:vehicle_xlsx"],
        "joins": [{"source": "context:insurance_pdf"}],
        "operations": [{"action": "transform", "source_reference": "context:insurance_pdf"}],
    }]
    _constrain_table_operation_sources(operations, ["vehicle_xlsx", "payroll_xlsx"])
    serialized = __import__("json").dumps(operations)
    assert "insurance_pdf" not in serialized
    assert "context:vehicle_xlsx" in serialized
    assert "context:payroll_xlsx" in serialized


def test_table_operation_source_aliases_cannot_bypass_context_normalization():
    operations = [
        {"action": "transform", "input": "fuel/generated.csv"},
        {"action": "transform", "input_file": "fuel/generated.csv"},
        {"action": "transform", "file": "fuel/generated.csv"},
    ]
    _constrain_table_operation_sources(operations, ["registered_xlsx"])
    for operation in operations:
        assert operation["source"] == "context:registered_xlsx"
        assert "input" not in operation
        assert "input_file" not in operation
        assert "file" not in operation


def test_table_operation_sources_fail_when_only_documents_are_assigned():
    with pytest.raises(ValueError, match="CSV/TSV/XLSX/XLSM"):
        _constrain_table_operation_sources(
            [{"action": "transform", "source": "context:insurance_pdf"}], []
        )

def test_table_operation_prefers_source_that_contains_requested_sheet():
    operations = [{"action": "transform", "source": "made-up.xlsx", "sheet": "走行距離"}]

    _constrain_table_operation_sources(
        operations,
        ["vehicle", "mileage"],
        {"vehicle": ["車両"], "mileage": ["走行距離"]},
    )

    assert operations[0]["source"] == "context:mileage"
    assert operations[0]["sheet"] == "走行距離"


def test_table_operation_replaces_nonexistent_sheet_with_populated_real_sheet():
    operations = [{"action": "transform", "source": "context:vehicle", "sheet_name": "給与"}]

    _constrain_table_operation_sources(
        operations,
        ["vehicle"],
        {"vehicle": ["車両一覧", "空シート"]},
    )

    assert operations[0]["source"] == "context:vehicle"
    assert operations[0]["sheet"] == "車両一覧"
    assert "sheet_name" not in operations[0]


def test_decode_final_assessment_fails_closed_for_non_json():
    report, status, unmet = ProjectOrchestrator._decode_final_assessment(
        "All tasks look complete."
    )

    assert report == "All tasks look complete."
    assert status == "failed"
    assert unmet == ["最終評価JSONを解析できませんでした"]


def test_plan_quality_rejects_unsupported_and_placeholder_activity():
    quality = assess_plan_quality({"tasks": [{"title": "Conduct interviews", "description": "Use placeholder data and save result/report.pdf", "acceptance_criteria": "result/report.pdf exists"}]})
    assert quality["passed"] is False
    assert any("未対応" in issue for issue in quality["issues"])


def test_plan_quality_accepts_verifiable_local_artifacts():
    quality = assess_plan_quality({"tasks": [
        {"title": "市場整理", "description": "調査結果をresult/market.mdへ保存する", "acceptance_criteria": "result/market.mdが存在し、必須見出し「市場」と200文字以上を含む"},
        {"title": "最終検証", "description": "全成果物を検証してresult/final_verification.mdへ保存する", "acceptance_criteria": "result/final_verification.mdが存在し、各成果物のPASS行を含む"},
    ]})
    assert quality == {"score": 100, "passed": True, "issues": []}


def test_plan_quality_rejects_missing_input_and_fake_audit_log():
    quality = assess_plan_quality({"tasks": [
        {"title": "集計", "description": "Read workspace:result/missing.csv. Create workspace:result/report.csv.", "acceptance_criteria": "result/report.csv exists with 3 rows; audit log entry recorded"},
        {"title": "最終検証", "description": "result/final_verification.mdを作成", "acceptance_criteria": "result/final_verification.mdが存在する"},
    ]})
    assert quality["passed"] is False
    assert any("入力ファイル" in issue for issue in quality["issues"])
    assert any("独自監査ログ" in issue for issue in quality["issues"])


def test_final_verification_task_is_added_with_leaf_dependencies():
    plan = {"summary": "x", "tasks": [
        {"task_key": "A", "depends_on": [], "title": "A"},
        {"task_key": "B", "depends_on": ["A"], "title": "B"},
        {"task_key": "C", "depends_on": ["A"], "title": "C"},
        {"task_key": "D", "depends_on": ["B"], "title": "D"},
        {"task_key": "E", "depends_on": ["C"], "title": "E"},
    ]}
    result = ensure_final_verification_task(plan)
    final = result["tasks"][-1]
    assert final["task_key"] == "final_verification"
    assert final["depends_on"] == ["D", "E"]
    assert "result/final_verification.md" in final["description"]
