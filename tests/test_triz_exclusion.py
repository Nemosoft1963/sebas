import json
from unittest.mock import AsyncMock, patch

import pytest

from app.automatic_triz import (
    adopt_adapter, is_business_success, on_failure, on_vehicle_failure, triz_display,
)
from app.triz_exclusion import exclusion_for
from app.upgrade_runtime import ReviewRequired
from test_vehicle_workflow import setup


def test_tz_01_existing_defects_and_structured_facts_are_excluded():
    cases = [
        ("読取失敗", None, None, "SOURCE_READ_FAILED", "source_fix"),
        ("原本統制 mismatched", None, None, "SOURCE_RECONCILIATION_FAILED", "source_fix"),
        ("", {"data": {"auto_extraction": {"issues": []}, "source_controls": []}}, None,
         "SOURCE_CONTROLS_MISSING", "source_fix"),
        ("", {"data": {"auto_extraction": {"invoices": [{"extraction_complete": False}]},
                          "source_controls": [{}]}}, None,
         "SOURCE_INVOICE_INCOMPLETE", "source_fix"),
        ("確認待ち", {"data": {"issues": [{"kind": "allocation", "status": "open"}]}}, None,
         "ALLOCATION_UNRESOLVED", "human_fact"),
        ("確認待ち", {"data": {"issues": [{"kind": "business_fact", "status": "open"}]}}, None,
         "FACT_MISSING", "human_fact"),
        ("development_required", None, None, "DEVELOPMENT_REQUIRED", "development"),
    ]
    for text, envelope, task, code, owner in cases:
        result = exclusion_for(text, envelope, task)
        assert result and result["code"] == code
        assert result["next_owner"] == owner
    assert exclusion_for("未対応形式の adapter") is None
    assert exclusion_for("vehicle review required") is None


@pytest.mark.asyncio
async def test_tz_02_excluded_failure_never_calls_invention_or_adapter(tmp_path):
    manager, pid = setup(tmp_path)
    task = {"id": "t-excluded", "title": "抽出", "description": "", "acceptance_criteria": "{}"}
    error = ReviewRequired("読取失敗を確認してください")
    with patch("app.triz_general.create") as create, \
         patch("app.triz_adapters.execute_step", new=AsyncMock()) as execute, \
         patch("app.automatic_triz.register_adapter") as register:
        result = await on_failure(manager, pid, task, error)
    assert result["excluded_from_invention"] is True
    assert result["exclusion_code"] == "SOURCE_READ_FAILED"
    assert result["candidates"] == [] and result["experiments"] == []
    assert is_business_success(result["status"]) is False
    assert triz_display(result["status"])["business_recovered"] is False
    create.assert_not_called(); execute.assert_not_called(); register.assert_not_called()
    files = list((tmp_path / "workspace").rglob("*.json"))
    body = next(json.loads(path.read_text(encoding="utf-8")) for path in files
                if "triz" in path.parts and path.name != "input.json")
    assert body["excluded_from_invention"] is True


@pytest.mark.asyncio
async def test_tz_03_unknown_format_retains_original_path(tmp_path):
    manager, pid = setup(tmp_path)
    task = {"id": "t-old", "title": "未対応形式", "description": "", "acceptance_criteria": "{}"}
    error = ReviewRequired("未対応形式の adapter が必要です")
    # The old off/shadow branch still frames and saves without entering general invention.
    with patch("app.automatic_triz.frame", wraps=__import__("app.automatic_triz", fromlist=["frame"]).frame) as framed:
        result = await on_failure(manager, pid, task, error)
    assert framed.called
    assert result is None  # Existing on_failure contract is unchanged for this branch.
    files = [path for path in (tmp_path / "workspace").rglob("*.json") if "triz" in path.parts]
    body = json.loads(files[0].read_text(encoding="utf-8"))
    assert not body.get("excluded_from_invention", False)
    assert body["status"] == "development_required"


def test_tz_04_adopt_remains_denied_by_default():
    spec = {"adapter_id": "identity_passthrough", "function_id": "identity_passthrough"}
    with pytest.raises(ValueError, match="確認者"):
        adopt_adapter("p", spec, "", "", {}, {}, {})
    with pytest.raises(ValueError, match="両方"):
        adopt_adapter("p", spec, "reviewer", "evidence", {},
                      {"business_passed": False}, {"business_passed": False})
