"""P0-4 local planning failure normalization tests (fictional only, no network)."""
import json
import re
from pathlib import Path

import pytest

from app.planning_errors import (
    PlanningError, candidate_hash, fix_instruction, normalize_planning_error,
)
from app.structured_planning import compile_task

import test_goal_review as goal_review_test


def _proposal(title="架空文書", scope="架空の内容を整理する。", headings=None, extra=None):
    row = {
        "title": title,
        "scope": scope,
        "headings": list(headings or ["目的", "実施内容"]),
        "depends_on": [],
    }
    if extra:
        row.update(extra)
    return row


def _criterion_two_outputs():
    return "架空イベントの案内文とチェックリストを作成する"


def test_coverage_all_compile_task_raises_are_known(tmp_path):
    source = (Path(__file__).resolve().parents[1] / "app" / "structured_planning.py").read_text(encoding="utf-8")
    messages = []
    ok = True
    for chunk in re.findall(r"raise ValueError\((.*?)\)", source, re.S):
        text = chunk.strip()
        literal = None
        if "EMPTY_TASK_KEY" in text:
            continue
        simple = re.fullmatch(r'"([^"]+)"', text, re.S)
        if simple:
            literal = simple.group(1)
        elif "artifact requirement is undetermined" in text:
            literal = "seed: artifact requirement is undetermined; refusing to save"
        elif "artifact requirement invariant broken" in text:
            literal = "seed: artifact requirement invariant broken; refusing to save"
        elif text.startswith('f"goal requires'):
            literal = "goal requires 2 Markdown output(s)"
        elif text.startswith("f\"{REQUIRED_OUTPUT_PATH_UNKNOWN}"):
            literal = "required_output_path_unknown: criterion has undetermined required output paths"
        elif text.startswith("f\"{REQUIRED_OUTPUT_PATH_MISMATCH}"):
            literal = "required_output_path_mismatch: artifact requirement is contradictory"
        elif text.startswith("'既存タスクと同じ題名です"):
            literal = "既存タスクと同じ題名です。今回の達成条件に対応する内容に修正してください"
        else:
            continue
        if "JSON object required" in literal or "criterion count" in literal:
            continue
        if "INVALID_PLAN_STRUCTURE" in literal or "RETENTION_INCOMPLETE" in literal:
            continue
        if "COVERAGE_INCOMPLETE" in literal or "DUPLICATE_KEY" in literal:
            continue
        if "MISSING_CONTRACT" in literal or "EMPTY_OUTPUT_PATH" in literal:
            continue
        if "DUPLICATE_OUTPUT_PATH" in literal or "UNKNOWN_DEPENDENCY" in literal:
            continue
        if "CIRCULAR_DEPENDENCY" in literal or "INVALID_ARTIFACT_CONTRACT" in literal:
            continue
        if "MISSING_APPROVAL_GATE" in literal or "CRITERION_MISMATCH" in literal:
            continue
        if "ABSTRACT_TASK" in literal or "REBUILD_NOT_STRUCTURAL" in literal:
            continue
        if "SCHEMA" in literal or "tasks リスト" in literal or "タスクが空" in literal:
            continue
        if "目標要求の保持率" in literal or "被覆判定不合格" in literal:
            continue
        messages.append(literal)
    assert messages, "compile_task raise messages not found"
    unknown = [text for text in messages if normalize_planning_error(text).code == "unknown"]
    assert unknown == [], unknown


def test_no_leak_secret_title_or_path(tmp_path):
    secret_title = "極秘計画書XYZ-123"
    secret_scope = "秘密の取引条件と社内資料を含む説明です。"
    secret_path = "result/secret_private.md"
    blob = (
        "document output must not duplicate checklist headings "
        + secret_title + " " + secret_scope + " " + secret_path
    )
    err = normalize_planning_error(ValueError(blob))
    assert err.code == "duplicate_checklist_heading"
    fixed = fix_instruction(err)
    assert secret_title not in fixed
    assert secret_scope not in fixed
    assert secret_path not in fixed
    assert "極秘" not in fixed and "XYZ-123" not in fixed
    unknown = normalize_planning_error(ValueError("機密メモ pac-999: " + secret_title))
    assert unknown.code == "unknown"
    assert unknown.observed == "unknown"
    assert secret_title not in unknown.observed
    assert secret_title not in fix_instruction(unknown)
    prompt_text = "前回の形式エラー: エラーコード: duplicate_checklist_heading"
    assert secret_title not in prompt_text


def test_deterministic_and_hash_order_independent():
    first = normalize_planning_error("headings must contain 2-8 nonempty strings")
    second = normalize_planning_error(ValueError("headings must contain 2-8 nonempty strings"))
    assert first == second
    left = candidate_hash({"b": 2, "a": 1})
    right = candidate_hash({"a": 1, "b": 2})
    assert left == right
    assert candidate_hash({"a": 1}) != candidate_hash({"a": 2})


def test_planning_errors_module_has_no_forbidden_patterns():
    source = (Path(__file__).resolve().parents[1] / "app" / "planning_errors.py").read_text(encoding="utf-8")
    for token in ["add_event(", "open(", "print(", "except Exception", "except:"]:
        assert token not in source, token


def _setup_manager(tmp_path):
    manager, pid, _task = goal_review_test.setup(tmp_path, True)
    return manager, pid


@pytest.mark.asyncio
async def test_loop_uses_fix_instruction_and_recovers(tmp_path):
    manager, pid = _setup_manager(tmp_path)
    seen_prompts = []
    calls = {"count": 0}

    async def fake_complete(system, prompt, schema=None):
        seen_prompts.append(prompt)
        calls["count"] += 1
        if calls["count"] == 1:
            return json.dumps(_proposal(
                "架空案内",
                "架空イベントの案内内容を整理する。",
                ["案内文", "注意事項"],
                {"outputs": [
                    {"kind": "document", "required_headings": ["案内文", "チェックリスト"]},
                    {"kind": "checklist", "required_headings": ["準備", "当日対応"]},
                ]},
            ), ensure_ascii=False)
        return json.dumps(_proposal(
            "架空案内",
            "架空イベントの案内内容を整理する。",
            ["案内文", "連絡方法"],
            {"outputs": [
                {"kind": "document", "required_headings": ["案内文", "連絡方法"]},
                {"kind": "checklist", "required_headings": ["準備", "当日対応"]},
            ]},
        ), ensure_ascii=False)

    manager._local_complete = fake_complete
    mission = await manager._generate_structured_plan(pid, [_criterion_two_outputs()])
    assert mission["tasks"]
    assert calls["count"] == 2
    assert len(seen_prompts) == 2
    retry_prompt = seen_prompts[1]
    assert "文書側の見出しからチェックリストの見出しを除き、チェックリスト出力にだけ置く" in retry_prompt
    assert "document output must not duplicate checklist headings" not in retry_prompt


def _switch_to_single_sc(manager, pid, goal_text="架空目標", criterion="架空の案内文を作る"):
    from app.memory.short_term import ShortTermMemory

    mission = manager.memory.get_mission(pid)
    assert isinstance(mission, dict)
    manager.memory.save_mission(
        pid, goal_text, "## 達成条件\n1. " + criterion, "", False, [],
    )
    updated = manager.memory.get_mission(pid)
    manager.memory.replace_plan(
        pid, updated.get("plan_summary") or "test",
        updated.get("tasks") or [],
        expected_version=updated.get("plan_version"),
    )


@pytest.mark.asyncio
async def test_same_hash_same_code_stops_regeneration(tmp_path):
    manager, pid = _setup_manager(tmp_path)
    _switch_to_single_sc(manager, pid)
    calls = {"count": 0}

    async def always_bad(system, prompt, schema=None):
        calls["count"] += 1
        return json.dumps(_proposal(
            "架空案内",
            "架空イベントの案内内容を整理する。",
            ["案内文", "注意事項"],
            {"outputs": [
                {"kind": "document", "required_headings": ["案内文", "チェックリスト"]},
                {"kind": "checklist", "required_headings": ["準備", "当日対応"]},
            ]},
        ), ensure_ascii=False)

    manager._local_complete = always_bad
    with pytest.raises(ValueError, match="無益な再生成|確定できません"):
        await manager._generate_structured_plan(pid, [_criterion_two_outputs()])
    assert calls["count"] == 2
    kinds = [event["kind"] for event in manager.memory.get_mission(pid)["events"]]
    assert "planning_blocked" in kinds
    detail = next(
        json.loads(event["detail"])
        for event in manager.memory.get_mission(pid)["events"]
        if event["kind"] == "planning_blocked"
    )
    assert detail["criterion_id"] == "SC01"
    assert len(detail["attempts"]) == 2
    assert all(set(row) == {"attempt", "code", "output_index", "field", "candidate_hash"} for row in detail["attempts"])
    assert detail["attempts"][0]["code"] == detail["attempts"][1]["code"] == "duplicate_checklist_heading"
    assert detail["attempts"][0]["candidate_hash"] == detail["attempts"][1]["candidate_hash"]


@pytest.mark.asyncio
async def test_three_failures_keep_value_error_and_blocked_detail(tmp_path):
    manager, pid = _setup_manager(tmp_path)
    mission = manager.memory.get_mission(pid)
    mission["plan_version"] = 1
    manager.memory.save_mission(
        pid, "架空目標", "## 達成条件\n1. 架空の案内文を作る", "", False, [],
    )
    calls = {"count": 0}

    async def heading_cases(system, prompt, schema=None):
        calls["count"] += 1
        if calls["count"] == 1:
            proposal = _proposal("A", "内容", ["一つだけ"])
        elif calls["count"] == 2:
            proposal = _proposal("B", "内容", ["a", "b", "c", "d", "e", "f", "g", "h", "i"])
        else:
            proposal = _proposal("C", "x" * 1700, ["目的", "実施内容"])
        return json.dumps(proposal, ensure_ascii=False)

    manager._local_complete = heading_cases
    with pytest.raises(ValueError, match="3回で確定できません"):
        await manager._generate_structured_plan(pid, ["架空の案内文を作る"])
    assert calls["count"] == 3
    events = manager.memory.get_mission(pid)["events"]
    kinds = [event["kind"] for event in events]
    assert "planning_blocked" in kinds
    detail = json.loads(next(event["detail"] for event in events if event["kind"] == "planning_blocked"))
    assert detail["criterion_id"] == "SC01"
    assert len(detail["attempts"]) == 3
    codes = [row["code"] for row in detail["attempts"]]
    assert codes == ["headings_invalid", "headings_invalid", "proposal_too_long"]
    blob = json.dumps(detail, ensure_ascii=False)
    assert "架空目標" not in blob and "架空の案内文" not in blob
    assert "switch_candidates" in detail
    assert set(detail["switch_candidates"]) == {"models", "template"}
    assert "template" in detail["switch_candidates"]
    assert "available" in detail["switch_candidates"]["template"]
    assert isinstance(detail["switch_candidates"]["models"], list)
    assert all("chatgpt" not in str(value).lower() for value in detail["switch_candidates"]["models"])
    assert all("claude" not in str(value).lower() for value in detail["switch_candidates"]["models"])


@pytest.mark.asyncio
async def test_no_model_switch_is_executed(tmp_path):
    manager, pid = _setup_manager(tmp_path)
    _switch_to_single_sc(manager, pid)
    before = str(getattr(manager.llm, "model", "") or "")
    calls = {"count": 0}

    async def always_bad(system, prompt, schema=None):
        calls["count"] += 1
        return json.dumps(_proposal(
            "架空案内",
            "架空イベントの案内内容を整理する。",
            ["案内文", "注意事項"],
            {"outputs": [
                {"kind": "document", "required_headings": ["案内文", "チェックリスト"]},
                {"kind": "checklist", "required_headings": ["準備", "当日対応"]},
            ]},
        ), ensure_ascii=False)

    manager._local_complete = always_bad
    with pytest.raises(ValueError, match="無益な再生成|確定できません"):
        await manager._generate_structured_plan(pid, [_criterion_two_outputs()])
    assert str(getattr(manager.llm, "model", "") or "") == before


@pytest.mark.asyncio
async def test_missing_candidates_do_not_stop_before_third_attempt(tmp_path):
    manager, pid = _setup_manager(tmp_path)
    _switch_to_single_sc(manager, pid)
    calls = {"count": 0}

    async def invalid_json(system, prompt, schema=None):
        calls["count"] += 1
        return "not-json-one" if calls["count"] == 1 else "not-json-two"

    manager._local_complete = invalid_json
    with pytest.raises(ValueError, match="3回で確定できません"):
        await manager._generate_structured_plan(pid, ["架空の案内文を作る"])
    assert calls["count"] == 3
    detail = json.loads(next(
        event["detail"] for event in manager.memory.get_mission(pid)["events"]
        if event["kind"] == "planning_blocked"
    ))
    assert [row["candidate_hash"] for row in detail["attempts"]] == ["", "", ""]


@pytest.mark.asyncio
async def test_candidate_hash_does_not_leak_from_previous_criterion(tmp_path):
    manager, pid = _setup_manager(tmp_path)
    prompts = []
    calls = {"count": 0}

    async def staged(system, prompt, schema=None):
        prompts.append(prompt)
        calls["count"] += 1
        if calls["count"] == 1:
            return json.dumps(_proposal("SC01失敗", "内容", ["一つだけ"]), ensure_ascii=False)
        if calls["count"] == 2:
            return json.dumps(_proposal("SC01成功", "内容", ["目的", "内容"]), ensure_ascii=False)
        return "sc02-invalid-json"

    manager._local_complete = staged
    with pytest.raises(ValueError, match="SC02.*3回で確定できません"):
        await manager._generate_structured_plan(pid, ["架空資料一を作る", "架空資料二を作る"])
    blocked = [
        json.loads(event["detail"])
        for event in manager.memory.get_mission(pid)["events"]
        if event["kind"] == "planning_blocked"
    ]
    sc02 = next(item for item in blocked if item["criterion_id"] == "SC02")
    assert len(sc02["attempts"]) == 3
    assert all(row["candidate_hash"] == "" for row in sc02["attempts"])


@pytest.mark.asyncio
async def test_initial_prompt_keeps_empty_previous_error(tmp_path):
    manager, pid = _setup_manager(tmp_path)
    seen = []

    async def valid(system, prompt, schema=None):
        seen.append(prompt)
        return json.dumps(_proposal(), ensure_ascii=False)

    manager._local_complete = valid
    await manager._generate_structured_plan(pid, ["架空資料を作る"])
    marker = "前回の形式エラー: "
    suffix = seen[0].split(marker, 1)[1]
    assert suffix == ""
    assert "JSON形式と制約を再確認して修正" not in suffix


@pytest.mark.asyncio
async def test_unknown_errors_do_not_nest_prompt_text(tmp_path):
    manager, pid = _setup_manager(tmp_path)
    _switch_to_single_sc(manager, pid)
    seen = []
    messages = ["unknown-one", "unknown-two", "unknown-three"]

    async def invalid_json(system, prompt, schema=None):
        seen.append(prompt)
        return messages[len(seen) - 1]

    manager._local_complete = invalid_json
    with pytest.raises(ValueError, match="3回で確定できません") as raised:
        await manager._generate_structured_plan(pid, ["架空資料を作る"])
    assert len(seen) == 3
    assert seen[1].count("前回の形式エラー(短縮)") == 1
    assert seen[2].count("前回の形式エラー(短縮)") == 1
    assert "前回の形式エラー(短縮): JSON形式と制約" not in seen[2]
    assert str(raised.value).endswith("JSON object required")
