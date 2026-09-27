"""Phase 1.5-B: structured failures, positive checks, coverage of real plans."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.completion_gate import evaluate
from app.goal_checks import CHECKS, collect_context, evaluate_vehicle_criteria
from app.goal_completion_flag import enable
from app.goal_contract import activate, from_mission
from app.plan_coverage import build, ensure_approvable
from app.structured_planning import contract_of
from app.vehicle_workflow import (
    digest,
    goal_failure_records,
    goal_failures,
    make_allocation_rule,
    resolve,
    sources,
    write_json,
    INPUT_PATH,
)
from test_vehicle_workflow import fixture, setup as vehicle_setup


CRITERION_IDS = [f"C{i:02d}" for i in range(1, 13)]


def _write_input(manager, pid, data, extra=None):
    snapshot = sources(manager, pid)
    envelope = {
        "data": data,
        "confirmed": True,
        "source_hash": digest(snapshot),
        "assignments": extra.get("assignments") if extra else [],
    }
    if extra:
        envelope.update({k: v for k, v in extra.items() if k != "assignments"})
    write_json(resolve(manager, pid, INPUT_PATH), envelope)
    return envelope


def _write_calc(manager, pid, mission, *, status="completed", checks=None, extra=None):
    task = next(t for t in mission["tasks"] if contract_of(t)["execution_kind"] == "vehicle_calculate")
    calc_path = contract_of(task)["outputs"][0]["path"]
    xlsx_path = contract_of(task)["outputs"][1]["path"]
    body = {
        "status": status,
        "version": mission["plan_version"],
        "checks": checks or {
            "recalculation": True,
            "formula_errors": 0,
            "independent_profit_match": True,
            "vehicle_summary_match": True,
            "input_allocation_reconciled": True,
            "coverage": True,
            "source_controls": True,
            "source_reconciliation": {"passed": True, "matched": 1, "mismatched": 0, "unavailable": 0, "incomplete": 0},
        },
        "expected": extra.get("expected") if extra else None,
        "rows": extra.get("rows") if extra else None,
        "company_total": extra.get("company_total") if extra else 1240,
    }
    if extra:
        for key, value in extra.items():
            if key not in {"expected", "rows", "company_total"}:
                body[key] = value
    write_json(resolve(manager, pid, calc_path), body)
    return calc_path, xlsx_path


def _write_workbook(manager, pid, xlsx_path, vehicle_ids):
    from openpyxl import Workbook
    path = resolve(manager, pid, xlsx_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    book = Workbook()
    book.active.title = "サマリー"
    for index, vid in enumerate(vehicle_ids, 1):
        sheet = book.create_sheet(f"車両_{index:03d}")
        sheet["A1"] = vid
    book.save(path)
    return path


def _approved_rules(data):
    rules = []
    for record in data["records"]:
        if record.get("category") not in {"payroll", "insurance"}:
            continue
        allocations = list(record.get("allocations") or [])
        if not allocations:
            continue
        rules.append(make_allocation_rule(
            subject_key={"company": record.get("company"), "employee": record.get("id")},
            effective_from=record.get("month"),
            effective_to=record.get("month"),
            allocations=allocations,
            status="active",
        ))
    return rules


@pytest.mark.asyncio
async def test_gt05_empty_failures_untestable_is_not_achieved(tmp_path):
    manager, pid = vehicle_setup(tmp_path)
    await manager.generate_plan(pid)
    activate(manager, pid)
    with patch("app.goal_checks.goal_failure_records", return_value=[]), patch(
        "app.vehicle_workflow.goal_failures", return_value=[]
    ):
        gate = evaluate(manager, pid)
    assert gate["achieved"] is False
    statuses = {row["criterion_id"]: row["status"] for row in gate["criteria"]}
    assert any(status == "UNTESTABLE" for status in statuses.values())
    assert not any(row["status"] == "PASS" and not (row.get("check_id") and (row.get("evidence_path") or row.get("evidence_summary"))) for row in gate["criteria"])


@pytest.mark.asyncio
async def test_gt06_rewritten_message_keeps_criterion_binding(tmp_path):
    manager, pid = vehicle_setup(tmp_path)
    item = manager.memory.add_context_file(pid, "source.txt", "原本", 6, source="upload")
    mission = await manager.generate_plan(pid)
    activate(manager, pid)
    data = fixture()
    for row in data["records"] + data["source_controls"] + data["source_dispositions"]:
        row["source_ref"] = "context:" + item["id"]
    _write_input(manager, pid, data)
    rewritten = "文言を書き換えた失敗メッセージ"
    records = [{
        "code": "CHECK_MISSING",
        "criterion_ids": ["C07"],
        "evidence_path": "result/vehicle/v1/calculation.json",
        "message": rewritten,
    }]
    with patch("app.goal_checks.goal_failure_records", return_value=records):
        rows = evaluate_vehicle_criteria(manager, pid, from_mission(mission)["criteria"], from_mission(mission))
    by_id = {row["criterion_id"]: row for row in rows}
    assert by_id["C07"]["status"] == "FAIL"
    assert rewritten in (by_id["C07"]["message"] or "")
    assert by_id["C07"]["reason_code"] == "CHECK_MISSING"
    assert by_id["C10"]["status"] != "FAIL" or by_id["C10"]["message"] != rewritten


@pytest.mark.asyncio
async def test_one_failure_fails_only_its_criterion(tmp_path):
    manager, pid = vehicle_setup(tmp_path)
    item = manager.memory.add_context_file(pid, "source.txt", "原本", 6, source="upload")
    mission = await manager.generate_plan(pid)
    activate(manager, pid)
    data = fixture()
    for row in data["records"] + data["source_controls"] + data["source_dispositions"]:
        row["source_ref"] = "context:" + item["id"]
    envelope = _write_input(manager, pid, data, extra={"assignments": _approved_rules(data)})
    calc_path, xlsx_path = _write_calc(manager, pid, mission, extra={
        "expected": [
            {"vehicle": "A-1234", "month": "2026-01", "profit": 620},
            {"vehicle": "B-1234", "month": "2026-01", "profit": 620},
        ],
        "company_total": 1240,
        "input_hash": digest(envelope),
        "source_hash": digest(sources(manager, pid)),
        "requirements_hash": __import__("app.vehicle_workflow", fromlist=["requirements_hash"]).requirements_hash(mission),
        "workbook_hash": "x",
    })
    _write_workbook(manager, pid, xlsx_path, ["A-1234", "B-1234"])
    unknown = "これは未知の文言で旧マッパーならC10へ落ちる"
    records = [{
        "code": "CHECK_MISSING",
        "criterion_ids": ["C07"],
        "evidence_path": calc_path,
        "message": unknown,
    }]
    with patch("app.goal_checks.goal_failure_records", return_value=records), patch(
        "app.vehicle_workflow.goal_failures", return_value=[unknown]
    ):
        gate = evaluate(manager, pid)
    by_id = {row["criterion_id"]: row for row in gate["criteria"]}
    assert by_id["C07"]["status"] == "FAIL"
    assert by_id["C07"]["message"] == unknown
    assert by_id["C10"]["status"] in {"PASS", "UNTESTABLE"}
    assert by_id["C10"].get("message") != unknown


@pytest.mark.asyncio
async def test_pass_requires_check_id_and_evidence_for_all_twelve(tmp_path):
    manager, pid = vehicle_setup(tmp_path)
    item = manager.memory.add_context_file(pid, "source.txt", "原本", 6, source="upload")
    mission = await manager.generate_plan(pid)
    activate(manager, pid)
    data = fixture()
    for row in data["records"] + data["source_controls"] + data["source_dispositions"]:
        row["source_ref"] = "context:" + item["id"]
    envelope = _write_input(manager, pid, data, extra={"assignments": _approved_rules(data)})
    calc_path, xlsx_path = _write_calc(manager, pid, mission, extra={
        "expected": [
            {"vehicle": "A-1234", "month": "2026-01", "profit": 620},
            {"vehicle": "B-1234", "month": "2026-01", "profit": 620},
        ],
        "company_total": 1240,
        "input_hash": digest(envelope),
        "source_hash": digest(sources(manager, pid)),
        "requirements_hash": __import__("app.vehicle_workflow", fromlist=["requirements_hash"]).requirements_hash(mission),
        "workbook_hash": "x",
    })
    _write_workbook(manager, pid, xlsx_path, ["A-1234", "B-1234"])
    with patch("app.goal_checks.goal_failure_records", return_value=[]):
        rows = evaluate_vehicle_criteria(manager, pid, from_mission(mission)["criteria"], from_mission(mission))
    assert [row["criterion_id"] for row in rows] == CRITERION_IDS
    for row in rows:
        if row["status"] == "PASS":
            assert row.get("check_id")
            assert row.get("evidence_path") or row.get("evidence_summary")
        else:
            assert row["status"] == "UNTESTABLE"
            assert row.get("check_id") or row.get("reason_code") == "CHECK_UNIMPLEMENTED"
    empty_pass = [row for row in rows if row["status"] == "PASS" and not row.get("check_id")]
    assert empty_pass == []


def test_unimplemented_check_is_untestable_not_pass():
    from app.goal_checks import _untestable
    row = _untestable("C99", "", "C99の検査が未実装です", code="CHECK_UNIMPLEMENTED")
    assert row["status"] == "UNTESTABLE"
    assert row["status"] != "PASS"
    assert set(CHECKS) >= set(CRITERION_IDS)


@pytest.mark.asyncio
async def test_legacy_goal_failures_keeps_messages_and_order(tmp_path):
    manager, pid = vehicle_setup(tmp_path)
    await manager.generate_plan(pid)
    messages = goal_failures(manager, pid)
    records = goal_failure_records(manager, pid)
    assert messages == [item["message"] for item in records]
    assert messages
    assert messages[0] == "現行計画の計算・Excel検証記録がありません"
    assert records[0]["criterion_ids"] == ["C07"]
    assert records[0]["code"] == "CHECK_MISSING"


@pytest.mark.asyncio
async def test_cv02_exec_only_or_verify_only_real_plan_is_rejected(tmp_path):
    manager, pid = vehicle_setup(tmp_path)
    mission = await manager.generate_plan(pid)
    enable(manager.memory.path, pid)
    contract = from_mission(mission)
    exec_only = dict(manager.memory.get_mission(pid))
    exec_only["tasks"] = [task for task in mission["tasks"] if task["task_key"] != "final_verification"]
    coverage = build(exec_only, contract)
    assert coverage["passed"] is False
    assert any("uncovered" in issue or "verify" in issue for issue in coverage["issues"])
    manager.memory.replace_plan(pid, mission["plan_summary"], exec_only["tasks"])
    with pytest.raises(ValueError, match="COVERAGE_INCOMPLETE"):
        ensure_approvable(manager, pid, contract)

    manager, pid = vehicle_setup(tmp_path)
    mission = await manager.generate_plan(pid)
    enable(manager.memory.path, pid)
    contract = from_mission(mission)
    verify_only = dict(manager.memory.get_mission(pid))
    verify_only["tasks"] = [task for task in mission["tasks"] if task["task_key"] not in {"vehicle_calculate", "vehicle_extract", "SC00"}]
    coverage = build(verify_only, contract)
    assert coverage["passed"] is False
    assert any("uncovered" in issue or "exec" in issue for issue in coverage["issues"])
    manager.memory.replace_plan(pid, mission["plan_summary"], verify_only["tasks"])
    with pytest.raises(ValueError, match="COVERAGE_INCOMPLETE"):
        ensure_approvable(manager, pid, contract)


def test_same_exec_verify_task_is_uncovered():
    mission = {
        "plan_version": 1,
        "tasks": [{
            "task_key": "only",
            "acceptance_criteria": __import__("json").dumps({
                "schema": "local-cowork-plan/v1",
                "execution_kind": "vehicle_calculate",
                "outputs": [{"path": "result/vehicle/v1/calculation.json"}],
                "criterion_ids": ["C07"],
            }),
        }],
    }
    contract = {
        "criteria": [{
            "criterion_id": "C07",
            "statement": "損益",
            "exec_task_keys": ["only"],
            "verify_task_keys": ["only"],
        }],
    }
    coverage = build(mission, contract)
    assert coverage["passed"] is False
    assert coverage["rows"][0]["status"] == "uncovered"
    assert any("same task" in issue for issue in coverage["issues"])


@pytest.mark.asyncio
async def test_empty_failures_without_evidence_are_not_pass(tmp_path):
    manager, pid = vehicle_setup(tmp_path)
    await manager.generate_plan(pid)
    activate(manager, pid)
    ctx = collect_context(manager, pid)
    assert ctx["envelope"] is None
    with patch("app.goal_checks.goal_failure_records", return_value=[]):
        rows = evaluate_vehicle_criteria(manager, pid, [{"criterion_id": cid} for cid in CRITERION_IDS])
    assert all(row["status"] != "PASS" for row in rows)
    assert any(row["status"] == "UNTESTABLE" for row in rows)
