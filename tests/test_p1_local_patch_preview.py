from types import SimpleNamespace
from unittest.mock import patch

from app.local_patch_preview import preview


class Memory:
    path = ":memory:"
    def get_mission(self, pid):
        return {"id": pid, "goal": "営業改善", "plan_version": 3, "tasks": self.tasks}


def _data(completed=False):
    task = {"task_key": "SC01", "title": "施策", "description": "既存成果を確認する", "depends_on": [],
            "status": "completed" if completed else "pending"}
    contract = {"goal_id": "gc-stable", "content_hash": "gc-hash", "criteria": [{
        "criterion_id": "SC01", "statement": "営業成果", "exec_task_keys": ["SC01"]}]}
    issues = [
        {"id": "i1", "criterion": "SC01", "text": "公開前に承認し、送信前の検証順序を明記する"},
        {"id": "i2", "criterion": "SC01", "text": "既存フォームを再作成してはいけない"},
        {"id": "i3", "criterion": "SC01", "text": "リードの有効条件を決定する"},
        {"id": "i4", "criterion": "SC01", "text": "外部API連携を新規実装する"},
        {"id": "i5", "criterion": "SC01", "text": "成果物を検証後に公開する依存順序を追加する"},
        {"id": "i6", "criterion": "SC01", "text": "公開済みサイトを維持し禁止事項を明記する"},
    ]
    mem = Memory(); mem.tasks = [task]
    return SimpleNamespace(memory=mem), task, contract, issues


def test_six_issues_are_deterministically_bound_and_classified():
    manager, task, contract, issues = _data()
    with patch("app.local_patch_preview.plan_snapshot", return_value=({"tasks": [task]}, "plan-sig")), \
         patch("app.local_patch_preview.contract_preview", return_value=contract), \
         patch("app.local_patch_preview.ReviewStore.get", return_value=None):
        first = preview(manager, "p", issues)
        second = preview(manager, "p", issues)
    assert first == second
    assert len(first["candidates"]) == 6
    assert {x["issue_id"] for x in first["candidates"]} == {f"i{x}" for x in range(1, 7)}
    assert all(x["plan_signature"] == "plan-sig" and x["goal_id"] == "gc-stable" for x in first["candidates"])
    assert all(x["criterion_id"] == "SC01" and x["task_key"] == "SC01" and x["basis"] for x in first["candidates"])
    kinds = {x["issue_id"]: x["classification"] for x in first["candidates"]}
    assert kinds["i3"] == "business_fact"
    assert kinds["i4"] == "development_required"
    assert kinds["i1"] == "safe_plan_patch"
    assert [x["issue_id"] for x in first["questions"]] == ["i3"]
    assert first["automatic_apply_allowed"] is False
    assert first["preserve_existing_artifacts"] is True
    assert first["recreate_published_assets"] is False


def test_completed_or_two_invalidations_never_auto_apply_and_failed_review_blocks_start():
    manager, task, contract, issues = _data(completed=True)
    def get(_self, _pid, kind, _sig):
        if kind in {"plan", "send_approval"}:
            return {"status": "not_passed"}
        return None
    with patch("app.local_patch_preview.plan_snapshot", return_value=({"tasks": [task]}, "same-signature")), \
         patch("app.local_patch_preview.contract_preview", return_value=contract), \
         patch("app.local_patch_preview.ReviewStore.get", get):
        row = preview(manager, "p", issues)
    assert row["touches_completed_task"] is True
    assert len(row["invalidated_approvals"]) == 2
    assert row["automatic_apply_allowed"] is False
    assert row["external_validation_signature"] == "same-signature"
    assert row["external_validation_passed"] is False
    assert row["execution_start_allowed"] is False
    assert row["saved"] is False


def test_unknown_criterion_is_not_patched_to_first_task():
    manager, task, contract, _issues = _data()
    issue = {"id": "unknown", "criterion": "SC99", "text": "公開前に承認する順序を明記する"}
    with patch("app.local_patch_preview.plan_snapshot", return_value=({"tasks": [task]}, "plan-sig")), \
         patch("app.local_patch_preview.contract_preview", return_value=contract), \
         patch("app.local_patch_preview.ReviewStore.get", return_value={"status": "passed"}):
        row = preview(manager, "p", [issue])
    candidate = row["candidates"][0]
    assert candidate["bound"] is False
    assert candidate["classification"] == "development_required"
    assert candidate["criterion_id"] == ""
    assert candidate["task_key"] == ""
    assert candidate["patch"] is None
    assert "現行の達成条件・工程に再結合できない" in candidate["basis"]
    assert row["execution_start_allowed"] is False
