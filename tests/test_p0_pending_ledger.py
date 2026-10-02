"""P0受入テスト: 承認・停止状態を単一台帳 (Pending Ledger) から表示する。

1. workflow-readiness と next-action の可否・理由・blocked/executable 一致
2. not_passed と unapproved の明確な分離
3. 外部操作 承認待ち0件 と 計画未承認 の区別
4. 失効版・別案件・未作成結果レビューを承認済みにしないフィルタ
5. navigable (詳細を開ける) と executable (実行できる) の分離
6. 上位サマリの可視性 (折りたたみを開かずに未解決件数と承認不可理由が確認可能)
"""
from types import SimpleNamespace
from unittest.mock import patch
import time
import pytest

from app.goal_completion_flag import enable
from app.goal_completion_store import GoalCompletionStore
from app.goal_review import ReviewStore
from app.memory.short_term import ShortTermMemory
import app.next_action_controller as nac
import app.pending_ledger as pending_ledger
import app.workflow_readiness as readiness


class MockMemory:
    def __init__(self, path):
        self.path = path
        self._missions = {}
        self._actions = {}

    def save_mission_dict(self, pid, mission):
        self._missions[pid] = dict(mission)

    def get_mission(self, pid):
        return dict(self._missions.get(pid, {}))

    def list_actions(self, pid):
        return list(self._actions.get(pid, []))

    def list_context_files(self, pid, include_content=False):
        return []


@pytest.fixture
def manager(tmp_path):
    mem = MockMemory(tmp_path / "memory.db")
    mgr = SimpleNamespace(
        memory=mem,
        provider_statuses=lambda: [],
    )
    return mgr


def _init_mission(manager, pid="proj-p0", goal="車両別損益の作成", status="planning", tasks=None):
    mission = {
        "id": pid,
        "goal": goal,
        "status": status,
        "plan_version": 1,
        "tasks": tasks or [{"id": "t1", "title": "抽出", "status": "pending"}],
    }
    manager.memory.save_mission_dict(pid, mission)
    return mission


def test_p0_1_readiness_and_nac_consistency(manager):
    """workflow-readiness と next-action で同一台帳・同一判定が返ること。"""
    pid = "proj-p0-1"
    _init_mission(manager, pid)
    enable(manager.memory.path, pid)

    # 1. 計画指摘がある場合
    store = ReviewStore(manager.memory.path)
    _, sig = readiness.plan_snapshot(manager, pid)
    store.put(
        pid,
        "plan",
        sig,
        {"status": "not_passed", "stop_reason": "利益計算ロジックに不整合があります"},
    )
    store.put(
        pid,
        "plan_feedback",
        sig,
        {
            "issues": [
                {"id": "iss-1", "provider": "codex", "text": "配賦基準の定義が不足しています"}
            ]
        },
    )

    rd = readiness.build_readiness(manager, pid)
    nac_view = nac.compute(manager, pid)

    # 次アクションの一致
    assert rd["next_action"]["id"] == nac_view["action_id"]
    assert rd["next_action"]["executable"] == nac_view["executable"]
    assert rd["next_action"]["blocked"] == nac_view["blocked"]
    assert rd["next_action"]["navigable"] == nac_view["navigable"]
    assert rd["next_action"]["allowed"] == nac_view["allowed"]
    assert rd["next_action"]["reason"] == nac_view["reason"]

    # 上位サマリの確認
    assert rd["pending_summary"]["issue_count"] >= 1
    assert rd["pending_summary"]["plan_status"] == "not_passed"
    assert rd["pending_summary"]["plan_approval_blocked"] is True


def test_p0_2_not_passed_vs_unapproved_separation(manager):
    """not_passed (指摘あり) と unapproved (検証Passedだが未承認) の明確な分離。"""
    pid_not_passed = "proj-p0-not-passed"
    _init_mission(manager, pid_not_passed, status="planning")
    store = ReviewStore(manager.memory.path)
    _, sig1 = readiness.plan_snapshot(manager, pid_not_passed)
    store.put(pid_not_passed, "plan", sig1, {"status": "not_passed"})

    ledger1 = pending_ledger.build(manager, pid_not_passed)
    plan_item1 = next(item for item in ledger1["items"] if item["kind"] == pending_ledger.KIND_PLAN_APPROVAL)
    assert plan_item1["state"] == "not_passed"
    assert "修正案" in plan_item1["basis"] or "not_passed" in plan_item1["basis"]
    assert ledger1["summary"]["plan_status"] == "not_passed"
    assert ledger1["summary"]["plan_approval_blocked"] is True

    # 検証 passed だが 人間承認前 (status="planning")
    pid_unapproved = "proj-p0-unapproved"
    _init_mission(manager, pid_unapproved, status="planning")
    _, sig2 = readiness.plan_snapshot(manager, pid_unapproved)
    store.put(pid_unapproved, "plan", sig2, {"status": "passed"})

    ledger2 = pending_ledger.build(manager, pid_unapproved)
    plan_item2 = next(item for item in ledger2["items"] if item["kind"] == pending_ledger.KIND_PLAN_APPROVAL)
    assert plan_item2["state"] == "unapproved"
    assert "人間の承認待ち" in plan_item2["basis"]
    assert ledger2["summary"]["plan_status"] == "unapproved"
    assert ledger2["summary"]["plan_approval_blocked"] is True


def test_p0_3_external_action_zero_vs_plan_unapproved(manager):
    """外部操作承認待ち0件と計画未承認は別集計であること。"""
    pid = "proj-p0-3"
    _init_mission(manager, pid, status="planning")
    ledger = pending_ledger.build(manager, pid)

    assert ledger["summary"]["external_pending_count"] == 0
    assert ledger["summary"]["external_waiting_zero"] is True
    # 外部操作が0件でも計画未承認なら plan_approval_blocked は True
    assert ledger["summary"]["plan_approval_blocked"] is True


def test_p0_4_stale_and_expired_result_review_filtered(manager):
    """失効版・期限切れ・未達条件つきの結果レビューを承認済みにしない。"""
    pid = "proj-p0-4"
    _init_mission(manager, pid, status="completed")
    store = ReviewStore(manager.memory.path)
    _, current_sig = readiness.plan_snapshot(manager, pid)

    # 1. 旧版への承認 (snap_plan が現行と不一致)
    with patch("app.goal_review.execution_snapshot", return_value=({"proof": 1}, "res-sig-1", [])):
        store.put(
            pid,
            "result",
            "res-sig-1",
            {
                "status": "approved",
                "snapshot": {"plan": "old-plan-sig"},
                "expires": time.time() + 3600,
            },
        )
        ledger_stale = pending_ledger.build(manager, pid)
        res_item = next(item for item in ledger_stale["items"] if item["kind"] == pending_ledger.KIND_RESULT_APPROVAL)
        assert res_item["state"] == "stale"
        assert ledger_stale["summary"]["result_approved"] is False

    # 2. 承認期限切れ
    with patch("app.goal_review.execution_snapshot", return_value=({"proof": 1}, "res-sig-2", [])):
        store.put(
            pid,
            "result",
            "res-sig-2",
            {
                "status": "approved",
                "snapshot": {"plan": current_sig},
                "expires": time.time() - 100,  # 過去
            },
        )
        ledger_expired = pending_ledger.build(manager, pid)
        res_item = next(item for item in ledger_expired["items"] if item["kind"] == pending_ledger.KIND_RESULT_APPROVAL)
        assert res_item["state"] == "expired"
        assert ledger_expired["summary"]["result_approved"] is False

    # 3. 未達条件(failures)が残っている場合
    with patch("app.goal_review.execution_snapshot", return_value=({"proof": 1}, "res-sig-3", ["未達の検証項目あり"])):
        store.put(
            pid,
            "result",
            "res-sig-3",
            {
                "status": "approved",
                "snapshot": {"plan": current_sig},
                "expires": time.time() + 3600,
            },
        )
        ledger_failures = pending_ledger.build(manager, pid)
        res_item = next(item for item in ledger_failures["items"] if item["kind"] == pending_ledger.KIND_RESULT_APPROVAL)
        assert res_item["state"] == "unapproved"
        assert ledger_failures["summary"]["result_approved"] is False


def test_p0_5_navigable_vs_executable_separation(manager):
    """navigable (詳細を開ける) と executable (処理を実行できる) が分離されていること。"""
    pid = "proj-p0-5"
    _init_mission(manager, pid, status="planning")
    enable(manager.memory.path, pid)

    store = ReviewStore(manager.memory.path)
    _, sig = readiness.plan_snapshot(manager, pid)
    store.put(pid, "plan", sig, {"status": "not_passed"})

    decision = pending_ledger.decide_action(
        action_id="propose_feedback",
        action_class="local_safe",
        auto=False,
        allowed_actions={"propose_feedback": {"allowed": False, "reason": "計画指摘の反映待ち"}},
        stop_reason="停止中",
    )

    # 停止中であっても詳細画面への誘導 (navigable) は True、自動実行 (executable) は False
    assert decision["navigable"] is True
    assert decision["executable"] is False
    assert decision["auto_executable"] is False
    assert decision["blocked"] is True
    assert "計画指摘の反映待ち" in decision["reason"]


def test_p0_6_top_level_visibility_unresolved_and_blocked(manager):
    """折りたたみを開かずに上位サマリ (pending_summary) で未解決ブロッカーと承認不可理由が把握できること。"""
    pid = "proj-p0-6"
    _init_mission(manager, pid, status="planning")
    store = ReviewStore(manager.memory.path)
    _, sig = readiness.plan_snapshot(manager, pid)

    store.put(
        pid,
        "revision",
        sig,
        {
            "status": "draft",
            "blockers": [
                {"disposition": "development", "reason": "専用の配賦ルール関数の作成が必要"},
                {"disposition": "business_fact", "reason": "車検費用原本の読取確認が必要"},
            ],
            "changes": [{"task_id": "t1", "action": "update"}],
        },
    )

    rd = readiness.build_readiness(manager, pid)
    summary = rd.get("pending_summary") or {}

    assert summary["unresolved_count"] == 2
    assert summary["plan_approval_blocked"] is True
    assert summary["result_approved"] is False
    assert len(rd.get("pending_ledger") or []) >= 2


def test_p0_7_read_failure_is_not_zero_or_approved(manager):
    pid = "proj-p0-failure"
    _init_mission(manager, pid, status="planning")
    with patch("app.pending_ledger._safe_issues", return_value=None), \
            patch("app.pending_ledger._safe_actions", return_value=None):
        ledger = pending_ledger.build(manager, pid)
    summary = ledger["summary"]
    assert summary["issue_count"] is None
    assert summary["external_pending_count"] is None
    assert summary["external_waiting_zero"] is None
    assert summary["plan_approval_blocked"] is True
    assert all(item["executable"] is False for item in ledger["items"])


def test_p0_8_unresolved_revision_cannot_appear_approved(manager):
    pid = "proj-p0-blocked-approved"
    _init_mission(manager, pid, status="ready")
    store = ReviewStore(manager.memory.path)
    _, signature = readiness.plan_snapshot(manager, pid)
    store.put(pid, "plan", signature, {"status": "passed"})
    store.put(pid, "revision", signature, {
        "status": "draft", "blockers": [{"disposition": "development", "reason": "未解決"}]
    })
    summary = pending_ledger.build(manager, pid)["summary"]
    assert summary["plan_status"] == "unapproved"
    assert summary["plan_approval_blocked"] is True
    assert summary["unresolved_count"] == 1
