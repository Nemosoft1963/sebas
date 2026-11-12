"""P1 原本外の事実の計画検査。疑似PJ・実関数・外部通信なし。特定案件の語彙なし。"""
import asyncio
import json

from app.structured_planning import (
    P0A_UNSUPPORTED_VERSION,
    check_p0a_fact_consistency,
    check_p0a_unsupported_values,
    compile_plan,
    compile_task,
    contract_of,
    validate_plan,
)


def _proposal(title, scope, headings):
    return {"title": title, "scope": scope, "headings": headings, "depends_on": []}


def test_supported_values_are_kept_not_hypothesis():
    registered = "日時2026年10月20日 電話03-1234-5678 メールtest@example.com 参加者30人"
    plan = "日時2026/10/20 電話０３-１２３４-５６７８ メールTEST@example.com 参加者３０人"
    result = check_p0a_unsupported_values(plan, registered)
    assert result["version"] == P0A_UNSUPPORTED_VERSION
    assert result["checked"] is True
    assert result["checkable"] is True
    assert result["unsupported_values"] == []
    assert result["unverified_assumptions"] == []


def test_unsupported_values_recorded_as_hypothesis_not_confirmed():
    registered = "案内を作成する"
    plan = "スタッフ整列は午前8時、担当は山田様、連絡先090-1111-2222、yamada@example.com、参加者30人"
    result = check_p0a_unsupported_values(plan, registered)
    assert result["checked"] is True
    assert result["checkable"] is True
    assert result["passed"] is False
    assert result["reason"] == "out_of_scope_and_unsupported_values_present"
    kinds = {row["kind"] for row in result["unsupported_values"]}
    assert {"datetime", "contact_phone", "contact_email", "number", "proper"} <= kinds
    assert len(result["unverified_assumptions"]) == len(result["unsupported_values"])
    for row in result["unverified_assumptions"]:
        assert "確定" not in str(row.get("value") or "")
        assert "要確認" in str(row.get("reason") or "")
    # 計画本文は消さないが確定扱いにしない(呼び出し側が別フィールドで保持する)。
    assert "午前8時" in plan
    assert "山田様" in plan


def test_common_word_with_room_character_is_not_a_proper_name():
    result = check_p0a_unsupported_values(
        "持ち物は飲み物と室内履きです", "架空の案内を作る")
    assert not any(row["kind"] == "proper" for row in result["unsupported_values"])


def test_llm_self_claimed_flags_do_not_override():
    registered = "案内を作成する"
    plan = "午前8時に整列する。出典あり 確認済み"
    result = check_p0a_unsupported_values(plan, registered)
    assert any(row["value"] == "午前8時" for row in result["unsupported_values"])


def test_out_of_scope_proper_name_is_not_passed():
    registered = "案内を作成する"
    plan = "担当田中が受付する"
    result = check_p0a_unsupported_values(plan, registered)
    assert result["checked"] is True
    assert result["passed"] is False
    assert any(row["value"] == "田中" for row in result["out_of_scope"])


def test_unreadable_source_is_not_passed():
    result = check_p0a_unsupported_values("午前8時に整列する", "")
    assert result["checked"] is False
    assert result["passed"] is False
    assert result["checkable"] is False
    assert "source_unreadable" in str(result.get("reason") or "")


def test_contradiction_still_stops_before_hypothesis():
    registered = "日時2026年10月20日"
    plan = "日時2026年10月21日の作業"
    fact = check_p0a_fact_consistency(plan, registered)
    assert fact["passed"] is False
    assert fact["conflicts"]
    unsupported = check_p0a_unsupported_values(plan, registered)
    assert any(row["value"] == "2026年10月21日" for row in unsupported["unsupported_values"])


def test_unknown_criterion_id_rejected_by_existing_coverage():
    tasks = [compile_task(1, "架空作業Aを作成する",
                          _proposal("架空作業A", "架空Aの内容を整理する。", ["目的", "実施内容"]), [])]
    plan = compile_plan(["架空作業Aを作成する"], tasks, goal="架空目標")
    tampered = json.loads(json.dumps(plan))
    for task in tampered["tasks"]:
        contract = contract_of(task)
        if contract and not contract.get("final_verification") and contract.get("criterion_ids"):
            payload = dict(contract)
            payload["criterion_ids"] = ["SC99"]
            task["acceptance_criteria"] = json.dumps(payload, ensure_ascii=False)
    assessment = validate_plan(tampered, {"SC01"})
    assert assessment["passed"] is False
    assert any("coverage" in str(issue) for issue in assessment["issues"])


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_integration_fake_llm_unsupported_reaches_saved_plan(tmp_path):
    from app.memory.short_term import ShortTermMemory
    from app.project_manager import ProjectOrchestrator
    from app.workspace_files import WorkspaceSandbox

    mem = ShortTermMemory(tmp_path / "memory" / "conversations.db")
    project = mem.create_project("p1-pseudo")
    pid = project["id"]
    mem.save_mission(pid, "## 達成条件\n1. 案内文を作成する",
                     "1. 案内文を作成する", "", False, [])

    class FakeLlm:
        async def complete_json(self, _messages, _schema):
            return json.dumps({
                "title": "案内文作成",
                "scope": "午前8時に30人で整列し、連絡先090-1111-2222へ連絡する。担当は山田様。",
                "headings": ["日時", "連絡先"],
                "depends_on": [],
            }, ensure_ascii=False)

    mgr = ProjectOrchestrator(
        mem, FakeLlm(), lambda _p: ("案内を作成する", []), None, lambda: [],
        workspace=WorkspaceSandbox(tmp_path / "ws"),
    )
    out = _run(mgr.generate_plan(pid))
    assert out.get("unsupported_checkable") is True
    assert out.get("unsupported_count", 0) >= 3
    assert out.get("unsupported_check_passed") is False
    assert len(out.get("unverified_assumptions") or []) == out.get("unsupported_count")
    assert "unverified_assumptions" in str(out.get("plan_summary") or "")
    assert any(task.get("task_key") == "SC01" for task in out.get("tasks") or [])
    assert out.get("unsupported_check_version") == P0A_UNSUPPORTED_VERSION
