import pytest
"""P0-0前段修正の受入試験。疑似PJ・実関数・固定のフェイク応答で再現する。

特定案件の語彙を製品コードに入れない。LLMは固定のフェイク応答で再現する。
"""
from app.structured_planning import (
    build_p0a_artifact_slots,
    check_p0a_separate_artifacts,
    compile_plan,
    compile_task,
    count_p0a_required_files,
    ensure_p0a_artifact_slots,
    normalize_p0a_artifact_requirement,
)


CRITERION = "資料Aを1ファイル、資料Bを1ファイル、合計2ファイルで作成する"


def _proposal(title, scope, headings, extra=None):
    row = {"title": title, "scope": scope, "headings": headings, "depends_on": []}
    if extra:
        row.update(extra)
    return row


def _split_task():
    return compile_task(1, CRITERION, _proposal(
        "疑似分離", "疑似の2資料を整理する。", ["概要", "詳細"],
        {"outputs": [
            {"kind": "document", "required_headings": ["資料Aの概要", "資料Aの詳細"]},
            {"kind": "document", "required_headings": ["資料Bの概要", "資料Bの詳細"]},
        ]}), [])


def _merged_task():
    return compile_task(1, CRITERION, _proposal(
        "疑似統合", "疑似の2資料を整理する。", ["概要", "詳細"]), [])


def test_p0a0_two_slots_are_normalized():
    norm = normalize_p0a_artifact_requirement(CRITERION)
    assert norm["consistent"] is True
    assert norm["required"] == 2
    assert norm["item_total"] == 2
    assert norm["explicit_total"] == 2
    slots = build_p0a_artifact_slots(CRITERION)
    assert [row["slot_id"] for row in slots] == ["slot-01", "slot-02"]
    assert count_p0a_required_files(CRITERION) == 2
    # 先行する『1ファイル』だけで合計を1件にしていないこと。
    first_only = normalize_p0a_artifact_requirement("資料Aを1ファイルで作成する")
    assert first_only["required"] == 1


def test_p0a0_two_files_pass_and_merged_fails():
    split = compile_plan([CRITERION], [_split_task()], goal="疑似P0-0")
    good = check_p0a_separate_artifacts([CRITERION], split["tasks"])
    assert good["passed"] is True
    merged = compile_plan([CRITERION], [_merged_task()], goal="疑似P0-0")
    bad = check_p0a_separate_artifacts([CRITERION], merged["tasks"])
    assert bad["passed"] is False
    assert bad["issues"]


def test_p0a0_limited_regen_saves_when_fake_returns_two():
    merged = compile_plan([CRITERION], [_merged_task()], goal="疑似P0-0")
    split_tasks = compile_plan([CRITERION], [_split_task()], goal="疑似P0-0")["tasks"]
    calls: list[list[str]] = []

    def fake_regen(missing):
        calls.append(list(missing))
        assert missing == ["slot-02"]
        return {"tasks": split_tasks}

    result = ensure_p0a_artifact_slots([CRITERION], merged["tasks"], fake_regen)
    assert result["saved"] is True
    assert result["attempts_used"] == 1
    assert calls == [["slot-02"]]
    assert result["check"]["passed"] is True


def test_p0a0_still_merged_after_two_attempts_is_not_saved():
    merged = compile_plan([CRITERION], [_merged_task()], goal="疑似P0-0")
    calls: list[list[str]] = []

    def fake_still_merged(missing):
        calls.append(list(missing))
        return {"tasks": list(merged["tasks"])}

    result = ensure_p0a_artifact_slots(
        [CRITERION], merged["tasks"], fake_still_merged, max_attempts=2)
    assert result["saved"] is False
    assert result["attempts_used"] == 2
    assert len(calls) == 2
    assert result["check"]["passed"] is False


def test_p0a0_contradictory_total_is_not_saved():
    bad_criterion = "資料Aを1ファイル、資料Bを1ファイル、合計3ファイルで作成する"
    norm = normalize_p0a_artifact_requirement(bad_criterion)
    assert norm["consistent"] is False
    assert build_p0a_artifact_slots(bad_criterion) == []
    merged = compile_plan([bad_criterion], [_merged_task()], goal="疑似P0-0")
    check = check_p0a_separate_artifacts([bad_criterion], merged["tasks"])
    assert check["passed"] is False
    # 矛盾時・欠落時は件数を1件に倒さず判定不能(ValueError)にする。
    try:
        count_p0a_required_files(bad_criterion)
    except ValueError:
        pass
    else:
        raise AssertionError("contradictory requirement must be undetermined")
    result = ensure_p0a_artifact_slots([bad_criterion], merged["tasks"], lambda missing: {
        "tasks": list(merged["tasks"])})
    assert result["saved"] is False


def test_p0a0_undetermined_requirement_is_not_defaulted_to_one():
    # 欠落・None・不正値のときは1に倒さず判定不能にする。
    import app.structured_planning as sp
    for broken in ({"consistent": True},
                   {"consistent": True, "required": None},
                   {"consistent": True, "required": "2"},
                   {"consistent": True, "required": 0},
                   {"consistent": False, "required": 2}):
        try:
            sp._p0a_required_or_raise(dict(broken), where="test")
        except ValueError:
            pass
        else:
            raise AssertionError(f"must be undetermined: {broken}")



def test_p0a0_single_file_regression():
    criterion = "1つの日本語Markdownに手順とFAQを記載する"
    assert normalize_p0a_artifact_requirement(criterion)["required"] == 1
    assert build_p0a_artifact_slots(criterion)[0]["slot_id"] == "slot-01"
    task = compile_task(1, criterion, _proposal(
        "疑似単一", "手順とFAQを一つの成果物にまとめる。",
        ["申請手順", "よくある質問"]), [])
    plan = compile_plan([criterion], [task], goal="疑似P0-0")
    check = check_p0a_separate_artifacts([criterion], plan["tasks"])
    assert check["passed"] is True

@pytest.mark.asyncio
async def test_p0a0_regenerated_candidate_is_actually_saved(tmp_path):
    """The manager must save the regenerated plan, not its original merged candidate."""
    import json
    from app.memory.short_term import ShortTermMemory
    from app.project_manager import ProjectOrchestrator
    from app.workspace_files import WorkspaceSandbox

    class FakeLlm:
        def __init__(self):
            self.calls = 0

        async def stream(self, messages):
            self.calls += 1
            proposal = {"title": "疑似分離計画", "scope": "2資料を別々に作成する",
                        "headings": ["概要", "詳細"], "depends_on": []}
            if self.calls > 1:
                proposal["outputs"] = [
                    {"kind": "document", "required_headings": ["資料Aの概要", "資料Aの詳細"]},
                    {"kind": "document", "required_headings": ["資料Bの概要", "資料Bの詳細"]},
                ]
            yield json.dumps(proposal, ensure_ascii=False)

    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("疑似P0-0保存試験")
    memory.save_mission(project["id"], "## 達成条件\n1. " + CRITERION,
                        "", "", False, [])
    llm = FakeLlm()
    manager = ProjectOrchestrator(
        memory, llm, lambda _: ("", []), None, lambda: [],
        workspace=WorkspaceSandbox(tmp_path / "workspace"))
    result = await manager.generate_plan(project["id"])
    assert llm.calls == 2
    assert result["slot_regen_attempts"] == 1
    assert check_p0a_separate_artifacts([CRITERION], result["tasks"])["passed"] is True
    assert check_p0a_separate_artifacts(
        [CRITERION], memory.get_mission(project["id"])["tasks"])["passed"] is True