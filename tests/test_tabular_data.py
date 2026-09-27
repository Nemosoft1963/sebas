import csv
import io

import pytest

from app.memory.short_term import ShortTermMemory
from app.project_manager import ProjectOrchestrator
from app.tabular_data import SafeTableExecutor
from app.workspace_files import WorkspaceSandbox
from app.yayoi_accounting import read_workbook


def add_csv(memory, project_id, filename, text, encoding="cp932"):
    data = text.encode(encoding)
    return memory.add_context_file(
        project_id, filename, text, len(data), data,
        "text/csv", "text", "test table", "",
    )


def read_csv(path):
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def test_safe_table_executor_aggregates_cp932_csv_by_driver_and_month(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("vehicle analysis", workspace_path="projects/vehicle")
    source = add_csv(memory, project["id"], "monthly.csv", (
        "日付,運転者,売上\n"
        "2026/01/03,田中,1000\n"
        "2026/01/20,田中,2500\n"
        "2026/02/01,田中,700\n"
        "2026/01/05,佐藤,3000\n"
    ))
    workspace = WorkspaceSandbox(tmp_path / "workspace")
    executor = SafeTableExecutor(memory, workspace)

    audit = executor.apply_operations(project, project["id"], [{
        "action": "transform", "source": f"context:{source['id']}",
        "header_row": 1,
        "derived_columns": [{"column": "日付", "date_part": "month", "as": "年月"}],
        "group_by": ["運転者", "年月"],
        "aggregations": [
            {"column": "売上", "function": "sum", "as": "売上合計"},
            {"function": "count", "as": "件数"},
        ],
        "sort_by": ["運転者", "年月"],
        "output_path": "output/driver_monthly.csv",
    }])

    rows = read_csv(tmp_path / "workspace/projects/vehicle/output/driver_monthly.csv")
    assert rows == [
        {"運転者": "佐藤", "年月": "2026-01", "売上合計": "3000", "件数": "1"},
        {"運転者": "田中", "年月": "2026-01", "売上合計": "3500", "件数": "2"},
        {"運転者": "田中", "年月": "2026-02", "売上合計": "700", "件数": "1"},
    ]
    assert audit[0]["source_rows"] == 4
    assert audit[0]["output_rows"] == 3
    assert "田中" not in str(audit)


def test_safe_table_executor_writes_xlsx_output(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("xlsx output", workspace_path="projects/xlsx-output")
    source = add_csv(memory, project["id"], "fuel.csv", "vehicle,fuel\nA,100\n", "utf-8")
    executor = SafeTableExecutor(memory, WorkspaceSandbox(tmp_path / "workspace"))

    audit = executor.apply_operations(project, project["id"], [{
        "action": "transform", "source": f"context:{source['id']}",
        "select": ["vehicle", "fuel"], "output_path": "output/fuel.xlsx",
    }])

    output = tmp_path / "workspace/projects/xlsx-output/output/fuel.xlsx"
    sheets = read_workbook(output.read_bytes())
    assert sheets == [{"name": "Result", "rows": [["vehicle", "fuel"], ["A", "100"]]}]
    assert audit[0]["output_path"] == "output/fuel.xlsx"
    assert audit[0]["write"]["action"] == "write_bytes"


def test_safe_table_executor_creates_source_free_csv(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("generated KPI", workspace_path="projects/generated-kpi")
    executor = SafeTableExecutor(memory, WorkspaceSandbox(tmp_path / "workspace"))

    audit = executor.apply_operations(project, project["id"], [{
        "action": "create_table",
        "columns": ["week", "leads_generated", "contracts_signed"],
        "rows": [
            {"week": 1, "leads_generated": "TBD", "contracts_signed": 0},
            {"week": 2, "leads_generated": "TBD", "contracts_signed": 0},
        ],
        "output_path": "result/kpi_dashboard.csv",
    }])

    assert read_csv(tmp_path / "workspace/projects/generated-kpi/result/kpi_dashboard.csv") == [
        {"week": "1", "leads_generated": "TBD", "contracts_signed": "0"},
        {"week": "2", "leads_generated": "TBD", "contracts_signed": "0"},
    ]
    assert audit[0]["action"] == "table_create"
    assert audit[0]["requested_action"] == "create_table"


def test_safe_table_executor_rejects_nested_create_values(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("unsafe generated")
    executor = SafeTableExecutor(memory, WorkspaceSandbox(tmp_path / "workspace"))

    with pytest.raises(ValueError, match="セル値"):
        executor.apply_operations(project, project["id"], [{
            "action": "create_table", "columns": ["value"],
            "rows": [{"value": {"nested": "blocked"}}], "output_path": "output/result.csv",
        }])

def test_safe_table_executor_can_chain_aggregate_and_one_to_one_join(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("profit", workspace_path="projects/profit")
    revenue = add_csv(memory, project["id"], "revenue.csv", (
        "運転者,年月,売上\n田中,2026-01,10000\n佐藤,2026-01,8000\n"
    ), "utf-8")
    payroll = add_csv(memory, project["id"], "payroll.csv", (
        "運転者,年月,給与\n田中,2026-01,4000\n田中,2026-01,1000\n佐藤,2026-01,3000\n"
    ), "utf-8")
    executor = SafeTableExecutor(memory, WorkspaceSandbox(tmp_path / "workspace"))

    executor.apply_operations(project, project["id"], [
        {
            "action": "transform", "source": f"context:{payroll['id']}", "header_row": 1,
            "group_by": ["運転者", "年月"],
            "aggregations": [{"column": "給与", "function": "sum", "as": "給与合計"}],
            "output_path": "temp/payroll_monthly.csv",
        },
        {
            "action": "transform", "source": f"context:{revenue['id']}", "header_row": 1,
            "joins": [{
                "source": "workspace:temp/payroll_monthly.csv", "header_row": 1,
                "left_on": ["運転者", "年月"], "right_on": ["運転者", "年月"],
                "select": [{"column": "給与合計", "as": "給与"}],
            }],
            "derived_columns": [{
                "operation": "subtract", "columns": ["売上", "給与"], "as": "粗利益",
            }],
            "select": ["運転者", "年月", "売上", "給与", "粗利益"],
            "sort_by": ["運転者"], "output_path": "output/profit.csv",
        },
    ])

    rows = read_csv(tmp_path / "workspace/projects/profit/output/profit.csv")
    assert rows == [
        {"運転者": "佐藤", "年月": "2026-01", "売上": "8000", "給与": "3000", "粗利益": "5000"},
        {"運転者": "田中", "年月": "2026-01", "売上": "10000", "給与": "5000", "粗利益": "5000"},
    ]


def test_safe_table_executor_rejects_unknown_code_and_cross_project_source(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    first = memory.create_project("first")
    second = memory.create_project("second")
    foreign = add_csv(memory, second["id"], "secret.csv", "id,value\n1,secret\n", "utf-8")
    executor = SafeTableExecutor(memory, WorkspaceSandbox(tmp_path / "workspace"))

    with pytest.raises(ValueError, match="profile.*transform"):
        executor.apply_operations(first, first["id"], [{"action": "python", "code": "import os"}])
    with pytest.raises(ValueError, match="見つかりません"):
        executor.apply_operations(first, first["id"], [{
            "action": "profile", "source": f"context:{foreign['id']}",
            "output_path": "output/profile.md",
        }])
    with pytest.raises(ValueError, match="inside /workspace|stay inside|relative"):
        local = add_csv(memory, first["id"], "local.csv", "id,value\n1,ok\n", "utf-8")
        executor.apply_operations(first, first["id"], [{
            "action": "profile", "source": f"context:{local['id']}",
            "output_path": "../../escape.md",
        }])
    assert not (tmp_path / "escape.md").exists()


def test_safe_table_executor_rejects_more_than_twenty_operations(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("operation limit", workspace_path="projects/operation-limit")
    executor = SafeTableExecutor(memory, WorkspaceSandbox(tmp_path / "workspace"))
    with pytest.raises(ValueError, match="20件以内"):
        executor.apply_operations(project, project["id"], [{} for _ in range(21)])

def test_table_inventory_exposes_exact_source_and_detected_schema(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("inventory")
    source = add_csv(
        memory, project["id"], "vehicle_monthly.csv",
        "車両ID,年月,売上\nV01,2026-01,1000\nV02,2026-01,2000\n", "utf-8",
    )
    executor = SafeTableExecutor(memory, WorkspaceSandbox(tmp_path / "workspace"))

    inventory = executor.inventory(project, project["id"])

    assert f"source=context:{source['id']}" in inventory
    assert "sheet=vehicle_monthly.csv" in inventory
    assert "header_row=1" in inventory and "rows=2" in inventory
    assert "columns=[車両ID, 年月, 売上]" in inventory


def test_table_inventory_continues_after_empty_excel_sheet(tmp_path, monkeypatch):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("multi sheet inventory")
    source = memory.add_context_file(
        project["id"], "multi.xlsx", "", 4, b"xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "spreadsheet", "test", "hash",
    )
    monkeypatch.setattr("app.tabular_data.read_workbook", lambda data: [
        {"name": "empty", "rows": []},
        {"name": "data", "rows": [["ID", "金額"], ["A", 100]]},
    ])
    executor = SafeTableExecutor(memory, WorkspaceSandbox(tmp_path / "workspace"))

    inventory = executor.inventory(project, project["id"])

    assert f"source=context:{source['id']}" in inventory
    assert "sheet=empty | schema_preview_error=" in inventory
    assert "sheet=data | header_row=1 | rows=1 | columns=[ID, 金額]" in inventory


@pytest.mark.asyncio
async def test_project_executor_runs_declared_table_operation_and_records_event(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("monthly", workspace_path="projects/monthly")
    source = add_csv(memory, project["id"], "monthly.csv", "運転者,金額\n田中,100\n田中,200\n", "utf-8")
    memory.save_mission(project["id"], "driver totals", "CSV saved", "local only", False, [])
    mission = memory.replace_plan(project["id"], "aggregate", [{
        "task_key": "aggregate", "depends_on": [], "title": "aggregate CSV",
        "description": "sum by driver", "acceptance_criteria": "output exists", "mode": "local",
    }])

    class FakeLlm:
        def __init__(self):
            self.prompt = ""

        async def stream(self, messages):
            self.prompt = messages[-1]["content"]
            yield (
                '{"result":"集計完了","operations":[],"table_operations":['
                '{"action":"transform","source":"context:' + source["id"] + '",'
                '"header_row":1,"group_by":["運転者"],'
                '"aggregations":[{"column":"金額","function":"sum","as":"合計"}],'
                '"output_path":"output/totals.csv"}],"capability_gaps":[]}'
            )

    llm = FakeLlm()
    workspace = WorkspaceSandbox(tmp_path / "workspace")
    orchestrator = ProjectOrchestrator(
        memory, llm,
        lambda selected: (selected["context_text"], memory.list_context_files(selected["id"], True)),
        None, lambda: [], workspace=workspace,
        table_executor=SafeTableExecutor(memory, workspace),
    )

    result = await orchestrator._execute_task(project["id"], mission["tasks"][0]["id"])

    assert "集計完了" in result and "2行から1行" in result
    assert "Safe local table executor" in llm.prompt
    assert read_csv(tmp_path / "workspace/projects/monthly/output/totals.csv")[0]["合計"] == "300"
    assert any(event["kind"] == "table_operation" for event in memory.get_mission(project["id"])["events"])
    await orchestrator.shutdown()


@pytest.mark.asyncio
async def test_project_executor_corrects_missing_table_evidence_once(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("monthly correction", workspace_path="projects/correction")
    source = add_csv(
        memory, project["id"], "monthly.csv",
        "運転者,金額\n田中,100\n田中,200\n", "utf-8",
    )
    memory.save_mission(project["id"], "月次集計", "集計CSVを保存", "local only", False, [])
    mission = memory.replace_plan(project["id"], "aggregate", [{
        "task_key": "aggregate", "depends_on": [], "title": "CSV月次集計",
        "description": "運転者別に集計して保存", "acceptance_criteria": "集計CSVが存在する",
        "mode": "local",
    }])

    class FakeLlm:
        def __init__(self):
            self.responses = [
                '{"result":"集計CSVを作成しました","operations":[],"table_operations":[],"capability_gaps":[]}',
                (
                    '{"result":"安全な表処理で補正しました","operations":[],"table_operations":['
                    '{"action":"transform","source":"context:' + source["id"] + '",'
                    '"header_row":1,"group_by":["運転者"],'
                    '"aggregations":[{"column":"金額","function":"sum","as":"合計"}],'
                    '"output_path":"output/totals.csv"}],"capability_gaps":[]}'
                ),
            ]
            self.prompts = []

        async def stream(self, messages):
            self.prompts.append(messages[-1]["content"])
            yield self.responses.pop(0)

    llm = FakeLlm()
    workspace = WorkspaceSandbox(tmp_path / "workspace")
    orchestrator = ProjectOrchestrator(
        memory, llm,
        lambda selected: (selected["context_text"], memory.list_context_files(selected["id"], True)),
        None, lambda: [], workspace=workspace,
        table_executor=SafeTableExecutor(memory, workspace),
    )

    result = await orchestrator._execute_task(project["id"], mission["tasks"][0]["id"])

    assert len(llm.prompts) == 2
    assert "バックエンド強制検証で不合格" in llm.prompts[1]
    assert "自動補正後に合格" in result
    assert read_csv(tmp_path / "workspace/projects/correction/output/totals.csv") == [
        {"運転者": "田中", "合計": "300"},
    ]
    kinds = [event["kind"] for event in memory.get_mission(project["id"])["events"]]
    assert "task_verification_failed" in kinds
    assert "task_correction_succeeded" in kinds


@pytest.mark.asyncio
async def test_pdf_extraction_uses_source_reference_without_false_table_requirement(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("insurance extraction", workspace_path="projects/insurance")
    pdf = memory.add_context_file(
        project["id"], "insurance.pdf", "車名: トラックA\n保険料: 120000円", 20,
        b"pdf-original", "application/pdf", "pdf", "PDF文字抽出済み", "pdfhash",
    )
    add_csv(memory, project["id"], "other_table.csv", "id,value\n1,x\n", "utf-8")
    memory.save_mission(project["id"], "保険情報整理", "車名と保険料を確認", "", False, [])
    mission = memory.replace_plan(project["id"], "extract", [{
        "task_key": "pdf", "depends_on": [], "title": "保険見積書PDFから車両情報の抽出",
        "description": "登録済みPDF原本を読み取り車名と保険料を列挙する",
        "acceptance_criteria": "車名と保険料が確認できる", "mode": "local",
    }])

    class FakeLlm:
        def __init__(self):
            self.prompt = ""

        async def stream(self, messages):
            self.prompt = messages[-1]["content"]
            yield (
                '{"result":"トラックAの保険料は120000円です",'
                '"operations":[],"table_operations":[],"source_references":["context:'
                + pdf["id"] + '"],"capability_gaps":[]}'
            )

    llm = FakeLlm()
    workspace = WorkspaceSandbox(tmp_path / "workspace")
    orchestrator = ProjectOrchestrator(
        memory, llm,
        lambda selected: (selected["context_text"], memory.list_context_files(selected["id"], True)),
        None, lambda: [], workspace=workspace,
        table_executor=SafeTableExecutor(memory, workspace),
    )

    result = await orchestrator._execute_task(project["id"], mission["tasks"][0]["id"])

    assert "トラックA" in result
    assert f"ref=context:{pdf['id']}" in llm.prompt
    kinds = [event["kind"] for event in memory.get_mission(project["id"])["events"]]
    assert "source_reference" in kinds
    assert "task_verification_failed" not in kinds


def test_source_reference_cannot_cross_project_boundary(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    first = memory.create_project("first")
    second = memory.create_project("second")
    foreign = memory.add_context_file(
        second["id"], "secret.pdf", "secret", 6, b"secret",
        "application/pdf", "pdf", "extracted", "hash",
    )
    workspace = WorkspaceSandbox(tmp_path / "workspace")
    orchestrator = ProjectOrchestrator(
        memory, object(), lambda selected: ("", []), None, lambda: [], workspace=workspace,
        table_executor=SafeTableExecutor(memory, workspace),
    )

    with pytest.raises(ValueError, match="登録原本参照が見つかりません"):
        orchestrator._apply_execution_response(
            first, first["id"], "task-id",
            '{"result":"read","source_references":["context:' + foreign["id"] + '"]}',
        )


def test_document_reference_without_extracted_text_does_not_prove_extraction(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("scanned pdf")
    scanned = memory.add_context_file(
        project["id"], "scanned.pdf", "", 10, b"pdf-binary",
        "application/pdf", "pdf", "文字抽出なし", "hash",
    )
    workspace = WorkspaceSandbox(tmp_path / "workspace")
    orchestrator = ProjectOrchestrator(
        memory, object(), lambda selected: ("", []), None, lambda: [], workspace=workspace,
        table_executor=SafeTableExecutor(memory, workspace),
    )
    task = {
        "title": "PDF原本の読取", "description": "文書抽出する",
        "acceptance_criteria": "内容を確認",
    }
    source_audit = orchestrator._validate_source_references(
        project, project["id"], [f"context:{scanned['id']}"]
    )

    failures, metadata = orchestrator._verify_execution_evidence(
        project, project["id"], task, "抽出しました",
        {"workspace_operations": [], "table_operations": [], "source_references": source_audit},
        memory.list_context_files(project["id"]),
    )

    assert any("抽出本文" in failure for failure in failures)
    assert metadata["readable_source_count"] == 0


def test_markdown_price_table_does_not_require_table_operation():
    task = {
        "title": "Design contract plans and pricing",
        "description": (
            "Draft three contract plans with price ranges. "
            "Output workspace:result/contract_plans.md."
        ),
        "acceptance_criteria": (
            "The markdown exists, lists three plans, and includes a price table."
        ),
        "depends_on": ["feature-mapping"],
    }

    requirements = ProjectOrchestrator._task_evidence_requirements(task, [])

    assert requirements["artifact"] is True
    assert requirements["table_operation"] is False


def test_workspace_colon_markdown_contract_path_is_extracted():
    paths = ProjectOrchestrator._expected_artifact_paths(
        "Output `workspace:result/contract_plans.md` and output/audit.txt."
    )

    assert paths == {"result/contract_plans.md", "output/audit.txt"}


def test_csv_output_still_requires_table_operation_without_registered_source():
    task = {
        "title": "月次集計",
        "description": "結果をoutput/monthly.csvへ出力する",
        "acceptance_criteria": "CSVが存在する",
    }

    requirements = ProjectOrchestrator._task_evidence_requirements(task, [])

    assert requirements["table_operation"] is True


def test_completed_mission_verification_reopens_unproved_work(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("legacy result", workspace_path="projects/legacy")
    add_csv(memory, project["id"], "monthly.csv", "運転者,金額\n田中,100\n", "utf-8")
    memory.save_mission(project["id"], "集計完了", "CSV保存", "local only", False, [])
    mission = memory.replace_plan(project["id"], "legacy", [{
        "task_key": "aggregate", "depends_on": [], "title": "CSV月次集計",
        "description": "運転者別に集計", "acceptance_criteria": "CSVを保存", "mode": "local",
    }, {
        "task_key": "report", "depends_on": ["aggregate"], "title": "レポート保存",
        "description": "結果を報告", "acceptance_criteria": "report.mdを保存", "mode": "local",
    }])
    memory.update_task(mission["tasks"][0]["id"], "completed", result="Python/pandasで集計しました")
    memory.update_task(mission["tasks"][1]["id"], "completed", result="完了と報告しました")
    memory.set_final_report(project["id"], "# 最終報告\n\n目標は未完全です")
    workspace = WorkspaceSandbox(tmp_path / "workspace")
    orchestrator = ProjectOrchestrator(
        memory, object(), lambda selected: ("", []), None, lambda: [], workspace=workspace,
        table_executor=SafeTableExecutor(memory, workspace),
    )

    verified = orchestrator.verify_completed(project["id"])

    assert verified["status"] == "paused"
    assert [task["status"] for task in verified["tasks"]] == ["pending", "pending"]
    kinds = [event["kind"] for event in verified["events"]]
    assert "task_verification_reopened" in kinds
    assert "mission_verification_failed" in kinds
    assert "mission_correction_ready" in kinds


def test_completed_mission_with_pending_tasks_is_reconciled_to_paused(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("inconsistent")
    memory.save_mission(project["id"], "成果を作る", "全工程完了", "", False, [])
    mission = memory.replace_plan(project["id"], "two steps", [{
        "task_key": "done", "depends_on": [], "title": "完了工程",
        "description": "整理する", "acceptance_criteria": "", "mode": "local",
    }, {
        "task_key": "todo", "depends_on": ["done"], "title": "未着手工程",
        "description": "確認する", "acceptance_criteria": "", "mode": "local",
    }])
    memory.update_task(mission["tasks"][0]["id"], "completed", result="完了")
    memory.set_mission_status(project["id"], "completed")
    orchestrator = ProjectOrchestrator(
        memory, object(), lambda selected: ("", []), None, lambda: [],
    )

    verified = orchestrator.verify_completed(project["id"])

    assert verified["status"] == "paused"
    assert verified["tasks"][1]["status"] == "pending"
    assert any(
        event["kind"] == "mission_state_reconciled"
        for event in verified["events"]
    )


def test_safe_table_executor_accepts_safe_extract_alias(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("extract", workspace_path="projects/extract")
    source = add_csv(memory, project["id"], "fuel.csv", "vehicle,fuel\nA,100\n", "utf-8")
    executor = SafeTableExecutor(memory, WorkspaceSandbox(tmp_path / "workspace"))
    audit = executor.apply_operations(project, project["id"], [{
        "action": "extract", "source": f"context:{source['id']}",
        "header_row": 1, "select": ["vehicle", "fuel"],
        "output_path": "output/fuel.csv",
    }])
    assert audit[0]["action"] == "table_transform"
    assert audit[0]["requested_action"] == "extract"
    assert read_csv(tmp_path / "workspace/projects/extract/output/fuel.csv") == [{"vehicle": "A", "fuel": "100"}]

def test_safe_table_executor_accepts_read_csv_alias(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("read csv", workspace_path="projects/read-csv")
    source = add_csv(memory, project["id"], "fuel.csv", "vehicle,fuel\nA,100\n", "utf-8")
    executor = SafeTableExecutor(memory, WorkspaceSandbox(tmp_path / "workspace"))
    audit = executor.apply_operations(project, project["id"], [{
        "action": "read_csv", "source": f"context:{source['id']}",
        "output": "output/fuel.xlsx",
    }])
    assert audit[0]["action"] == "table_transform"
    assert audit[0]["requested_action"] == "read_csv"
    assert (tmp_path / "workspace/projects/read-csv/output/fuel.xlsx").is_file()

def test_safe_table_executor_normalizes_merge_inputs_and_keys(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("merge inputs", workspace_path="projects/merge-inputs")
    revenue = add_csv(memory, project["id"], "revenue.csv", "vehicle,month,revenue\nA,2026-01,1000\n", "utf-8")
    fuel = add_csv(memory, project["id"], "fuel.csv", "vehicle,month,fuel\nA,2026-01,300\n", "utf-8")
    executor = SafeTableExecutor(memory, WorkspaceSandbox(tmp_path / "workspace"))
    audit = executor.apply_operations(project, project["id"], [{
        "action": "merge",
        "inputs": [f"context:{revenue['id']}", f"context:{fuel['id']}"],
        "keys": ["vehicle", "month"],
        "output_path": "output/profit.xlsx",
    }])
    output = tmp_path / "workspace/projects/merge-inputs/output/profit.xlsx"
    assert read_workbook(output.read_bytes())[0]["rows"] == [
        ["vehicle", "month", "revenue", "fuel"], ["A", "2026-01", "1000", "300"]
    ]
    assert audit[0]["requested_action"] == "merge"

def test_safe_table_executor_chains_source_less_write_alias(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("pipeline", workspace_path="projects/pipeline")
    source = add_csv(memory, project["id"], "fuel.csv", "vehicle,fuel\nA,100\n", "utf-8")
    executor = SafeTableExecutor(memory, WorkspaceSandbox(tmp_path / "workspace"))
    audit = executor.apply_operations(project, project["id"], [
        {"action": "read_csv", "source": f"context:{source['id']}", "output_path": "temp/fuel.csv"},
        {"action": "write_csv", "file_path": "output/fuel.xlsx"},
    ])
    assert [item["requested_action"] for item in audit] == ["read_csv", "write_csv"]
    output = tmp_path / "workspace/projects/pipeline/output/fuel.xlsx"
    assert read_workbook(output.read_bytes())[0]["rows"] == [["vehicle", "fuel"], ["A", "100"]]

def test_safe_table_executor_infers_bounded_unknown_extract_action(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("extract inferred", workspace_path="projects/extract-inferred")
    source = add_csv(memory, project["id"], "fuel.csv", "vehicle,fuel\nA,100\n", "utf-8")
    executor = SafeTableExecutor(memory, WorkspaceSandbox(tmp_path / "workspace"))
    audit = executor.apply_operations(project, project["id"], [{
        "action": "extract_rows", "source": f"context:{source['id']}",
        "header_row": 1, "select": ["vehicle", "fuel"],
        "output_path": "output/fuel.csv",
    }])
    assert audit[0]["action"] == "table_transform"
    assert audit[0]["requested_action"] == "extract_rows"


def test_safe_table_executor_normalizes_legacy_operation_keys(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("legacy extract", workspace_path="projects/legacy-extract")
    source = add_csv(memory, project["id"], "fuel.csv", "vehicle,fuel\nA,100\n", "utf-8")
    executor = SafeTableExecutor(memory, WorkspaceSandbox(tmp_path / "workspace"))
    audit = executor.apply_operations(project, project["id"], [{
        "type": "table_extract", "source_ref": source["id"],
        "columns": ["vehicle", "fuel"], "output_file": "output/fuel.csv",
    }])
    assert audit[0]["action"] == "table_transform"
    assert audit[0]["requested_action"] == "table_extract"
    assert read_csv(tmp_path / "workspace/projects/legacy-extract/output/fuel.csv") == [{"vehicle": "A", "fuel": "100"}]


def test_safe_table_executor_expands_legacy_nested_operations(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("nested extract", workspace_path="projects/nested-extract")
    source = add_csv(memory, project["id"], "fuel.csv", "vehicle,fuel\nA,100\n", "utf-8")
    executor = SafeTableExecutor(memory, WorkspaceSandbox(tmp_path / "workspace"))
    audit = executor.apply_operations(project, project["id"], [{
        "source_reference": {"id": source["id"]},
        "table_name": "fuel",
        "operations": [{"operation": "extract", "columns": ["vehicle", "fuel"],
                        "output_file": "workspace:/output/fuel.csv"}],
    }])
    assert audit[0]["action"] == "table_transform"
    assert read_csv(tmp_path / "workspace/projects/nested-extract/output/fuel.csv") == [{"vehicle": "A", "fuel": "100"}]

def test_safe_table_executor_rejects_legacy_operation_with_code(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("unsafe legacy", workspace_path="projects/unsafe-legacy")
    source = add_csv(memory, project["id"], "fuel.csv", "vehicle,fuel\nA,100\n", "utf-8")
    executor = SafeTableExecutor(memory, WorkspaceSandbox(tmp_path / "workspace"))
    with pytest.raises(ValueError, match="profileまたはtransform"):
        executor.apply_operations(project, project["id"], [{
            "type": "table_extract", "source_ref": source["id"],
            "output_file": "output/fuel.csv", "python": "print('unsafe')",
        }])

def test_execution_decoder_normalizes_source_reference_objects(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("references")
    orchestrator = ProjectOrchestrator(memory, object(), lambda selected: ("", []), None, lambda: [])
    report, operations, table_operations, gaps, references = orchestrator._decode_execution_response(
        '{"result":"ok","source_references":['
        '{"reference":"context:first"},'
        '{"type":"context","id":"second"},null,""],'
        '"operations":[],"table_operations":[],"capability_gaps":[]}'
    )
    assert references == ["context:first", "context:second"]
