"""Phase 3-B: read-only evidence graph (vehicle × month × category → source)."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from app.completion_gate import evaluate
from app.evidence_graph import (
    STATUS_NO_DATA,
    STATUS_NOT_APPLICABLE,
    STATUS_STALE,
    STATUS_TRACED,
    STATUS_UNTRACEABLE,
    company_totals_from_input,
    summary,
    trace,
)
from app.goal_checks import check_c12_trace, collect_context
from app.goal_contract import activate
from app.vehicle_workflow import INPUT_PATH, digest, load_input, resolve, sources, write_json
from test_vehicle_workflow import fixture, setup as vehicle_setup


def _write_input(manager, pid, data, extra=None):
    snapshot = sources(manager, pid)
    payload = dict(data)
    payload.setdefault("source_snapshot", snapshot)
    envelope = {
        "data": payload,
        "confirmed": True,
        "source_hash": digest(snapshot),
    }
    if extra:
        envelope.update(extra)
    write_json(resolve(manager, pid, INPUT_PATH), envelope)
    return envelope


def _bind_source(data, source_id):
    ref = "context:" + source_id
    for row in data.get("records") or []:
        row["source_ref"] = ref
    for row in data.get("source_controls") or []:
        row["source_ref"] = ref
    for row in data.get("source_dispositions") or []:
        row["source_ref"] = ref
    return data


def _tree_bytes(root):
    root = Path(root)
    items = {}
    if not root.exists():
        return items
    for path in sorted(root.rglob("*")):
        if path.is_file():
            items[str(path.relative_to(root))] = path.read_bytes()
    return items


def test_eg01_trace_follows_records_sources_allocations(tmp_path):
    manager, pid = vehicle_setup(tmp_path)
    item = manager.memory.add_context_file(pid, "source.txt", "原本", 6, source="upload")
    data = _bind_source(fixture(), item["id"])
    fuel = next(r for r in data["records"] if r["id"] == "A-1234fuel")
    fuel["allocation_reused"] = True
    fuel["allocation_rule_id"] = "rule-fuel-a"
    _write_input(manager, pid, data)

    result = trace(manager, pid, "A-1234", "2026-01", "fuel")
    assert result["applicable"] is True
    assert result["status"] == STATUS_TRACED
    assert result["vehicle_id"] == "A-1234"
    assert result["month"] == "2026-01"
    assert result["category"] == "fuel"
    assert result["issues"] == []
    assert result["records"]
    assert {row["id"] for row in result["records"]} == {"A-1234fuel"}
    assert all(row["kind"] == "record" for row in result["records"])
    assert result["amount"] == sum(row["contributed_amount"] for row in result["records"])
    assert result["amount"] == 100
    assert result["cell"]["kind"] == "cell"
    assert result["cell"]["amount"] == result["amount"]
    assert result["sources"]
    assert result["sources"][0]["kind"] == "source"
    assert result["sources"][0]["source_ref"] == "context:" + item["id"]
    assert result["sources"][0]["locator"] == "A-1234/fuel"
    assert result["sources"][0]["exists"] is True
    assert result["sources"][0]["source_hash"]
    assert result["allocations"]
    assert result["allocations"][0]["kind"] == "allocation"
    assert result["allocations"][0]["allocations"][0]["vehicle_id"] == "A-1234"
    assert result["allocations"][0]["allocation_reused"] is True
    assert result["allocations"][0]["allocation_rule_id"] == "rule-fuel-a"

    envelope = load_input(manager, pid)
    totals = company_totals_from_input(envelope["data"])
    cell = totals[("2026-01", "fuel")]
    assert cell["vehicles"]["A-1234"] == 100
    assert cell["vehicles"]["B-1234"] == 100
    assert cell["company_total"] == 200


def test_eg02_missing_source_ref_or_locator_is_untraceable(tmp_path):
    manager, pid = vehicle_setup(tmp_path)
    item = manager.memory.add_context_file(pid, "source.txt", "原本", 6, source="upload")
    data = _bind_source(fixture(), item["id"])
    missing_ref = next(r for r in data["records"] if r["id"] == "A-1234fuel")
    missing_ref["source_ref"] = ""
    missing_loc = next(r for r in data["records"] if r["id"] == "A-1234payroll")
    missing_loc["source_locator"] = "   "
    _write_input(manager, pid, data)

    fuel = trace(manager, pid, "A-1234", "2026-01", "fuel")
    assert fuel["status"] == STATUS_UNTRACEABLE
    assert fuel["status"] != STATUS_TRACED
    assert any(i["record_id"] == "A-1234fuel" and i["field"] == "source_ref" for i in fuel["issues"])
    assert all(not row["source_ref"] for row in fuel["records"] if row["id"] == "A-1234fuel")

    payroll = trace(manager, pid, "A-1234", "2026-01", "payroll")
    assert payroll["status"] == STATUS_UNTRACEABLE
    assert any(i["record_id"] == "A-1234payroll" and i["field"] == "source_locator" for i in payroll["issues"])
    assert payroll["records"][0]["source_locator"] == ""


def test_eg03_missing_source_untraceable_changed_hash_stale(tmp_path):
    manager, pid = vehicle_setup(tmp_path)
    item = manager.memory.add_context_file(pid, "source.txt", "原本", 6, source="upload")
    data = _bind_source(fixture(), item["id"])
    _write_input(manager, pid, data)

    gone = trace(manager, pid, "A-1234", "2026-01", "fuel")
    assert gone["status"] == STATUS_TRACED

    missing = dict(load_input(manager, pid))
    missing_data = dict(missing["data"])
    records = [dict(r) for r in missing_data["records"]]
    for row in records:
        if row["id"] == "A-1234fuel":
            row["source_ref"] = "context:does-not-exist"
            row["source_locator"] = "Sheet!A1"
    missing_data["records"] = records
    write_json(resolve(manager, pid, INPUT_PATH), {**missing, "data": missing_data})
    absent = trace(manager, pid, "A-1234", "2026-01", "fuel")
    assert absent["status"] == STATUS_UNTRACEABLE
    assert absent["status"] != STATUS_TRACED
    assert any(i["reason"] == "source_missing" and i["record_id"] == "A-1234fuel" for i in absent["issues"])
    assert any(s["exists"] is False for s in absent["sources"])

    data = _bind_source(fixture(), item["id"])
    _write_input(manager, pid, data)
    manager.memory.add_context_file(pid, "other.txt", "別原本", 8, source="upload")
    stale = trace(manager, pid, "A-1234", "2026-01", "fuel")
    assert stale["status"] == STATUS_STALE
    assert stale["status"] != STATUS_TRACED
    assert any(i["reason"] == "source_hash_mismatch" for i in stale["issues"])


def test_eg04_no_data_amount_none_distinct_from_zero(tmp_path):
    manager, pid = vehicle_setup(tmp_path)
    item = manager.memory.add_context_file(pid, "source.txt", "原本", 6, source="upload")
    data = _bind_source(fixture(), item["id"])
    zero = next(r for r in data["records"] if r["id"] == "A-1234toll")
    zero["amount"] = 0
    _write_input(manager, pid, data)

    empty = trace(manager, pid, "Z-9999", "2026-01", "fuel")
    assert empty["status"] == STATUS_NO_DATA
    assert empty["amount"] is None
    assert empty["records"] == []

    zeroed = trace(manager, pid, "A-1234", "2026-01", "toll")
    assert zeroed["status"] == STATUS_TRACED
    assert zeroed["amount"] == 0
    assert zeroed["status"] != STATUS_NO_DATA
    assert zeroed["amount"] is not None


def test_eg05_lazy_build_no_persist(tmp_path, monkeypatch):
    manager, pid = vehicle_setup(tmp_path)
    item = manager.memory.add_context_file(pid, "source.txt", "原本", 6, source="upload")
    data = _bind_source(fixture(), item["id"])
    _write_input(manager, pid, data)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("full graph must not be built")

    monkeypatch.setattr("app.evidence_graph.build_full_graph", forbidden)

    loads = {"n": 0}
    real_load = load_input

    def counting_load(*args, **kwargs):
        loads["n"] += 1
        return real_load(*args, **kwargs)

    monkeypatch.setattr("app.evidence_graph.load_input", counting_load)

    def no_write(*_args, **_kwargs):
        raise AssertionError("evidence graph must not write")

    monkeypatch.setattr("app.vehicle_workflow.write_json", no_write)

    before_input = resolve(manager, pid, INPUT_PATH).read_bytes()
    before_db = Path(manager.memory.path).read_bytes()
    before_tree = _tree_bytes(tmp_path)

    result = trace(manager, pid, "A-1234", "2026-01", "fuel")
    assert result["status"] == STATUS_TRACED
    assert loads["n"] == 1

    after_input = resolve(manager, pid, INPUT_PATH).read_bytes()
    after_db = Path(manager.memory.path).read_bytes()
    after_tree = _tree_bytes(tmp_path)
    assert after_input == before_input
    assert after_db == before_db
    assert after_tree == before_tree


def test_eg06_summary_counts_match_input_and_honors_limit(tmp_path):
    manager, pid = vehicle_setup(tmp_path)
    item = manager.memory.add_context_file(pid, "source.txt", "原本", 6, source="upload")
    data = _bind_source(fixture(), item["id"])
    next(r for r in data["records"] if r["id"] == "A-1234fuel")["source_locator"] = ""
    next(r for r in data["records"] if r["id"] == "B-1234lease")["source_ref"] = ""
    data["records"] = [r for r in data["records"] if r["id"] != "A-1234other"]
    _write_input(manager, pid, data)

    full = summary(manager, pid, limit=50)
    assert full["applicable"] is True
    assert full["cell_total"] == 2 * 1 * 7
    assert full["counts"][STATUS_UNTRACEABLE] == 2
    assert full["counts"][STATUS_NO_DATA] == 1
    assert full["counts"][STATUS_TRACED] == full["cell_total"] - 3
    assert full["counts"][STATUS_STALE] == 0
    assert full["untraceable_total"] == 2
    assert full["stale_total"] == 0
    keys = {(row["vehicle_id"], row["month"], row["category"]) for row in full["untraceable"]}
    assert ("A-1234", "2026-01", "fuel") in keys
    assert ("B-1234", "2026-01", "lease") in keys
    no_data_cells = [
        (vid, month, cat)
        for vid in ("A-1234", "B-1234")
        for month in ("2026-01",)
        for cat in ("revenue", "payroll", "insurance", "fuel", "toll", "lease", "other")
        if (vid, month, cat) == ("A-1234", "2026-01", "other")
    ]
    assert no_data_cells

    limited = summary(manager, pid, limit=1)
    assert limited["limit"] == 1
    assert len(limited["untraceable"]) == 1
    assert limited["untraceable_total"] == 2
    assert limited["untraceable_omitted"] == 1

    manager.memory.add_context_file(pid, "other.txt", "別原本", 8, source="upload")
    drifted = summary(manager, pid, limit=50)
    assert drifted["counts"][STATUS_STALE] >= 1
    assert drifted["counts"][STATUS_TRACED] == 0
    assert drifted["stale_total"] == drifted["counts"][STATUS_STALE]
    assert drifted["stale"]


@pytest.mark.asyncio
async def test_eg07_not_applicable_and_api_status_codes(tmp_path, monkeypatch):
    import app.web as web

    other_memory = vehicle_setup(tmp_path)[0].memory
    generic = other_memory.create_project("文書", workspace_path="projects/docs")
    gid = generic["id"]
    other_memory.save_mission(gid, "報告書をMarkdownで作成", "体裁を整える", "", False, [])
    generic_manager, _ = vehicle_setup(tmp_path / "generic")
    generic_manager.memory = other_memory

    na = trace(generic_manager, gid, "A-1234", "2026-01", "fuel")
    assert na["applicable"] is False
    assert na["status"] == STATUS_NOT_APPLICABLE
    assert na["status"] != STATUS_TRACED
    assert "未実装" in na["reason"]
    na_summary = summary(generic_manager, gid)
    assert na_summary["status"] == STATUS_NOT_APPLICABLE

    manager, pid = vehicle_setup(tmp_path / "vehicle")
    item = manager.memory.add_context_file(pid, "source.txt", "原本", 6, source="upload")
    _write_input(manager, pid, _bind_source(fixture(), item["id"]))
    monkeypatch.setattr(web, "memory", manager.memory)
    monkeypatch.setattr(web, "orchestrator", manager)

    with pytest.raises(HTTPException) as missing_args:
        await web.get_evidence_trace(pid, vehicle_id="", month="", category="")
    assert missing_args.value.status_code == 400

    with pytest.raises(HTTPException) as missing_project:
        await web.get_evidence_trace("missing-project", vehicle_id="A-1234", month="2026-01", category="fuel")
    assert missing_project.value.status_code == 404

    ok = await web.get_evidence_trace(pid, vehicle_id="A-1234", month="2026-01", category="fuel")
    assert ok["status"] == STATUS_TRACED
    overview = await web.get_evidence_summary(pid)
    assert overview["applicable"] is True
    assert overview["counts"][STATUS_TRACED] == overview["cell_total"]

    monkeypatch.setattr(web, "memory", other_memory)
    monkeypatch.setattr(web, "orchestrator", generic_manager)
    sampled = await web.get_evidence_trace(gid, vehicle_id="A-1234", month="2026-01", category="fuel")
    assert sampled["status"] == STATUS_NOT_APPLICABLE


@pytest.mark.asyncio
async def test_eg08_c12_and_completion_gate_unchanged(tmp_path):
    manager, pid = vehicle_setup(tmp_path)
    item = manager.memory.add_context_file(pid, "source.txt", "原本", 6, source="upload")
    await manager.generate_plan(pid)
    activate(manager, pid)
    data = _bind_source(fixture(), item["id"])
    _write_input(manager, pid, data)

    ctx = collect_context(manager, pid)
    before_c12 = check_c12_trace(ctx)
    before_gate = evaluate(manager, pid)
    graph = trace(manager, pid, "A-1234", "2026-01", "fuel")
    overview = summary(manager, pid)
    after_c12 = check_c12_trace(collect_context(manager, pid))
    after_gate = evaluate(manager, pid)

    assert graph["status"] == STATUS_TRACED
    assert overview["counts"][STATUS_TRACED]
    assert after_c12 == before_c12
    assert after_gate == before_gate
    assert before_c12["check_id"] == "check_amount_trace_to_source"

    broken = dict(load_input(manager, pid))
    broken_data = dict(broken["data"])
    records = [dict(r) for r in broken_data["records"]]
    for row in records:
        if row["category"] == "fuel":
            row["source_locator"] = ""
    broken_data["records"] = records
    write_json(resolve(manager, pid, INPUT_PATH), {**broken, "data": broken_data})
    fail_c12 = check_c12_trace(collect_context(manager, pid))
    fail_graph = trace(manager, pid, "A-1234", "2026-01", "fuel")
    fail_gate = evaluate(manager, pid)
    assert fail_c12["status"] == "FAIL"
    assert fail_graph["status"] == STATUS_UNTRACEABLE
    assert fail_gate["achieved"] is False
    assert fail_c12["reason_code"] == "CHECK_MISSING"
    assert "原本参照または位置" in (fail_c12["message"] or "")

    data = _bind_source(fixture(), item["id"])
    _write_input(manager, pid, data)
    c12_ok = check_c12_trace(collect_context(manager, pid))
    manager.memory.add_context_file(pid, "other.txt", "別原本", 8, source="upload")
    stale_graph = trace(manager, pid, "A-1234", "2026-01", "payroll")
    c12_after_stale = check_c12_trace(collect_context(manager, pid))
    assert stale_graph["status"] == STATUS_STALE
    assert stale_graph["status"] != STATUS_TRACED
    assert c12_after_stale == c12_ok
