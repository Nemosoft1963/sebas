"""Phase 3-D: generic mission LLM-verbatim decompose, correspondence, confirm."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.completion_gate import evaluate
from app.core import Ollama
from app.goal_completion_flag import enable
from app.goal_completion_store import GoalCompletionStore
from app.goal_contract import get_active, get_latest, put_draft
from app.goal_decompose import (
    DecomposeRejected,
    DecomposeUnavailable,
    confirm,
    decompose,
)
from app.goal_state_machine import settle_achieved
from app.memory.short_term import ShortTermMemory
from app.requirement_retention import inspect
from test_vehicle_workflow import setup as vehicle_setup


LONG_GOAL = (
    "市場調査を実施する。"
    "競合比較表を作成する。"
    "顧客ヒアリング結果を整理する。"
    "提案資料の骨子を文書化する。"
) * 20  # >400 chars, multiple independent requirements


def _generic_setup(tmp_path, goal=None, success="", constraints=""):
    memory = ShortTermMemory(tmp_path / "memory.sqlite3")
    project = memory.create_project("generic-decompose")
    pid = project["id"]
    memory.save_mission(
        pid,
        goal if goal is not None else LONG_GOAL,
        success,
        constraints,
        False,
        [],
    )
    return memory, pid


class FakeOllama(Ollama):
    def __init__(self, payload, healthy=True, model="fake-local"):
        self.url = "http://local-ollama.test"
        self.model = model
        self._healthy = healthy
        self.payload = payload
        self.calls = []
        self._capabilities_model = None
        self._capabilities = set()

    async def health(self):
        return bool(self._healthy)

    async def complete_json(self, messages, schema):
        self.calls.append({"messages": messages, "schema": schema})
        if isinstance(self.payload, Exception):
            raise self.payload
        if isinstance(self.payload, str):
            return self.payload
        return json.dumps(self.payload, ensure_ascii=False)


class ForeignLlm:
    def __init__(self):
        self.called = False
        self.model = "external"

    async def health(self):
        self.called = True
        return True

    async def complete_json(self, messages, schema):
        self.called = True
        raise AssertionError("external AI must not receive requirement text")


def _manager(tmp_path, llm, goal=None, success="", constraints=""):
    memory, pid = _generic_setup(tmp_path, goal=goal, success=success, constraints=constraints)
    return SimpleNamespace(memory=memory, llm=llm), pid


def _snapshot(memory, pid):
    mission = memory.get_mission(pid)
    ledger = GoalCompletionStore(memory.path).path
    return {
        "goal": mission.get("goal"),
        "success": mission.get("success_criteria"),
        "constraints": mission.get("constraints_text"),
        "plan_version": mission.get("plan_version"),
        "ledger": ledger.read_bytes() if ledger.exists() else None,
        "memory": memory.path.read_bytes(),
    }


@pytest.mark.asyncio
async def test_gd01_verbatim_quotes_are_adopted_and_nothing_is_saved(tmp_path):
    quotes = [
        "市場調査を実施する。",
        "競合比較表を作成する。",
        "顧客ヒアリング結果を整理する。",
    ]
    llm = FakeOllama({"items": [{"quote": q, "kind": "requirement"} for q in quotes]})
    manager, pid = _manager(tmp_path, llm)
    before = _snapshot(manager.memory, pid)
    result = await decompose(manager, pid)
    after = _snapshot(manager.memory, pid)
    assert result["applicable"] is True
    assert result["saved"] is False
    assert result["draft"] is True
    assert [item["quote"] for item in result["items"]] == quotes
    assert all(item["item_id"].startswith("DC") for item in result["items"])
    assert after == before
    assert get_latest(manager, pid) is None
    assert get_active(manager, pid) is None


@pytest.mark.asyncio
async def test_gd02a_paraphrase_is_dropped_not_verbatim(tmp_path):
    llm = FakeOllama({"items": [
        {"quote": "市場調査を実施する。", "kind": "requirement"},
        {"quote": "市場を調べてレポートを書く", "kind": "requirement"},
    ]})
    manager, pid = _manager(tmp_path, llm)
    result = await decompose(manager, pid)
    assert [item["quote"] for item in result["items"]] == ["市場調査を実施する。"]
    reasons = {row["reason"] for row in result["dropped"]}
    assert "not_verbatim" in reasons
    assert any(row["quote"] == "市場を調べてレポートを書く" for row in result["dropped"])


@pytest.mark.asyncio
async def test_gd02b_whitespace_only_difference_is_adopted(tmp_path):
    llm = FakeOllama({"items": [
        {"quote": "市場調査を  実施する。", "kind": "requirement"},
    ]})
    manager, pid = _manager(tmp_path, llm)
    result = await decompose(manager, pid)
    assert len(result["items"]) == 1
    assert result["items"][0]["field"] == "goal"


@pytest.mark.asyncio
async def test_gd02c_duplicate_empty_and_too_long_are_dropped(tmp_path):
    long_quote = "市場調査を実施する。" + ("あ" * 400)
    llm = FakeOllama({"items": [
        {"quote": "市場調査を実施する。", "kind": "requirement"},
        {"quote": "市場調査を実施する。", "kind": "requirement"},
        {"quote": "   ", "kind": "requirement"},
        {"quote": long_quote, "kind": "requirement"},
    ]})
    manager, pid = _manager(tmp_path, llm)
    result = await decompose(manager, pid)
    assert [item["quote"] for item in result["items"]] == ["市場調査を実施する。"]
    dropped_reasons = {row["reason"] for row in result["dropped"]}
    assert {"duplicate", "empty", "too_long"} <= dropped_reasons


@pytest.mark.asyncio
async def test_gd02d_invalid_schema_fails_entirely(tmp_path):
    llm = FakeOllama({"items": "not-a-list"})
    manager, pid = _manager(tmp_path, llm)
    with pytest.raises(DecomposeUnavailable):
        await decompose(manager, pid)


@pytest.mark.asyncio
async def test_gd03a_uncovered_sentences_are_detected_by_code(tmp_path):
    llm = FakeOllama({"items": [
        {"quote": "市場調査を実施する。", "kind": "requirement"},
    ]})
    manager, pid = _manager(tmp_path, llm)
    result = await decompose(manager, pid)
    spans = [row["span"] for row in result["uncovered"]]
    assert any("競合比較表を作成する。" in span for span in spans)
    assert any("顧客ヒアリング結果を整理する。" in span for span in spans)
    covered = {cid for row in result["correspondence"] for cid in row["item_ids"]}
    assert "DC01" in covered


@pytest.mark.asyncio
async def test_gd03b_full_coverage_has_empty_uncovered(tmp_path):
    goal = "市場調査を実施する。競合比較表を作成する。"
    llm = FakeOllama({"items": [
        {"quote": "市場調査を実施する。", "kind": "requirement"},
        {"quote": "競合比較表を作成する。", "kind": "requirement"},
    ]})
    manager, pid = _manager(tmp_path, llm, goal=goal)
    result = await decompose(manager, pid)
    assert result["uncovered"] == []
    assert len(result["items"]) == 2


@pytest.mark.asyncio
async def test_gd04a_vehicle_mission_is_not_applicable(tmp_path):
    manager, pid = vehicle_setup(tmp_path)
    manager.llm = FakeOllama({"items": [{"quote": "車両別損益をExcelで作成", "kind": "requirement"}]})
    result = await decompose(manager, pid)
    assert result["applicable"] is False
    assert "車両案件" in result["reason"]
    assert result["items"] == []
    assert manager.llm.calls == []


@pytest.mark.asyncio
async def test_gd04b_unhealthy_local_llm_raises_without_fake_items(tmp_path):
    llm = FakeOllama({"items": [{"quote": "市場調査を実施する。", "kind": "requirement"}]}, healthy=False)
    manager, pid = _manager(tmp_path, llm)
    with pytest.raises(DecomposeUnavailable):
        await decompose(manager, pid)
    assert llm.calls == []


@pytest.mark.asyncio
async def test_gd04c_non_ollama_llm_is_unavailable_and_not_called(tmp_path):
    foreign = ForeignLlm()
    manager, pid = _manager(tmp_path, foreign)
    with pytest.raises(DecomposeUnavailable):
        await decompose(manager, pid)
    assert foreign.called is False


def test_gd05a_unknown_add_edit_operations_are_rejected(tmp_path):
    llm = FakeOllama({"items": []})
    manager, pid = _manager(tmp_path, llm, goal="市場調査を実施する。競合比較表を作成する。")
    enable(manager.memory.path, pid)
    items = [
        {"item_id": "DC01", "kind": "requirement", "quote": "市場調査を実施する。", "field": "goal"},
        {"item_id": "DC02", "kind": "requirement", "quote": "競合比較表を作成する。", "field": "goal"},
    ]
    with pytest.raises(DecomposeRejected) as caught:
        confirm(manager, pid, items, [{"op": "add", "quote": "新しい要求"}], "reviewer")
    assert caught.value.status_code == 422
    with pytest.raises(DecomposeRejected):
        confirm(manager, pid, items, [{"op": "edit", "item_id": "DC01", "quote": "書き換え"}], "reviewer")
    with pytest.raises(DecomposeRejected):
        confirm(manager, pid, items, [{"op": "rewrite"}], "reviewer")
    assert get_latest(manager, pid) is None


def test_gd05b_blank_reviewed_by_is_rejected(tmp_path):
    manager, pid = _manager(tmp_path, FakeOllama({"items": []}), goal="市場調査を実施する。")
    enable(manager.memory.path, pid)
    items = [{"item_id": "DC01", "kind": "requirement", "quote": "市場調査を実施する。", "field": "goal"}]
    with pytest.raises(DecomposeRejected) as caught:
        confirm(manager, pid, items, [], "  ")
    assert caught.value.code == "REVIEWED_BY_REQUIRED"
    assert caught.value.status_code == 422
    assert get_latest(manager, pid) is None


def test_gd05c_tampered_quote_is_rejected_on_server_recheck(tmp_path):
    manager, pid = _manager(tmp_path, FakeOllama({"items": []}), goal="市場調査を実施する。")
    enable(manager.memory.path, pid)
    items = [{"item_id": "DC01", "kind": "requirement", "quote": "市場を調べてレポートを書く", "field": "goal"}]
    with pytest.raises(DecomposeRejected) as caught:
        confirm(manager, pid, items, [], "reviewer")
    assert caught.value.code == "NOT_VERBATIM"
    assert get_latest(manager, pid) is None


def test_gd05d_merge_concatenates_quotes_in_source_order(tmp_path):
    goal = "市場調査を実施する。競合比較表を作成する。"
    manager, pid = _manager(tmp_path, FakeOllama({"items": []}), goal=goal)
    enable(manager.memory.path, pid)
    items = [
        {"item_id": "DC01", "kind": "requirement", "quote": "競合比較表を作成する。", "field": "goal"},
        {"item_id": "DC02", "kind": "requirement", "quote": "市場調査を実施する。", "field": "goal"},
    ]
    result = confirm(manager, pid, items, [{"op": "merge", "item_ids": ["DC01", "DC02"]}], "reviewer")
    assert len(result["items"]) == 1
    assert result["items"][0]["quote"] == "市場調査を実施する。競合比較表を作成する。"
    assert "新しい" not in result["items"][0]["quote"]
    assert result["activated"] is False
    stored = get_latest(manager, pid)
    assert stored["_status"] == "draft"
    assert get_active(manager, pid) is None


def test_gd05e_removing_all_items_is_rejected_and_uncovered_after_is_returned(tmp_path):
    goal = "市場調査を実施する。競合比較表を作成する。"
    manager, pid = _manager(tmp_path, FakeOllama({"items": []}), goal=goal)
    enable(manager.memory.path, pid)
    items = [
        {"item_id": "DC01", "kind": "requirement", "quote": "市場調査を実施する。", "field": "goal"},
        {"item_id": "DC02", "kind": "requirement", "quote": "競合比較表を作成する。", "field": "goal"},
    ]
    with pytest.raises(DecomposeRejected) as caught:
        confirm(manager, pid, items, [{"op": "remove", "item_id": "DC01"}, {"op": "remove", "item_id": "DC02"}], "reviewer")
    assert caught.value.code == "EMPTY_AFTER_CONFIRM"
    remaining = confirm(manager, pid, items, [{"op": "remove", "item_id": "DC02"}], "reviewer")
    assert remaining["uncovered_after"]
    assert any("競合比較表を作成する。" in row["span"] for row in remaining["uncovered_after"])


def test_gd06a_confirm_saves_draft_and_retention_passes(tmp_path):
    manager, pid = _manager(tmp_path, FakeOllama({"items": []}))
    enable(manager.memory.path, pid)
    quotes = [
        "市場調査を実施する。",
        "競合比較表を作成する。",
        "顧客ヒアリング結果を整理する。",
        "提案資料の骨子を文書化する。",
    ]
    items = [
        {"item_id": f"DC{i:02d}", "kind": "requirement", "quote": quote, "field": "goal"}
        for i, quote in enumerate(quotes, 1)
    ]
    result = confirm(manager, pid, items, [], "human-reviewer")
    stored = get_latest(manager, pid)
    assert stored is not None
    assert stored["_status"] == "draft"
    assert get_active(manager, pid) is None
    assert result["activated"] is False
    assert stored["source_versions"]["decomposed_by"] == "llm-verbatim"
    assert stored["source_versions"]["reviewed_by"] == "human-reviewer"
    mission = manager.memory.get_mission(pid)
    retention = inspect(mission, stored)
    assert retention["collapsed"] is False
    assert retention["passed"] is True
    assert len(stored["extracted_criteria"]) == 4


def test_gd06b_flag_off_confirm_saves_nothing(tmp_path):
    manager, pid = _manager(tmp_path, FakeOllama({"items": []}), goal="市場調査を実施する。")
    items = [{"item_id": "DC01", "kind": "requirement", "quote": "市場調査を実施する。", "field": "goal"}]
    with pytest.raises(DecomposeRejected) as caught:
        confirm(manager, pid, items, [], "reviewer")
    assert caught.value.code == "FLAG_OFF"
    assert caught.value.status_code == 409
    assert get_latest(manager, pid) is None


def test_gd07_generic_evaluate_never_achieves_and_settle_is_refused(tmp_path):
    manager, pid = _manager(tmp_path, FakeOllama({"items": []}), goal="市場調査を実施する。競合比較表を作成する。")
    enable(manager.memory.path, pid)
    items = [
        {"item_id": "DC01", "kind": "requirement", "quote": "市場調査を実施する。", "field": "goal"},
        {"item_id": "DC02", "kind": "requirement", "quote": "競合比較表を作成する。", "field": "goal"},
    ]
    confirm(manager, pid, items, [], "reviewer")
    from app.goal_contract import activate
    activate(manager, pid)
    store = GoalCompletionStore(manager.memory.path)
    hashes = {"contract_hash": "c", "plan_signature": "p", "input_hash": "", "source_hash": "", "artifact_hash": "a"}
    store.record_acceptance(
        pid, hashes["contract_hash"], hashes["plan_signature"],
        hashes["input_hash"], hashes["source_hash"], hashes["artifact_hash"],
        "human", "ok",
    )
    with patch("app.completion_gate.build_coverage", return_value={"passed": True, "rows": []}), patch(
        "app.completion_gate._current_hashes", return_value=hashes,
    ):
        gate = evaluate(manager, pid)
    assert gate["achieved"] is False
    assert gate["reason_code"] in {"GENERIC_NOT_ACHIEVED", "GENERIC_VERIFIED_CAP"} or gate["failed_criteria"]
    with pytest.raises(ValueError, match="ACHIEVED_REQUIRES_GATE"):
        with patch("app.completion_gate.build_coverage", return_value={"passed": True, "rows": []}), patch(
            "app.completion_gate._current_hashes", return_value=hashes,
        ):
            settle_achieved(manager, pid)


def _web_setup(tmp_path, monkeypatch, llm, flag=True, goal=None):
    import app.web as web
    manager, pid = _manager(tmp_path, llm, goal=goal)
    if flag:
        enable(manager.memory.path, pid)
    monkeypatch.setattr(web, "memory", manager.memory)
    monkeypatch.setattr(web, "orchestrator", manager)
    monkeypatch.setattr(web, "require_project", lambda _: {"id": pid})
    return web, manager, pid


@pytest.mark.asyncio
async def test_gd08a_decompose_api_does_not_save(tmp_path, monkeypatch):
    llm = FakeOllama({"items": [{"quote": "市場調査を実施する。", "kind": "requirement"}]})
    web, manager, pid = _web_setup(tmp_path, monkeypatch, llm, flag=False)
    before = _snapshot(manager.memory, pid)
    body = await web.decompose_goal_contract(pid)
    after = _snapshot(manager.memory, pid)
    assert body["saved"] is False
    assert body["items"]
    assert after == before


@pytest.mark.asyncio
async def test_gd08b_decompose_api_returns_503_when_llm_unavailable(tmp_path, monkeypatch):
    llm = FakeOllama({"items": []}, healthy=False)
    web, manager, pid = _web_setup(tmp_path, monkeypatch, llm, flag=False)
    with pytest.raises(HTTPException) as caught:
        await web.decompose_goal_contract(pid)
    assert caught.value.status_code == 503


@pytest.mark.asyncio
async def test_gd08c_confirm_api_flag_off_is_409(tmp_path, monkeypatch):
    from app.web import GoalDecomposeConfirmPayload
    llm = FakeOllama({"items": []})
    web, manager, pid = _web_setup(
        tmp_path, monkeypatch, llm, flag=False, goal="市場調査を実施する。",
    )
    payload = GoalDecomposeConfirmPayload(
        draft_items=[{"item_id": "DC01", "kind": "requirement", "quote": "市場調査を実施する。", "field": "goal"}],
        operations=[],
        reviewed_by="reviewer",
    )
    with pytest.raises(HTTPException) as caught:
        await web.confirm_goal_decompose(pid, payload)
    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "FLAG_OFF"
    assert get_latest(manager, pid) is None


@pytest.mark.asyncio
async def test_gd08d_confirm_api_blank_reviewed_by_is_422(tmp_path, monkeypatch):
    from app.web import GoalDecomposeConfirmPayload
    llm = FakeOllama({"items": []})
    web, manager, pid = _web_setup(tmp_path, monkeypatch, llm, goal="市場調査を実施する。")
    payload = GoalDecomposeConfirmPayload(
        draft_items=[{"item_id": "DC01", "kind": "requirement", "quote": "市場調査を実施する。", "field": "goal"}],
        operations=[],
        reviewed_by="   ",
    )
    with pytest.raises(HTTPException) as caught:
        await web.confirm_goal_decompose(pid, payload)
    assert caught.value.status_code == 422
    assert get_latest(manager, pid) is None


@pytest.mark.asyncio
async def test_gd08e_existing_goal_contract_routes_unchanged(tmp_path, monkeypatch):
    llm = FakeOllama({"items": []})
    web, manager, pid = _web_setup(tmp_path, monkeypatch, llm, flag=False, goal="方針を文書化する")
    previewed = await web.get_goal_contract(pid)
    assert previewed["schema"] == "sebas-goal-contract/v1"
    stored = await web.put_goal_contract(pid, web.GoalContractPayload(payload={}))
    assert stored["_status"] == "draft"
    activated = await web.activate_goal_contract(pid)
    assert activated["_status"] == "active"
    retention = await web.goal_contract_retention(pid)
    assert "passed" in retention
    assert "collapsed" in retention
